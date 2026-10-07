"""Export the describe/verify/synthesize prompts as files and import the answers back.

This is how the Claude Code skill enriches a map without an API key: subagents read the
task files, write JSON answers next to them, and `blockmap apply` folds them in with the
same validation the API path uses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .merge import apply_describe, apply_verify, clean_synth
from .prompts import (
    DESCRIBE_SCHEMA,
    DESCRIBE_SYSTEM,
    SYNTH_SCHEMA,
    SYNTH_SYSTEM,
    VERIFY_SCHEMA,
    VERIFY_SYSTEM,
    describe_user,
    repo_context,
    synth_user,
)


def _safe(rel: str) -> str:
    return re.sub(r"[^\w.-]+", "__", rel)


def export_tasks(analysis: dict, meta: dict, root: Path, out: Path, batch_lines: int = 2500) -> dict:
    """Write describe tasks in batches of roughly `batch_lines` source lines each."""
    out.mkdir(parents=True, exist_ok=True)
    ctx = repo_context(analysis, meta)
    (out / "CONTEXT.md").write_text(ctx + "\n")
    targets = sorted(
        (r for r, f in analysis["files"].items() if f["kind"] != "test" and not f.get("parse_error")), key=lambda r: r
    )
    batches, cur, size = [], [], 0
    for rel in targets:
        n = analysis["files"][rel]["lines"]
        if cur and size + n > batch_lines:
            batches.append(cur)
            cur, size = [], 0
        cur.append(rel)
        size += n
    if cur:
        batches.append(cur)
    index = {"describe": [], "verify_instructions": "VERIFY.md", "synth_instructions": "SYNTH.md"}
    for i, batch in enumerate(batches, start=1):
        bdir = out / f"describe_{i:02d}"
        bdir.mkdir(exist_ok=True)
        parts = [
            DESCRIBE_SYSTEM,
            "",
            "Answer JSON schema:",
            json.dumps(DESCRIBE_SCHEMA),
            "",
            f"For EACH file below write one JSON answer to {bdir}/<answer name>.json (name given per file). "
            "The answer must validate against the schema.",
            "",
        ]
        for rel in batch:
            f = analysis["files"][rel]
            src = (root / rel).read_text(errors="replace")
            parts += [f"===== answer name: {_safe(rel)} =====", describe_user(rel, f, src), ""]
        (bdir / "TASK.md").write_text("\n".join(parts))
        index["describe"].append({"dir": str(bdir), "files": batch})
    (out / "VERIFY.md").write_text(
        "\n".join(
            [
                VERIFY_SYSTEM,
                "",
                "Answer JSON schema:",
                json.dumps(VERIFY_SCHEMA),
                "",
                "For each describe answer <name>.json, read the matching source file and write <name>.verify.json next to it.",
            ]
        )
    )
    (out / "SYNTH.md").write_text(
        "\n".join(
            [
                SYNTH_SYSTEM,
                "",
                "Answer JSON schema:",
                json.dumps(SYNTH_SCHEMA),
                "",
                f"Run `blockmap synth-input {out}` after the describe/verify answers exist to get the facts, then write {out}/synth.json.",
            ]
        )
    )
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return index


def collect_answers(analysis: dict, out: Path, enrichment: dict, log=print) -> dict:
    by_name = {_safe(r): r for r in analysis["files"]}
    n_desc = n_ver = 0
    for p in sorted(out.glob("describe_*/*.json")):
        stem = p.name.removesuffix(".json")
        if stem.endswith(".verify"):
            continue
        rel = by_name.get(stem)
        if not rel:
            log(f"  ~ {p.name}: no such file in the map")
            continue
        try:
            ans = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            log(f"  ! {p}: invalid JSON ({e})")
            continue
        for w in apply_describe(enrichment["files"][rel], ans, analysis["files"][rel]):
            log(f"  ~ {rel}: {w}")
        n_desc += 1
        vp = p.with_name(stem + ".verify.json")
        if vp.exists():
            try:
                apply_verify(enrichment["files"][rel], json.loads(vp.read_text()))
                n_ver += 1
            except json.JSONDecodeError as e:
                log(f"  ! {vp}: invalid JSON ({e})")
    sp = out / "synth.json"
    if sp.exists():
        enrichment["synth"] = clean_synth(json.loads(sp.read_text()), set(analysis["files"]))
    enrichment["source"] = "skill"
    log(f"  applied {n_desc} descriptions, {n_ver} reviews, synth={'yes' if sp.exists() else 'no'}")
    return enrichment


def synth_input(analysis: dict, meta: dict, enrichment: dict) -> str:
    notes = {r: v.get("notes", []) for r, v in enrichment["files"].items()}
    return synth_user(analysis, meta, enrichment["files"], notes)
