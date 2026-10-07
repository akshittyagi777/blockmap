import json
from itertools import pairwise

import pytest

from blockmap.assemble import assemble, base_enrichment
from blockmap.cli import main
from blockmap.enrich.merge import apply_describe, apply_verify, clean_synth
from blockmap.layout import layout
from blockmap.render.render import render_html


def test_layout_has_no_overlaps_and_wraps_wide_rows():
    nodes = [{"id": "src", "label": "source", "lane": "L"}] + [{"id": f"n{i}", "label": "x" * 40, "lane": "L"} for i in range(12)]
    edges = [["src", f"n{i}"] for i in range(12)]
    g = layout(nodes, edges, ["L"])
    assert g["width"] < 1200
    rows = {}
    for n in g["nodes"]:
        rows.setdefault(n["y"], []).append((n["x"] - n["w"] / 2, n["x"] + n["w"] / 2))
    for spans in rows.values():
        spans.sort()
        for (_, r), (l2, _) in pairwise(spans):
            assert r <= l2


def test_layout_survives_cycles():
    nodes = [{"id": c, "label": c, "lane": "L"} for c in "abc"]
    g = layout(nodes, [["a", "b"], ["b", "c"], ["c", "a"]], ["L"])
    assert len({n["y"] for n in g["nodes"]}) >= 2


FACTS = {
    "blocks": [
        {"id": 0, "start": 1, "end": 10, "type": "setup"},
        {"id": 1, "start": 11, "end": 100, "type": "def", "name": "main"},
    ]
}


def _cur():
    return {
        "purpose": "",
        "reads": [],
        "writes": [],
        "blocks": {
            "0": {"label": "a", "desc": "", "role": "setup", "sub": []},
            "1": {"label": "b", "desc": "", "role": "helper", "sub": []},
        },
    }


def test_describe_rejects_phases_that_do_not_tile():
    cur = _cur()
    warns = apply_describe(
        cur,
        {
            "purpose": "p",
            "reads": ["x"],
            "writes": [],
            "blocks": [
                {
                    "id": 1,
                    "label": "Main",
                    "desc": "d",
                    "role": "entry",
                    "sub": [{"start": 11, "end": 50, "label": "a"}, {"start": 52, "end": 100, "label": "b"}],
                },
                {"id": 7, "label": "?", "desc": "", "role": "helper", "sub": []},
            ],
        },
        FACTS,
    )
    assert cur["blocks"]["1"]["role"] == "entry" and cur["blocks"]["1"]["sub"] == []
    assert any("unknown block id 7" in w for w in warns) and any("do not tile" in w for w in warns)


def test_describe_accepts_tiling_phases_and_verify_overrides():
    cur = _cur()
    apply_describe(
        cur,
        {
            "purpose": "p",
            "reads": [],
            "writes": [],
            "blocks": [
                {
                    "id": 1,
                    "label": "Main",
                    "desc": "d",
                    "role": "entry",
                    "sub": [{"start": 11, "end": 50, "label": "a"}, {"start": 51, "end": 100, "label": "b"}],
                }
            ],
        },
        FACTS,
    )
    assert len(cur["blocks"]["1"]["sub"]) == 2
    n = apply_verify(
        cur,
        {
            "corrections": [
                {"block_id": 1, "field": "desc", "value": "fixed", "reason": "r"},
                {"block_id": 1, "field": "role", "value": "nonsense", "reason": "r"},
            ],
            "purpose": "q",
            "reads": ["a.csv"],
            "writes": [],
            "notes": ["n1"],
        },
    )
    assert n == 1 and cur["blocks"]["1"]["desc"] == "fixed" and cur["blocks"]["1"]["role"] == "entry"
    assert cur["verified"] and cur["notes"] == ["n1"] and cur["reads"] == ["a.csv"]


def test_synth_drops_unknown_files_and_duplicates():
    s = clean_synth(
        {
            "groups": [
                {"title": "A", "blurb": "", "files": ["x.py", "nope.py"], "library": False},
                {"title": "B", "blurb": "", "files": ["x.py"], "library": False},
            ],
            "statuses": [{"path": "x.py", "status": "core"}, {"path": "nope.py", "status": "core"}],
        },
        {"x.py"},
    )
    assert [g["title"] for g in s["groups"]] == ["A"] and s["statuses"] == {"x.py": "core"}


def test_render_escapes_script_breakouts(toy_repo, tmp_path):
    work = tmp_path / "w"
    main(["analyze", str(toy_repo), "--ref", "main", str(work)])
    meta, analysis = (json.loads((work / f).read_text()) for f in ("meta.json", "analysis.json"))
    enr = base_enrichment(analysis)
    enr["files"]["scripts/02_fit.py"]["purpose"] = "</script><script>alert(1)</script>"
    html = render_html(assemble(meta, analysis, enr))
    assert "</script><script>alert(1)" not in html
    m = assemble(meta, analysis, enr)
    m["schema"] = 999
    with pytest.raises(ValueError):
        render_html(m)


def test_cli_build_static(toy_repo, tmp_path):
    out = tmp_path / "map.html"
    main(["build", str(toy_repo), "--ref", "main", "-o", str(out), "--work", str(tmp_path / "w")])
    html = out.read_text()
    assert "01_extract" in html and "<svg" in html
    m = json.loads((tmp_path / "w" / "map.json").read_text())
    assert m["graph"]["nodes"] and m["run_order"][0]["path"] == "scripts/run.sh"
    assert m["files"]["src/lib/metrics.py"]["used_by"] == ["scripts/02_fit.py", "tests/test_metrics.py"]


def test_skill_round_trip_tasks_then_apply(toy_repo, tmp_path):
    work = tmp_path / "w"
    main(["analyze", str(toy_repo), "--ref", "main", str(work)])
    main(["tasks", str(work)])
    idx = json.loads((work / "tasks" / "index.json").read_text())
    bdir = work / "tasks" / idx["describe"][0]["dir"].rsplit("/", 1)[-1]
    assert "answer name: scripts__02_fit.py" in (bdir / "TASK.md").read_text()
    analysis = json.loads((work / "analysis.json").read_text())
    f = analysis["files"]["scripts/02_fit.py"]
    answer = {
        "purpose": "Fits and scores.",
        "reads": ["results/features/*.h5"],
        "writes": ["results/metrics.csv"],
        "blocks": [{"id": b["id"], "label": f"block {b['id']}", "desc": "d", "role": "helper", "sub": []} for b in f["blocks"]],
    }
    (bdir / "scripts__02_fit.py.json").write_text(json.dumps(answer))
    (work / "tasks" / "synth.json").write_text(
        json.dumps(
            {
                "title": "Toy",
                "tagline": "t",
                "overview": ["o"],
                "concepts": [],
                "groups": [
                    {"title": "Pipeline", "blurb": "", "files": ["scripts/01_extract.py", "scripts/02_fit.py"], "library": False}
                ],
                "statuses": [{"path": "scripts/02_fit.py", "status": "core"}],
                "findings": [{"title": "F", "detail": "d"}],
            }
        )
    )
    main(["apply", str(work)])
    main(["render", str(work), "-o", str(tmp_path / "m.html")])
    m = json.loads((work / "map.json").read_text())
    assert m["files"]["scripts/02_fit.py"]["purpose"] == "Fits and scores."
    assert m["overview"]["title"] == "Toy" and m["groups"][0]["title"] == "Pipeline"
    assert m["enrichment"]["source"] == "skill"
