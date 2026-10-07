"""Combine static analysis + enrichment into the single map.json the renderer draws."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import SCHEMA_VERSION, __version__
from .enrich.heuristic import heuristic_file, heuristic_groups, heuristic_statuses
from .layout import layout


def base_enrichment(analysis: dict) -> dict:
    return {
        "files": {rel: heuristic_file(f) for rel, f in analysis["files"].items()},
        "synth": None,
        "source": "static",
        "models": {},
    }


def _short_label(rel: str) -> str:
    return rel.rsplit("/", 1)[-1].removesuffix(".py")


def _package_of(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] == "src" and len(parts) > 2:
        return "/".join(parts[:3]) if len(parts) > 3 else parts[1]
    return parts[0] if len(parts) > 1 else "(root)"


def assemble(meta: dict, analysis: dict, enrichment: dict) -> dict:
    files_in = analysis["files"]
    synth = enrichment.get("synth") or {}
    statuses = heuristic_statuses(analysis) | synth.get("statuses", {})
    groups = synth.get("groups") or heuristic_groups(analysis, statuses)
    placed = {f for g in groups for f in g["files"]}
    rest = sorted(set(files_in) - placed)
    if rest:
        groups.append(
            {"title": "Other files", "blurb": "", "files": rest, "library": all(files_in[r]["kind"] != "script" for r in rest)}
        )

    used_by: dict[str, set[str]] = defaultdict(set)
    for a, b in analysis["import_edges"]:
        used_by[b].add(a)

    files = {}
    for rel, f in files_in.items():
        e = enrichment["files"].get(rel) or heuristic_file(f)
        blocks = []
        for b in f["blocks"]:
            eb = e["blocks"].get(str(b["id"]), {})
            blocks.append(
                {
                    "id": b["id"],
                    "start": b["start"],
                    "end": b["end"],
                    "name": b.get("name"),
                    "type": b["type"],
                    "label": eb.get("label", ""),
                    "desc": eb.get("desc", ""),
                    "role": eb.get("role", "helper"),
                    "sub": eb.get("sub", []),
                    "uses": b.get("uses_internal", []),
                    "corrected": bool(eb.get("corrected")),
                }
            )
        files[rel] = {
            "path": rel,
            "kind": f["kind"],
            "lines": f["lines"],
            "status": statuses.get(rel, "auxiliary"),
            "purpose": e.get("purpose", ""),
            "reads": e.get("reads", []),
            "writes": e.get("writes", []),
            "notes": e.get("notes", []),
            "verified": bool(e.get("verified")),
            "blocks": blocks,
            "calls": f["calls"],
            "used_by": sorted(used_by.get(rel, [])),
            "imports": sorted(set(f.get("internal_imports", {}).values())),
            "parse_error": f.get("parse_error"),
        }

    # ---- data-flow graph: scripts/modules that touch artifacts, plus the artifacts ----
    lane_of_file = {}
    lane_titles = []
    for g in groups:
        if g.get("library") and not any(files_in[x]["kind"] == "script" for x in g["files"]):
            continue
        lane_titles.append(g["title"])
        for x in g["files"]:
            lane_of_file[x] = g["title"]
    flow_nodes, flow_edges = [], []
    in_flow = {n for e in analysis["flow_edges"] for n in e}
    for rel in files_in:
        if rel in in_flow:
            lane = lane_of_file.get(rel) or ("Library" if files_in[rel]["kind"] == "module" else "Other")
            flow_nodes.append(
                {
                    "id": rel,
                    "kind": "script",
                    "label": _short_label(rel),
                    "lane": lane,
                    "status": statuses.get(rel, "auxiliary"),
                    "file": rel,
                }
            )
    file_lane = {n["id"]: n["lane"] for n in flow_nodes}
    for aid, a in analysis["artifacts"].items():
        if aid not in in_flow:
            continue
        anchor = (a["writers"] or a["readers"] or [None])[0]
        label = a["label"].removeprefix("hf:")
        if len(label) > 46:
            cut = label.rfind("/", 0, len(label) - 20)
            label = (label[: cut + 1] + "\n" + label[cut + 1 :]) if cut > 10 else label
        flow_nodes.append(
            {
                "id": aid,
                "kind": "source" if a["kind"] == "source" else "artifact",
                "label": ("HF: " if a["key"].startswith("hf:") else "") + label,
                "lane": file_lane.get(anchor, "Other"),
                "readers": a["readers"],
                "writers": a["writers"],
            }
        )
    flow_edges = [list(e) for e in analysis["flow_edges"]]
    if "Library" in {n["lane"] for n in flow_nodes}:
        lane_titles.append("Library")
    lane_titles.append("Other")
    graph = layout(flow_nodes, flow_edges, lane_titles) if flow_nodes else {"nodes": [], "lanes": [], "width": 0, "height": 0}
    graph["edges"] = flow_edges

    # ---- script x package import matrix ----
    pkgs = sorted({_package_of(t) for rel, f in files.items() if f["kind"] == "script" for t in f["imports"]})
    matrix = {
        "packages": pkgs,
        "rows": [
            {"file": rel, "uses": sorted({_package_of(t) for t in f["imports"]})}
            for rel, f in sorted(files.items())
            if f["kind"] == "script"
        ],
    }

    run_order = []
    for rel, sh in sorted(analysis["shells"].items()):
        if sh["steps"]:
            run_order.append({"path": rel, "steps": sh["steps"]})

    tests = [
        {"file": rel, "tests": [b["name"] for b in f["blocks"] if (b.get("name") or "").startswith("test")]}
        for rel, f in sorted(files_in.items())
        if f["kind"] == "test"
    ]

    return {
        "schema": SCHEMA_VERSION,
        "generator": f"blockmap {__version__}",
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "meta": meta,
        "enrichment": {"source": enrichment.get("source"), "models": enrichment.get("models", {})},
        "overview": {k: synth.get(k) for k in ("title", "tagline", "overview", "concepts")} if synth else None,
        "findings": synth.get("findings", []) if synth else [],
        "groups": groups,
        "files": files,
        "graph": graph,
        "matrix": matrix,
        "run_order": run_order,
        "tests": tests,
        "other_files": analysis.get("other_files", []),
    }
