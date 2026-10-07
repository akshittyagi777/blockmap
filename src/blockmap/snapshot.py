"""Materialise one ref of a git repo (local path or URL) into a scratch directory."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Snapshot:
    root: Path  # extracted tree, read-only by convention
    repo_name: str
    ref: str
    commit: str
    source: str  # what the user passed in

    def as_dict(self) -> dict:
        return {"repo": self.repo_name, "ref": self.ref, "commit": self.commit, "source": self.source}


def _git(args: list[str], cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "git@", "ssh://")) or source.endswith(".git")


def _copy_worktree(repo_dir: Path, dest: Path) -> None:
    """Copy tracked + untracked-but-not-ignored files, i.e. what `git status` would consider."""
    listing = _git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=repo_dir)
    for rel in filter(None, listing.split("\0")):
        src = repo_dir / rel
        if src.is_file():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)


def take_snapshot(source: str, ref: str | None = None, workdir: Path | None = None, worktree: bool = False) -> Snapshot:
    """Export `ref` (default: the repo's current HEAD) without touching the source checkout.

    Local repos are read with `git archive`, so uncommitted changes and the current
    branch of the user's working tree are never modified. URLs are cloned bare into
    the scratch directory first.
    """
    base = Path(workdir or tempfile.mkdtemp(prefix="blockmap-"))
    base.mkdir(parents=True, exist_ok=True)
    if _is_url(source):
        repo_dir = base / "clone.git"
        if not repo_dir.exists():
            _git(["clone", "--bare", "--filter=blob:none", "--quiet", source, str(repo_dir)])
        name = source.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    else:
        repo_dir = Path(_git(["rev-parse", "--show-toplevel"], cwd=Path(source).expanduser()))
        name = repo_dir.name
    tree = base / "tree"
    tree.mkdir(exist_ok=True)
    if worktree:
        if _is_url(source):
            raise ValueError("--worktree needs a local repo, not a URL")
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo_dir, capture_output=True, text=True).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo_dir, capture_output=True, text=True
        ).stdout.strip()
        _copy_worktree(repo_dir, tree)
        return Snapshot(root=tree, repo_name=name, ref=f"{branch or 'HEAD'} (working tree)", commit=head or "", source=source)

    ref = ref or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    commit = _git(["rev-parse", f"{ref}^{{commit}}"], cwd=repo_dir)
    archive = base / "tree.tar"
    _git(["archive", "--format=tar", "-o", str(archive), commit], cwd=repo_dir)
    with tarfile.open(archive) as tf:
        tf.extractall(tree, filter="data")
    archive.unlink()
    return Snapshot(root=tree, repo_name=name, ref=ref, commit=commit, source=source)
