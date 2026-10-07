from itertools import pairwise

from blockmap.analyze.python_ast import analyze_python
from blockmap.analyze.repo import analyze_repo, build_artifacts
from blockmap.analyze.shell import analyze_shell
from blockmap.snapshot import take_snapshot

from .conftest import PIPE_EXTRACT, PIPE_FIT, RUN


def _tiles(blocks, n_lines):
    assert blocks[0]["start"] == 1
    assert blocks[-1]["end"] == n_lines
    for a, b in pairwise(blocks):
        assert a["end"] + 1 == b["start"]


def test_blocks_tile_the_file_and_calls_point_at_callees():
    f = analyze_python("x.py", PIPE_EXTRACT)
    _tiles(f.blocks, f.lines)
    names = {b["name"]: b["id"] for b in f.blocks}
    assert [names["main"], names["_save"]] in f.calls
    assert f.has_main_guard
    assert f.docstring.startswith("Extract features")


def test_io_follows_paths_into_helpers_and_cli_defaults():
    f = analyze_python("x.py", PIPE_EXTRACT)
    writes = {h.path for h in f.io if h.mode == "write"}
    assert "results/features/{model}.h5" in writes  # through _save(path) -> h5py.File(path, "w")
    g = analyze_python("y.py", PIPE_FIT)
    reads = {h.path for h in g.io if h.mode == "read"}
    assert "results/features/*.h5" in reads
    assert "results/metrics.csv" in {h.path for h in g.io if h.mode == "write"}


def test_nonconstant_open_mode_counts_as_write_and_hf_ids_are_sources():
    src = 'from datasets import load_dataset\n\ndef go(p, append):\n    open("out/x.csv", "a" if append else "w")\n    load_dataset("org/name")\n'
    f = analyze_python("z.py", src)
    modes = {(h.path, h.mode) for h in f.io}
    assert ("out/x.csv", "write") in modes
    assert ("hf:org/name", "read") in modes


def test_syntax_error_still_gives_one_block():
    f = analyze_python("bad.py", "def (:\n")
    assert f.parse_error and len(f.blocks) == 1


def test_shell_steps_in_order_with_continuations_and_conditionals():
    sh = analyze_shell("run.sh", RUN)
    targets = [s["target"] for s in sh["steps"]]
    assert targets == ["scripts/01_extract.py", "scripts/02_fit.py"]
    assert "--out-dir results/features" in sh["steps"][0]["args"]
    assert sh["steps"][0]["note"] == "extract once"
    assert sh["steps"][1]["conditional"] is True


def test_artifacts_connect_writer_to_reader_and_ignore_mkdir():
    files = {
        "w.py": {
            "io": [
                {"path": "res/feat/{m}.h5", "mode": "write", "line": 1, "how": "h5py.File(w)"},
                {"path": "res/feat", "mode": "write", "line": 2, "how": "mkdir"},
            ]
        },
        "r.py": {"io": [{"path": "res/feat/*.h5", "mode": "read", "line": 1, "how": "glob"}]},
        "s.py": {"io": [{"path": "res/feat/{model}.h5", "mode": "read", "line": 1, "how": "h5py.File(r)"}]},
    }
    arts, edges = build_artifacts(files)
    assert len(arts) == 1
    (aid,) = arts
    assert ["w.py", aid] in edges and [aid, "r.py"] in edges and [aid, "s.py"] in edges


def test_containers_do_not_merge_distinct_files():
    files = {
        "a.py": {"io": [{"path": "out/a.csv", "mode": "write", "line": 1, "how": "open(w)"}]},
        "b.py": {"io": [{"path": "out/b.csv", "mode": "write", "line": 1, "how": "open(w)"}]},
        "c.py": {"io": [{"path": "out", "mode": "read", "line": 1, "how": "iterdir"}]},
    }
    arts, edges = build_artifacts(files)
    assert len(arts) == 2
    assert sum(1 for e in edges if e[1] == "c.py") == 2


def test_repo_analysis_end_to_end(toy_repo, tmp_path):
    snap = take_snapshot(str(toy_repo), "main", workdir=tmp_path / "w")
    a = analyze_repo(snap.root)
    assert a["files"]["scripts/02_fit.py"]["kind"] == "script"
    assert a["files"]["src/lib/metrics.py"]["kind"] == "module"
    assert a["files"]["tests/test_metrics.py"]["kind"] == "test"
    assert ["scripts/02_fit.py", "src/lib/metrics.py"] in a["import_edges"]
    writers = {w for art in a["artifacts"].values() for w in art["writers"]}
    readers = {r for art in a["artifacts"].values() for r in art["readers"]}
    assert "scripts/01_extract.py" in writers and "scripts/02_fit.py" in readers
    steps = a["shells"]["scripts/run.sh"]["steps"]
    assert [s["file"] for s in steps] == ["scripts/01_extract.py", "scripts/02_fit.py"]
