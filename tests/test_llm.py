"""The API path against a fake client: request shape, merging, caching. No network."""

import json
from types import SimpleNamespace

import pytest

anthropic = pytest.importorskip("anthropic")

from blockmap.cli import main  # noqa: E402


class FakeMessages:
    def __init__(self, log, beta=False):
        self.log, self.beta = log, beta

    async def create(self, **kw):
        self.log.append({"beta": self.beta, **kw})
        schema = kw["output_config"]["format"]["schema"]["properties"]
        user = kw["messages"][0]["content"]
        if "groups" in schema:
            body = {
                "title": "Toy",
                "tagline": "A toy.",
                "overview": ["It extracts then fits."],
                "concepts": [],
                "groups": [
                    {"title": "Pipeline", "blurb": "", "files": ["scripts/01_extract.py", "scripts/02_fit.py"], "library": False}
                ],
                "statuses": [],
                "findings": [{"title": "Order", "detail": "Run 01 before 02."}],
            }
        elif "corrections" in schema:
            body = {"corrections": [], "purpose": "Reviewed purpose.", "reads": [], "writes": [], "notes": ["note at line 3"]}
        else:
            ids = json.loads(user.split("Blocks (fixed):\n", 1)[1].split("\n\n", 1)[0])
            body = {
                "purpose": "Worker purpose.",
                "reads": [],
                "writes": [],
                "blocks": [{"id": b["id"], "label": f"L{b['id']}", "desc": "d", "role": "helper", "sub": []} for b in ids],
            }
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0)
        return SimpleNamespace(stop_reason="end_turn", usage=usage, content=[SimpleNamespace(type="text", text=json.dumps(body))])


class FakeClient:
    def __init__(self, log):
        self.messages = FakeMessages(log)
        self.beta = SimpleNamespace(messages=FakeMessages(log, beta=True))


def test_api_path_shapes_requests_and_caches(toy_repo, tmp_path, monkeypatch):
    log: list[dict] = []
    monkeypatch.setenv("BLOCKMAP_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda: FakeClient(log))
    work = tmp_path / "w"
    main(["build", str(toy_repo), "--ref", "main", "--llm", "api", "--work", str(work), "-o", str(tmp_path / "m.html")])
    m = json.loads((work / "map.json").read_text())
    assert m["overview"]["title"] == "Toy" and m["findings"][0]["title"] == "Order"
    assert m["files"]["scripts/02_fit.py"]["purpose"] == "Reviewed purpose."
    assert m["files"]["scripts/02_fit.py"]["notes"] == ["note at line 3"]
    worker = [c for c in log if c["model"] == "claude-haiku-4-5"]
    judge = [c for c in log if c["model"] == "claude-opus-5-5"]
    assert worker and judge
    assert all(not c["beta"] and "effort" not in c["output_config"] for c in worker)
    assert all(c["beta"] and c["fallbacks"] == "default" and c["output_config"]["effort"] == "high" for c in judge)
    assert all(c["system"][-1].get("cache_control") for c in worker)
    n = len(log)
    main(
        ["build", str(toy_repo), "--ref", "main", "--llm", "api", "--work", str(tmp_path / "w2"), "-o", str(tmp_path / "m2.html")]
    )
    assert len(log) == n  # second run is served from the answer cache
