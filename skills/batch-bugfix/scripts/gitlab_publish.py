"""Create one authorized GitLab Issue or same-project MR with readback and a journal.

Default: read-only plan. --apply is an execution switch, not evidence of consent.
Use one persistent journal per (host, project_id, kind, key), with one coordinator.
The journal lock prevents concurrent use of that file, not unrelated publishers.

Input JSON version 1 fields:
  host: hostname[:port] matching an existing glab login (no scheme/credentials)
  project_id: positive integer; project_path: expected namespace/project
  kind: issue | mr; key: stable non-secret case key ([A-Za-z0-9_.-]+)
  title, description: exact reviewed text (no credentials)
  MR additionally: source_branch, target_branch, source_sha, target_sha
The adapter appends a stable hidden key to description for exact-key recovery.
It rejects an existing object with different content; it never overwrites it.
Before first creation, separately inspect semantically similar remote issues.

After an uncertain request, rerun with the SAME input and journal. It only
reconciles: if no exact object is visible it stops, rather than posting twice.
To resolve a persistent unknown outcome, inspect the server and journal manually;
do not discard the journal or choose another key to force a retry. A stale lock
requires verifying that its recorded process has ended before removing that lock.

This does not push branches, upload attachments, edit/close/merge, or certify CI.
API contracts: https://docs.gitlab.com/api/issues/
https://docs.gitlab.com/api/merge_requests/ https://docs.gitlab.com/api/branches/
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlencode, urlsplit

from publication_state import PublishError, exclusive, save


class Glab:
    def __init__(self, host, executable="glab"):
        self.host, self.executable = host, executable

    def request(self, method, endpoint, body=None):
        # stdin keeps reviewed text out of shell interpolation and process arguments.
        command = [self.executable, "api", "--hostname", self.host, "--method", method, endpoint]
        if body is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishError(f"glab {method} unavailable or timed out ({type(error).__name__})") from None
        if result.returncode:
            # Raw stderr can contain sensitive response data; do not persist it.
            raise PublishError(f"glab {method} failed with exit {result.returncode}; inspect authentication, policy or server separately")
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise PublishError("glab returned an unreadable JSON response") from None


def validate(spec):
    if spec.get("version") != 1 or spec.get("kind") not in {"issue", "mr"}:
        raise PublishError("version 1 and kind issue/mr required")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", spec.get("host", "")):
        raise PublishError("host must be hostname[:port] without credentials")
    if type(spec.get("project_id")) is not int or spec["project_id"] <= 0:
        raise PublishError("positive numeric project_id required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", spec.get("key", "")):
        raise PublishError("stable non-secret key required")
    for key in ("project_path", "title", "description"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise PublishError("nonempty text required: " + key)
    if spec["kind"] == "mr":
        for name in ("source", "target"):
            if not isinstance(spec.get(name + "_branch"), str) or not spec[name + "_branch"]:
                raise PublishError("branch required")
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", spec.get(name + "_sha", "")):
                raise PublishError("full branch SHA required")
        if spec["source_branch"] == spec["target_branch"]:
            raise PublishError("source and target branches must differ")


def expected_body(spec):
    marker = "<!-- batch-bugfix:" + spec["key"] + " -->"
    if "<!-- batch-bugfix:" in spec["description"]:
        raise PublishError("description already contains a publication marker")
    body = {"title": spec["title"], "description": spec["description"] + "\n\n" + marker}
    if spec["kind"] == "mr":
        body.update({key: spec[key] for key in ("source_branch", "target_branch")})
    return body, marker


def guard(api, spec):
    prefix = f"projects/{spec['project_id']}"
    project = api.request("GET", prefix)
    if project.get("id") != spec["project_id"] or project.get("path_with_namespace") != spec["project_path"]:
        raise PublishError("project identity mismatch")
    if spec["kind"] == "mr":
        for name in ("source", "target"):
            branch = api.request("GET", prefix + "/repository/branches/" + quote(spec[name + "_branch"], safe=""))
            if branch.get("commit", {}).get("id") != spec[name + "_sha"]:
                raise PublishError(name + " branch moved; refresh review and test evidence")


def find_existing(api, spec, endpoint, marker):
    found, seen = [], set()
    for page in range(1, 1001):
        query = {"state": "all", "scope": "all", "per_page": 100, "page": page}
        if spec["kind"] == "mr":
            query["source_branch"] = spec["source_branch"]
        records = api.request("GET", endpoint + "?" + urlencode(query))
        if not isinstance(records, list):
            raise PublishError("unexpected listing response")
        for record in records:
            iid = record.get("iid")
            if not isinstance(iid, int) or iid in seen:
                raise PublishError("unstable or invalid paginated listing")
            seen.add(iid)
            matches = marker in (record.get("description") or "")
            if spec["kind"] == "mr":
                matches = matches or record.get("source_branch") == spec["source_branch"]
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
    iid = record.get("iid")
    if type(iid) is not int or iid <= 0:
        raise PublishError("missing remote object id")
    current = api.request("GET", endpoint + "/" + str(iid))
    if current.get("project_id") != spec["project_id"] or current.get("state") != "opened":
        raise PublishError("remote identity/state differs; preserve object and investigate")
    for key, value in body.items():
        if current.get(key) != value:
            raise PublishError("remote readback differs: " + key)
    if spec["kind"] == "mr":
        for key in ("source_project_id", "target_project_id"):
            if current.get(key) != spec["project_id"]:
                raise PublishError("cross-project MR is outside this adapter")
        if current.get("sha") != spec["source_sha"]:
            raise PublishError("remote MR SHA differs")
    url = urlsplit(current.get("web_url") or "")
    if url.scheme not in {"http", "https"} or url.netloc != spec["host"] or url.username:
        raise PublishError("remote URL is missing or has an unexpected host")
    guard(api, spec)
    return {"status": "verified", "iid": iid, "url": current["web_url"]}


def publish(spec, api, journal, apply=False):
    validate(spec)
    journal = Path(journal)
    body, marker = expected_body(spec)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    endpoint = f"projects/{spec['project_id']}/" + ("issues" if spec["kind"] == "issue" else "merge_requests")

    def execute():
        has_journal = journal.exists()
        old = json.loads(journal.read_text(encoding="utf-8")) if has_journal else None
        if has_journal and (not isinstance(old, dict) or old.get("digest") != digest):
            raise PublishError("journal belongs to different input; reconcile the original attempt")
        guard(api, spec)
        found = find_existing(api, spec, endpoint, marker)
        if found:
            result = verify(api, spec, endpoint, found, body)
            if apply:
                save(journal, {"digest": digest, **result})
            return result
        if has_journal:
            raise PublishError("previous attempt is not visible; reconciliation only, no repeated POST")
        if not apply:
            return {"status": "planned", "kind": spec["kind"], "key": spec["key"], "project_path": spec["project_path"]}
        # Guard again at the mutation boundary; a later race is reported on readback.
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
    # Parent must already be an explicitly selected project-local directory.
    with exclusive(journal):
        return execute()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--glab", default="glab", help="Executable path if not on PATH")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        spec = json.loads(args.input.read_text(encoding="utf-8-sig"))
        validate(spec)
        result = publish(spec, Glab(spec["host"], args.glab), args.journal, args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"status": "blocked_or_uncertain", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
