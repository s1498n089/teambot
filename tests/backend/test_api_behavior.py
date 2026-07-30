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

    def test_optimistic_lock_409_carries_no_messages(self, client):
        """409 只說「房間現在到哪」,**不夾帶訊息**。

        訊息只有一條取得路徑(撈訊息那個端點)。順便夾一份等於開第二條路,
        而兩條路各自維護正確性 —— 那正是漏讀 bug 曾經同時長在兩個地方的原因。
        """
        post_msg(client, "t1", "alice", "first")
        r = post_msg(client, "t1", "bob", "stale write", expect_last_id=0)
        assert r.status_code == 409
        body = r.json()
        assert body["last_id"] == 1
        assert "missed" not in body

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


# ---------- last_id 的語意:本批的尾,不是房間的尾 ----------

class TestFetchContract:
    """撈訊息的契約:**沒有筆數上限**,而過濾模式回的欄位名字不一樣。

    ★ 這個類別刻意造一個 600 則的房間,而它的任務是【證明不會被截斷】——
      哪天有人把上限加回來,第一個測試就會紅。

      (上限曾經存在,而它最貴的地方是:切一半的回應跟完整的回應
       在 JSON 裡長得一模一樣,照文件把回傳的 id 寫進 cursor 就會靜默漏讀。
       現在的做法是不切 —— 一次要撈幾百則的情境由 AGENTS.md 的加入流程
       接手:讀最近 50 則 + 掃點名,中間刻意跳過。)
    """

    ROOM = "big"
    TOTAL = 600

    def fill(self, client, total=None):
        """直接灌進 store,不走 HTTP —— 限流是 10 秒 10 則,600 則走不完。"""
        store = client.app.state.hub.store
        for i in range(total if total is not None else self.TOTAL):
            store.append(self.ROOM, "alice", f"m{i}", [])

    def test_no_limit_returns_everything(self, client):
        """600 則就給 600 則。這一條紅了 = 有人把上限加回來了。"""
        self.fill(client)
        data = client.get(f"/api/rooms/{self.ROOM}/messages?since_id=0").json()

        assert len(data["messages"]) == self.TOTAL
        assert [m["id"] for m in data["messages"]] == list(range(1, self.TOTAL + 1))
        assert data["last_id"] == self.TOTAL
        assert "room_last_id" not in data      # 不會截斷,就不需要第二個 id

    def test_mentioned_mode_filters_but_last_id_is_room_tail(self, client):
        """過濾模式只回被點名的那些,但 `last_id` 仍是**房間的尾**。

        這個模式只有一個用途:加入時掃一遍整段歷史,確認跳過舊訊息
        不會漏掉找自己的人。在那裡把 cursor 推到房間尾正是【刻意跳過】的決定。
        """
        store = client.app.state.hub.store
        store.append(self.ROOM, "alice", "hi @bob", ["bob"])
        store.append(self.ROOM, "alice", "plain", [])

        data = client.get(f"/api/rooms/{self.ROOM}/messages?mentioned=bob").json()
        assert [m["id"] for m in data["messages"]] == [1]
        assert data["last_id"] == 2                   # 房間的尾,不是這批的

    def test_stale_409_only_reports_where_the_room_is(self, client):
        """409 不論落後多少、或 cursor 超前,都只回一個 `last_id` = 房間的尾。

        ★ 超前的情況(房間被清空重建過)也靠這個值把 cursor 拉回來 ——
          方向是刻意選的:cursor 落後只是下次多讀幾則(無害),
          超前才會漏讀(有害,而且沒有人會發現)。
        """
        self.fill(client)
        behind = post_msg(client, self.ROOM, "bob", "stale", expect_last_id=0)
        assert behind.status_code == 409
        assert behind.json() == {"error": "stale", "last_id": self.TOTAL}

        ahead = post_msg(client, self.ROOM, "bob", "ahead", expect_last_id=9999)
        assert ahead.status_code == 409
        assert ahead.json() == {"error": "stale", "last_id": self.TOTAL}


class TestForceRing:
    """人類的「喂,醒醒」:往房間的 SSE 丟一則指名事件,對應的敲鈴器收到就強制敲。

    ★ 伺服器【不直接戳敲鈴器】—— 那是別台機器上的另一個行程。
      但它一直掛在這個房間的直播上,所以用現成的通道就夠了。
    """

    def test_ring_reports_target_and_online(self, client):
        r = client.post("/api/rooms/r1/ring/alice")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["target"] == "alice"
        assert body["online"] is False        # 沒有敲鈴器連著線

    def test_ring_event_reaches_the_room_stream(self, client):
        """事件要真的流進房間的直播 —— 敲鈴器就是靠那條線收到的。"""
        bus = client.app.state.hub.bus
        sub = bus.subscribe("r1", watcher="alice", is_agent=True)
        try:
            client.post("/api/rooms/r1/ring/alice")
            assert sub.queue.qsize() == 1
            assert sub.queue.get_nowait() == {"type": "ring", "target": "alice"}
        finally:
            bus.unsubscribe("r1", sub)

    def test_ring_knows_the_bell_is_connected(self, client):
        bus = client.app.state.hub.bus
        sub = bus.subscribe("r1", watcher="bob", is_agent=True)
        try:
            assert client.post("/api/rooms/r1/ring/bob").json()["online"] is True
        finally:
            bus.unsubscribe("r1", sub)


# ---------- AUTH 矩陣 ----------

class TestAuth:
    def _token_of(self, base, name):
        """從 tokens.json 拿不到明文(只存 hash)——改由 TokenStore.issue 重生已知明文。"""
        import server as server_mod
        # ★ 路徑要跟伺服器一致:資料檔全部在 hub_data/ 底下。
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


# ---------- SSE 直播 ----------


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

    def test_for_you_stamp_differs_per_connection(self, client):
        """同一則訊息,對被 @ 的人是急件、對別人不是 —— 戳蓋在【每條連線】上。

        ★ 只檢查 bus 佇列【抓不到這個】:佇列裡只有一份 dict,per-連線的差異
          是在序列化那一步才產生的 —— 跟下面那個 ring 事件是同一條教訓。
          真正要防的 bug 是「就地改共享 dict」,而它的症狀是隨機的:
          誰先序列化誰贏,看起來像「偶爾有人沒被敲醒」。
        """
        post_msg(client, "stamp", "carol", "hi @alice")
        alice = asyncio.run(collect_sse_frames(
            client.app, "/api/rooms/stamp/stream?since_id=0&watcher=alice&kind=agent",
            [], n_frames=1))
        bob = asyncio.run(collect_sse_frames(
            client.app, "/api/rooms/stamp/stream?since_id=0&watcher=bob&kind=agent",
            [], n_frames=1))
        assert '"for_you": true' in alice[0]
        assert '"for_you": false' in bob[0]

    def test_browser_stream_has_no_stamp(self, client):
        """瀏覽器不帶 watcher —— 那個戳是給敲鈴器看的,前端用不到。"""
        post_msg(client, "stamp2", "carol", "hi @alice")
        frames = asyncio.run(collect_sse_frames(
            client.app, "/api/rooms/stamp2/stream?since_id=0", [], n_frames=1))
        assert "for_you" not in frames[0]

    def test_non_message_event_survives_serialization(self, client):
        """直播上不是只有訊息:強制敲鈴那種事件【沒有 id】,序列化不能因此炸掉。

        ★ 這個測試是被一個真的 bug 逼出來的:第一版 to_sse 寫成
          f"id: {msg['id']}…",而 ring 事件沒有 id → KeyError。
          它在 async generator 裡,所以後果不是「這一則送不出去」,
          是**整條直播結束**,房間裡每個訂閱者(含瀏覽器)一起被踢掉。

        ★ 而只檢查 bus 佇列的測試【抓不到它】—— 佇列在序列化之前。
          要抓到就得真的走完這條路,所以這個測試在這裡而不是在單元層。
        """
        async def drive():
            bus = client.app.state.hub.bus

            async def ring_soon():
                await asyncio.sleep(0.15)      # 等串流掛上去再敲
                bus.publish("ringroom", {"type": "ring", "target": "alice"})

            pending = asyncio.ensure_future(ring_soon())
            got = await collect_sse_frames(
                client.app, "/api/rooms/ringroom/stream", [], n_frames=1)
            await pending
            return got

        frames = asyncio.run(drive())
        assert '"ring"' in frames[0] and '"alice"' in frames[0]
        assert "id:" not in frames[0]          # 沒有 id 就不寫那一行(續傳點不受影響)

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
