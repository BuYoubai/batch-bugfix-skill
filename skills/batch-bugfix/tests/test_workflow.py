"""Behavior tests; generated fixtures stay under a new temporary test directory."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_patch_manifest import check
from compare_test_results import compare, snapshot
from gitlab_publish import publish, PublishError, Glab
from unittest.mock import patch

ROOT = None
A, B, C = "a" * 40, "b" * 40, "c" * 40


def setUpModule():
    global ROOT
    fixtures = tempfile.TemporaryDirectory(prefix="batch-bugfix-tests-")
    ROOT = Path(fixtures.name)
    unittest.addModuleCleanup(fixtures.cleanup)


def workdir():
    return Path(tempfile.mkdtemp(dir=ROOT))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.repo = workdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture Author")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.base = self.commit("module.txt", "initial\n")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.first = self.commit("module.txt", "fix\n")
        self.last = self.commit("test.txt", "follow-up\n")
        self.git("update-ref", "refs/heads/fix/case", self.last)
        self.data = {"version": 1, "repo": str(self.repo), "target_ref": "refs/remotes/origin/main", "target_sha": self.base,
                     "cases": [{"id": "one", "status": "ready", "base": self.base, "commits": [self.first, self.last],
                                "head_ref": "refs/heads/fix/case", "files": ["module.txt", "test.txt"]}]}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", *args], stderr=subprocess.PIPE).decode().strip()

    def commit(self, file, text):
        (self.repo / file).write_text(text, encoding="utf-8")
        self.git("add", file)
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_complete_chain(self):
        self.assertTrue(check(self.data, ROOT)["ok"])

    def test_missing_predecessor(self):
        self.data["cases"][0]["commits"] = [self.last]
        with self.assertRaisesRegex(ValueError, "predecessor"):
            check(self.data, ROOT)

    def test_unreviewed_followup(self):
        new = self.commit("module.txt", "unexpected\n")
        self.git("update-ref", "refs/heads/fix/case", new)
        with self.assertRaisesRegex(ValueError, "head"):
            check(self.data, ROOT)

    def test_stale_replacement(self):
        self.git("update-ref", "refs/heads/fix/case", self.first)
        with self.assertRaisesRegex(ValueError, "head"):
            check(self.data, ROOT)

    def test_target_moved(self):
        self.git("update-ref", "refs/remotes/origin/main", self.first)
        with self.assertRaisesRegex(ValueError, "target"):
            check(self.data, ROOT)

    def test_touched_scope_includes_reverted_files(self):
        reverted = self.commit("module.txt", "initial\n")
        self.git("update-ref", "refs/heads/fix/case", reverted)
        case = self.data["cases"][0]
        case["commits"].append(reverted)
        case["files"] = ["test.txt"]
        with self.assertRaisesRegex(ValueError, "scope"):
            check(self.data, ROOT)

    def test_partial_is_not_ready(self):
        self.data["cases"][0]["status"] = "partial"
        self.assertFalse(check(self.data, ROOT)["ok"])


def results(tests, revision=A):
    return {"version": 1, "target_sha": A, "revision": revision, "environment": "synthetic-env", "suite": "all-local", "tests": tests}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.baseline = results([{"id": "test.a", "outcome": "passed"}, {"id": "test.b", "outcome": "failed", "fingerprint": "known reason"}])
        self.candidate = copy.deepcopy(self.baseline)
        self.candidate["revision"] = B

    def test_known_failure_and_new_pass(self):
        self.candidate["tests"].append({"id": "test.new", "outcome": "passed"})
        report = compare(self.baseline, self.candidate)
        self.assertTrue(report["ok"])
        self.assertEqual(report["unchanged_nonpassing"], ["test.b"])

    def test_equal_counts_hide_missing_case(self):
        self.candidate["tests"][0]["id"] = "test.other"
        self.assertEqual(compare(self.baseline, self.candidate)["missing"], ["test.a"])

    def test_failure_fingerprint_changes(self):
        self.candidate["tests"][1]["fingerprint"] = "different defect"
        self.assertFalse(compare(self.baseline, self.candidate)["ok"])

    def test_new_skip_and_regression(self):
        for outcome in ("failed", "error", "skipped"):
            with self.subTest(outcome=outcome):
                candidate = copy.deepcopy(self.candidate)
                candidate["tests"][0].update(outcome=outcome, fingerprint="new")
                self.assertFalse(compare(self.baseline, candidate)["ok"])

    def test_added_nonpassing(self):
        self.candidate["tests"].append({"id": "test.new", "outcome": "skipped", "fingerprint": "disabled"})
        self.assertFalse(compare(self.baseline, self.candidate)["ok"])

    def test_improvement(self):
        self.candidate["tests"][1] = {"id": "test.b", "outcome": "passed"}
        self.assertEqual(compare(self.baseline, self.candidate)["improvements"], ["test.b"])

    def test_wrong_environment_or_baseline(self):
        for key in ("environment", "suite", "target_sha"):
            with self.subTest(key=key):
                candidate = copy.deepcopy(self.candidate)
                candidate[key] = C if key == "target_sha" else "other"
                with self.assertRaises(ValueError):
                    compare(self.baseline, candidate)
        self.baseline["revision"] = C
        with self.assertRaises(ValueError):
            compare(self.baseline, self.candidate)

    def test_duplicate_or_empty_reports(self):
        for tests in ([], [self.baseline["tests"][0]] * 2):
            with self.subTest(tests=tests), self.assertRaises(ValueError):
                compare(self.baseline, results(tests, B))

    def test_junit_observable_cases(self):
        xml = workdir() / "result.xml"
        xml.write_text('<testsuite tests="2" failures="1"><testcase classname="unit" name="case[a]"/><testcase classname="unit" name="case[b]"><failure message="wrong">trace</failure></testcase></testsuite>', encoding="utf-8")
        data = snapshot(xml, target_sha=A, revision=A, environment="env", suite="unit")
        self.assertEqual(data["tests"][1]["id"], "unit::case[b]")
        self.assertEqual(data["tests"][1]["outcome"], "failed")
        self.assertTrue(data["tests"][1]["fingerprint"])

    def test_junit_collection_error_is_not_green(self):
        xml = workdir() / "result.xml"
        xml.write_text('<testsuite tests="1" errors="1"><testcase name="ok"/></testsuite>', encoding="utf-8")
        with self.assertRaises(ValueError):
            snapshot(xml, target_sha=A, revision=A, environment="env", suite="unit")

    def test_junit_standalone_error_or_nameless_case(self):
        xml = workdir() / "result.xml"
        for text in ('<testsuite><testcase name="ok"/><error>collection failed</error></testsuite>',
                     '<testsuite><testcase/></testsuite>'):
            with self.subTest(text=text):
                xml.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    snapshot(xml, target_sha=A, revision=A, environment="env", suite="unit")


def spec(kind="mr"):
    result = {"version": 1, "host": "gitlab.example.invalid", "project_id": 7, "project_path": "example/demo", "kind": kind,
              "key": "case-01", "title": "修复可复现问题", "description": "预期与实际\n\n复现：`sample`"}
    if kind == "mr":
        result.update(source_branch="fix/case-01", target_branch="main", source_sha=B, target_sha=A)
    return result


class Server:
    """A remote store whose creation may commit before the client sees a timeout."""
    def __init__(self):
        self.objects = []
        self.calls = []
        self.source, self.target = B, A
        self.timeout = None
        self.rewrite_body = False
        self.move_after_post = False
        self.hidden = False

    def request(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, copy.deepcopy(body)))
        path, query = urlsplit(endpoint).path, parse_qs(urlsplit(endpoint).query)
        if path == "projects/7":
            return {"id": 7, "path_with_namespace": "example/demo"}
        if "/repository/branches/" in path:
            name = unquote(path.split("/branches/")[1])
            return {"commit": {"id": self.target if name == "main" else self.source}}
        if method == "POST":
            if self.timeout == "before":
                raise TimeoutError("no visible result")
            item = {**body, "iid": len(self.objects) + 1, "project_id": 7, "source_project_id": 7,
                    "target_project_id": 7, "state": "opened", "sha": self.source,
                    "web_url": "https://gitlab.example.invalid/example/demo/-/objects/1"}
            if self.rewrite_body:
                item["description"] = "unexpected server body"
            self.objects.append(item)
            if self.move_after_post:
                self.target = C
            if self.timeout == "after":
                raise TimeoutError("server committed; acknowledgement lost")
            return copy.deepcopy(item)
        if query:
            if self.hidden:
                return []
            items = self.objects
            if "source_branch" in query:
                items = [i for i in items if i.get("source_branch") == query["source_branch"][0]]
            start = (int(query["page"][0]) - 1) * 100
            return copy.deepcopy(items[start:start + 100])
        return copy.deepcopy(next(i for i in self.objects if i["iid"] == int(path.rsplit("/", 1)[1])))

    @property
    def posts(self):
        return sum(m == "POST" for m, _, _ in self.calls)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.api, self.plan = Server(), spec()
        self.journal = workdir() / "publication.json"

    def run_publish(self, apply=True):
        return publish(self.plan, self.api, self.journal, apply)

    def test_plan_is_readonly(self):
        self.assertEqual(self.run_publish(False)["status"], "planned")
        self.assertEqual(self.api.posts, 0)
        self.assertFalse(self.journal.exists())

    def test_create_and_rerun_both_kinds(self):
        for kind in ("issue", "mr"):
            with self.subTest(kind=kind):
                self.setUp()
                self.plan = spec(kind)
                self.assertEqual(self.run_publish()["status"], "verified")
                self.assertEqual(self.run_publish()["iid"], 1)
                self.assertEqual(self.api.posts, 1)

    def test_timeout_after_server_commit_recovers_without_second_post(self):
        self.api.timeout = "after"
        self.assertEqual(self.run_publish()["status"], "verified")
        self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_unknown_absent_does_not_retry(self):
        self.api.timeout = "before"
        with self.assertRaises(PublishError):
            self.run_publish()
        with self.assertRaisesRegex(PublishError, "reconciliation"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_readback_mismatch_is_not_success(self):
        self.api.rewrite_body = True
        with self.assertRaisesRegex(PublishError, "description"):
            self.run_publish()
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)
        self.assertEqual(json.loads(self.journal.read_text())["status"], "pending")

    def test_upstream_move_before_post(self):
        self.api.target = C
        with self.assertRaisesRegex(PublishError, "target branch moved"):
            self.run_publish()
        self.assertEqual(self.api.posts, 0)

    def test_upstream_move_during_post_leaves_unverified(self):
        self.api.move_after_post = True
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)
        self.assertNotEqual(json.loads(self.journal.read_text())["status"], "verified")

    def test_source_move(self):
        self.api.source = C
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 0)

    def test_wrong_project_path_blocks(self):
        self.plan["project_path"] = "another/project"
        with self.assertRaisesRegex(PublishError, "project identity"):
            self.run_publish()
        self.assertEqual(self.api.posts, 0)

    def test_pending_request_becomes_visible_later(self):
        self.api.timeout, self.api.hidden = "after", True
        with self.assertRaises(PublishError):
            self.run_publish()
        self.api.hidden = False
        self.assertEqual(self.run_publish()["status"], "verified")
        self.assertEqual(self.api.posts, 1)

    def test_same_source_wrong_target_is_not_overwritten(self):
        self.run_publish()
        self.api.objects[0]["target_branch"] = "release"
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_journal_scope_cannot_change(self):
        self.run_publish()
        self.plan["description"] = "different reviewed body"
        with self.assertRaisesRegex(PublishError, "different input"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_existing_invalid_journal_prevents_new_creation(self):
        for content in ("null", "[]", "{}", "invalid JSON"):
            with self.subTest(content=content):
                self.setUp()
                self.journal.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.run_publish()
                self.assertEqual(self.api.calls, [])

    def test_existing_closed_object_not_recreated(self):
        self.run_publish()
        self.api.objects[0]["state"] = "closed"
        with self.assertRaises(PublishError):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_ambiguous_duplicates_block(self):
        self.run_publish()
        other = copy.deepcopy(self.api.objects[0])
        other["iid"] = 2
        self.api.objects.append(other)
        with self.assertRaisesRegex(PublishError, "duplicate"):
            self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_issue_on_second_page_is_found(self):
        self.plan = spec("issue")
        self.run_publish()
        target = self.api.objects[0]
        target["iid"] = 101
        self.api.objects = [{"iid": n, "description": "unrelated"} for n in range(1, 101)] + [target]
        self.run_publish()
        self.assertEqual(self.api.posts, 1)

    def test_lock_prevents_parallel_apply(self):
        Path(str(self.journal) + ".lock").write_text("another process")
        with self.assertRaises(FileExistsError):
            self.run_publish()
        self.assertEqual(self.api.calls, [])

    def test_glab_uses_structured_stdin_not_shell(self):
        response = subprocess.CompletedProcess([], 0, b'{"ok":true}', b'')
        body = {"description": "中文\n`literal` $(never execute)"}
        with patch("gitlab_publish.subprocess.run", return_value=response) as command:
            self.assertEqual(Glab("gitlab.example.invalid").request("POST", "projects/7/issues", body), {"ok": True})
        args, kwargs = command.call_args
        self.assertNotIn("shell", kwargs)
        self.assertEqual(json.loads(kwargs["input"]), body)
        self.assertEqual(args[0][-2:], ["--input", "-"])


if __name__ == "__main__":
    unittest.main()
