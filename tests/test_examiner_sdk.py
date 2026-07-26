"""考官層(slow):官方 a2a-sdk 對真 HTTP server 驗互通。

命名空間注記:專案的 a2a.py 會遮蔽官方 `a2a` SDK 套件,因此考官必須在
「專案目錄之外」的子行程執行 —— 順帶把整條路徑升級成真 E2E:
tmp 部署副本 → 真 uvicorn 子行程 → 官方 SDK 型別逐一驗證回應形狀。

考官驗什麼:
1. A2ACardResolver 能解析我們的 Agent Card(官方 AgentCard 模型驗形)
2. SendMessage 回應能被官方 SendMessageResponse/Task 模型無損解析
3. reader= 已讀 → WORKING、reply_to → COMPLETED,每一步的 Task 形狀都過官方模型
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


EXAMINER_SCRIPT = textwrap.dedent('''
    """官方 SDK 考官:在專案目錄外執行,import a2a 解析到真 SDK(protobuf 型別)。
    ParseDict 預設嚴格(未知欄位即失敗)= 官方 schema 的無情驗形。"""
    import asyncio, json, sys, urllib.request

    import httpx
    from a2a.client import A2ACardResolver
    from a2a.types import Task, TaskState
    from google.protobuf.json_format import ParseDict

    BASE = sys.argv[1]

    def rpc(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        req = urllib.request.Request(f"{BASE}/agents/bob/a2a", data=body,
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    def parse_task(payload: dict) -> Task:
        return ParseDict(payload, Task())  # 官方 proto schema 嚴格驗形

    async def main():
        # 1) 官方 resolver 讀卡(SDK 自己的 AgentCard 驗證)
        async with httpx.AsyncClient() as hc:
            card = await A2ACardResolver(
                hc, BASE, agent_card_path="/agents/bob/.well-known/agent-card.json"
            ).get_agent_card()
        assert card.name == "bob", card

        # 2) SendMessage → result 過官方 Task proto 驗形
        out = rpc("SendMessage", {
            "message": {"role": "ROLE_USER", "parts": [{"text": "examiner task"}],
                        "messageId": "sdk-m1", "contextId": "exam"},
            "configuration": {"returnImmediately": True},
            "metadata": {"senderName": "examiner"}})
        task = parse_task(out["result"])
        assert task.status.state == TaskState.TASK_STATE_SUBMITTED, task.status.state

        # 3) 已讀 → WORKING;reply → COMPLETED(每步都過官方 proto)
        urllib.request.urlopen(f"{BASE}/api/rooms/exam/messages?since_id=0&reader=bob")
        working = parse_task(rpc("GetTask", {"id": task.id})["result"])
        assert working.status.state == TaskState.TASK_STATE_WORKING, working.status.state

        feed = json.load(urllib.request.urlopen(f"{BASE}/api/rooms/exam/messages?since_id=0"))
        feed_mid = feed["messages"][-1]["id"]
        reply = json.dumps({"from": "bob", "text": "sdk exam done",
                            "reply_to": feed_mid}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"{BASE}/api/rooms/exam/messages", data=reply,
            headers={"Content-Type": "application/json"}))
        done = parse_task(rpc("GetTask", {"id": task.id})["result"])
        assert done.status.state == TaskState.TASK_STATE_COMPLETED, done.status.state
        assert done.status.message.parts[0].text == "sdk exam done"

        print("EXAMINER PASS")

    asyncio.run(main())
''')


@pytest.mark.slow
def test_official_sdk_examiner(tmp_path):
    # tmp 部署副本:不汙染專案的 chat.jsonl / tasks.json
    #
    # ★ 這份清單就是「伺服器端的最小部署集」—— 少一個檔案,子行程就會 import 失敗、
    #   起不來,測試會以「server 子行程在時限內未就緒」的形式失敗。
    #   所以要往這裡加檔案之前,先想清楚:那真的是伺服器跑起來必需的嗎?
    #   (envfile.py 是 2026-07-26 加入設定檔功能時進來的,server.py 啟動時要用它讀 server.env)
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for f in ("server.py", "a2a.py", "envfile.py"):
        shutil.copy(ROOT / f, deploy / f)
    (deploy / "static").mkdir()
    (deploy / "static" / "index.html").write_text("<html></html>", encoding="utf-8")

    port = _free_port()
    proc = subprocess.Popen(
        [PYTHON, "server.py"], cwd=deploy,
        env={"PORT": str(port), "HOST": "127.0.0.1",
             "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
             "PATH": __import__("os").environ.get("PATH", "")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):  # 等 server 就緒
            try:
                urllib.request.urlopen(f"{base}/api/rooms/exam/state", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("server 子行程未在時限內就緒")

        exam = tmp_path / "examiner.py"  # 放在專案外,import a2a = 官方 SDK
        exam.write_text(EXAMINER_SCRIPT, encoding="utf-8")
        result = subprocess.run([PYTHON, str(exam), base], cwd=tmp_path,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, f"考官失敗:\n{result.stdout}\n{result.stderr}"
        assert "EXAMINER PASS" in result.stdout
    finally:
        proc.kill()
