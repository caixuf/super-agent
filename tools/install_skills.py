#!/usr/bin/env python3
"""Install this repo's generic skills into a Hermes profile (idempotent).

    python3 tools/install_skills.py --profile caixuf [--dry-run] [--only name ...]

Rules (see skills/README.md): ``skills/<name>/SKILL.md`` is installed; ``skills/examples/``
is a template and never installed. A skill whose directory name already exists but whose
content differs is reported as CONFLICT rather than overwritten — a local edit may be
better than the repo copy, and that decision belongs to a human.
"""
from __future__ import annotations

import argparse
import filecmp
import os
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]


def skills_dir():
    return REPO / "skills"


def generic_skills(only=None):
    found = sorted(p for p in skills_dir().iterdir()
                   if p.is_dir() and p.name != "examples" and (p / "SKILL.md").is_file())
    if only:
        found = [p for p in found if p.name in set(only)]
    return found


def skill_dest(profile, name, home=""):
    """``--home`` targets a bare Hermes home (installer's ``--hermes-home``); else a profile."""
    base = Path(os.path.expanduser(home)) if home else (
        Path(os.path.expanduser("~/.hermes")) / "profiles" / profile)
    return base / "skills" / name


def _same(source, target):
    comparison = filecmp.dircmp(source, target, ignore=["__pycache__"])
    if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
        return False
    return all(_same(source / d, target / d) for d in comparison.common_dirs)


def install(profile, *, dry_run=False, only=None, home=""):
    rows = []
    for source in generic_skills(only):
        target = skill_dest(profile, source.name, home)
        if target.exists() and _same(source, target):
            rows.append(("UNCHANGED", source.name, "", str(target)))
            continue
        if target.exists():
            rows.append(("CONFLICT", source.name, "", str(target)))
            continue
        rows.append(("INSTALL", source.name, "", str(target)))
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="", help="profile name under ~/.hermes/profiles")
    parser.add_argument("--home", default="", help="a bare Hermes home (same as the installer's --hermes-home)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args(argv)
    if not args.profile and not args.home:
        parser.error("need --profile or --home")
    rows = install(args.profile, dry_run=args.dry_run, only=args.only, home=args.home)
    for status, name, _note, target in rows:
        print("%-9s %-28s %s" % (status, name, target))
    conflicts = [row for row in rows if row[0] == "CONFLICT"]
    if conflicts:
        print("\n%d skill(s) differ from the repo copy; inspect before replacing:" %
              len(conflicts), file=sys.stderr)
        for _status, name, _note, target in conflicts:
            print("  " + target, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
