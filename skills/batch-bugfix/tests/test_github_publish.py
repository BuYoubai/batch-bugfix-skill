"""Offline behavior checks using GitHub REST response shapes; no credentials."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from github_publish import Gh, PublishError, main, publish

A, B, C = "a" * 40, "b" * 40, "c" * 40


def spec(kind="pr"):
    data = {"version": 1, "host": "github.example.invalid", "repository": "example/demo", "repository_id": 123,
            "kind": kind, "key": "batch-001", "title": "修复已确认问题", "body": "原始复现与正常对照\n\n精确证据"}
    if kind == "pr":
        data.update(head_branch="fix/batch-001", base_branch="main", head_sha=B, base_sha=A, draft=True)
    return data


class GitHubServer:
    """A repository store with optional acknowledgement loss and branch movement."""
    def __init__(self):
        self.repository = {"id": 123, "full_name": "example/demo"}
        self.head, self.base = B, A
        self.objects, self.calls = [], []
        self.timeout = None
        self.hidden = False
        self.rewrite_body = False
        self.move_after_post = False
        self.rewrite_created = None

    @property
    def posts(self):
        return sum(method == "POST" for method, _, _ in self.calls)

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, copy.deepcopy(body)))
        path, query = urlsplit(endpoint).path, parse_qs(urlsplit(endpoint).query)
        if path.casefold() == "repos/example/demo":
            return copy.deepcopy(self.repository)
        if "/branches/" in path:
            branch = unquote(path.split("/branches/")[1])
            return {"name": branch, "commit": {"sha": self.base if branch == "main" else self.head}}
        if method == "POST":
            if self.timeout == "before":
                raise TimeoutError("unknown server outcome")
            number = max((item["number"] for item in self.objects), default=0) + 1
            is_pr = path.endswith("/pulls")
            item = {"number": number, "title": body["title"], "body": body["body"], "state": "open",
                    "html_url": f"https://github.example.invalid/example/demo/{'pull' if is_pr else 'issues'}/{number}"}
            if is_pr:
                item.update(head={"repo": copy.deepcopy(self.repository), "ref": body["head"], "sha": self.head},
                            base={"repo": copy.deepcopy(self.repository), "ref": body["base"], "sha": self.base},
                            draft=body["draft"], merged=False)
            if self.rewrite_body:
                item["body"] = "server changed the reviewed text"
            if self.rewrite_created:
                self.rewrite_created(item)
            self.objects.append(item)
            if self.move_after_post:
                self.base = C
            if self.timeout == "after":
                raise TimeoutError("created but acknowledgement lost")
            return copy.deepcopy(item)
        if query:
            if self.hidden:
                return []
            items = copy.deepcopy(self.objects)
            if path.endswith("/pulls"):
                items = [item for item in items if "head" in item]
            else:
                for item in items:
                    if "head" in item:
                        item["pull_request"] = {"url": "https://api.example.invalid/pulls/1"}
            start = (int(query["page"][0]) - 1) * 100
            return items[start:start + 100]
        return copy.deepcopy(next(item for item in self.objects if item["number"] == int(path.rsplit("/", 1)[1])))


class GitHubPublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="batch-github-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.journal = self.directory / "publication-state.json"
        self.api, self.plan = GitHubServer(), spec()

    def run_publish(self, apply=True):
        return publish(self.plan, self.api, self.journal, apply)

    def test_default_preview_has_no_writes(self):
        self.assertEqual(self.run_publish(False)["status"], "planned")
        self.assertEqual(self.api.posts, 0)
        self.assertFalse(self.journal.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_create_then_repeat_both_kinds_once(self):
        for kind in ("issue", "pr"):
            with self.subTest(kind=kind):
                api, plan = GitHubServer(), spec(kind)
                journal = self.directory / (kind + ".json")
                result = publish(plan, api, journal, True)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["number"], 1)
                self.assertEqual(publish(plan, api, journal, True), result)
                self.assertEqual(api.posts, 1)
                self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "verified")

    def test_lost_acknowledgement_recovers_without_second_post(self):
        self.api.timeout = "after"
        self.assertEqual(self.run_publish()["status"], "verified")
        self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_unknown_absent_outcome_never_reposts(self):
        self.api.timeout = "before"
        with self.assertRaisesRegex(PublishError, "unknown"):
            self.run_publish()
        with self.assertRaisesRegex(PublishError, "reconciliation"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(json.loads(self.journal.read_text())["status"], "uncertain")

    def test_delayed_visibility_recovers_later(self):
        self.api.timeout, self.api.hidden = "after", True
        with self.assertRaises(PublishError):
            self.run_publish()
        self.api.hidden = False
        self.assertEqual(self.run_publish()["number"], 1)
        self.assertEqual(self.api.posts, 1)

    def test_content_change_keeps_pending_and_never_overwrites(self):
        self.api.rewrite_body = True
        with self.assertRaisesRegex(PublishError, "body"):
            self.run_publish()
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(json.loads(self.journal.read_text())["status"], "pending")

    def test_changed_repository_identity_stops_before_post(self):
        for field, value in (("id", 321), ("full_name", "other/repository")):
            with self.subTest(field=field):
                api = GitHubServer()
                api.repository[field] = value
                with self.assertRaisesRegex(PublishError, "identity"):
                    publish(self.plan, api, self.journal, True)
                self.assertEqual(api.posts, 0)

    def test_either_branch_movement_stops_before_post(self):
        for side in ("head", "base"):
            with self.subTest(side=side):
                api = GitHubServer()
                setattr(api, side, C)
                with self.assertRaisesRegex(PublishError, side + " branch moved"):
                    publish(self.plan, api, self.journal, True)
                self.assertEqual(api.posts, 0)

    def test_movement_during_creation_is_not_verified(self):
        self.api.move_after_post = True
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)
        self.assertNotEqual(json.loads(self.journal.read_text())["status"], "verified")

    def test_sha_or_repository_mismatch_in_pr_readback(self):
        for side in ("head", "base"):
            for mismatch in ("sha", "repository"):
                with self.subTest(side=side, mismatch=mismatch):
                    api = GitHubServer()
                    def rewrite(item):
                        if mismatch == "sha":
                            item[side]["sha"] = C
                        else:
                            item[side]["repo"] = {"id": 999, "full_name": "fork/demo"}
                    api.rewrite_created = rewrite
                    journal = self.directory / (side + mismatch + ".json")
                    with self.assertRaises(PublishError):
                        publish(self.plan, api, journal, True)
                    self.assertNotEqual(json.loads(journal.read_text())["status"], "verified")

    def test_same_open_head_wrong_base_is_not_overwritten(self):
        self.run_publish()
        self.api.objects[0]["base"]["ref"] = "release"
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_closed_matching_key_is_not_recreated(self):
        self.run_publish()
        self.api.objects[0]["state"] = "closed"
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_unrelated_closed_pr_allows_branch_reuse(self):
        self.run_publish()
        self.api.objects[0]["state"] = "closed"
        self.api.objects[0]["body"] = "old unrelated batch"
        self.plan["key"] = "new-batch"
        self.journal = self.directory / "new-state.json"
        self.assertEqual(self.run_publish()["number"], 2)
        self.assertEqual(self.api.posts, 2)

    def test_duplicate_keys_are_ambiguous(self):
        self.run_publish()
        other = copy.deepcopy(self.api.objects[0])
        other["number"] = 2
        self.api.objects.append(other)
        with self.assertRaisesRegex(PublishError, "duplicate"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_issue_listing_filters_pr_and_counts_raw_pages(self):
        self.run_publish()  # PR with the same key must not masquerade as an Issue.
        self.plan = spec("issue")
        self.journal = self.directory / "issue-state.json"
        self.api.objects += [{"number": i, "body": "unrelated", "state": "open"} for i in range(2, 101)]
        self.assertEqual(self.run_publish()["number"], 101)
        self.assertEqual(self.run_publish()["number"], 101)
        self.assertEqual(self.api.posts, 2)
        self.assertTrue(any("page=2" in endpoint for _, endpoint, _ in self.api.calls))

    def test_verified_preview_preserves_journal_bytes(self):
        self.run_publish()
        before = self.journal.read_bytes()
        self.assertEqual(self.run_publish(False)["status"], "verified")
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(self.api.posts, 1)

    def test_changed_input_cannot_reuse_journal(self):
        self.run_publish()
        self.plan["body"] = "different content"
        with self.assertRaisesRegex(PublishError, "different input"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_corrupt_or_null_journal_never_allows_creation(self):
        for value in ("null", "{}", "[]", "broken JSON"):
            with self.subTest(value=value):
                self.journal.write_text(value, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_publish()
                self.assertEqual(self.api.posts, 0)

    def test_lock_conflict_stops_before_network(self):
        Path(str(self.journal) + ".lock").write_text("another coordinator")
        with self.assertRaises(FileExistsError):
            self.run_publish()
        self.assertEqual(self.api.calls, [])

    def test_unexpected_url_or_draft_state_is_not_success(self):
        for field, value in (("html_url", "https://other.example.invalid/example/demo/pull/1"),
                             ("html_url", "https://github.example.invalid/other/repo/pull/1"),
                             ("html_url", "https://github.example.invalid/example/demo/pull/2"),
                             ("draft", False), ("merged", True)):
            with self.subTest(field=field, value=value):
                api = GitHubServer()
                api.rewrite_created = lambda item: item.update({field: value})
                journal = self.directory / (str(len(list(self.directory.iterdir()))) + ".json")
                with self.assertRaises(PublishError):
                    publish(self.plan, api, journal, True)

    def test_missing_draft_defaults_to_ready_pr(self):
        del self.plan["draft"]
        self.run_publish()
        self.assertIs(self.api.objects[0]["draft"], False)

    def test_repository_name_case_is_insensitive(self):
        self.plan["repository"] = "Example/Demo"
        self.assertEqual(self.run_publish()["status"], "verified")

    def test_unsupported_or_invalid_input_does_no_network(self):
        mutations = [{"head_repository": "fork/demo"}, {"labels": ["bug"]}, {"host": "https://github.com"},
                     {"repository_id": True}, {"draft": "true"}, {"head_sha": "short"},
                     {"head_branch": "fix/../main"}, {"head_branch": "main"},
                     {"body": "<!-- batch-bugfix:already -->"}, {"version": True}]
        for change in mutations:
            with self.subTest(change=change):
                plan = {**self.plan, **change}
                with self.assertRaises(PublishError):
                    publish(plan, self.api, self.journal, True)
                self.assertEqual(self.api.calls, [])

    def test_cli_reads_bom_and_exact_unicode_text(self):
        input_file = self.directory / "input.json"
        input_file.write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8-sig")
        argv = ["github_publish.py", str(input_file), "--journal", str(self.journal), "--apply"]
        with patch.object(sys, "argv", argv), patch("github_publish.Gh", return_value=self.api), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        self.assertEqual(self.api.objects[0]["body"], self.plan["body"] + "\n\n<!-- batch-bugfix:batch-001 -->")


class GhTransportTests(unittest.TestCase):
    def test_exact_json_stdin_and_explicit_host(self):
        response = subprocess.CompletedProcess([], 0, b'{"ok":true}', b'')
        body = {"body": "中文\n`literal` $(never execute)"}
        with patch("github_publish.subprocess.run", return_value=response) as command:
            self.assertEqual(Gh("enterprise.example.invalid").request("POST", "repos/example/demo/issues", body), {"ok": True})
        args, kwargs = command.call_args
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"]), body)
        self.assertEqual(args[0][-2:], ["--input", "-"])
        self.assertIn("enterprise.example.invalid", args[0])
        self.assertNotIn(body["body"], args[0])

    def test_transport_errors_do_not_expose_stderr(self):
        response = subprocess.CompletedProcess([], 1, b'', b'SECRET_RESPONSE')
        with patch("github_publish.subprocess.run", return_value=response):
            with self.assertRaises(PublishError) as caught:
                Gh("github.com").request("POST", "repos/example/demo/issues", {})
        self.assertNotIn("SECRET_RESPONSE", str(caught.exception))

    def test_unreadable_json_is_not_success(self):
        response = subprocess.CompletedProcess([], 0, b'not JSON', b'')
        with patch("github_publish.subprocess.run", return_value=response):
            with self.assertRaisesRegex(PublishError, "unreadable JSON"):
                Gh("github.com").request("GET", "repos/example/demo")


if __name__ == "__main__":
    unittest.main()
