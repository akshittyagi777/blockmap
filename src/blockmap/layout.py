"""Layered layout for the data-flow graph: rows by pipeline depth, columns by lane.

A small Sugiyama-style pass with no dependencies: break cycles, assign each node the
longest-path depth from a source, order nodes inside each (lane, row) by the mean x of
their parents, then pack left to right. Widths are estimated from label length; the
renderer measures real text, so estimates are kept generous.
"""

from __future__ import annotations

from collections import defaultdict

ROW_H = 84
TOP = 56
NODE_GAP = 18
LANE_GAP = 28
LANE_PAD = 14
CHAR_W = 7.1  # IBM Plex Mono at 11.5px is ~6.9px per glyph
MAX_ROW_W = 1000
SUB_ROW_H = 52


def node_width(label: str) -> float:
    return max(len(s) for s in label.split("\n")) * CHAR_W + 24


def _acyclic(nodes: list[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
    state: dict[str, int] = {}
    back: set[tuple[str, str]] = set()

    def visit(n: str) -> None:
        stack = [(n, iter(adj[n]))]
        state[n] = 1
        while stack:
            cur, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[cur] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                back.add((cur, nxt))
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(adj[nxt])))

    for n in nodes:
        if n not in state:
            visit(n)
    return [e for e in edges if e not in back and e[0] != e[1]]


def layout(nodes: list[dict], edges: list[list[str]], lane_order: list[str]) -> dict:
    """nodes: [{id, label, lane}] -> adds x, y (centers), w; returns {nodes, lanes, width, height}."""
    ids = [n["id"] for n in nodes]
    by_id = {n["id"]: n for n in nodes}
    es = _acyclic(ids, [(a, b) for a, b, *_ in edges if a in by_id and b in by_id])
    parents = defaultdict(list)
    for a, b in es:
        parents[b].append(a)
    # longest-path depth
    depth: dict[str, int] = {}

    def d(n: str, guard: int = 0) -> int:
        if n in depth:
            return depth[n]
        if guard > len(ids):
            return 0
        depth[n] = 0 if not parents[n] else 1 + max(d(p, guard + 1) for p in parents[n])
        return depth[n]

    for n in ids:
        d(n)
    # pull sinks (outputs nobody reads) up to sit just under their writer
    children = defaultdict(list)
    for a, b in es:
        children[a].append(b)
    for n in ids:
        if not children[n] and parents[n]:
            depth[n] = 1 + max(depth[p] for p in parents[n])

    lanes = [ln for ln in lane_order if any(n["lane"] == ln for n in nodes)]
    lanes += sorted({n["lane"] for n in nodes} - set(lanes))
    cells: dict[tuple[str, int], list[str]] = defaultdict(list)
    for n in nodes:
        n["w"] = node_width(n["label"])
        cells[(n["lane"], depth[n["id"]])].append(n["id"])

    # initial order: stable by label; then two barycenter sweeps using provisional x
    for k in cells:
        cells[k].sort(key=lambda i: by_id[i]["label"])
    lane_x0: dict[str, float] = {}
    by_lane_w: dict[str, float] = {}
    max_depth = max(depth.values(), default=0)

    def chunks(row: list[str]) -> list[list[str]]:
        """Wrap a long row into sub-rows no wider than MAX_ROW_W."""
        out, cur, w = [], [], 0.0
        for i in row:
            wi = by_id[i]["w"] + (NODE_GAP if cur else 0)
            if cur and w + wi > MAX_ROW_W:
                out.append(cur)
                cur, w = [], 0.0
                wi = by_id[i]["w"]
            cur.append(i)
            w += wi
        if cur:
            out.append(cur)
        return out

    sub_of: dict[str, int] = {}

    def pack() -> float:
        x = 6.0
        for ln in lanes:
            rows = [c for r in range(max_depth + 1) if cells.get((ln, r)) for c in chunks(cells[(ln, r)])]
            for r in range(max_depth + 1):
                for k, c in enumerate(chunks(cells.get((ln, r), []))):
                    for i in c:
                        sub_of[i] = k
            lane_w = max((sum(by_id[i]["w"] for i in row) + NODE_GAP * (len(row) - 1) for row in rows), default=120)
            lane_w = max(lane_w, len(ln) * 7.5 + 24)
            lane_x0[ln] = x
            for row in rows:
                row_w = sum(by_id[i]["w"] for i in row) + NODE_GAP * (len(row) - 1)
                cx = x + LANE_PAD + (lane_w - row_w) / 2
                for i in row:
                    by_id[i]["x"] = cx + by_id[i]["w"] / 2
                    cx += by_id[i]["w"] + NODE_GAP
            by_lane_w[ln] = lane_w + 2 * LANE_PAD
            x += lane_w + 2 * LANE_PAD + LANE_GAP
        return x

    pack()
    for _ in range(3):
        for row in cells.values():

            def bary(i: str) -> float:
                ps = parents[i] or children[i]
                return sum(by_id[p]["x"] for p in ps) / len(ps) if ps else by_id[i]["x"]

            row.sort(key=bary)
        pack()
    total_w = pack()
    # each depth is as tall as its most-wrapped lane
    subrows = defaultdict(int)
    for n in nodes:
        subrows[depth[n["id"]]] = max(subrows[depth[n["id"]]], sub_of.get(n["id"], 0) + 1)
    y0, base = TOP + 30, {}
    for r in range(max_depth + 1):
        base[r] = y0
        y0 += ROW_H + (subrows[r] - 1) * SUB_ROW_H
    for n in nodes:
        n["y"] = base[depth[n["id"]]] + sub_of.get(n["id"], 0) * SUB_ROW_H
    height = y0 + 20
    lane_boxes = [{"title": ln, "x0": lane_x0[ln], "x1": lane_x0[ln] + by_lane_w[ln]} for ln in lanes]
    return {"nodes": nodes, "lanes": lane_boxes, "width": round(total_w), "height": round(height)}
