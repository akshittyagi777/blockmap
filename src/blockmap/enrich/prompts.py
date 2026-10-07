"""Prompts and JSON schemas shared by the API runner and the Claude Code skill path.

Three stages, mirroring how a careful human would read an unfamiliar repo:
  describe  (worker model, one call per file)  label every block, list reads/writes
  verify    (judge model, one call per file)   check the worker against the code, add notes
  synthesize(judge model, one call per repo)   overview, groups, statuses, findings
"""

from __future__ import annotations

import json

PROMPT_VERSION = "1"
ROLES = ["setup", "helper", "load", "compute", "save", "plot", "entry"]
STATUSES = ["core", "auxiliary", "dormant", "library", "test"]

_STYLE = (
    "Write plain, concrete English for a reader who has never seen this repo. "
    "State facts that are visible in the code; do not speculate about intent you cannot see. "
    "Prefer specifics (parameter values, file names, branches) over adjectives. No marketing words."
)

DESCRIBE_SYSTEM = f"""You document source files for an interactive code map.
For one file you receive: its numbered source, the exact block boundaries already computed from the syntax tree
(do not change them), and file paths a static scanner believes the file reads or writes.

Return:
- purpose: one sentence on what the file is for.
- blocks: one entry per given block id. label = 2-5 words; desc = 1-2 sentences on what the block does and why;
  role = setup (docstring/imports/constants) | helper (small utility) | load (reads data) | compute (core logic) |
  save (writes outputs) | plot (draws figures/UI) | entry (main/CLI orchestration).
  For blocks longer than ~70 lines add `sub`: 2-6 contiguous phases that exactly tile the block (first start = block
  start, last end = block end, each start = previous end + 1). Otherwise `sub` is [].
- reads / writes: the files, directories, datasets or URLs the code actually reads/writes, written as path templates
  like results/{{model}}/x.csv. Reuse the scanner's strings when they are right; drop ones that are wrong; add missed ones.
{_STYLE}"""

VERIFY_SYSTEM = f"""You are the reviewer for an interactive code map. A junior writer described one file; check every
claim against the source. Return corrections only where the writer is wrong or misleading (an empty list is fine),
the final purpose, reads and writes for the file, and up to 4 notes.
Notes are facts a maintainer must know before changing this file: bugs, docstrings that contradict the code,
hard-coded values that duplicate data elsewhere, dead code, surprising behaviour. Each note cites line numbers.
{_STYLE}"""

SYNTH_SYSTEM = f"""You write the overview page of an interactive code map for one git repository at one ref.
You get: the README excerpt, every file with its purpose, kind and reads/writes, the data-flow artifacts with their
writers and readers, the order shell scripts run things in, import edges, and reviewer notes.

Return:
- title: the project's name (2-4 words). tagline: one sentence saying what the repo does.
- overview: 2-3 short paragraphs: what problem it solves, how the code is organised, how data moves through it.
- concepts: up to 6 domain terms a newcomer needs (term + one-sentence meaning), only if the code uses them.
- groups: partition the non-test files into 3-8 groups a reader would recognise (e.g. "Core pipeline",
  "Evaluation add-ons", "Library: data loading"). Use exact file paths. Set library=true for groups of imported modules.
  Order groups as a reader should read them.
- statuses: for every file, one of core (on the main path / produces headline outputs), auxiliary (supporting or
  optional analyses that ran), dormant (unused, broken or exploratory), library (imported module), test.
- findings: 3-6 things to know before changing anything: duplication, ordering hazards, stale docs, hard-coded
  values, files nothing uses. Ground each in the provided facts.
{_STYLE}"""


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


_STR = {"type": "string"}
_STRS = {"type": "array", "items": _STR}
_SUB = _obj({"start": {"type": "integer"}, "end": {"type": "integer"}, "label": _STR})

DESCRIBE_SCHEMA = _obj(
    {
        "purpose": _STR,
        "blocks": {
            "type": "array",
            "items": _obj(
                {
                    "id": {"type": "integer"},
                    "label": _STR,
                    "desc": _STR,
                    "role": {"type": "string", "enum": ROLES},
                    "sub": {"type": "array", "items": _SUB},
                }
            ),
        },
        "reads": _STRS,
        "writes": _STRS,
    }
)

VERIFY_SCHEMA = _obj(
    {
        "corrections": {
            "type": "array",
            "items": _obj(
                {
                    "block_id": {"type": "integer"},
                    "field": {"type": "string", "enum": ["label", "desc", "role"]},
                    "value": _STR,
                    "reason": _STR,
                }
            ),
        },
        "purpose": _STR,
        "reads": _STRS,
        "writes": _STRS,
        "notes": _STRS,
    }
)

SYNTH_SCHEMA = _obj(
    {
        "title": _STR,
        "tagline": _STR,
        "overview": _STRS,
        "concepts": {"type": "array", "items": _obj({"term": _STR, "meaning": _STR})},
        "groups": {
            "type": "array",
            "items": _obj({"title": _STR, "blurb": _STR, "files": _STRS, "library": {"type": "boolean"}}),
        },
        "statuses": {"type": "array", "items": _obj({"path": _STR, "status": {"type": "string", "enum": STATUSES}})},
        "findings": {"type": "array", "items": _obj({"title": _STR, "detail": _STR})},
    }
)


def repo_context(analysis: dict, meta: dict) -> str:
    """Shared, stable prefix for every per-file call (cacheable): the repo's file list with one-line docstrings."""
    lines = [f"Repository: {meta['repo']} @ {meta['ref']} ({meta['commit'][:10]})", "", "Files:"]
    for rel, f in sorted(analysis["files"].items()):
        doc = (f.get("docstring") or "").split(". ")[0][:110]
        lines.append(f"- {rel} [{f['kind']}, {f['lines']} lines]{': ' + doc if doc else ''}")
    for rel in sorted(analysis["shells"]):
        lines.append(f"- {rel} [shell]")
    return "\n".join(lines)


def numbered(source: str) -> str:
    return "\n".join(f"{i:>5}  {line}" for i, line in enumerate(source.splitlines(), start=1))


def describe_user(rel: str, f: dict, source: str) -> str:
    blocks = [{"id": b["id"], "lines": f"{b['start']}-{b['end']}", "kind": b["type"], "name": b.get("name")} for b in f["blocks"]]
    io = [f"{h['mode']}: {h['path']}  (line {h['line']}, {h['how']})" for h in f.get("io", []) if h["mode"] != "mention"]
    return (
        f"File: {rel}\n\nBlocks (fixed):\n{json.dumps(blocks)}\n\nScanner I/O guesses:\n"
        + ("\n".join(io) or "(none)")
        + f"\n\nSource:\n{numbered(source)}"
    )


def verify_user(rel: str, f: dict, source: str, described: dict) -> str:
    return describe_user(rel, f, source) + "\n\nWriter's description to review:\n" + json.dumps(described, indent=1)


def synth_user(analysis: dict, meta: dict, per_file: dict[str, dict], notes: dict[str, list[str]]) -> str:
    files = []
    for rel, f in sorted(analysis["files"].items()):
        d = per_file.get(rel, {})
        files.append(
            {
                "path": rel,
                "kind": f["kind"],
                "lines": f["lines"],
                "purpose": d.get("purpose", ""),
                "reads": d.get("reads", [])[:12],
                "writes": d.get("writes", [])[:12],
                "imports": sorted(set(f.get("internal_imports", {}).values()))[:12],
            }
        )
    arts = [{"label": a["label"], "writers": a["writers"], "readers": a["readers"]} for a in analysis["artifacts"].values()]
    runs = {
        k: [s["target"] + (" (conditional)" if s["conditional"] else "") for s in v["steps"]]
        for k, v in analysis["shells"].items()
    }
    payload = {
        "repo": meta,
        "readme_excerpt": analysis.get("readme", "")[:6000],
        "files": files,
        "artifacts": arts,
        "run_order": runs,
        "other_code_files": [o["path"] for o in analysis.get("other_files", [])][:80],
        "reviewer_notes": {k: v for k, v in notes.items() if v},
    }
    return json.dumps(payload, indent=1)
