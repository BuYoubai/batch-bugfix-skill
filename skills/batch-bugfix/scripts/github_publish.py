"""Create one authorized GitHub Issue or same-repository PR and verify readback.

Default: network reads only, no journal writes. --apply is an execution switch,
not consent. Use one stable input/key and journal, with one coordinator. The
file lock only serializes users of that journal; inspect semantic duplicates
before first creation. Unknown POST outcomes are reconciled, never reposted.

Schema and recovery: ../references/github.md
This adapter does not push, edit, close, merge, upload attachments, or certify CI.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlencode, urlsplit

from publication_state import PublishError, exclusive, save


class Gh:
    def __init__(self, host, executable="gh"):
        self.host, self.executable = host, executable

    def request(self, method, endpoint, body=None):
        command = [self.executable, "api", "--hostname", self.host, "--method", method,
                   "--header", "Accept: application/vnd.github+json", endpoint]
        if body is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishError(f"gh {method} unavailable or timed out ({type(error).__name__})") from None
        if result.returncode:
            # Do not persist raw response bodies, stderr, or authentication data.
            raise PublishError(f"gh {method} failed with exit {result.returncode}; inspect authentication, policy or server separately")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise PublishError("gh returned an unreadable JSON response") from None


def valid_branch(value):
    if not isinstance(value, str) or not value or value == "@":
        return False
    if value.startswith(("-", "/")) or value.endswith(("/", ".")):
        return False
    if any(part in value for part in ("..", "@{", "//")) or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value):
        return False
    return all(not segment.startswith(".") and not segment.endswith(".lock") for segment in value.split("/"))


def validate(spec):
    if not isinstance(spec, dict) or type(spec.get("version")) is not int or spec["version"] != 1:
        raise PublishError("version 1 object required")
    if spec.get("kind") not in {"issue", "pr"}:
        raise PublishError("kind issue/pr required")
    fields = {"version", "host", "repository", "repository_id", "kind", "key", "title", "body"}
    if spec["kind"] == "pr":
        fields.update({"head_branch", "base_branch", "head_sha", "base_sha", "draft"})
    if set(spec) - fields:
        raise PublishError("unsupported input fields; only same-repository Issue/PR creation is supported")
    host = spec.get("host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise PublishError("host must be a hostname without scheme, port or credentials")
    repository = spec.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise PublishError("repository must be owner/name")
    if repository.split("/")[1] in {".", ".."}:
        raise PublishError("invalid repository name")
    if type(spec.get("repository_id")) is not int or spec["repository_id"] <= 0:
        raise PublishError("positive numeric repository_id required")
    key = spec.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        raise PublishError("stable non-secret key required (1-128 characters)")
    for field in ("title", "body"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise PublishError("nonempty text required: " + field)
    if "<!-- batch-bugfix:" in spec["body"]:
        raise PublishError("body already contains a publication marker")
    if spec["kind"] == "pr":
        for side in ("head", "base"):
            if not valid_branch(spec.get(side + "_branch")):
                raise PublishError("invalid branch: " + side)
            sha = spec.get(side + "_sha")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                raise PublishError("full lowercase branch SHA required: " + side)
        if spec["head_branch"] == spec["base_branch"]:
            raise PublishError("head and base branches must differ")
        if type(spec.get("draft", False)) is not bool:
            raise PublishError("draft must be a boolean")


def expected_body(spec):
    marker = "<!-- batch-bugfix:" + spec["key"] + " -->"
    body = {"title": spec["title"], "body": spec["body"] + "\n\n" + marker}
    if spec["kind"] == "pr":
        body.update(head=spec["head_branch"], base=spec["base_branch"], draft=spec.get("draft", False))
    return body, marker


def same_repository(record, spec):
    return (isinstance(record, dict) and record.get("id") == spec["repository_id"]
            and isinstance(record.get("full_name"), str)
            and record["full_name"].casefold() == spec["repository"].casefold())


def guard(api, spec):
    prefix = "repos/" + spec["repository"]
    if not same_repository(api.request("GET", prefix), spec):
        raise PublishError("repository identity mismatch")
    if spec["kind"] == "pr":
        for side in ("head", "base"):
            record = api.request("GET", prefix + "/branches/" + quote(spec[side + "_branch"], safe=""))
            if not isinstance(record, dict) or record.get("commit", {}).get("sha") != spec[side + "_sha"]:
                raise PublishError(side + " branch moved; refresh review and test evidence")


def find_existing(api, spec, endpoint, marker):
    found, seen = [], set()
    for page in range(1, 1001):
        records = api.request("GET", endpoint + "?" + urlencode({"state": "all", "per_page": 100, "page": page}))
        if not isinstance(records, list):
            raise PublishError("unexpected listing response")
        for record in records:
            number = record.get("number") if isinstance(record, dict) else None
            if type(number) is not int or number <= 0 or number in seen:
                raise PublishError("unstable or invalid paginated listing")
            seen.add(number)
            # GitHub Issues listings include PRs. Count raw pages before filtering.
            if spec["kind"] == "issue" and "pull_request" in record:
                continue
            matches = marker in (record.get("body") or "")
            if spec["kind"] == "pr":
                head = record.get("head") or {}
                same_head = (same_repository(head.get("repo"), spec) and head.get("ref") == spec["head_branch"])
                matches = matches or (record.get("state") == "open" and same_head)
            if matches:
                found.append(record)
        if len(records) < 100:
            break
    else:
        raise PublishError("pagination bound reached; inspect manually")
    if len(found) > 1:
        raise PublishError("ambiguous duplicate objects; inspect before continuing")
    return found[0] if found else None


def verify(api, spec, endpoint, record, body):
    number = record.get("number") if isinstance(record, dict) else None
    if type(number) is not int or number <= 0:
        raise PublishError("missing remote object number")
    current = api.request("GET", endpoint + "/" + str(number))
    if not isinstance(current, dict) or current.get("number") != number or current.get("state") != "open":
        raise PublishError("remote identity/state differs; preserve object and investigate")
    for key in ("title", "body"):
        if current.get(key) != body[key]:
            raise PublishError("remote readback differs: " + key)
    if spec["kind"] == "issue":
        if "pull_request" in current:
            raise PublishError("remote object is a pull request, not an issue")
    else:
        if current.get("merged") is not False or current.get("draft") is not body["draft"]:
            raise PublishError("remote PR merge/draft state differs")
        for side in ("head", "base"):
            branch = current.get(side) or {}
            if not same_repository(branch.get("repo"), spec):
                raise PublishError("cross-repository or missing PR repository is outside this adapter")
            if branch.get("ref") != spec[side + "_branch"] or branch.get("sha") != spec[side + "_sha"]:
                raise PublishError("remote PR branch/SHA differs: " + side)
    url = urlsplit(current.get("html_url") or "")
    route = "issues" if spec["kind"] == "issue" else "pull"
    expected_path = f"/{spec['repository']}/{route}/{number}"
    if (url.scheme != "https" or url.netloc.casefold() != spec["host"].casefold()
            or url.username or url.query or url.fragment or url.path.casefold() != expected_path.casefold()):
        raise PublishError("remote URL has an unexpected host or object path")
    guard(api, spec)
    return {"status": "verified", "number": number, "url": current["html_url"]}


def publish(spec, api, journal, apply=False):
    validate(spec)
    journal = Path(journal)
    body, marker = expected_body(spec)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    endpoint = "repos/" + spec["repository"] + ("/issues" if spec["kind"] == "issue" else "/pulls")

    def execute():
        has_journal = journal.exists()
        old = json.loads(journal.read_text(encoding="utf-8")) if has_journal else None
        if has_journal and (not isinstance(old, dict) or old.get("digest") != digest):
            raise PublishError("journal belongs to different input; reconcile the original attempt")
        guard(api, spec)
        found = find_existing(api, spec, endpoint, marker)
        if found is not None:
            result = verify(api, spec, endpoint, found, body)
            if apply:
                save(journal, {"digest": digest, **result})
            return result
        if has_journal:
            raise PublishError("previous attempt is not visible; reconciliation only, no repeated POST")
        if not apply:
            return {"status": "planned", "kind": spec["kind"], "key": spec["key"], "repository": spec["repository"]}
        guard(api, spec)
        save(journal, {"digest": digest, "status": "pending"})
        try:
            created = api.request("POST", endpoint, body)
        except (ValueError, OSError, RuntimeError):
            save(journal, {"digest": digest, "status": "uncertain"})
            created = find_existing(api, spec, endpoint, marker)
            if created is None:
                raise PublishError("POST result unknown; rerun same input/journal for read-only reconciliation") from None
        result = verify(api, spec, endpoint, created, body)
        save(journal, {"digest": digest, **result})
        return result

    if not apply:
        return execute()
    with exclusive(journal):
        return execute()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--gh", default="gh", help="Executable path if not on PATH")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        spec = json.loads(args.input.read_text(encoding="utf-8-sig"))
        validate(spec)
        result = publish(spec, Gh(spec["host"], args.gh), args.journal, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"status": "blocked_or_uncertain", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
