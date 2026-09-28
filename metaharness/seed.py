#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"
GA_REPO_PATH = Path(os.environ.get("GA_REPO_PATH", "/opt/genericagent"))
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".git")


def make_seed(force=False):
    dst = AGENTS / "baseline_generic"
    if dst.exists():
        if not force:
            print(f"{dst} exists (use --force to rebuild)")
            return dst
        shutil.rmtree(dst)
    if not (GA_REPO_PATH / "agentmain.py").exists():
        sys.exit(f"GenericAgent not found at {GA_REPO_PATH} (set GA_REPO_PATH)")
    shutil.copytree(GA_REPO_PATH, dst, ignore=IGNORE)
    for sub in ("temp", "memory"):
        (dst / sub).mkdir(exist_ok=True)
    for junk in (dst / "temp").iterdir():
        shutil.rmtree(junk) if junk.is_dir() else junk.unlink()
    print(f"seed: {dst}")
    return dst


def make_candidate(name, force=False):
    diff = ROOT / "metaharness" / f"{name}.diff"
    if not diff.exists():
        sys.exit(f"no diff for candidate {name!r}: {diff}")
    seed = make_seed()
    dst = AGENTS / name
    if dst.exists():
        if not force:
            print(f"{dst} exists (use --force to rebuild)")
            return dst
        shutil.rmtree(dst)
    shutil.copytree(seed, dst, ignore=IGNORE)
    r = subprocess.run(["patch", "-p1", "-s", "-i", str(diff)], cwd=dst, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dst)
        sys.exit(f"patch failed for {name}:\n{r.stdout}{r.stderr}")
    print(f"candidate: {dst}")
    return dst


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", help="candidate name (metaharness/<name>.diff)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    make_candidate(a.candidate, a.force) if a.candidate else make_seed(a.force)
