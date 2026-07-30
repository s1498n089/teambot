"""測試共用夾具。

隔離原則:每個測試拿到自己的 tmp 目錄當 BASE(chat.jsonl / tasks.json /
tokens.json 全部落在裡面),測試之間零共享狀態 —
「多實例互洗」的教訓在這裡制度化。

create_app() 是 composition root:monkeypatch server.BASE 後呼叫它,
整個 app(含 store / bus / a2a_layer)就建在隔離目錄上。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import a2a as a2a_mod  # noqa: E402
import server as server_mod  # noqa: E402


@pytest.fixture
def isolated_base(tmp_path, monkeypatch):
    """把 server.BASE 指到 tmp 目錄;清乾淨會影響行為的環境變數。"""
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(server_mod, "BASE", tmp_path)
    for var in ("AUTH", "ROTATE_TOKEN", "HOST", "PUBLIC_HOST", "TASKS_PATH", "PORT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def make_app(isolated_base, monkeypatch):
    """app 工廠:make_app(AUTH="on") 這樣帶環境變數建 app。
    回傳 (app, base_path);同一個 base 可重建 app 以測「重啟復原」。"""
    def _make(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        app = server_mod.create_app(host="127.0.0.1")

        # ★ 建好就讓 alice / bob「上線」。
        #
        #   現在名冊是「誰連著線」,所以要派任務給 bob 之前得先有它的連線 ——
        #   就像真實世界裡你得先把它的敲鈴器開起來。
        #
        #   放在這裡(而不是每個測試各寫一次)是因為絕大多數案例都需要它,
        #   而「重啟」類的測試會建好幾個 app,漏掉任何一個都會得到看不懂的 -32004。
        #   要測「派給離線的 agent」,自己 unsubscribe 掉即可。
        for name in ("alice", "bob"):
            bring_agent_online(app, name)
        return app
    return _make


@pytest.fixture
def client(make_app):
    """預設組態(AUTH off)的 TestClient;with 觸發 lifespan(restore)。

    ★ 預設讓 alice 與 bob「上線」。

      現在名冊是「誰連著線」,所以測試要派任務給 bob 之前,
      得先讓 bob 有一條連線 —— 就像真實世界裡你得先把它的敲鈴器開起來。

      放在 fixture 而不是每個案例各寫一次,是因為絕大多數案例都需要它;
      要測「派給離線的 agent」那種案例,自己 unsubscribe 掉即可。
    """
    with TestClient(make_app()) as c:
        yield c


import contextlib  # noqa: E402

import httpx  # noqa: E402


@contextlib.asynccontextmanager
async def live_client(app):
    """async 版 client:lifespan 手動驅動、事件迴圈由測試持有 —
    TestClient 的迴圈在請求空檔不推進背景計時器(deadline watch 永不開火),
    這個 helper 讓計時器活在測試自己的 loop 上,await asyncio.sleep 期間照常運轉。"""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def bring_agent_online(app, name: str, room: str = "main"):
    """讓某個 agent「上線」—— 動態名冊時代的測試前置動作。

    為什麼需要這個:名冊不再是寫死的清單,而是「現在誰連著線」。
    所以測試裡要派任務給 bob 之前,得先讓 bob 有一條連線 ——
    就像真實世界裡,你得先把 bob 的敲鈴器開起來。

    直接建立訂閱而不是真的開一條 SSE,是因為測試要的只是「名冊上有他」這個事實,
    不需要真的收推播。回傳那個訂閱,測試可以用它模擬離線(bus.unsubscribe)。
    """
    return app.state.hub.bus.subscribe(room, watcher=name, is_agent=True)


def post_msg(client, room: str, sender: str, text: str, **kw):
    """測試用發言 helper:回傳 response(斷言交給呼叫端)。"""
    body = {"from": sender, "text": text, **kw}
    headers = kw.pop("headers", None)
    return client.post(f"/api/rooms/{room}/messages", json=body, headers=headers or {})
