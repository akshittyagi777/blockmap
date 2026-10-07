"""Inject a map into the single-file HTML template."""

from __future__ import annotations

import html
import json
from importlib.resources import files

from .. import SCHEMA_VERSION


def render_html(m: dict) -> str:
    if m.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"map schema {m.get('schema')} does not match this blockmap ({SCHEMA_VERSION}); re-run analyze")
    template = files("blockmap.render").joinpath("template.html").read_text()
    title = (m.get("overview") or {}).get("title") or m["meta"]["repo"]
    title = f"{title} · {m['meta']['ref']}"
    # "</" would let a string inside the JSON close the <script> tag early.
    payload = json.dumps(m, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    return template.replace("__TITLE__", html.escape(title)).replace("__MAP__", payload)
