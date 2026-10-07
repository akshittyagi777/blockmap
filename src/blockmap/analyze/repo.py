"""Walk a snapshot, analyse every file, and assemble the repo-level map (files, imports, data flow)."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path

from .python_ast import analyze_python
from .shell import analyze_shell

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "build",
    "dist",
    ".tox",
    ".idea",
    ".vscode",
    "site-packages",
    ".eggs",
}
OTHER_CODE_EXTS = {
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".scala",
    ".r",
    ".R",
    ".jl",
    ".m",
    ".php",
    ".lua",
    ".sql",
}
MAX_FILE_BYTES = 400_000


def _kind(rel: str, has_main_guard: bool) -> str:
    parts = rel.split("/")
    name = parts[-1]
    if name.startswith("test_") or name.endswith("_test.py") or "tests" in parts[:-1] or name == "conftest.py":
        return "test"
    if has_main_guard or parts[0] in {"scripts", "bin", "tools", "experiments", "examples"}:
        return "script"
    return "module"


def _module_index(py_files: list[str]) -> dict[str, str]:
    """Map importable dotted names to repo files, with and without a leading src/ layout dir."""
    idx: dict[str, str] = {}
    for rel in py_files:
        mod = rel[:-3].replace("/", ".")
        mod = mod.removesuffix(".__init__")
        idx[mod] = rel
        if mod.startswith("src."):
            idx.setdefault(mod[4:], rel)
    return idx


def _resolve_import(spec: str, importer: str, idx: dict[str, str]) -> str | None:
    """Return the repo file an import string points at (longest matching module prefix)."""
    if spec.startswith("."):
        level = len(spec) - len(spec.lstrip("."))
        base = importer[:-3].replace("/", ".").split(".")
        base = base[: len(base) - level]
        spec = ".".join([*base, spec.lstrip(".")]).strip(".")
    parts = spec.split(".")
    for k in range(len(parts), 0, -1):
        cand = ".".join(parts[:k])
        if cand in idx:
            return idx[cand]
    return None


def normalize_path(p: str) -> str:
    """Canonical artifact key: placeholders -> '*', no leading ./, collapse '//'."""
    if p.startswith("hf:"):
        return p
    p = re.sub(r"\{[^{}]*\}", "*", p)
    p = re.sub(r"\*+", "*", p).replace("//", "/")
    p = p.removeprefix("./")
    return p.rstrip("/")


def _is_dirlike(key: str) -> bool:
    last = key.rsplit("/", 1)[-1]
    return "." not in last or last == "*"


_GLOBBY = ("glob", "rglob", "iterdir")


def build_artifacts(files: dict[str, dict]) -> tuple[dict[str, dict], list[list[str]]]:
    """Merge every script's read/write paths into shared artifact nodes and return (artifacts, edges).

    Concrete paths (a file pattern with an extension, wildcards only where an f-string had a
    placeholder) are merged when one pattern matches the other. Directories and glob/iterdir
    patterns are containers: they never merge things together; a reader of a container gets an
    edge from every concrete artifact inside it. `mkdir` calls are ignored.
    """
    concrete: dict[str, dict] = {}
    containers: list[tuple[str, str, str, str]] = []  # (key, label, rel, mode)
    for rel, f in files.items():
        for h in f.get("io", []):
            if h["mode"] == "mention" or h["how"] == "mkdir" or "{repo_root}" in h["path"] or h["path"].startswith("{"):
                continue
            key = normalize_path(h["path"])
            if not key or key in {"*", "."}:
                continue
            globby = any(g in h["how"] for g in _GLOBBY)
            if not key.startswith("hf:") and (globby or _is_dirlike(key)):
                containers.append((key, h["path"], rel, h["mode"]))
                continue
            a = concrete.setdefault(key, {"labels": set(), "readers": set(), "writers": set()})
            a["labels"].add(h["path"])
            (a["writers"] if h["mode"] == "write" else a["readers"]).add(rel)

    parent = {k: k for k in concrete}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    keys = sorted(concrete, key=len)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if a.startswith("hf:") or b.startswith("hf:"):
                continue
            if a.count("/") == b.count("/") and (fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)):
                parent[find(b)] = find(a)

    groups: dict[str, list[str]] = {}
    for k in concrete:
        groups.setdefault(find(k), []).append(k)
    artifacts: dict[str, dict] = {}
    key_to_aid: dict[str, str] = {}
    for root, members in groups.items():
        readers = set().union(*(concrete[m]["readers"] for m in members))
        writers = set().union(*(concrete[m]["writers"] for m in members))
        written = [m for m in members if concrete[m]["writers"]]
        pick = max(written or members, key=lambda m: (m.count("/"), -m.count("*"), len(m)))
        label = min(concrete[pick]["labels"], key=len)
        aid = "a:" + hashlib.sha1(root.encode()).hexdigest()[:10]
        artifacts[aid] = {
            "id": aid,
            "label": label,
            "key": root,
            "members": sorted(members),
            "kind": "source" if root.startswith("hf:") or not writers else "artifact",
            "readers": set(readers),
            "writers": set(writers),
        }
        for m in members:
            key_to_aid[m] = aid

    # containers: readers fan in from every concrete artifact inside; otherwise they stand alone
    for key, label, rel, mode in containers:
        pattern = key if not _is_dirlike(key) or key.endswith("*") else key + "/*"
        inside = {key_to_aid[k] for k in concrete if fnmatch.fnmatch(k, pattern) or k.startswith(key + "/")}
        if inside and mode == "read":
            for aid in inside:
                artifacts[aid]["readers"].add(rel)
            continue
        if inside:
            continue  # a writer of a whole directory: its specific writes are already recorded
        if "/" not in key and not key.startswith("hf:"):
            continue  # a bare top-level dir like "results" is too vague to draw
        aid = "a:" + hashlib.sha1(("c:" + key).encode()).hexdigest()[:10]
        a = artifacts.setdefault(
            aid,
            {
                "id": aid,
                "label": label,
                "key": key,
                "members": [key],
                "kind": "artifact",
                "readers": set(),
                "writers": set(),
                "container": True,
            },
        )
        (a["writers"] if mode == "write" else a["readers"]).add(rel)

    edges: list[list[str]] = []
    for aid, a in artifacts.items():
        if not a["writers"]:
            a["kind"] = "source"
        a["readers"], a["writers"] = sorted(a["readers"]), sorted(a["writers"])
        edges += [[w, aid] for w in a["writers"]] + [[aid, r] for r in a["readers"] if r not in a["writers"]]
    return artifacts, edges


def analyze_repo(root: Path, include: list[str] | None = None, exclude: list[str] | None = None) -> dict:
    files: dict[str, dict] = {}
    shells: dict[str, dict] = {}
    others: list[dict] = []
    readme = ""
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root).as_posix()
        if include and not any(fnmatch.fnmatch(rel, g) for g in include):
            continue
        if exclude and any(fnmatch.fnmatch(rel, g) for g in exclude):
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        if rel.lower() in {"readme.md", "readme.rst", "readme.txt", "readme"} and not readme:
            readme = p.read_text(errors="replace")[:12000]
        if p.suffix == ".py":
            src = p.read_text(errors="replace")
            facts = analyze_python(rel, src)
            files[rel] = {
                "path": rel,
                "lang": "python",
                "lines": facts.lines,
                "docstring": facts.docstring,
                "blocks": facts.blocks,
                "calls": facts.calls,
                "imports": facts.imports,
                "io": [h.as_dict() for h in facts.io],
                "cli_args": facts.cli_args,
                "has_main_guard": facts.has_main_guard,
                "parse_error": facts.parse_error,
                "sha": hashlib.sha256(src.encode()).hexdigest(),
            }
        elif p.suffix in {".sh", ".bash"}:
            shells[rel] = analyze_shell(rel, p.read_text(errors="replace"))
        elif p.suffix in OTHER_CODE_EXTS:
            others.append({"path": rel, "lines": p.read_text(errors="replace").count("\n") + 1})

    idx = _module_index(list(files))
    for rel, f in files.items():
        f["kind"] = _kind(rel, f["has_main_guard"])
        internal: dict[str, str] = {}
        for local, spec in f["imports"].items():
            target = _resolve_import(spec, rel, idx)
            if target and target != rel:
                internal[local] = target
        f["internal_imports"] = internal
        for b in f["blocks"]:
            if "uses" in b:
                b["uses_internal"] = sorted({f"{internal[n]}:{n}" for n in b["uses"] if n in internal})
                b["uses_external"] = sorted(n for n in b["uses"] if n not in internal)
                del b["uses"]

    # shell steps -> resolve targets to repo files
    for sh in shells.values():
        for st in sh["steps"]:
            t = st["target"]
            st["file"] = (
                t if t in files or t in shells else next((r for r in [*files, *shells] if r.endswith("/" + t) or r == t), None)
            )

    artifacts, edges = build_artifacts(files)
    module_edges = sorted({(rel, tgt) for rel, f in files.items() for tgt in f["internal_imports"].values()})
    return {
        "files": files,
        "shells": shells,
        "other_files": others,
        "readme": readme,
        "artifacts": artifacts,
        "flow_edges": edges,
        "import_edges": [list(e) for e in module_edges],
    }
