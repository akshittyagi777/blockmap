"""Command line: `blockmap build` does everything; the step commands exist for the Claude Code skill."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import webbrowser
from pathlib import Path

from . import __version__
from .analyze.repo import analyze_repo
from .assemble import assemble, base_enrichment
from .render.render import render_html
from .snapshot import take_snapshot

WORK_FILES = ("meta.json", "analysis.json", "enrichment.json")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _analyze_into(work: Path, source: str, ref: str | None, include, exclude, worktree: bool = False) -> tuple[dict, dict]:
    snap = take_snapshot(source, ref, workdir=work, worktree=worktree)
    _log(f"snapshot {snap.repo_name} @ {snap.ref} ({snap.commit[:10] or 'uncommitted'})")
    analysis = analyze_repo(snap.root, include=include, exclude=exclude)
    meta = snap.as_dict()
    n_blocks = sum(len(f["blocks"]) for f in analysis["files"].values())
    _log(
        f"analysed {len(analysis['files'])} Python files ({n_blocks} blocks), {len(analysis['shells'])} shell scripts, "
        f"{len(analysis['artifacts'])} data artifacts"
    )
    (work / "meta.json").write_text(json.dumps(meta, indent=1))
    (work / "analysis.json").write_text(json.dumps(analysis))
    (work / "enrichment.json").write_text(json.dumps(base_enrichment(analysis)))
    return meta, analysis


def _load(work: Path) -> tuple[dict, dict, dict]:
    missing = [f for f in WORK_FILES if not (work / f).exists()]
    if missing:
        raise SystemExit(f"{work} is not a blockmap work dir (missing {', '.join(missing)}); run `blockmap analyze` first")
    return tuple(json.loads((work / f).read_text()) for f in WORK_FILES)  # type: ignore[return-value]


def _render_to(work: Path, out: Path, open_after: bool) -> None:
    meta, analysis, enrichment = _load(work)
    m = assemble(meta, analysis, enrichment)
    (work / "map.json").write_text(json.dumps(m))
    out.write_text(render_html(m))
    _log(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    if open_after:
        webbrowser.open(out.resolve().as_uri())


def _run_api(work: Path, args) -> None:
    from .enrich.llm import run_api

    meta, analysis, enrichment = _load(work)
    usage = run_api(
        analysis,
        meta,
        work / "tree",
        enrichment,
        worker=args.worker_model,
        judge=args.judge_model,
        verify=not args.no_verify,
        synth=not args.no_synth,
        concurrency=args.concurrency,
        effort=args.effort,
    )
    (work / "enrichment.json").write_text(json.dumps(enrichment))
    _log(
        f"API calls: {usage['calls']} (+{usage['cached_answers']} cached answers), "
        f"tokens in {usage['input']:,} (cache reads {usage['cache_read']:,}), out {usage['output']:,}"
    )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="blockmap", description="Draw a git repo/branch as an interactive code map.")
    p.add_argument("--version", action="version", version=f"blockmap {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_source(sp):
        sp.add_argument("source", help="path inside a local git repo, or a git URL")
        sp.add_argument("--ref", help="branch, tag or commit (default: the repo's current HEAD)")
        sp.add_argument(
            "--worktree", action="store_true", help="map the working tree as it is on disk, including uncommitted changes"
        )
        sp.add_argument("--include", action="append", help="glob of paths to keep (repeatable)")
        sp.add_argument("--exclude", action="append", help="glob of paths to drop (repeatable)")

    def add_llm(sp):
        sp.add_argument("--worker-model", default="claude-haiku-4-5", help="model that labels blocks (default: %(default)s)")
        sp.add_argument(
            "--judge-model", default="claude-opus-5-5", help="model that reviews and writes the overview (default: %(default)s)"
        )
        sp.add_argument(
            "--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"], help="effort for the judge model"
        )
        sp.add_argument("--no-verify", action="store_true", help="skip the per-file review by the judge model")
        sp.add_argument("--no-synth", action="store_true", help="skip the repo overview, groups and findings")
        sp.add_argument("--concurrency", type=int, default=6)

    b = sub.add_parser("build", help="snapshot + analyse + (optionally) enrich + render, in one go")
    add_source(b)
    b.add_argument("-o", "--out", type=Path, help="output HTML (default: <repo>-<ref>.html)")
    b.add_argument("--llm", choices=["none", "api"], default="none", help="'api' uses the Claude API (needs credentials)")
    b.add_argument("--work", type=Path, help="keep intermediate files here (default: a temp dir)")
    b.add_argument("--open", action="store_true", help="open the result in a browser")
    add_llm(b)

    a = sub.add_parser("analyze", help="snapshot + static analysis into a work dir")
    add_source(a)
    a.add_argument("work", type=Path, help="work directory to create")

    e = sub.add_parser("enrich", help="enrich a work dir through the Claude API")
    e.add_argument("work", type=Path)
    add_llm(e)

    t = sub.add_parser("tasks", help="write prompt files for agent-driven enrichment (Claude Code skill)")
    t.add_argument("work", type=Path)
    t.add_argument("--batch-lines", type=int, default=2500, help="source lines per describe batch")

    si = sub.add_parser("synth-input", help="print the facts the overview step needs, after describe/verify answers exist")
    si.add_argument("work", type=Path)

    ap = sub.add_parser("apply", help="fold agent-written answers from <work>/tasks into the map")
    ap.add_argument("work", type=Path)

    r = sub.add_parser("render", help="render a work dir to HTML")
    r.add_argument("work", type=Path)
    r.add_argument("-o", "--out", type=Path, required=True)
    r.add_argument("--open", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "build":
        work = args.work or Path(tempfile.mkdtemp(prefix="blockmap-"))
        meta, _ = _analyze_into(work, args.source, args.ref, args.include, args.exclude, args.worktree)
        if args.llm == "api":
            _run_api(work, args)
        safe_ref = meta["ref"].split(" ")[0].replace("/", "-")
        out = args.out or Path(f"{meta['repo']}-{safe_ref}.html")
        _render_to(work, out, args.open)
    elif args.cmd == "analyze":
        _analyze_into(args.work, args.source, args.ref, args.include, args.exclude, args.worktree)
        _log(f"next: `blockmap render {args.work} -o map.html`, or enrich first with `blockmap enrich` / `blockmap tasks`")
    elif args.cmd == "enrich":
        _run_api(args.work, args)
    elif args.cmd == "tasks":
        from .enrich.tasks import export_tasks

        meta, analysis, _ = _load(args.work)
        idx = export_tasks(analysis, meta, args.work / "tree", args.work / "tasks", args.batch_lines)
        _log(f"wrote {len(idx['describe'])} describe batches under {args.work / 'tasks'}")
    elif args.cmd == "synth-input":
        from .enrich.tasks import collect_answers, synth_input

        meta, analysis, enrichment = _load(args.work)
        collect_answers(analysis, args.work / "tasks", enrichment, log=_log)
        print(synth_input(analysis, meta, enrichment))
    elif args.cmd == "apply":
        from .enrich.tasks import collect_answers

        meta, analysis, enrichment = _load(args.work)
        collect_answers(analysis, args.work / "tasks", enrichment, log=_log)
        (args.work / "enrichment.json").write_text(json.dumps(enrichment))
    elif args.cmd == "render":
        _render_to(args.work, args.out, args.open)


if __name__ == "__main__":
    main()
