"""Run the describe -> verify -> synthesize stages against the Claude API.

The worker model labels blocks (cheap, one call per file); the judge model reviews every
file against its source and writes the repo overview. Answers are cached on disk keyed by
model, stage, prompt version and file content, so re-running on another branch only pays
for files that changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from .merge import apply_describe, apply_verify, clean_synth
from .prompts import (
    DESCRIBE_SCHEMA,
    DESCRIBE_SYSTEM,
    PROMPT_VERSION,
    SYNTH_SCHEMA,
    SYNTH_SYSTEM,
    VERIFY_SCHEMA,
    VERIFY_SYSTEM,
    describe_user,
    repo_context,
    synth_user,
    verify_user,
)

DEFAULT_WORKER = "claude-haiku-4-5"
DEFAULT_JUDGE = "claude-opus-5-5"
# Models whose requests go through the server-side refusal fallback (see README).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_NO_EFFORT = {"claude-haiku-4-5"}


def cache_dir() -> Path:
    base = os.environ.get("BLOCKMAP_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")), "blockmap"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class Runner:
    def __init__(self, worker: str, judge: str, concurrency: int = 6, effort: str = "high", log=print):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise SystemExit("The API path needs the anthropic package: uv pip install 'blockmap[llm]'") from e
        self.anthropic = anthropic
        self.client = anthropic.AsyncAnthropic()
        self.worker, self.judge = worker, judge
        self.sem = asyncio.Semaphore(concurrency)
        self.effort = effort
        self.log = log
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "calls": 0, "cached_answers": 0}
        self.cache = cache_dir()

    async def _call(self, model: str, system: str, context: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        system_blocks = [{"type": "text", "text": system}]
        if context:
            # Stable across every file of one repo: cached after the first call.
            system_blocks.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if model not in _NO_EFFORT:
            kwargs["output_config"]["effort"] = self.effort
        async with self.sem:
            try:
                if model in _FALLBACK_MODELS:
                    resp = await self.client.beta.messages.create(
                        **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                    )
                else:
                    resp = await self.client.messages.create(**kwargs)
            except self.anthropic.BadRequestError as e:
                self.log(f"  ! request rejected ({e.message}); falling back to static labels")
                return None
            except (self.anthropic.RateLimitError, self.anthropic.APIConnectionError, self.anthropic.InternalServerError) as e:
                self.log(f"  ! API unavailable after retries ({type(e).__name__}); falling back to static labels")
                return None
        u = resp.usage
        self.usage["calls"] += 1
        self.usage["input"] += u.input_tokens or 0
        self.usage["output"] += u.output_tokens or 0
        self.usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        if resp.stop_reason in {"refusal", "max_tokens"}:
            self.log(f"  ! {model} stopped with {resp.stop_reason}; keeping static labels")
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            self.log("  ! model returned invalid JSON; keeping static labels")
            return None

    async def _cached(self, stage: str, model: str, ident: str, coro_fn):
        path = self.cache / f"{_key(stage, model, PROMPT_VERSION, ident)}.json"
        if path.exists():
            self.usage["cached_answers"] += 1
            return json.loads(path.read_text())
        ans = await coro_fn()
        if ans is not None:
            path.write_text(json.dumps(ans))
        return ans

    async def run(
        self, analysis: dict, meta: dict, root: Path, enrichment: dict, verify: bool = True, synth: bool = True
    ) -> None:
        ctx = repo_context(analysis, meta)
        targets = [r for r, f in analysis["files"].items() if f["kind"] != "test" and not f.get("parse_error")]
        done = 0

        async def one(rel: str) -> None:
            nonlocal done
            f = analysis["files"][rel]
            src = (root / rel).read_text(errors="replace")
            ident = f["sha"] + json.dumps([[b["start"], b["end"]] for b in f["blocks"]])
            desc = await self._cached(
                "describe",
                self.worker,
                ident,
                lambda: self._call(self.worker, DESCRIBE_SYSTEM, ctx, describe_user(rel, f, src), DESCRIBE_SCHEMA, 16000),
            )
            cur = enrichment["files"][rel]
            if desc:
                for w in apply_describe(cur, desc, f):
                    self.log(f"  ~ {rel}: {w}")
            if verify and desc:
                ver = await self._cached(
                    "verify",
                    self.judge,
                    ident + json.dumps(desc, sort_keys=True),
                    lambda: self._call(self.judge, VERIFY_SYSTEM, ctx, verify_user(rel, f, src, desc), VERIFY_SCHEMA, 16000),
                )
                if ver:
                    apply_verify(cur, ver)
            done += 1
            print(f"\r  described {done}/{len(targets)} files", end="", file=sys.stderr, flush=True)

        await asyncio.gather(*(one(r) for r in targets))
        print(file=sys.stderr)
        if synth:
            per_file = enrichment["files"]
            notes = {r: v.get("notes", []) for r, v in per_file.items()}
            user = synth_user(analysis, meta, per_file, notes)
            ans = await self._cached(
                "synth", self.judge, _key(user), lambda: self._call(self.judge, SYNTH_SYSTEM, "", user, SYNTH_SCHEMA, 32000)
            )
            if ans:
                enrichment["synth"] = clean_synth(ans, set(analysis["files"]))
        enrichment["source"] = "api"
        enrichment["models"] = {"worker": self.worker, "judge": self.judge if (verify or synth) else None}


def run_api(
    analysis: dict,
    meta: dict,
    root: Path,
    enrichment: dict,
    *,
    worker: str,
    judge: str,
    verify: bool,
    synth: bool,
    concurrency: int,
    effort: str,
) -> dict:
    runner = Runner(worker, judge, concurrency=concurrency, effort=effort)
    asyncio.run(runner.run(analysis, meta, root, enrichment, verify=verify, synth=synth))
    return runner.usage
