"""qa_sync.py — mirrors installed apps' ``contributes.qa`` into
aw-knowledgeable (design ref qa-section-design-v2). No real network: the
module's own ``httpx.AsyncClient`` is faked; the apps root is a tmp dir."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledgeable_app import qa_sync  # noqa: E402
from knowledgeable_app.mcp import client  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class _FakeHttp:
    _CALLS: list = []
    _QUEUE: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def put(self, url, json=None, headers=None):
        _FakeHttp._CALLS.append({"url": url, "json": json, "headers": headers})
        item = _FakeHttp._QUEUE.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    _FakeHttp._CALLS = []
    _FakeHttp._QUEUE = []
    apps = tmp_path / "apps"
    apps.mkdir()
    monkeypatch.setenv("AW_APPS_ROOT", str(apps))
    monkeypatch.delenv("AW_WORKSPACE", raising=False)
    monkeypatch.setattr(qa_sync, "httpx", type("M", (), {"AsyncClient": _FakeHttp, "HTTPError": httpx.HTTPError}))
    client.configure(lambda: "http://aw-knowledgeable:8090", lambda: "shh")
    qa_sync.configure(lambda: {}, None)
    yield apps
    client.configure(lambda: client.DEFAULT_BASE_URL, lambda: "")


def _app(apps: Path, slug: str, items=None, *, qa=..., raw: str | None = None, app_id: str | None = None):
    d = apps / slug
    d.mkdir()
    contributes = {} if qa is None else {"qa": {"path": "qa/items.json"} if qa is ... else qa}
    (d / "aw-app.json").write_text(json.dumps({"id": app_id or slug, "contributes": contributes}))
    if raw is not None or items is not None:
        (d / "qa").mkdir()
        (d / "qa" / "items.json").write_text(raw if raw is not None else json.dumps(items))
    return d


def _item(i, **kw):
    return {"id": i, "question": f"q {i}", "answer": f"a {i}", "tags": ["t"], **kw}


def test_collect_reads_every_apps_items_in_wire_shape(_env):
    _app(_env, "alpha", [_item("1"), _item("2")])
    _app(_env, "beta", [_item("1")])
    _app(_env, "plain", qa=None)  # contributes nothing
    items, problems = qa_sync.collect()
    assert problems == []
    assert [(i["app_slug"], i["external_id"]) for i in items] == [("alpha", "1"), ("alpha", "2"), ("beta", "1")]
    assert items[0] == {"app_slug": "alpha", "external_id": "1", "question": "q 1", "answer": "a 1", "tags": ["t"]}


def test_app_slug_is_the_manifest_id(_env):
    _app(_env, "dirname", [_item("1")], app_id="real-id")
    items, _ = qa_sync.collect()
    assert items[0]["app_slug"] == "real-id"


def test_one_apps_broken_file_never_blocks_the_others(_env):
    _app(_env, "a-bad-json", raw="{not json")
    _app(_env, "b-not-list", raw='{"id": "x"}')
    _app(_env, "c-missing", qa=...)  # declared, file absent
    _app(_env, "d-badspec", qa={"path": ""})
    _app(_env, "e-good", [_item("1")])
    items, problems = qa_sync.collect()
    assert [(i["app_slug"], i["external_id"]) for i in items] == [("e-good", "1")]
    assert len(problems) == 4
    assert any("a-bad-json" in p for p in problems) and any("must be a JSON list" in p for p in problems)


def test_unreadable_manifest_is_skipped(_env):
    d = _env / "broken"
    d.mkdir()
    (d / "aw-app.json").write_text("{nope")
    _app(_env, "ok", [_item("1")])
    items, problems = qa_sync.collect()
    assert len(items) == 1 and any("broken" in p for p in problems)


def test_item_without_a_usable_id_is_skipped_but_bad_content_is_still_forwarded(_env):
    _app(_env, "alpha", ["not an object", {"question": "q", "answer": "a"}, {"id": 5}, _item("ok"),
                          {"id": "bad", "question": "", "answer": 7}])
    items, problems = qa_sync.collect()
    # the bad-content item travels so the BACKEND rejects it (keeping its last good row)
    assert [i["external_id"] for i in items] == ["ok", "bad"]
    assert len(problems) == 3


def test_path_cannot_escape_the_app_directory(_env, tmp_path):
    (tmp_path / "secret.json").write_text(json.dumps([_item("x")]))
    d = _app(_env, "evil", qa={"path": "../../secret.json"})
    items, problems = qa_sync.collect()
    assert items == [] and "escapes" in problems[0]


def test_sync_once_puts_an_exact_mirror_and_logs_rejections(_env, caplog):
    _app(_env, "alpha", [_item("1")])
    _FakeHttp._QUEUE.append(_Resp(200, {
        "created": 0, "updated": 0, "unchanged": 0, "deleted": 0,
        "rejected": [{"index": 0, "app_slug": "alpha", "external_id": "1", "reason": "question is required"}],
    }))
    with caplog.at_level("WARNING", logger="aw_apps.knowledgeable.qa_sync"):
        result = _run(qa_sync.sync_once())
    call = _FakeHttp._CALLS[0]
    assert call["url"] == "http://aw-knowledgeable:8090/api/qa/sync"
    assert call["headers"]["X-Internal-Secret"] == "shh"
    assert call["json"]["workspace_slug"] == "aw"
    assert [i["external_id"] for i in call["json"]["items"]] == ["1"]
    assert result["status"] == "ok"
    assert "alpha/1: question is required" in caplog.text


def test_sync_once_with_no_contributions_still_sends_an_empty_list(_env):
    """items=[] is the declared 'this workspace offers none' — it is what makes
    a removed app's rows go away."""
    _FakeHttp._QUEUE.append(_Resp(200, {"deleted": 3}))
    result = _run(qa_sync.sync_once())
    assert _FakeHttp._CALLS[0]["json"]["items"] == []
    assert result["status"] == "ok"


def test_sync_once_tolerates_404_and_transport_errors(_env):
    _FakeHttp._QUEUE.append(_Resp(404, {"detail": "Not Found"}))
    assert _run(qa_sync.sync_once())["status"] == "not_deployed"
    _FakeHttp._QUEUE.append(httpx.ConnectError("down"))
    r = _run(qa_sync.sync_once())
    assert r["status"] == "error" and "could not reach" in r["error"]
    _FakeHttp._QUEUE.append(_Resp(403, {"detail": "qa sync is service-only"}))
    assert _run(qa_sync.sync_once())["status"] == "error"


def test_the_apps_own_seed_is_valid(monkeypatch):
    """The repo's own manifest declares contributes.qa and the file it points
    at is a list of well-formed items with unique ids."""
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "aw-app.json").read_text())
    rel = manifest["contributes"]["qa"]["path"]
    items = json.loads((root / rel).read_text())
    assert len(items) >= 5
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    for i in items:
        assert isinstance(i["question"], str) and i["question"].strip()
        assert isinstance(i["answer"], str) and i["answer"].strip()
        assert "knowledgeable" in i["tags"]
    assert "qa_search" in manifest["contributes"]["mcp"]["provides"]
    # and collect() picks it up as an installed app
    apps = Path(__import__("os").environ["AW_APPS_ROOT"])
    (apps / "knowledgeable").symlink_to(root)
    got, problems = qa_sync.collect()
    assert problems == [] and len(got) == len(items)
    assert {g["app_slug"] for g in got} == {"knowledgeable"}
