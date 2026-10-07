"""Fold model answers into the enrichment record, rejecting anything that does not fit the code."""

from __future__ import annotations

from .prompts import ROLES, STATUSES


def _subs_ok(sub: list[dict], start: int, end: int) -> bool:
    if not sub:
        return True
    if sub[0]["start"] != start or sub[-1]["end"] != end:
        return False
    return all(sub[i]["end"] + 1 == sub[i + 1]["start"] and sub[i]["start"] <= sub[i]["end"] for i in range(len(sub) - 1))


def apply_describe(cur: dict, answer: dict, facts: dict) -> list[str]:
    """Merge a describe answer into `cur` (a heuristic_file record). Returns warnings."""
    warns = []
    bounds = {b["id"]: (b["start"], b["end"]) for b in facts["blocks"]}
    if answer.get("purpose"):
        cur["purpose"] = answer["purpose"].strip()
    for b in answer.get("blocks", []):
        bid = b.get("id")
        if bid not in bounds:
            warns.append(f"unknown block id {bid}")
            continue
        slot = cur["blocks"][str(bid)]
        slot["label"] = (b.get("label") or slot["label"]).strip()
        slot["desc"] = (b.get("desc") or slot["desc"]).strip()
        if b.get("role") in ROLES:
            slot["role"] = b["role"]
        sub = sorted(b.get("sub") or [], key=lambda s: s["start"])
        if _subs_ok(sub, *bounds[bid]):
            slot["sub"] = [{"start": s["start"], "end": s["end"], "label": s["label"]} for s in sub]
        else:
            warns.append(f"block {bid}: phases do not tile lines {bounds[bid][0]}-{bounds[bid][1]}; dropped")
    missing = set(bounds) - {b.get("id") for b in answer.get("blocks", [])}
    if missing:
        warns.append(f"no description for blocks {sorted(missing)}")
    for k in ("reads", "writes"):
        if isinstance(answer.get(k), list):
            cur[k] = sorted({s.strip() for s in answer[k] if s and s.strip()})
    return warns


def apply_verify(cur: dict, answer: dict) -> int:
    n = 0
    for c in answer.get("corrections", []):
        slot = cur["blocks"].get(str(c.get("block_id")))
        if slot is None or c.get("field") not in {"label", "desc", "role"}:
            continue
        if c["field"] == "role" and c["value"] not in ROLES:
            continue
        slot[c["field"]] = c["value"].strip()
        slot.setdefault("corrected", []).append(c["field"])
        n += 1
    if answer.get("purpose"):
        cur["purpose"] = answer["purpose"].strip()
    for k in ("reads", "writes"):
        if isinstance(answer.get(k), list):
            cur[k] = sorted({s.strip() for s in answer[k] if s and s.strip()})
    cur["notes"] = [s.strip() for s in answer.get("notes", []) if s.strip()][:4]
    cur["verified"] = True
    return n


def clean_synth(synth: dict, files: set[str]) -> dict:
    groups, placed = [], set()
    for g in synth.get("groups", []):
        fs = [f for f in g.get("files", []) if f in files and f not in placed]
        placed.update(fs)
        if fs:
            groups.append({"title": g["title"], "blurb": g.get("blurb", ""), "files": fs, "library": bool(g.get("library"))})
    statuses = {
        s["path"]: s["status"] for s in synth.get("statuses", []) if s.get("path") in files and s.get("status") in STATUSES
    }
    return {
        "title": synth.get("title", ""),
        "tagline": synth.get("tagline", ""),
        "overview": [p for p in synth.get("overview", []) if p.strip()][:4],
        "concepts": synth.get("concepts", [])[:8],
        "groups": groups,
        "statuses": statuses,
        "findings": synth.get("findings", [])[:8],
    }
