"""Compare named test outcomes; optionally convert JUnit XML to a JSON snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def validate(data):
    if data.get("version") != 1 or not data.get("tests"):
        raise ValueError("version 1 and nonempty tests required")
    for name in ("target_sha", "revision"):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", data.get(name, "")):
            raise ValueError("full SHA required: " + name)
    if not data.get("environment") or not data.get("suite"):
        raise ValueError("environment and suite required")
    tests = {}
    for case in data["tests"]:
        key, outcome = case["id"], case["outcome"]
        if not key or key in tests or outcome not in {"passed", "failed", "error", "skipped"}:
            raise ValueError("duplicate/invalid test identity or outcome")
        if outcome != "passed" and not case.get("fingerprint"):
            raise ValueError("non-passing result requires a reason fingerprint")
        tests[key] = (outcome, case.get("fingerprint", "") if outcome != "passed" else "")
    return tests


def compare(baseline, candidate):
    before, after = validate(baseline), validate(candidate)
    for key in ("target_sha", "environment", "suite"):
        if baseline[key] != candidate[key]:
            raise ValueError("incomparable snapshots: " + key)
    if baseline["revision"] != baseline["target_sha"]:
        raise ValueError("baseline must test the exact target revision")
    missing = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    changed, improved, existing = [], [], []
    for key in sorted(before.keys() & after.keys()):
        if before[key] == after[key]:
            if after[key][0] != "passed":
                existing.append(key)
        elif after[key][0] == "passed":
            improved.append(key)
        else:
            changed.append(key)
    added_bad = [key for key in added if after[key][0] != "passed"]
    return {"ok": not (missing or changed or added_bad), "missing": missing,
            "changed_nonpassing": changed, "new_nonpassing": added_bad,
            "added": added, "improvements": improved, "unchanged_nonpassing": existing,
            "baseline_count": len(before), "candidate_count": len(after)}


def snapshot(path, **metadata):
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("unsupported JUnit root")
    cases = list(root.iter("testcase"))
    case_outcomes = {id(n) for case in cases for n in case if n.tag in {"error", "failure", "skipped"}}
    if any(n.tag in {"error", "failure", "skipped"} and id(n) not in case_outcomes for n in root.iter()):
        raise ValueError("JUnit contains a suite-level outcome without a testcase")
    tests = []
    for case in cases:
        if not case.get("name"):
            raise ValueError("JUnit testcase has no name")
        key = case.get("classname", "") + "::" + case.get("name", "")
        outcomes = [n for n in case if n.tag in {"error", "failure", "skipped"}]
        outcome = "passed"
        record = {"id": key}
        if outcomes:
            tag = "error" if any(n.tag == "error" for n in outcomes) else outcomes[0].tag
            outcome = {"error": "error", "failure": "failed", "skipped": "skipped"}[tag]
            reason = "\n".join(n.tag + " " + n.get("type", "") + " " + n.get("message", "") + "\n" + (n.text or "") for n in outcomes)
            record["fingerprint"] = hashlib.sha256(reason.replace("\r\n", "\n").encode()).hexdigest()
        record["outcome"] = outcome
        tests.append(record)
    # A suite-level collection error without a testcase must not look green.
    for suite in root.iter("testsuite"):
        if list(suite.findall("testsuite")):
            continue
        children = list(suite.findall("testcase"))
        for attr, tag in (("errors", "error"), ("failures", "failure"), ("skipped", "skipped")):
            actual = sum(any(n.tag == tag for n in c) for c in children)
            if attr in suite.attrib and int(suite.get(attr)) != actual:
                raise ValueError("JUnit summary disagrees with collected cases: " + attr)
        if "tests" in suite.attrib and int(suite.get("tests")) != len(children):
            raise ValueError("JUnit summary has missing cases")
    result = {"version": 1, **metadata, "tests": tests}
    validate(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    comp = commands.add_parser("compare")
    comp.add_argument("baseline", type=Path)
    comp.add_argument("candidate", type=Path)
    snap = commands.add_parser("snapshot")
    snap.add_argument("xml", type=Path)
    for flag in ("target-sha", "revision", "environment", "suite", "out"):
        snap.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    try:
        if args.command == "compare":
            result = compare(*[json.loads(p.read_text(encoding="utf-8-sig")) for p in (args.baseline, args.candidate)])
        else:
            result = snapshot(args.xml, target_sha=args.target_sha, revision=args.revision, environment=args.environment, suite=args.suite)
            with Path(args.out).open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            result = {"ok": True, "count": len(result["tests"]), "output": args.out}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
