"""A2A 協定層行為:JSON-RPC 生命週期、deadline、錯誤碼、跨重啟持久化。"""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from conftest import post_msg


import pytest
import server as server_mod  # noqa: E402


@pytest.fixture
def fast_deadline(monkeypatch):
    """timing 測試專用:把 deadline 下限降到 0.05s(生產值 5s 會讓測試慢到哭)。"""
    import a2a as a2a_mod
    monkeypatch.setattr(a2a_mod, "MIN_DEADLINE_SECONDS", 0.05)


def _rpc_body(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def _send_params(target_room: str = "main", sender: str = "alice", **meta) -> dict:
    return {"message": {"role": "ROLE_USER", "parts": [{"text": "task text"}],
                        "messageId": f"m-{time.time_ns()}", "contextId": target_room},
            "configuration": {"returnImmediately": True},
            "metadata": {"senderName": sender, **meta}}


def rpc(client, agent: str, method: str, params: dict) -> dict:
    r = client.post(f"/agents/{agent}/a2a",
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return r.json()


def send_task(client, target: str = "bob", sender: str = "alice",
              text: str = "task text", room: str = "main", **meta) -> dict:
    """發一個 task(非阻塞),回傳 result(Task 物件)。"""
    out = rpc(client, target, "SendMessage", {
        "message": {"role": "ROLE_USER", "parts": [{"text": text}],
                    "messageId": f"m-{time.time_ns()}", "contextId": room},
        "configuration": {"returnImmediately": True},
        "metadata": {"senderName": sender, **meta},
    })
    assert "result" in out, out
    return out["result"]


class TestSenderKind:
    """task 長出來的那則訊息,身分標對了沒有。

    ★ 這組測試鎖的是一個已經發生過的漏洞:kind 欄位上線時只鋪了聊天門
      (POST /api/rooms/.../messages),協定門(A2A SendMessage)漏了 ——
      於是 agent 透過 A2A 派的任務,那則 feed 訊息會掛上 HUMAN 徽章。
      而訊息只增不改,錯了就永遠錯。
    """

    def test_agent_sender_kind_reaches_feed_message(self, client):
        send_task(client, senderKind="agent")
        feed = client.get("/api/rooms/main/messages?since_id=0").json()["messages"]
        assert feed[-1]["kind"] == "agent"

    def test_missing_sender_kind_defaults_to_human(self, client):
        """不聲明就當人類 —— 與聊天門同一個預設,不能讓「AI 說的」變便宜。"""
        send_task(client)
        feed = client.get("/api/rooms/main/messages?since_id=0").json()["messages"]
        assert feed[-1]["kind"] == "human"


class TestLifecycle:
    def test_submit_creates_feed_message_with_mention(self, client):
        task = send_task(client)
        assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
        feed = client.get("/api/rooms/main/messages?since_id=0").json()["messages"]
        assert feed[-1]["task_id"] == task["id"]
        assert "bob" in feed[-1]["mentions"]  # 目標自動注入 mentions 觸發喚醒

    def test_reader_first_fetch_flips_working(self, client):
        task = send_task(client)
        client.get("/api/rooms/main/messages?since_id=0&reader=bob")
        got = rpc(client, "bob", "GetTask", {"id": task["id"]})["result"]
        assert got["status"]["state"] == "TASK_STATE_WORKING"

    def test_bystander_reply_does_not_complete(self, client):
        task = send_task(client)
        feed_mid = client.get("/api/rooms/main/messages?since_id=0").json()["messages"][-1]["id"]
        post_msg(client, "main", "dev", "路過引用", reply_to=feed_mid)
        got = rpc(client, "bob", "GetTask", {"id": task["id"]})["result"]
        assert got["status"]["state"] == "TASK_STATE_SUBMITTED"  # 旁人不動狀態

    def test_target_reply_completes_with_agent_message(self, client):
        task = send_task(client)
        feed_mid = client.get("/api/rooms/main/messages?since_id=0").json()["messages"][-1]["id"]
        post_msg(client, "main", "bob", "答案在此", reply_to=feed_mid)
        got = rpc(client, "bob", "GetTask", {"id": task["id"]})["result"]
        assert got["status"]["state"] == "TASK_STATE_COMPLETED"
        status_msg = got["status"]["message"]
        assert status_msg["role"] == "ROLE_AGENT"
        assert status_msg["parts"][0]["text"] == "答案在此"
        assert len(got["history"]) == 2  # 原始請求 + 完成回覆

    def test_deadline_timeout_fails(self, make_app, fast_deadline):
        """計時器測試走 live_client:TestClient 的迴圈在請求空檔不走錶。"""
        import asyncio

        from conftest import live_client

        async def flow():
            async with live_client(make_app()) as c:
                out = (await c.post("/agents/bob/a2a", json=_rpc_body(
                    "SendMessage", _send_params(deadlineSeconds=0.3)))).json()
                tid = out["result"]["id"]
                await asyncio.sleep(0.8)
                got = (await c.post("/agents/bob/a2a", json=_rpc_body(
                    "GetTask", {"id": tid}))).json()["result"]
                assert got["status"]["state"] == "TASK_STATE_FAILED"

        asyncio.run(flow())

    def test_blocking_send_waits_until_terminal(self, client, fast_deadline):
        """returnImmediately 預設 false:阻塞到終態(用短 deadline 讓它以 FAILED 解除)。"""
        out = rpc(client, "bob", "SendMessage", {
            "message": {"role": "ROLE_USER", "parts": [{"text": "block me"}],
                        "messageId": "m-block", "contextId": "main"},
            "metadata": {"senderName": "alice", "deadlineSeconds": 0.3},
        })
        assert out["result"]["status"]["state"] == "TASK_STATE_FAILED"

    def test_cancel_and_terminal_cancel_error(self, client):
        task = send_task(client)
        ok = rpc(client, "bob", "CancelTask", {"id": task["id"]})["result"]
        assert ok["status"]["state"] == "TASK_STATE_CANCELED"
        err = rpc(client, "bob", "CancelTask", {"id": task["id"]})["error"]
        assert err["code"] == -32002  # 終態不可再取消

    def test_error_codes(self, client):
        assert rpc(client, "bob", "GetTask", {"id": "ghost"})["error"]["code"] == -32001
        assert rpc(client, "bob", "NoSuchMethod", {})["error"]["code"] == -32601
        assert rpc(client, "nobody", "GetTask", {"id": "x"})["error"]["code"] == -32004

    def test_agent_card_required_fields(self, client):
        card = client.get("/agents/bob/.well-known/agent-card.json").json()
        for field in ("name", "description", "supportedInterfaces", "version",
                      "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
            assert field in card, f"Agent Card 缺 spec required 欄位 {field}"


class TestPersistence:
    def test_full_lifecycle_across_restarts(self, make_app):
        # 第一世:建 task
        with TestClient(make_app()) as c1:
            task = send_task(c1, deadlineSeconds=600)
            tid = task["id"]
        # 第二世:SUBMITTED 載回,首讀轉 WORKING
        with TestClient(make_app()) as c2:
            got = rpc(c2, "bob", "GetTask", {"id": tid})["result"]
            assert got["status"]["state"] == "TASK_STATE_SUBMITTED"
            c2.get("/api/rooms/main/messages?since_id=0&reader=bob")
        # 第三世:WORKING 載回,reply 完成(feed 反查索引跨重啟有效)
        with TestClient(make_app()) as c3:
            assert rpc(c3, "bob", "GetTask", {"id": tid})["result"]["status"]["state"] \
                == "TASK_STATE_WORKING"
            feed_mid = c3.get("/api/rooms/main/messages?since_id=0").json()["messages"][-1]["id"]
            post_msg(c3, "main", "bob", "done", reply_to=feed_mid)
        # 第四世:終態可查
        with TestClient(make_app()) as c4:
            assert rpc(c4, "bob", "GetTask", {"id": tid})["result"]["status"]["state"] \
                == "TASK_STATE_COMPLETED"

    def test_orphan_snapshot_failed_on_restore(self, make_app, isolated_base):
        """feed_mid 缺失的殭屍快照,復原時直接 FAILED。"""
        import a2a as a2a_mod
        snapshot = [{"id": "zombie", "context_id": "main", "target": "bob",
                     "deadline_seconds": 600.0, "state": "TASK_STATE_SUBMITTED",
                     "state_ts": a2a_mod.now_iso(), "state_message": None,
                     "history": [], "metadata": {}, "feed_mid": None,
                     "completed_mid": None, "created_ts": a2a_mod.now_iso()}]
        # 路徑要跟伺服器一致(hub_data/),不能寫在專案根目錄
        data_dir = isolated_base / server_mod.DATA_DIR_NAME
        data_dir.mkdir(exist_ok=True)
        (data_dir / "tasks.json").write_text(json.dumps(snapshot), encoding="utf-8")
        with TestClient(make_app()) as c:
            got = rpc(c, "bob", "GetTask", {"id": "zombie"})["result"]
            assert got["status"]["state"] == "TASK_STATE_FAILED"
            assert "orphaned" in got["metadata"].get("failureReason", "")

    def test_deadline_not_recounted_after_restart(self, make_app, fast_deadline):
        """重啟後以「剩餘時間」重掛:總時長仍是原 deadline,不是重新起算。
        時序:deadline=1.0s,停機吃掉 0.7s、復活後再過 0.6s(累計 1.3s)——
        剩餘時間制會 FAILED;若偷懶重新起算(復活後才過 0.6s)就還活著。"""
        import asyncio

        from conftest import live_client

        async def flow():
            async with live_client(make_app()) as c1:
                out = (await c1.post("/agents/bob/a2a", json=_rpc_body(
                    "SendMessage", _send_params(deadlineSeconds=1.0)))).json()
                tid = out["result"]["id"]
            await asyncio.sleep(0.7)  # 停機期間
            async with live_client(make_app()) as c2:
                await asyncio.sleep(0.6)
                got = (await c2.post("/agents/bob/a2a", json=_rpc_body(
                    "GetTask", {"id": tid}))).json()["result"]
                assert got["status"]["state"] == "TASK_STATE_FAILED"

        asyncio.run(flow())

    def test_expired_during_downtime_failed_at_boot(self, make_app, fast_deadline):
        with TestClient(make_app()) as c1:
            tid = send_task(c1, deadlineSeconds=0.2)["id"]
        time.sleep(0.5)  # 停機期間就過期
        with TestClient(make_app()) as c2:  # restore 在 lifespan 同步判死,TestClient 可測
            got = rpc(c2, "bob", "GetTask", {"id": tid})["result"]
            assert got["status"]["state"] == "TASK_STATE_FAILED"
            assert "downtime" in got["metadata"].get("failureReason", "")
