#!/usr/bin/env python3
"""Reject release artifacts that contain runtime data, lock files, or secrets."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_SUFFIXES = (".db", ".lock", ".pem", ".key", ".sqlite", ".sqlite3")
SECRET_PATTERNS = (
    re.compile(r"(?im)^\s*(?:AIM_)?(?:LLM_)?API[_-]?KEY\s*=\s*[^\s#]{6,}"),
    re.compile(r"(?i)(?:sk-|Bearer\s+)[A-Za-z0-9_\-]{12,}"),
    re.compile(
        r"(?im)^\s*['\"]?(?:api[_-]?key|password|private[_-]?key|token)['\"]?\s*[:=]\s*['\"]?[^\s'\"]{6,}"
    ),
)
SOURCE_SECRET_PATTERNS = (
    re.compile(r"(?im)^\s*(?:AIM_)?(?:LLM_)?API[_-]?KEY\s*=\s*['\"]?(?:sk-|[A-Za-z0-9_\-]{24,})"),
    SECRET_PATTERNS[1],
    re.compile(
        r"(?im)^\s*['\"]?(?:api[_-]?key|password|private[_-]?key|token)['\"]?\s*:\s*['\"](?:sk-|[A-Za-z0-9_\-]{24,})"
    ),
)
TEXT_SUFFIXES = {
    ".cfg",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(item for item in path.rglob("*") if item.is_file())


def _tracked_release_files(root: Path) -> list[Path]:
    """Return only tracked source files that can become part of a release bundle."""

    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--",
            "app",
            "packaging",
            "alembic.ini",
            "pyproject.toml",
        ],
        check=True,
        capture_output=True,
    )
    return [root / name for name in completed.stdout.decode("utf-8").split("\0") if name]


def _is_forbidden(path: Path) -> bool:
    lowered = path.name.casefold()
    # certifi's public CA trust store is required for HTTPS in the frozen app;
    # it is certificate data, not a private key or user credential.
    trusted_ca_bundle = (
        len(path.parts) >= 2 and path.parts[-2].casefold() == "certifi" and lowered == "cacert.pem"
    )
    return (
        lowered.startswith(".env")
        or lowered.endswith(("-shm", "-wal"))
        or (
            not trusted_ca_bundle and any(lowered.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES)
        )
    )


def _contains_secret(path: Path, *, source_mode: bool = False) -> bool:
    if path.suffix.casefold() not in TEXT_SUFFIXES or path.stat().st_size > 5 * 1024 * 1024:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    # Bundled migration modules are intentionally copied as data for Alembic.
    # Treat Python as source even while inspecting onedir so expressions such
    # as ``"api_key": str(row[...])`` are not mistaken for literal secrets.
    patterns = (
        SOURCE_SECRET_PATTERNS
        if source_mode or path.suffix.casefold() == ".py"
        else SECRET_PATTERNS
    )
    return any(pattern.search(content) for pattern in patterns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--path", type=Path, help="artifact file or unpacked onedir directory")
    mode.add_argument(
        "--source-root", type=Path, help="scan only git-tracked release source inputs"
    )
    parser.add_argument(
        "--require", action="append", default=[], help="required path suffix (repeatable)"
    )
    parser.add_argument(
        "--sha256-out", type=Path, help="write SHA256SUMS-style checksum for a file artifact"
    )
    args = parser.parse_args()
    target = args.path.resolve() if args.path else args.source_root.resolve()
    if not target.exists():
        parser.error(f"inspection target does not exist: {target}")

    source_mode = args.source_root is not None
    files = _tracked_release_files(target) if source_mode else _files(target)
    failures = [
        f"forbidden release content: {item.relative_to(target) if target.is_dir() else item.name}"
        for item in files
        if _is_forbidden(item)
    ]
    failures.extend(
        "possible secret in release content: "
        f"{item.relative_to(target) if target.is_dir() else item.name}"
        for item in files
        if _contains_secret(item, source_mode=source_mode)
    )
    if target.is_dir():
        relative_names = {item.relative_to(target).as_posix() for item in files}
    else:
        relative_names = {target.name}
    for expected in args.require:
        if not any(name.endswith(expected) for name in relative_names):
            failures.append(f"required bundled resource missing: {expected}")

    if failures:
        print("release artifact inspection failed:", *failures, sep="\n- ", file=sys.stderr)
        return 1

    if args.sha256_out:
        if not target.is_file():
            parser.error("--sha256-out accepts a file artifact, not a directory")
        args.sha256_out.parent.mkdir(parents=True, exist_ok=True)
        args.sha256_out.write_text(f"{_sha256(target)}  {target.name}\n", encoding="utf-8")
    inspected = "tracked release source inputs" if source_mode else "release artifact"
    print(f"{inspected} inspection passed: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
