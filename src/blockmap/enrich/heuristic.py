"""LLM-free labels, roles, statuses and groups, so a map is complete even without an API key."""

from __future__ import annotations

import re
from collections import defaultdict

ROLE_RULES = [
    ("entry", r"^(main|cli|run|app|entry|__main__)$"),
    ("plot", r"(plot|fig|render|draw|chart|viz|style)"),
    ("save", r"(save|write|dump|export|emit|store|persist|to_)"),
    ("load", r"(load|read|fetch|open|parse|ingest|download|discover|iter|_get)"),
    ("compute", r"(fit|train|compute|eval|predict|transform|build|process|score|calib|metric|aggregate|forward|step|solve)"),
]
STATUSES = ("core", "auxiliary", "dormant", "library", "test")


def humanize(name: str) -> str:
    s = re.sub(r"(?<!^)(?=[A-Z][a-z])", " ", name.strip("_"))
    return s.replace("_", " ").strip().lower() or name


def block_role(b: dict, index: int) -> str:
    if b["type"] == "setup":
        return "setup"
    if b["type"] == "guard":
        return "entry"
    name = (b.get("name") or "").lower()
    for role, rx in ROLE_RULES:
        if re.search(rx, name):
            return role
    return "compute" if b["type"] == "class" else "helper"


def heuristic_file(f: dict) -> dict:
    blocks = {}
    for i, b in enumerate(f["blocks"]):
        if b["type"] == "setup":
            label = "imports & constants" if i == 0 else "module-level constants"
        elif b["type"] == "guard":
            label = "run as a script"
        else:
            label = humanize(b["name"])
        blocks[str(b["id"])] = {"label": label, "desc": b.get("doc", "") or "", "role": block_role(b, i), "sub": []}
    reads = sorted({h["path"] for h in f.get("io", []) if h["mode"] == "read"})
    writes = sorted({h["path"] for h in f.get("io", []) if h["mode"] == "write" and h["how"] != "mkdir"})
    purpose = f.get("docstring", "")
    if len(purpose) > 240:
        purpose = purpose[:237].rsplit(" ", 1)[0] + "…"
    return {"purpose": purpose, "blocks": blocks, "reads": reads, "writes": writes}


def heuristic_statuses(analysis: dict) -> dict[str, str]:
    files = analysis["files"]
    imported = {tgt for _, tgt in analysis["import_edges"]}
    in_run = {st.get("file") for sh in analysis["shells"].values() for st in sh["steps"]}
    connected = {n for e in analysis["flow_edges"] for n in e if not n.startswith("a:")}
    out = {}
    for rel, f in files.items():
        if f["kind"] == "test":
            out[rel] = "test"
        elif f["kind"] == "module":
            out[rel] = "library" if rel in imported else "dormant"
        elif rel in in_run or rel in connected:
            out[rel] = "core" if rel in in_run else "auxiliary"
        else:
            out[rel] = "dormant" if not f.get("io") else "auxiliary"
    return out


def heuristic_groups(analysis: dict, statuses: dict[str, str]) -> list[dict]:
    """Scripts: connected components of the data-flow graph. Modules: by package directory."""
    files = analysis["files"]
    scripts = [r for r, f in files.items() if f["kind"] == "script"]
    adj = defaultdict(set)
    for a, b in analysis["flow_edges"]:
        adj[a].add(b)
        adj[b].add(a)
    seen, comps = set(), []
    for s in scripts:
        if s in seen or s not in adj:
            continue
        comp, stack = [], [s]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in files:
                comp.append(n)
            stack.extend(adj[n])
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    groups = []
    for i, comp in enumerate(comps):
        if len(comp) < 2:
            continue
        groups.append(
            {
                "title": "Main pipeline" if i == 0 else f"Pipeline {i + 1}",
                "blurb": "Scripts connected through files they write and read.",
                "files": comp,
            }
        )
    lone = sorted(s for s in scripts if not any(s in g["files"] for g in groups))
    if lone:
        groups.append({"title": "Standalone scripts", "blurb": "Scripts that share no files with other scripts.", "files": lone})
    pkgs: dict[str, list[str]] = defaultdict(list)
    for r, f in files.items():
        if f["kind"] == "module":
            pkgs[r.rsplit("/", 1)[0] if "/" in r else "(root)"].append(r)
    for pkg in sorted(pkgs):
        groups.append({"title": pkg + "/", "blurb": "", "files": sorted(pkgs[pkg]), "library": True})
    tests = sorted(r for r, f in files.items() if f["kind"] == "test")
    if tests:
        groups.append({"title": "Tests", "blurb": "", "files": tests, "library": True})
    return groups
