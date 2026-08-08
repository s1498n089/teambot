"""行為/整合層:TestClient 行程內直打 app,驗可視化層 API 的行為契約。"""
from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import server as server_mod

from conftest import bring_agent_online, post_msg


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


class TestWriteGate:
    """★★★ 三條寫入路徑都必須經過 `check_writer` —— 這條測試守的是**位置**。

    2026-08-07 拔掉 bearer token 認證之後,`check_writer` 變成一個空函式
    (只有 `return`)。空函式看起來像沒清乾淨的殘骸,而**刪掉它不會有任何症狀**:
    測試照樣全綠、功能照樣正常,直到有人要把「誰能寫」加回來,
    才發現得重新把入口一個一個找齊 —— 而**找漏一個仍然不會有症狀**。

    所以這裡不驗行為(它現在沒有行為),驗的是「這三條路還連在同一個點上」。
    ★ 用攔截而不是讀原始碼:原始碼比對會被改寫格式弄紅,而攔截問的是
      真正想問的那件事 —— 這個請求到底有沒有經過那道關卡。
    """

    def _gate_calls(self, client, monkeypatch, fire):
        seen = []
        hub = client.app.state.hub
        original = hub.check_writer

        def spy(name, request):
            seen.append(name)
            return original(name, request)

        monkeypatch.setattr(hub, "check_writer", spy)
        fire()
        return seen

    def test_posting_goes_through_the_gate(self, client, monkeypatch):
        seen = self._gate_calls(client, monkeypatch,
                                lambda: post_msg(client, "gate", "alice", "hi"))
        assert seen == ["alice"]

    def test_cursor_put_goes_through_the_gate(self, client, monkeypatch):
        post_msg(client, "gate", "alice", "seed")
        seen = self._gate_calls(
            client, monkeypatch,
            lambda: client.put("/api/rooms/gate/cursor/alice?last_id=1"))
        assert seen == ["alice"]

    def test_room_delete_goes_through_the_gate(self, client, monkeypatch):
        post_msg(client, "doomed", "alice", "seed")
        seen = self._gate_calls(
            client, monkeypatch,
            lambda: client.delete("/api/rooms/doomed?by=alice"))
        assert seen == ["alice"]


class TestCursor:
    """cursor 搬到 hub 之後,要補回它在 client 時代【天生就有】的值域保證。

    ★★ 檔案在 client 的時代,這些值只有 agent 自己寫得出來,而它沒有動機寫錯。
      變成 API 之後,任何一次打錯的 curl、任何一支寫錯的工具都寫得進來 ——
      而後果是 agent 靜靜地變聾,查起來要二十分鐘。

      **從結構搬到 API,失去的不只是「誰能寫」(那個 check_writer 補回來了),
      還有「能寫成什麼」。**
    """

    def _room_with(self, client, room, n=3):
        for i in range(n):
            post_msg(client, room, "alice", f"m{i}")

    def test_unread_person_is_zero_not_404(self, client):
        """沒進過這個房的人,答案是 0 而不是錯誤 —— 那是誠實的答案,不是失敗。"""
        r = client.get("/api/rooms/main/cursor/nobody")
        assert r.status_code == 200 and r.json()["last_id"] == 0

    def test_put_and_get_roundtrip(self, client):
        self._room_with(client, "main")
        assert client.put("/api/rooms/main/cursor/alice?last_id=2").status_code == 200
        assert client.get("/api/rooms/main/cursor/alice").json()["last_id"] == 2

    def test_ahead_of_the_room_tail_is_rejected(self, client):
        """★★ 這條是這一族最重要的:**超前 = 永久漏讀,而且無聲。**

        宣稱讀過還不存在的訊息,房間就永遠追不上那個數字 ——
        敲鈴器再也不會敲他,而他自己不會知道。
        """
        self._room_with(client, "main")            # 只有 3 則
        r = client.put("/api/rooms/main/cursor/alice?last_id=999999")
        assert r.status_code == 422
        assert "只到 #3" in r.json()["detail"]      # 錯誤訊息要說出房間到哪
        assert client.get("/api/rooms/main/cursor/alice").json()["last_id"] == 0

    def test_negative_is_rejected(self, client):
        self._room_with(client, "main")
        assert client.put("/api/rooms/main/cursor/alice?last_id=-5").status_code == 422

    def test_unknown_room_does_not_grow_a_ghost(self, client, isolated_base):
        """打錯房名不該長出一間幽靈房 —— 它會出現在清單上,看起來像真的。

        ★ `last_id=0` 是刻意的:用 1 的話,擋下它的其實是【超前檢查】
          (幽靈房的 tail 是 0),於是這條測試看起來過了,卻沒測到房間存在檢查 ——
          突變測試就是這樣抓到我的。**要測某一道關,就得讓其他關都放行。**
        """
        assert client.put("/api/rooms/ghost/cursor/alice?last_id=0").status_code == 422
        assert all(r["name"] != "ghost" for r in client.get("/api/rooms").json()["rooms"])
        assert not (client.app.state.hub.store.rooms_dir / "ghost").exists()

    def test_rewind_is_allowed_on_purpose(self, client):
        """★ 倒退【允許】,而且不需要旗標 —— 這是刻意的,理由是危險不對稱:

            cursor 落後   最壞是多讀幾則(無害,而且 agent 會發現自己讀過了)
            cursor 超前   永久漏讀,而且沒有人會發現

        只擋危險的那一邊。擋倒退的話,「我想重讀一段」這個合法需求就得繞路,
        而它防的是一個無害的錯 —— **防護要跟危險成比例。**
        """
        self._room_with(client, "main")
        client.put("/api/rooms/main/cursor/alice?last_id=3")
        assert client.put("/api/rooms/main/cursor/alice?last_id=1").status_code == 200
        assert client.get("/api/rooms/main/cursor/alice").json()["last_id"] == 1

    def test_cursors_list_is_the_member_list(self, client):
        """★ 「cursors/ 底下有你的檔案」就是「你在這個房間」—— 零新狀態。

        跟「房間清單從訊息推導」是同一招:沒有名冊要維護,
        也就不會有名冊與實際不符的那種 bug。
        """
        self._room_with(client, "main")
        client.put("/api/rooms/main/cursor/alice?last_id=2")
        client.put("/api/rooms/main/cursor/bob?last_id=3")
        assert client.get("/api/rooms/main/cursors").json()["cursors"] == {
            "alice": 2, "bob": 3}

    def test_cursor_is_per_room(self, client):
        """★ 這一整批的理由:在 lab 推進度不會蓋掉 main 的。"""
        self._room_with(client, "main")
        self._room_with(client, "lab")
        client.put("/api/rooms/main/cursor/alice?last_id=3")
        client.put("/api/rooms/lab/cursor/alice?last_id=1")
        assert client.get("/api/rooms/main/cursor/alice").json()["last_id"] == 3


class TestDeleteRoom:
    """刪除房間 —— **全站唯一的破壞性操作**,所以測試的重點是它的邊界。"""

    def test_removes_messages_from_that_room_only(self, client):
        post_msg(client, "doomed", "alice", "會被刪掉")
        post_msg(client, "keep", "bob", "要留著")

        r = client.delete("/api/rooms/doomed?by=allen")
        assert r.status_code == 200 and r.json()["messages"] == 1

        assert client.get("/api/rooms/doomed/messages?since_id=0").json()["messages"] == []
        kept = client.get("/api/rooms/keep/messages?since_id=0").json()
        assert kept["messages"][0]["text"] == "要留著"

    def test_rewrites_the_file_not_just_memory(self, client, make_app, isolated_base):
        """★ 記憶體清掉不算刪掉 —— hub 重啟會從 chat.jsonl 重新載入。

        只清記憶體的話,症狀是「刪掉的房間重開伺服器就回來了」。
        """
        post_msg(client, "doomed", "alice", "會被刪掉")
        post_msg(client, "keep", "bob", "要留著")
        client.delete("/api/rooms/doomed?by=allen")

        with TestClient(make_app()) as restarted:      # 同一個 hub_data,重新載入
            assert restarted.get("/api/rooms/doomed/messages?since_id=0").json()["messages"] == []
            assert restarted.get("/api/rooms/keep/messages?since_id=0").json()["messages"]

    def test_main_is_protected_by_structure(self, client):
        """★ 用結構擋,不是用確認框擋。

        確認框可以按錯,而按錯的代價是所有人的預設房消失 ——
        能用結構擋掉的就不要靠人小心。
        """
        post_msg(client, "main", "alice", "預設房的訊息")
        r = client.delete("/api/rooms/main?by=allen")
        assert r.status_code == 403
        assert client.get("/api/rooms/main/messages?since_id=0").json()["messages"]

    def test_drops_the_rooms_tasks(self, client):
        """task 也要跟著走 —— 留下來的話會指向一個不存在的房間。"""
        bring_agent_online(client.app, "bob", room="doomed")
        client.post("/agents/bob/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "做事"}],
                                   "messageId": "m-drop", "contextId": "doomed"},
                       "configuration": {"returnImmediately": True},
                       "metadata": {"senderName": "alice"}}})
        assert client.get("/api/rooms/doomed/tasks").json()["tasks"]

        client.delete("/api/rooms/doomed?by=allen")
        assert client.get("/api/rooms/doomed/tasks").json()["tasks"] == []

    def _make_task_in(self, client, room: str, message_id: str) -> None:
        bring_agent_online(client.app, "bob", room=room)
        client.post("/agents/bob/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "做事"}],
                                   "messageId": message_id, "contextId": room},
                       "configuration": {"returnImmediately": True},
                       "metadata": {"senderName": "alice"}}})

    def test_tasks_do_not_come_back_after_a_checkpoint(self, client):
        """★★ 這條守的是一個會「鬧鬼」的 bug(記憶體那一半)。

        tasks.json 是**全量快照、最後寫者贏**。如果刪除時只動了檔案、沒清記憶體,
        那麼下一次任何狀態轉換觸發 checkpoint,就會把剛刪掉的 task 原封不動寫回去 ——
        症狀是「刪掉的東西過一會兒自己長回來」,而查的人會從檔案系統開始懷疑起。
        """
        self._make_task_in(client, "doomed", "m-ghost")
        client.delete("/api/rooms/doomed?by=allen")

        registry = client.app.state.hub.a2a_layer.registry
        registry.checkpoint()                       # 模擬刪除後的任何一次狀態轉換
        assert registry.in_room("doomed") == []
        assert not any(t.context_id == "doomed" for t in registry.all_tasks())

    def test_tasks_live_in_their_own_room_directory(self, client, isolated_base):
        """★ 一房一個 tasks.json —— 「全量快照、最後寫者贏」的範圍縮到單一房間。

        以前所有房間的任務擠在一個檔案裡,任何一次狀態轉換都會把【全部】重寫一次;
        A 房的 checkpoint 有機會洗掉 B 房剛寫進去的東西。分房之後那個交叉沒了。
        """
        self._make_task_in(client, "alpha", "m-alpha")
        self._make_task_in(client, "beta", "m-beta")

        rooms_dir = client.app.state.hub.store.rooms_dir

        # ★ 【後建】的那個房才是關鍵。只檢查 alpha 的話這條測試是裝飾:
        #   快照就算寫成「全部 task 混在一起」,alpha 落盤的當下也只有它自己一個,
        #   所以永遠是 1 —— 突變測試就是這樣抓到我的:改壞了它照樣綠。
        for room in ("alpha", "beta"):
            path = rooms_dir / room / "tasks.json"
            assert path.exists()
            snap = json.loads(path.read_text(encoding="utf-8"))
            assert len(snap) == 1, f"{room} 的快照混進了別房的 task"
            assert snap[0]["context_id"] == room

    def test_task_room_comes_from_the_directory(self, client, make_app, isolated_base):
        """★ 快照裡的 context_id 跟目錄不一致時,以【目錄】為準 —— 跟訊息同一條規則。

        目錄是這個 task 現在住在哪裡,欄位是它被寫下來時記的。兩者衝突通常代表
        有人手動搬過檔案(例如遷移腳本跑歪了),而那時該相信看得見的那個。
        """
        room_dir = isolated_base / server_mod.DATA_DIR_NAME / "rooms" / "moved"
        room_dir.mkdir(parents=True, exist_ok=True)
        (room_dir / "tasks.json").write_text(json.dumps([{
            "id": "t1", "context_id": "old-name", "target": "bob",
            "deadline_seconds": 600.0, "state": "TASK_STATE_COMPLETED",
            "state_ts": "2026-01-01T00:00:00+00:00", "state_message": None,
            "history": [], "metadata": {}, "feed_mid": 1, "completed_mid": None,
            "created_ts": "2026-01-01T00:00:00+00:00"}]), encoding="utf-8")

        with TestClient(make_app()) as c:
            assert len(c.get("/api/rooms/moved/tasks").json()["tasks"]) == 1
            assert c.get("/api/rooms/old-name/tasks").json()["tasks"] == []

            # ★ 索引查得到【不代表】欄位是對的:_by_room 用的是目錄名,
            #   所以就算沒把 context_id 改正,上面兩行照樣過 ——
            #   突變測試就是這樣抓到我的。要看 task 自己報的 contextId 才算數,
            #   因為那是 A2A 協定對外的答案(GetTask 回的東西)。
            task_id = c.get("/api/rooms/moved/tasks").json()["tasks"][0]["id"]
            spec = c.post("/agents/bob/a2a", json={
                "jsonrpc": "2.0", "id": 1, "method": "GetTask",
                "params": {"id": task_id}}).json()
            assert spec["result"]["contextId"] == "moved"

    def test_tasks_do_not_survive_a_restart(self, client, make_app, isolated_base):
        """★★ 同一個 bug 的【檔案那一半】—— 而這一半上面那條測不到。

        清了記憶體卻沒落盤的話,記憶體檢查全過(它真的空了),
        但 tasks.json 裡那些 task 原封不動 —— hub 一重啟就全部回來。

        ★ 這條是突變測試逼出來的:我把 drop_room 的 checkpoint 拿掉,
          上面那條照樣綠 —— 那時才知道它只守了一半。
        """
        self._make_task_in(client, "doomed", "m-persist")
        client.delete("/api/rooms/doomed?by=allen")

        with TestClient(make_app()) as restarted:      # 同一個 hub_data,重新載入
            assert restarted.get("/api/rooms/doomed/tasks").json()["tasks"] == []

    def test_disconnects_that_rooms_subscribers(self, client):
        """掛在被刪房間上的直播連線要收掉 —— 不然它們掛在一個不存在的房間上。"""
        sub = bring_agent_online(client.app, "carol", room="doomed")
        post_msg(client, "doomed", "alice", "x")
        client.delete("/api/rooms/doomed?by=allen")
        assert sub.dead is True

    def test_other_rooms_files_are_not_touched(self, client, isolated_base):
        """★ 刪一個房間不該碰到別人的檔案 —— 而分資料夾之後這是【結構保證】。

        這條測試取代了原本的 `test_keeps_unparseable_lines`。那條守的是
        「重寫 chat.jsonl 時壞行要原樣保留」,而那個規則隨著重寫路徑一起消失了:
        現在刪房是 rmtree 一個目錄,根本不會讀到別人的行。

        ★ 規則消失時,守著它的測試也該跟著走 —— 留著會變成「守著一個不存在的行為」,
          而那種測試永遠是綠的,還會讓人以為某個保護還在。
        """
        post_msg(client, "doomed", "alice", "會被刪掉")
        post_msg(client, "keep", "bob", "要留著")
        keep_path = client.app.state.hub.store.chat_path("keep")
        before = keep_path.read_text(encoding="utf-8")

        client.delete("/api/rooms/doomed?by=allen")
        assert keep_path.read_text(encoding="utf-8") == before   # 一個位元組都沒動

    def test_path_traversal_leaves_the_data_dir_intact(self, client, isolated_base):
        """★★ 房間名會變成路徑之後,`..` 這種東西不能讓資料目錄消失。

        ★ 斷言的是【後果】不是【狀態碼】:HTTP 那條路上 `..` 會先被 URL 正規化吃掉
          (回 404),而不是走到我們的白名單 —— 兩種都算擋住了,但擋的層不一樣。
          釘住狀態碼會讓這條測試變成在測 Starlette 的路由行為,那不是我們的東西。
        """
        post_msg(client, "keep", "alice", "要留著")
        rooms_dir = client.app.state.hub.store.rooms_dir

        for evil in ["..", "%2E%2E", "..%2F.."]:
            client.delete(f"/api/rooms/{evil}?by=attacker")

        assert rooms_dir.exists()
        assert (rooms_dir / "keep" / "chat.jsonl").exists()

    def test_a2a_context_id_cannot_escape_the_rooms_dir(self, client, isolated_base):
        """★★ 真正的攻擊面在這裡 —— A2A 的 contextId 【沒有 URL 正規化】。

        contextId 是 JSON body 裡的一個字串,原封不動變成房間名、再變成資料夾名。
        HTTP 那條路上 `..` 會被路由層吃掉,這條路上不會 —— 所以白名單是這裡唯一的關。
        """
        bring_agent_online(client.app, "bob", room="../escape")
        client.post("/agents/bob/a2a", json={
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
            "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "逃"}],
                                   "messageId": "m-escape", "contextId": "../escape"},
                       "configuration": {"returnImmediately": True},
                       "metadata": {"senderName": "attacker"}}})

        rooms_dir = client.app.state.hub.store.rooms_dir
        assert not (rooms_dir.parent / "escape").exists()   # 沒有跳出 rooms/
        assert not (rooms_dir / ".." / "escape").exists()


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
        """事件要真的流進房間的直播 —— 敲鈴器就是靠那條線收到的。

        ★ 這條測試不能假設「佇列裡只有我要找的那一則」:訂閱的當下 hub 會先廣播
          一則 presence(2026-08-08 加的),所以要**在佇列裡找**,不是拿第一則。
          寫成「第 N 則是什麼」的測試,會在每次新增一種訊號時無辜地紅。
        """
        bus = client.app.state.hub.bus
        sub = bus.subscribe("r1", watcher="alice", is_agent=True)
        try:
            client.post("/api/rooms/r1/ring/alice")
            events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
            assert {"type": "ring", "target": "alice"} in events
        finally:
            bus.unsubscribe("r1", sub)

    def test_ring_knows_the_bell_is_connected(self, client):
        bus = client.app.state.hub.bus
        sub = bus.subscribe("r1", watcher="bob", is_agent=True)
        try:
            assert client.post("/api/rooms/r1/ring/bob").json()["online"] is True
        finally:
            bus.unsubscribe("r1", sub)

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
            # ★ 要兩幀:掛上去的當下會先來一則 presence(2026-08-08 起),ring 是第二則。
            #   寫死 n_frames=1 的話會在 ring 到達之前就斷線,而那不是 ring 壞了。
            got = await collect_sse_frames(
                client.app, "/api/rooms/ringroom/stream", [], n_frames=2)
            await pending
            return got

        frames = asyncio.run(drive())
        # ★ 不假設它是第幾幀 —— 訊號的種類會隨時間增加
        ring_frames = [f for f in frames if '"ring"' in f]
        assert ring_frames and '"alice"' in ring_frames[0]
        # 沒有 id 就不寫那一行(續傳點不受影響)—— 每一幀訊號都要成立
        assert all("id:" not in frame for frame in frames)

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

    def test_presence_is_pushed_not_only_polled(self, client):
        """★★ 有人進出時,hub 要【主動廣播】在場名單 —— 不能只等別人來問。

        2026-08-08 之前,在場名單只有一條路徑:前端每 15 秒問一次。
        於是「誰上線了」最久要等 15 秒才看得到,而 **hub 在那一刻就知道了** ——
        知道的那一方沒有把話傳出去。

        ★ 這條測的是「有沒有推」,不是「多快」:推出去之後對方收不收得到
          是另一件事(斷線就漏了),所以前端那個輪詢仍然要留著當保底。
        """
        bus = client.app.state.hub.bus
        watcher = bus.subscribe("pr", watcher="watching-eye")
        try:
            # ★★ 順帶釘住一件反直覺的事:**你會收到自己上線那一則**。
            #   直覺會說「我連上的那一瞬間,我還沒開始聽,所以聽不到自己」——
            #   但這裡的訂閱是【先建佇列、再廣播】,而佇列會緩衝:
            #   等串流開始讀的時候,那一則還在裡面等著。
            #   所以「進場之後要自己再問一次」那個補丁**不需要**。
            mine = [watcher.queue.get_nowait() for _ in range(watcher.queue.qsize())]
            assert any(e.get("type") == "presence" and "watching-eye" in e["present"]
                       for e in mine), "自己上線的那一則,自己也要收得到"

            joiner = bus.subscribe("pr", watcher="newcomer")
            events = [watcher.queue.get_nowait() for _ in range(watcher.queue.qsize())]
            presence = [e for e in events if e.get("type") == "presence"]
            assert presence, "有人進來,在場的人要【立刻】收到通知"
            assert "newcomer" in presence[-1]["present"]

            bus.unsubscribe("pr", joiner)
            events = [watcher.queue.get_nowait() for _ in range(watcher.queue.qsize())]
            presence = [e for e in events if e.get("type") == "presence"]
            assert presence, "有人離開也要"
            assert "newcomer" not in presence[-1]["present"]
        finally:
            bus.unsubscribe("pr", watcher)

    def test_presence_event_carries_no_id(self, client):
        """★ presence 事件**不能有 id** —— id 是訊息的續傳游標。

        給訊號一個 id 會讓瀏覽器的 Last-Event-ID 跳到一個不是訊息的位置,
        斷線重連時就從錯的地方續傳。前端也靠「有沒有 id」分辨訊息與訊號。
        """
        bus = client.app.state.hub.bus
        sub = bus.subscribe("pr2", watcher="someone")
        try:
            events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
            presence = [e for e in events if e.get("type") == "presence"]
            assert presence
            assert all("id" not in event for event in presence)
        finally:
            bus.unsubscribe("pr2", sub)

    def test_leave_removes_watcher_immediately(self, client):
        """★★ 「我要走了」要【立刻】生效,不能等 keep-alive 超時才發現。

        2026-08-07 的 bug:換房會重新載入頁面,而伺服器要等 15 秒才知道
        上一頁走了 —— 於是新頁面查在場名單,查到**自己上一秒的鬼影**,
        然後把使用者擋在門外,畫面上寫著「這個名字有人正在用」。

        所以離開的那一方要能主動說一聲(瀏覽器用 navigator.sendBeacon 送)。
        這條測的就是「說了之後名單當場就對」。
        """
        async def flow():
            seen_before, seen_after = [], []
            done = asyncio.Event()

            async def receive():
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    seen_before.append(client.get("/api/rooms/p/presence").json()["present"])
                    # ★ 連線【還開著】的時候說再見 —— 那正是換頁瞬間的實況
                    reply = client.post("/api/rooms/p/leave?watcher=alice")
                    seen_after.append((reply.status_code, reply.json(),
                                       client.get("/api/rooms/p/presence").json()["present"]))
                    done.set()

            scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                     "path": "/api/rooms/p/stream", "root_path": "",
                     "query_string": b"since_id=0&watcher=alice", "headers": [],
                     "client": ("test", 1), "server": ("test", 80)}
            await asyncio.wait_for(client.app(scope, receive, send), timeout=5)
            return seen_before, seen_after

        before, after = asyncio.run(flow())
        assert before and "alice" in before[0], "說再見之前應該在場"
        status, payload, present = after[0]
        assert status == 200
        assert payload["dropped"] == 1
        assert "alice" not in present, "說完再見,名單上【當場】就不該有他"

    def test_leave_is_forgiving(self, client):
        """沒連線、名字不存在、名字空白 —— 一律回 200。

        ★ 告別是**盡力而為**的動作:送出它的時候頁面已經在關了,
          沒有人接得住錯誤。回 4xx 只會在瀏覽器主控台留下嚇人的紅字,
          而使用者什麼也做不了。
        """
        for query in ["?watcher=nobody", "?watcher=", ""]:
            reply = client.post(f"/api/rooms/p/leave{query}")
            assert reply.status_code == 200, query
            assert reply.json()["dropped"] == 0

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
