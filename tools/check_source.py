#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check the complete source inventory, staged blobs and reachable Git history.

This is a publication check, not a replacement for reviewing each new file.
It never prints matching secret contents and never writes the repository.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = "source-files.json"
FORBIDDEN_ROOTS = {"analysis", ".pensieve", "build", ".cache", ".venv"}
FORBIDDEN_NAMES = {"D11.bin", "D12.bin", "disk.img", ".gitmodules", ".env"}
CONTENT_RULES = {
    "private key": re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "cloud access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "embedded credentials": re.compile(rb"https?://[^\s/@:]+:[^\s/@]+@"),
    "archived evidence reference": re.compile(rb"analysis/(?:target-logs|host-logs|reverse-engineering|extracted|recipes)/"),
    "private knowledge reference": re.compile(rb"\.pensieve/(?:short-term|knowledge|decisions)/"),
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def check_blob(path, data, mode="100644"):
    errors = []
    parts = PurePosixPath(path).parts
    if (not parts or parts[0] in FORBIDDEN_ROOTS or ".." in parts
            or path.startswith("/") or any(p in FORBIDDEN_NAMES for p in parts)
            or path.startswith("docs/reference/")):
        errors.append(f"{path}: excluded path")
    if mode not in {"100644", "100755"}:
        errors.append(f"{path}: only regular source files are allowed")
    if len(data) > 2 * 1024 * 1024 or b"\0" in data:
        errors.append(f"{path}: binary or oversized source file")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path}: source is not UTF-8 text")
    for name, pattern in CONTENT_RULES.items():
        if pattern.search(data):
            errors.append(f"{path}: {name}")
    return errors


def tree_entries(revision):
    result = {}
    for entry in git("ls-tree", "-rz", revision).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            result[path.decode()] = (mode, oid)
    return result


def index_entries():
    result = {}
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            if stage != "0":
                raise ValueError("unmerged index")
            result[path.decode()] = (mode, oid)
    return result


def check_entries(entries, label):
    errors = []
    if INVENTORY not in entries:
        return [f"{label}: missing source inventory"]
    inventory = json.loads(git("cat-file", "blob", entries[INVENTORY][1]))
    listed = inventory["files"]
    if len(listed) != len(set(listed)) or set(listed) != set(entries):
        errors.append(f"{label}: inventory differs from committed file set")
    for path, (mode, oid) in entries.items():
        errors.extend(check_blob(path, git("cat-file", "blob", oid), mode))
    return errors


def check_worktree():
    inventory = json.loads((ROOT / INVENTORY).read_text())
    errors = []
    for path in inventory["files"]:
        target = ROOT / path
        if target.is_symlink() or not target.is_file():
            errors.append(f"{path}: missing regular source file")
        elif not target.resolve().is_relative_to(ROOT):
            errors.append(f"{path}: path escapes checkout")
        else:
            errors.extend(check_blob(path, target.read_bytes()))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true", help="check staged blobs, not working files")
    parser.add_argument("--history", action="store_true", help="check every commit reachable from all refs")
    args = parser.parse_args()
    errors = []
    if args.index or args.history:
        gitdir = Path(git("rev-parse", "--absolute-git-dir").decode().strip())
        if (gitdir / "objects/info/alternates").exists() or (ROOT / ".git").is_file():
            errors.append("repository must have its own object database")
    if args.index:
        errors.extend(check_entries(index_entries(), "index"))
    if args.history:
        commits = git("rev-list", "--all").decode().splitlines()
        if not commits:
            errors.append("repository has no reachable commits")
        for commit in commits:
            errors.extend(check_entries(tree_entries(commit), commit[:12]))
            message = git("show", "-s", "--format=%B", commit)
            for name, pattern in CONTENT_RULES.items():
                if pattern.search(message):
                    errors.append(f"{commit[:12]}: commit message contains {name}")
    if not args.index and not args.history:
        errors.extend(check_worktree())
    if errors:
        print("\n".join(sorted(set(errors))), file=sys.stderr)
        return 1
    print("PASS: source content boundary")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"source check failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
