"""行為/整合層:TestClient 行程內直打 app,驗可視化層 API 的行為契約。"""
from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import post_msg


# ---------- 訊息流:樂觀鎖 / reply_to / 邊界 ----------

class TestMessages:
    def test_post_and_fetch_roundtrip(self, client):
        r = post_msg(client, "t1", "alice", "hi @bob")
        assert r.status_code == 201 and r.json()["id"] == 1
        data = client.get("/api/rooms/t1/messages?since_id=0").json()
        assert data["last_id"] == 1
        assert data["messages"][0]["mentions"] == ["bob"]

    def test_optimistic_lock_409_with_missed(self, client):
        post_msg(client, "t1", "alice", "first")
        r = post_msg(client, "t1", "bob", "stale write", expect_last_id=0)
        assert r.status_code == 409
        body = r.json()
        assert body["last_id"] == 1
        assert [m["text"] for m in body["missed"]] == ["first"]  # 一個 round-trip 補齊

    def test_reply_to_unknown_422(self, client):
        r = post_msg(client, "t1", "alice", "quote ghost", reply_to=99)
        assert r.status_code == 422

    def test_bad_sender_422(self, client):
        assert post_msg(client, "t1", "a b", "x").status_code == 422
        assert post_msg(client, "t1", "@bob", "x").status_code == 422

    def test_text_length_boundary_8000(self, client):
        assert post_msg(client, "t1", "alice", "x" * 8000).status_code == 201
        assert post_msg(client, "t1", "alice", "x" * 8001).status_code == 422

    def test_unicode_sender_roundtrip(self, client):
        r = post_msg(client, "t1", "小明", "中文名字發言")
        assert r.status_code == 201
        members = client.get("/api/rooms/t1/members").json()["members"]
        assert any(m["name"] == "小明" for m in members)

    def test_state_endpoint(self, client):
        assert client.get("/api/rooms/empty/state").json() == {
            "room": "empty", "last_id": 0, "count": 0}


# ---------- AUTH 矩陣 ----------

class TestAuth:
    def _token_of(self, base, name):
        """從 tokens.json 拿不到明文(只存 hash)——改由 TokenStore.issue 重生已知明文。"""
        import server as server_mod
        # ★ 路徑要跟伺服器一致:資料檔全部在 hub_data/ 底下。
        #   這裡曾經寫成 base / "tokens.json"(根目錄),靠啟動時的搬移函式
        #   把它搬進 hub_data/ 才碰巧能動 —— 搬移函式退役後就當場現形。
        data_dir = base / server_mod.DATA_DIR_NAME
        data_dir.mkdir(exist_ok=True)
        return server_mod.TokenStore(data_dir / "tokens.json").issue(name)

    def test_auth_off_is_open(self, client):
        assert post_msg(client, "t", "anyone", "free speech").status_code == 201

    def test_auth_on_requires_token(self, make_app, isolated_base):
        with TestClient(make_app(AUTH="on")) as c:
            assert post_msg(c, "t", "alice", "no key").status_code == 401

    def test_auth_wrong_identity_403_names_owner(self, make_app, isolated_base):
        bob_token = self._token_of(isolated_base, "bob")
        with TestClient(make_app(AUTH="on")) as c:
            r = c.post("/api/rooms/t/messages",
                       json={"from": "alice", "text": "impersonation"},
                       headers={"Authorization": f"Bearer {bob_token}"})
            assert r.status_code == 403
            assert "bob" in r.json()["detail"]  # 403 要指出鑰匙真正的主人

    def test_auth_valid_token_201(self, make_app, isolated_base):
        alice_token = self._token_of(isolated_base, "alice")
        with TestClient(make_app(AUTH="on")) as c:
            r = c.post("/api/rooms/t/messages",
                       json={"from": "alice", "text": "with key"},
                       headers={"Authorization": f"Bearer {alice_token}"})
            assert r.status_code == 201

    def test_reads_stay_public_under_auth(self, make_app, isolated_base):
        with TestClient(make_app(AUTH="on")) as c:
            assert c.get("/api/rooms/t/messages?since_id=0").status_code == 200
            assert c.get("/api/config").json()["authEnabled"] is True


# ---------- 動態註冊鏈 ----------

async def collect_sse_frames(app, path: str, headers: list, n_frames: int, timeout: float = 5.0):
    """手動 ASGI 驅動 SSE 端點:收滿 n 個 data 幀就送 http.disconnect 終止串流。

    TestClient 對「無限串流」的關閉在 Windows 上會卡死,改由測試自己當 ASGI 伺服器 —
    Starlette 的 StreamingResponse 會監聽 disconnect 並取消生成器,乾淨收場。
    """
    frames: list[str] = []
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            for line in message.get("body", b"").decode("utf-8").splitlines():
                if line.startswith("data: "):
                    frames.append(line)
            if len(frames) >= n_frames:
                disconnect.set()

    scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path.split("?")[0], "raw_path": path.encode(), "root_path": "",
             "query_string": path.split("?", 1)[1].encode() if "?" in path else b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
             "client": ("test", 1), "server": ("test", 80)}
    await asyncio.wait_for(app(scope, receive, send), timeout=timeout)
    return frames



# 註:這裡曾經有 TestRegister —— 測「憑邀請碼註冊成員」的那套流程。
# 2026-07-27 名冊改成「誰現在連著線」之後,註冊這個動作本身就不存在了,
# 測試隨功能一起退役。考古請看 git 歷史。


class TestSSE:
    def test_stream_replays_since_id(self, client, make_app):
        post_msg(client, "s", "alice", "one")
        post_msg(client, "s", "bob", "two")
        frames = asyncio.run(collect_sse_frames(
            client.app, "/api/rooms/s/stream?since_id=0", [], n_frames=2))
        assert '"one"' in frames[0] and '"two"' in frames[1]

    def test_last_event_id_resume(self, client):
        post_msg(client, "s", "alice", "one")
        post_msg(client, "s", "bob", "two")
        frames = asyncio.run(collect_sse_frames(
            client.app, "/api/rooms/s/stream", [("Last-Event-ID", "1")], n_frames=1))
        assert '"two"' in frames[0]  # 從斷點續傳,不重播 id 1

    def test_slow_subscriber_marked_dead(self, make_app):
        """backpressure:佇列灌滿後再 publish,訂閱者被標記淘汰(單元式驗證)。"""
        import server as server_mod
        bus = server_mod.EventBus()
        sub = bus.subscribe("r")
        for i in range(server_mod.SUBSCRIBER_QUEUE_MAXSIZE):
            bus.publish("r", {"id": i})
        bus.publish("r", {"id": "overflow"})
        assert sub.dead is True


# ---------- presence(在場判定:SSE 訂閱者名單)----------

class TestPresence:
    """在場 = SSE 連線開著,而非「最近有沒有發言」——
    事件驅動的 agent 安靜待命時仍該算在場。"""

    def test_empty_when_nobody_watching(self, client):
        assert client.get("/api/rooms/p/presence").json()["present"] == []

    def test_named_watcher_appears_while_connected(self, client):
        """訂閱中 → 在場;連線結束 → 不在場。"""
        async def flow():
            seen_during, seen_after = [], []
            done = asyncio.Event()

            async def receive():
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    seen_during.append(client.get("/api/rooms/p/presence").json()["present"])
                    done.set()

            scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                     "path": "/api/rooms/p/stream", "root_path": "",
                     "query_string": b"since_id=0&watcher=alice", "headers": [],
                     "client": ("test", 1), "server": ("test", 80)}
            await asyncio.wait_for(client.app(scope, receive, send), timeout=5)
            seen_after.append(client.get("/api/rooms/p/presence").json()["present"])
            return seen_during, seen_after

        during, after = asyncio.run(flow())
        assert during and "alice" in during[0]   # 連線期間在場
        assert after[0] == []                    # 連線結束即離場

    def test_anonymous_watcher_not_counted(self, client):
        """不報名字的訂閱者(純觀眾)不計入在場名單。"""
        async def flow():
            names = []
            done = asyncio.Event()

            async def receive():
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    names.append(client.get("/api/rooms/p/presence").json()["present"])
                    done.set()

            scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                     "path": "/api/rooms/p/stream", "root_path": "",
                     "query_string": b"since_id=0", "headers": [],
                     "client": ("test", 1), "server": ("test", 80)}
            await asyncio.wait_for(client.app(scope, receive, send), timeout=5)
            return names

        assert asyncio.run(flow())[0] == []

    def test_impersonation_downgraded_under_auth(self, make_app, isolated_base):
        """AUTH=on 時冒名 watcher 降級為匿名 —— 否則誰都能假裝別人在線。"""
        app = make_app(AUTH="on")
        with TestClient(app) as c:
            async def flow():
                names = []
                done = asyncio.Event()

                async def receive():
                    await done.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    if message["type"] == "http.response.body":
                        names.append(c.get("/api/rooms/p/presence").json()["present"])
                        done.set()

                scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                         "path": "/api/rooms/p/stream", "root_path": "",
                         "query_string": b"since_id=0&watcher=alice", "headers": [],
                         "client": ("test", 1), "server": ("test", 80)}
                await asyncio.wait_for(app(scope, receive, send), timeout=5)
                return names

            assert asyncio.run(flow())[0] == []  # 無 token 冒名 → 不計入


class TestAgentKindWiring:
    """「連線宣告自己是 agent」這條線的兩端。

    bob 驗收時點出:其他測試用 conftest 的 bring_agent_online 直接建 Subscription,
    那繞過了真實通道 —— 從網址上的 kind=agent 到伺服器內部的 is_agent 旗標。
    線斷了的話所有測試照樣綠,而真實世界裡沒有 agent 進得了名冊。

    ★ 覆蓋範圍:這裡測的是【兩端】——
        送出端:敲鈴器組出來的網址確實帶 kind=agent
        接收端:is_agent=True 的訂閱確實會進 live_agents、False 的不會

      中間那段(HTTP query → FastAPI 參數 → subscribe)在這一層【故意不測】:
      SSE 的連線永遠不會結束,同步客戶端進得去出不來 —— 第一版就是這樣掛死 120 秒的。

      但它並非無人看守:考官測試(test_examiner_sdk,slow 層)跑的是真 server,
      而且會【真的開一條 SSE 連線】宣告 kind=agent,再從 /agents 目錄確認它進了名冊 ——
      那就是這條通道的端對端閉環。分層是刻意的:
      快測試鎖兩端(毫秒級、跑得勤),慢測試驗整條線(秒級、跑得少)。
    """

    def test_送出端_敲鈴器的網址帶著_kind_agent(self):
        import bell as bell_mod
        source = inspect.getsource(bell_mod.sse_watch)
        assert "kind=agent" in source, "敲鈴器沒有宣告自己包的是 agent"
        assert "watcher=" in source

    def test_接收端_宣告_agent_的訂閱會進名冊(self, make_app):
        bus = make_app().state.hub.bus
        bus.subs.clear()
        bus.subscribe("w", watcher="zed", is_agent=True)
        assert "zed" in bus.live_agents("w")

    def test_接收端_沒宣告的訂閱只算在場_不算_agent(self, make_app):
        """瀏覽器就是這樣連的 —— 人類要算在場,但不該出現在派任務名冊。"""
        bus = make_app().state.hub.bus
        bus.subs.clear()
        bus.subscribe("w", watcher="kevin")
        assert "kevin" not in bus.live_agents("w")
        assert "kevin" in bus.watchers("w")

    def test_連線消失後就不在名冊上(self, make_app):
        """這正是 dev 問題的解法:關掉視窗就從名冊消失。"""
        bus = make_app().state.hub.bus
        bus.subs.clear()
        sub = bus.subscribe("w", watcher="zed", is_agent=True)
        assert "zed" in bus.live_agents("w")
        bus.unsubscribe("w", sub)
        assert "zed" not in bus.live_agents("w")
