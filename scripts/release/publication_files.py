"""Copy reviewed paths to a new export or the owner's clean publication clone."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_release import DATA_FILES, EXACT_FILES, ROOT, _regular, valid_path

LISTS = ("01-git", "02-service", "03-qa", "03-judge-fixtures", "04-docs")
BASE = "eb84b1651e32a5a88871b3ba3cdc92295fafd712"
ORIGIN = "https://github.com/slim2115/aml-ai-agent-system-design.git"
PILOT = "verification/qa-step-07-2026-10-20/pilot-first/"
FIXTURES = {PILOT + name for name in (
    "result.json", "qualification.json", "cases/QA-S01/repeat-01.json",
    "cases/QA-S03/repeat-01.json", "cases/QA-S04/repeat-01.json")}
EXTENSIONS = {
    "src": {".py"}, "tests": {".py", ".json"}, "scripts": {".ps1", ".py", ".java"},
    "qa": {".py", ".json", ".md", ".ini", ".lock", ".sql"},
    "docs": {".md", ".txt", ".puml", ".bpmn"}, "requirements": {".lock", ".in"},
    "lab": {".sql", ".md", ".yaml"}, "prompts": {".md"},
}


def eligible(name):
    if not valid_path(name) or name == "qa/progress.json":
        return False
    if name in EXACT_FILES or name in DATA_FILES or name in FIXTURES or name == ".gitattributes":
        return True
    path = Path(name)
    return (all(not p.startswith(".") and p != "__pycache__" for p in path.parts)
            and path.suffix in EXTENSIONS.get(path.parts[0], set()))


def checked_ancestors(path):
    for part in (*reversed(path.parents), path):
        if part.exists() or part.is_symlink():
            _regular(part)


def read_lists():
    seen = set()
    groups = {}
    for group in LISTS:
        manifest = ROOT / "docs/publication" / (group + ".txt")
        checked_ancestors(manifest)
        names = manifest.read_text(encoding="utf-8").splitlines()
        if not names or (group == "03-judge-fixtures" and set(names) != FIXTURES):
            raise ValueError(f"Incomplete publication group: {group}")
        for name in names:
            if (not eligible(name) or name.casefold() in seen or
                    ((name in FIXTURES) != (group == "03-judge-fixtures"))):
                raise ValueError(f"Unexpected or repeated publication path: {name}")
            checked_ancestors(ROOT / name)
            if not (ROOT / name).is_file():
                raise ValueError(f"Missing file: {name}")
            seen.add(name.casefold())
        groups[group] = names
    return groups


def git(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()


def copy(groups, target, mode):
    target = target.absolute()
    if target != target.resolve() or target.is_relative_to(ROOT) or ROOT.is_relative_to(target):
        raise ValueError("Use a separate destination outside the rebuild tree.")
    checked_ancestors(target)
    if mode == "copy-to":
        if (target.name != "aml-ai-agent-system-publish" or
                Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target or
                git(target, "remote", "get-url", "origin") != ORIGIN or
                git(target, "rev-parse", "--abbrev-ref", "HEAD") != "main" or
                git(target, "rev-parse", "HEAD") != BASE or git(target, "rev-parse", "origin/main") != BASE or
                git(target, "status", "--porcelain", "--ignored")):
            raise ValueError("Expected a fresh publication clone on the reviewed main commit.")
    elif target.exists():
        raise ValueError("Export requires a new directory.")
    names = [name for group in groups.values() for name in group]
    for name in names:
        destination = target / name
        checked_ancestors(destination)
        if destination.exists() and not destination.is_file():
            raise ValueError(f"Expected a file: {name}")
    for name in names:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
        if destination.read_bytes() != (ROOT / name).read_bytes():
            raise OSError(f"Copy differs: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("check")
    for mode in ("export", "copy-to"):
        commands.add_parser(mode).add_argument("destination", type=Path)
    args = parser.parse_args()
    groups = read_lists()
    if args.mode != "check":
        copy(groups, args.destination, args.mode)
    print(", ".join(f"{group}={len(names)}" for group, names in groups.items()))


if __name__ == "__main__":
    main()
