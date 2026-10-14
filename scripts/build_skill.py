"""Make the keboola-access-suite skill self-contained, and optionally zip it for Agnes.

    .venv/bin/python scripts/build_skill.py            # refresh skills/keboola-access-suite/assets/suite/
    .venv/bin/python scripts/build_skill.py --zip dist/keboola-access-suite.zip

The skill deploys the suite's code, so it carries a copy of that code in
`assets/suite/`: the library, the apps, the policies, the deploy templates and
the bundle builder. The copy is generated (gitignored) so it can never drift
from the repo unnoticed — rebuild before every upload.

The zip follows the Agnes skill contract: SKILL.md at the root plus
scripts/, references/, assets/; no plugin manifest.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "keboola-access-suite"
SUITE = SKILL / "assets" / "suite"
# What the skill needs from the repo, relative to the repo root.
CONTENTS = [
    "src/kbc_access",
    "apps/__init__.py",
    "apps/admin",
    "apps/launcher",
    "apps/sales",
    "apps/supply",
    "apps/dashboard_common",
    "config/policies",
    "deploy",
    "scripts/build_app_bundle.py",
]
# node_modules/ and dist/ are local frontend build state; bundles build the frontend in the container.
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".DS_Store", "node_modules", "dist")
ZIP_EXCLUDE = {"__pycache__", ".DS_Store"}


def refresh_assets() -> int:
    if SUITE.exists():
        shutil.rmtree(SUITE)
    count = 0
    for rel in CONTENTS:
        src, dst = ROOT / rel, SUITE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=IGNORED)
            count += sum(1 for p in dst.rglob("*") if p.is_file())
        else:
            shutil.copy2(src, dst)
            count += 1
    return count


def write_zip(target: Path) -> int:
    if not (SKILL / "SKILL.md").is_file():
        raise FileNotFoundError(f"{SKILL / 'SKILL.md'} is missing")
    if (SKILL / ".claude-plugin").exists():
        raise ValueError("A skill bundle must not contain .claude-plugin/ (Agnes rejects it as a plugin)")
    target.parent.mkdir(parents=True, exist_ok=True)
    files = [p for p in SKILL.rglob("*") if p.is_file() and not (set(p.relative_to(SKILL).parts) & ZIP_EXCLUDE)]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            zf.write(path, path.relative_to(SKILL).as_posix())
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", type=Path, help="also write an Agnes-ready zip here")
    args = parser.parse_args(argv)
    copied = refresh_assets()
    print(f"assets/suite refreshed: {copied} files", file=sys.stderr)
    if args.zip:
        count = write_zip(args.zip)
        print(f"wrote {args.zip} ({count} files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
