"""Read-only verification of a reviewed linear commit chain and its touched files."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise ValueError("git inspection failed: " + args[0])
    return result.stdout.decode("utf-8", errors="strict").rstrip("\n")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("expected a full lowercase commit SHA")
    return value


def check(data, directory):
    if data.get("version") != 1 or not data.get("cases"):
        raise ValueError("version 1 and a nonempty cases list required")
    repo = (Path(directory) / data["repo"]).resolve()
    target = sha(data["target_sha"])
    if git(repo, "rev-parse", "--verify", data["target_ref"] + "^{commit}") != target:
        raise ValueError("target ref moved; refresh evidence and manifest")
    results, seen = [], set()
    for case in data["cases"]:
        key = case["id"]
        if not key or key in seen:
            raise ValueError("empty or duplicate case id")
        seen.add(key)
        if case["status"] not in {"ready", "partial", "blocked"}:
            raise ValueError("unknown case status")
        base = sha(case["base"])
        commits = [sha(c) for c in case["commits"]]
        if not commits or len(set(commits)) != len(commits):
            raise ValueError(f"{key}: empty or repeated commit chain")
        head = git(repo, "rev-parse", "--verify", case["head_ref"] + "^{commit}")
        if head != commits[-1]:
            raise ValueError(f"{key}: branch head differs from reviewed head")
        parent, touched = base, set()
        for commit in commits:
            if git(repo, "show", "-s", "--format=%P", commit) != parent:
                raise ValueError(f"{key}: missing predecessor, reordered chain or merge commit")
            names = git(repo, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit)
            touched.update(p for p in names.split("\0") if p)
            parent = commit
        expected = case["files"]
        if not expected or len(expected) != len(set(expected)) or touched != set(expected):
            raise ValueError(f"{key}: reviewed file scope differs from full chain")
        results.append({"id": key, "status": case["status"], "head": head, "commits": commits, "files": sorted(touched)})
    return {"ok": all(r["status"] == "ready" for r in results), "target_sha": target, "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        result = check(json.loads(args.manifest.read_text(encoding="utf-8-sig")), args.manifest.parent)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
