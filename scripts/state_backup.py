"""Create and restore a verified state-directory archive without Docker."""
from __future__ import annotations

import argparse
import os
import tarfile
import tempfile
from pathlib import Path


def backup(state: Path, archive: Path) -> None:
    if not state.is_dir():
        raise ValueError("state directory does not exist")
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=archive.parent, suffix=".tar.gz")
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz") as tar:
            for path in sorted(state.rglob("*")):
                tar.add(path, arcname=path.relative_to(state))
        os.replace(temporary, archive)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore(archive: Path, state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    root = state.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise ValueError("archive contains an unsafe path")
        tar.extractall(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("state", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        backup(args.state, args.archive)
    else:
        restore(args.archive, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
