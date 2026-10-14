"""Check the distributable Skill without external packages or network access."""
import ast
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "batch-bugfix"


def check():
    failures = []
    required = ["SKILL.md", "LICENSE", "scripts/github_publish.py", "scripts/gitlab_publish.py", "scripts/publication_state.py",
                "scripts/check_patch_manifest.py", "scripts/compare_test_results.py"]
    for name in required:
        if not (SKILL / name).is_file():
            failures.append("missing Skill resource: " + name)
    if (SKILL / "LICENSE").read_text(encoding="utf-8") != (ROOT / "LICENSE").read_text(encoding="utf-8"):
        failures.append("installed Skill license differs from repository license")
    for path in ROOT.rglob("*"):
        if ".git" in path.relative_to(ROOT).parts:
            continue
        if path.is_symlink():
            failures.append("package contains a symlink: " + str(path.relative_to(ROOT)))
            continue
        if not path.is_file() or path.suffix not in {".md", ".py", ".json", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            ast.parse(text, filename=str(path))
        if path.suffix == ".json":
            json.loads(text)
        if path.suffix == ".md":
            for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text):
                parsed = urlsplit(link)
                if parsed.scheme or not parsed.path:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                if not target.is_relative_to(ROOT) or not target.is_file():
                    failures.append(f"broken/local-outside-package link: {path.relative_to(ROOT)} -> {link}")
    entry = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    if not re.match(r"\A---\nname: batch-bugfix\ndescription: [^\n]+\n---\n", entry):
        failures.append("unexpected Skill frontmatter")
    sys.path.insert(0, str(SKILL / "scripts"))
    from github_publish import validate
    for path in (SKILL / "assets").glob("github-*.example.json"):
        validate(json.loads(path.read_text(encoding="utf-8")))
    return failures


if __name__ == "__main__":
    failures = check()
    for failure in failures:
        print(failure)
    print("Package checks failed." if failures else "Package checks passed.")
    raise SystemExit(bool(failures))
