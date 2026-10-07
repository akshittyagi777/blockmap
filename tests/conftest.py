import subprocess
import textwrap
from pathlib import Path

import pytest

PIPE_EXTRACT = '''\
"""Extract features for every model."""
import argparse
from pathlib import Path

import h5py


def _save(path, arr):
    with h5py.File(path, "w") as f:
        f["x"] = arr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="results/features")
    args = ap.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for model in ["a", "b"]:
        _save(out / f"{model}.h5", [1, 2])


if __name__ == "__main__":
    main()
'''

PIPE_FIT = '''\
"""Fit a probe on cached features and write metrics."""
import argparse
import csv
from pathlib import Path

import h5py

from src.lib.metrics import score


def load(path):
    with h5py.File(path, "r") as f:
        return f["x"][:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features-dir", default="results/features")
    ap.add_argument("--output", default="results/metrics.csv")
    args = ap.parse_args()
    rows = []
    for p in sorted(Path(args.features_dir).glob("*.h5")):
        rows.append({"model": p.stem, "score": score(load(p))})
    with open(args.output, "w") as f:
        csv.DictWriter(f, ["model", "score"]).writerows(rows)


if __name__ == "__main__":
    main()
'''

LIB = '''\
"""Scoring helpers."""


def score(x):
    return sum(x) / len(x)
'''

RUN = """\
#!/usr/bin/env bash
set -e
# extract once
uv run python scripts/01_extract.py \\
    --out-dir results/features
if [ -d results/features ]; then
    python3 scripts/02_fit.py
fi
"""

TEST = """\
from src.lib.metrics import score


def test_score():
    assert score([1, 3]) == 2
"""


@pytest.fixture()
def toy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toy"
    files = {
        "scripts/01_extract.py": PIPE_EXTRACT,
        "scripts/02_fit.py": PIPE_FIT,
        "src/lib/__init__.py": "",
        "src/lib/metrics.py": LIB,
        "scripts/run.sh": RUN,
        "tests/test_metrics.py": TEST,
        "README.md": "# Toy\nA toy pipeline.\n",
    }
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo
