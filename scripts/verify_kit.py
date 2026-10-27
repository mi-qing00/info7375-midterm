#!/usr/bin/env python3
"""
verify_kit.py - prove that you did not change a provided file.   (PROVIDED: do not modify)

    python3 scripts/verify_kit.py

The provided files (the baseline stack, the gate, the load tests, the tools) are listed with their SHA-256
checksums in kit_manifest.json. This script recomputes each checksum and reports:

    MODIFIED   a provided file whose contents changed
    MISSING    a provided file that is gone
    EXTRA      a file you added inside a provided folder (scripts/, load-test/, lambdas/order_producer/ and
               the three lambdas/*_consumer/ folders). Put your own files elsewhere.

Line endings do not matter: a Windows checkout does not count as a change. Exit code 0 means every provided
file is intact; 1 means something changed; 2 means the manifest itself cannot be trusted or read.

The instructor checks your submission with the master manifest, not yours. The line "manifest sha256" lets you
confirm that your manifest is the one that was announced:

    python3 scripts/verify_kit.py --expect-digest <the value your instructor announced>
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parent.parent
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}
IGNORED_SUFFIXES = {".pyc"}


def normalized(data: bytes) -> bytes:
    """Make Windows and Unix checkouts hash the same."""
    return data.replace(b"\r\n", b"\n")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(normalized(data)).hexdigest()


def extra_files(repo: Path, manifest: dict) -> list:
    known = set(manifest["files"]) | set(manifest.get("allowed_extra", []))
    found = []
    for folder in manifest.get("locked_dirs", []):
        base = repo / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if IGNORED_PARTS & set(path.parts) or path.suffix in IGNORED_SUFFIXES:
                continue
            rel = path.relative_to(repo).as_posix()
            if rel not in known:
                found.append(rel)
    return found


def verify(repo: Path, manifest: dict):
    """Return (modified, missing, extra, ok_count)."""
    modified, missing, ok = [], [], 0
    for rel, expected in sorted(manifest["files"].items()):
        path = repo / rel
        if not path.is_file():
            missing.append(rel)
        elif sha256_hex(path.read_bytes()) != expected:
            modified.append(rel)
        else:
            ok += 1
    return modified, missing, extra_files(repo, manifest), ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check that the provided files are unchanged.")
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="the repository to check (default: this one)")
    parser.add_argument("--manifest", default=None, help="the manifest to check against (default: kit_manifest.json)")
    parser.add_argument("--expect-digest", default=None, help="the manifest's announced SHA-256")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    manifest_path = Path(args.manifest) if args.manifest else repo / "kit_manifest.json"
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        manifest["files"]
    except (OSError, ValueError, KeyError) as e:
        print(f"cannot read the manifest {manifest_path}: {e}")
        return 2

    digest = sha256_hex(raw)
    print(f"manifest sha256: {digest}")
    if args.expect_digest and args.expect_digest.strip().lower() != digest:
        print(f"RESULT: FAIL - this manifest is not the announced one (expected {args.expect_digest.strip().lower()})")
        return 2

    modified, missing, extra, ok = verify(repo, manifest)
    print(f"checked {len(manifest['files'])} provided files")
    print(f"  OK        {ok}")
    for label, items in (("MODIFIED", modified), ("MISSING", missing), ("EXTRA", extra)):
        print(f"  {label:<9} {len(items)}")
        for rel in items:
            print(f"            {rel}")
    if modified or missing or extra:
        print("RESULT: FAIL - a provided file changed. Restore it from the original starter.")
        return 1
    print("RESULT: PASS - every provided file is intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
