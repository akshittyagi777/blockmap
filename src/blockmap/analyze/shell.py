"""Read shell scripts for the order in which they run Python scripts and other shell scripts."""

from __future__ import annotations

import re

_PY = re.compile(
    r"(?:^|[\s;&|(])(?:[\w./-]*python3?|uv run(?:\s+--?[\w-]+(?:\s+\S+)?)*\s+python3?)\s+(?:-u\s+)?(?P<target>-m\s+[\w.]+|[\w./-]+\.py)(?P<args>[^;&|#\n]*)"
)
_SH = re.compile(r"(?:^|[\s;&|(])(?:bash|sh|source|\.)\s+(?P<target>[\w./-]+\.sh)")
_DIRECT = re.compile(r"^\s*(?:\./)?(?P<target>[\w./-]+\.(?:py|sh))(?:\s|$)")


def analyze_shell(path: str, source: str) -> dict:
    """Return the ordered steps a shell script runs, joining backslash-continued lines first."""
    joined, starts, buf, buf_start = [], [], "", 1
    for i, raw in enumerate(source.splitlines(), start=1):
        if not buf:
            buf_start = i
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        joined.append(buf + line)
        starts.append(buf_start)
        buf = ""
    steps, comment = [], ""
    for line, lineno in zip(joined, starts, strict=True):
        stripped = line.strip()
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if text and not text.startswith("!"):
                comment = text
            continue
        if not stripped:
            comment = ""
            continue
        for rx in (_PY, _SH, _DIRECT):
            m = rx.search(line)
            if m:
                target = m.group("target")
                if target.startswith("-m"):
                    target = target.split()[-1].replace(".", "/") + ".py"
                args = " ".join((m.groupdict().get("args") or "").split())
                steps.append(
                    {
                        "line": lineno,
                        "target": target.removeprefix("./"),
                        "kind": "shell" if target.endswith(".sh") else "python",
                        "args": args[:200],
                        "note": comment,
                        "conditional": _inside_if(joined, starts, lineno),
                    }
                )
                break
        comment = ""
    return {"path": path, "steps": steps, "lines": source.count("\n") + 1}


def _inside_if(lines: list[str], starts: list[int], lineno: int) -> bool:
    depth = 0
    for line, ln in zip(lines, starts, strict=True):
        if ln >= lineno:
            break
        s = line.strip()
        if re.match(r"^(if|elif)\b", s):
            depth += 1 if s.startswith("if") else 0
        elif s.startswith("fi") or s.endswith("; fi"):
            depth = max(0, depth - 1)
    return depth > 0
