"""A2A Protocol 1.0 層(JSON-RPC 2.0 binding)— 專案的底層協定。

架構定位:房間 = contextId;聊天室 /api/* 與 UI 是本層之上的可視化。
本模組不做 HTTP,只有純狀態機與方法邏輯 — 路由掛載在 server.create_app()(composition root)。

Task 生命週期(共識 #112):
  feed 入流                        = TASK_STATE_SUBMITTED
  目標 agent 首次帶 reader= 撈到    = TASK_STATE_WORKING(已讀回條的協定化)
  目標 agent 對 task 訊息 reply_to  = TASK_STATE_COMPLETED(旁人引用不動狀態;首回定終態)
  deadline 逾時                    = TASK_STATE_FAILED

重構紀錄(code review #151/#152 共識):Task dataclass 合體 spec 欄位與 hub 管理欄位、
TaskRegistry 統一三索引(Repository)、宣告式狀態轉換表(OCP)、deadline timer 終態即取消。
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------- 協定常數 ----------

A2A_PROTOCOL_VERSION = "1.0"
DEFAULT_DEADLINE_SECONDS = 300.0   # 發起方未指定 metadata.deadlineSeconds 時的預設
MIN_DEADLINE_SECONDS = 5.0
MAX_DEADLINE_SECONDS = 3600.0
STREAM_QUEUE_MAXSIZE = 64          # 單一串流訂閱者的事件緩衝;滿了就丟棄該訂閱者
STREAM_KEEPALIVE_SECONDS = 15

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
INTERRUPTED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}

# 宣告式狀態轉換表(bob review #151-2):合法轉換的唯一事實來源。
# backlog 的 REJECTED / INPUT_REQUIRED 上線時,只需在這裡加表項。
# 注意:目前沒有任何通往 INTERRUPTED 的表項,_transition 內的 INTERRUPTED 分支
# 是「刻意預留」不是 dead code(bob 複審 #155)— 等 v2 讓 agent 能標記
# input-required 時,加表項即可啟用,阻塞中的 SendMessage 也會正確解除。
ALLOWED_TRANSITIONS = {
    ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"),
    ("TASK_STATE_SUBMITTED", "TASK_STATE_COMPLETED"),
    ("TASK_STATE_SUBMITTED", "TASK_STATE_FAILED"),
    ("TASK_STATE_SUBMITTED", "TASK_STATE_CANCELED"),
    ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED"),
    ("TASK_STATE_WORKING", "TASK_STATE_FAILED"),
    ("TASK_STATE_WORKING", "TASK_STATE_CANCELED"),
}

# 錯誤碼:已對原始 spec 驗證(A2A 專屬碼範圍 -32001 ~ -32099)
ERR_TASK_NOT_FOUND = -32001
ERR_TASK_NOT_CANCELABLE = -32002
ERR_PUSH_NOT_SUPPORTED = -32003
ERR_UNSUPPORTED_OPERATION = -32004
ERR_CONTENT_TYPE = -32005
ERR_EXTENDED_CARD_NOT_CONFIGURED = -32007

# Agent 註冊表:skills 為各 agent 自報;color 經 /api/config 下發給前端(SSOT,#151-7)。
AGENT_PROFILES = {
    "alice": {
        "color": "#ff79c6",
        "description": "UI/UX 視覺與可讀性導向的評審 agent:設計提案、可用性實測、規格挑戰。",
        "skills": [
            {"id": "design-critique", "name": "設計評論與提案", "description": "以實測為基礎的 UI/UX 主張與收斂", "tags": ["design", "ux"]},
            {"id": "spec-review", "name": "規格審讀", "description": "對照官方 spec 找出實作偏差", "tags": ["review"]},
        ],
    },
    "bob": {
        "color": "#00d4ff",
        "description": "語意嚴謹與邊界情境導向的評審 agent:協定挑戰、效能視角、對抗性測試。",
        "skills": [
            {"id": "adversarial-review", "name": "對抗性審查", "description": "找出提案的語意衝突與邊界漏洞", "tags": ["review", "qa"]},
            {"id": "protocol-design", "name": "協定設計", "description": "生命週期與狀態機的嚴謹化", "tags": ["protocol"]},
        ],
    },
    "dev": {
        "color": "#ffb86c",
        "description": "本聊天室的開發與維運 agent:接收需求、實作、修 bug、發佈。",
        "skills": [
            {"id": "implementation", "name": "功能實作", "description": "server / UI / 協定層的開發與部署", "tags": ["dev"]},
        ],
    },
}


def now_iso() -> str:
    """UTC RFC3339(帶時區),全系統時間戳的唯一格式。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class A2AError(Exception):
    """JSON-RPC 層的業務錯誤;由路由轉成 {"error": {code, message}}。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class Task:
    """一個 A2A Task:spec 欄位 + hub 內部管理欄位合體(#151-1)。

    spec 形狀一律經 to_spec() 輸出,內部欄位(target/feed_mid/訂閱者等)不外洩。
    """
    id: str
    context_id: str                 # = 房間名
    target: str                     # 被指派的 agent
    deadline_seconds: float
    state: str = "TASK_STATE_SUBMITTED"
    state_ts: str = field(default_factory=now_iso)
    state_message: dict | None = None      # 終態時附上的回覆 Message
    history: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    feed_mid: int | None = None            # 對應的聊天室訊息 id(完成訊號比對用)
    completed_mid: int | None = None       # 完成回覆的訊息 id(可視化反向連結用)
    subscribers: set[asyncio.Queue] = field(default_factory=set)  # 串流訂閱者
    done: asyncio.Event = field(default_factory=asyncio.Event)    # terminal/interrupted 時 set
    deadline_handle: asyncio.Task | None = None                   # 逾時計時器,終態即取消

    def to_spec(self, history_length: int | None = None) -> dict:
        """輸出 spec 1.0 形狀的 Task JSON。先截 history 再深拷,省一次全量複製(#151-8)。"""
        history = self.history
        if history_length is not None:
            n = max(0, int(history_length))
            history = history[-n:] if n else []
        status: dict = {"state": self.state, "timestamp": self.state_ts}
        if self.state_message is not None:
            status["message"] = self.state_message
        return copy.deepcopy({
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": history,
            "artifacts": [],  # MVP:不支援 artifacts(A2A_MAPPING.md 邊界)
            "metadata": {**self.metadata, "target": self.target, "deadlineSeconds": self.deadline_seconds},
        })


class TaskRegistry:
    """Task 的唯一儲存與索引(Repository,#151-1)。

    三個索引(id、房間、feed 訊息)的同步只發生在這個類別內 —
    外界不再有機會漏更新其中一本。
    """

    def __init__(self):
        self._by_id: dict[str, Task] = {}
        self._by_room: dict[str, list[str]] = defaultdict(list)
        self._by_feed: dict[tuple[str, int], str] = {}  # (room, feed_mid) -> task id

    def add(self, task: Task) -> None:
        self._by_id[task.id] = task
        self._by_room[task.context_id].append(task.id)

    def bind_feed(self, task: Task, feed_mid: int) -> None:
        """task 訊息落地後綁定 feed id,建立 O(1) 反查(reply_to 完成判定的熱路徑)。"""
        task.feed_mid = feed_mid
        self._by_feed[(task.context_id, feed_mid)] = task.id

    def get(self, task_id: str) -> Task | None:
        return self._by_id.get(task_id)

    def by_feed(self, room: str, feed_mid: int) -> Task | None:
        tid = self._by_feed.get((room, feed_mid))
        return self._by_id.get(tid) if tid else None

    def in_room(self, room: str) -> list[Task]:
        return [self._by_id[t] for t in self._by_room.get(room, [])]

    def all_ids(self) -> list[str]:
        return list(self._by_id)


class A2ALayer:
    """A2A 狀態機 + JSON-RPC 方法。

    依賴以建構子注入(DIP,#151-3/#151-5):
    - ingest:訊息入流的唯一入口(server 提供,含鎖與廣播)
    - sanitize_sender:名字白名單(與可視化層同一套規則)
    """

    def __init__(self, ingest, sanitize_sender, base_url: str):
        self._ingest = ingest
        self._sanitize = sanitize_sender
        self.base_url = base_url.rstrip("/")
        self.registry = TaskRegistry()

    # ---------- Agent Card ----------

    def agent_card(self, name: str) -> dict:
        """spec required 欄位齊備的 Agent Card(共識 #112)。"""
        profile = AGENT_PROFILES[name]
        return {
            "name": name,
            "description": profile["description"],
            "supportedInterfaces": [{
                "url": f"{self.base_url}/agents/{name}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }],
            "version": "1.0.0",
            "capabilities": {"streaming": True, "pushNotifications": False, "extendedAgentCard": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": profile["skills"],
        }

    # ---------- 狀態機核心 ----------

    def _transition(self, task: Task, state: str, message: dict | None = None) -> bool:
        """唯一的狀態變更入口:轉換表驗證 → 更新 → 廣播 → 終態收尾。

        不合法的轉換(例如 deadline 在 COMPLETED 之後才觸發)靜默忽略並回 False —
        這正是轉換表取代散落 guard 的價值(#151-2)。
        """
        if (task.state, state) not in ALLOWED_TRANSITIONS:
            return False
        task.state = state
        task.state_ts = now_iso()
        if message is not None:
            task.state_message = message
            task.history.append(message)
        event = {"statusUpdate": {"taskId": task.id, "contextId": task.context_id,
                                  "status": {"state": state, "timestamp": task.state_ts},
                                  "timestamp": task.state_ts}}
        for q in list(task.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                task.subscribers.discard(q)  # 慢訂閱者直接放生,串流端自行收尾
        if state in TERMINAL or state in INTERRUPTED:
            task.done.set()
            if state in TERMINAL and task.deadline_handle:
                task.deadline_handle.cancel()  # 終態即取消計時器,免空轉(#151-8)
        return True

    async def _deadline_watch(self, task: Task) -> None:
        """逾時看門狗:到點仍未終態 → FAILED(共識 #112:漏回的 task 要有收場)。"""
        try:
            await asyncio.sleep(task.deadline_seconds)
        except asyncio.CancelledError:
            return  # 正常完成時被取消
        task.metadata["failureReason"] = f"deadline {task.deadline_seconds}s exceeded"
        self._transition(task, "TASK_STATE_FAILED")

    # ---------- 可視化層 hooks(由 server 在對應時機呼叫)----------

    def on_reader_fetch(self, room: str, reader: str, msgs: list[dict]) -> None:
        """已讀回條 = WORKING:目標 agent 首次撈到自己名下的 task 訊息。"""
        fetched_ids = {m["id"] for m in msgs}
        for task in self.registry.in_room(room):
            if (task.state == "TASK_STATE_SUBMITTED"
                    and task.target == reader and task.feed_mid in fetched_ids):
                self._transition(task, "TASK_STATE_WORKING")

    def on_room_message(self, room: str, msg: dict) -> None:
        """完成橋接:僅目標 agent 對 task 訊息本身的 reply_to 定終態(共識 #112)。

        經 feed 索引 O(1) 反查(#151-1),在每則訊息的熱路徑上不掃全表。
        """
        if not msg.get("reply_to"):
            return
        task = self.registry.by_feed(room, msg["reply_to"])
        if not task or task.target != msg["from"]:
            return  # 旁人引用閒聊,不動狀態
        if task.state not in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"):
            return  # 首回已定終態,後續回覆只是聊天
        reply = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_AGENT",
            "parts": [{"text": msg["text"]}],
            "contextId": room,
            "taskId": task.id,
            "metadata": {"senderName": msg["from"], "feedMessageId": msg["id"]},
        }
        task.completed_mid = msg["id"]
        self._transition(task, "TASK_STATE_COMPLETED", reply)

    def tasks_for_room(self, room: str) -> list[dict]:
        """給可視化層的輕量摘要(UI 徽章與計數用,不含 history)。"""
        return [{"id": t.id, "state": t.state, "feedMid": t.feed_mid,
                 "target": t.target, "completedMid": t.completed_mid}
                for t in self.registry.in_room(room)]

    # ---------- JSON-RPC 方法(spec 1.0 §9.4,PascalCase)----------

    async def dispatch(self, agent: str, method: str, params: dict):
        """方法分派。回傳 dict(一般結果)或 async generator(SSE 串流)。"""
        if agent not in AGENT_PROFILES:
            raise A2AError(ERR_UNSUPPORTED_OPERATION, f"unknown agent: {agent}")
        handlers = {
            "SendMessage": self._send_message,
            "SendStreamingMessage": self._send_streaming_message,
            "GetTask": self._get_task,
            "ListTasks": self._list_tasks,
            "CancelTask": self._cancel_task,
            "SubscribeToTask": self._subscribe_task,
            "GetExtendedAgentCard": self._extended_card,
        }
        if "PushNotification" in method or method.startswith("pushNotification"):
            raise A2AError(ERR_PUSH_NOT_SUPPORTED, "push notifications not supported")
        if method not in handlers:
            raise A2AError(-32601, f"method not found: {method}")
        return await handlers[method](agent, params or {})

    async def _create_task(self, agent: str, params: dict) -> Task:
        """SendMessage / SendStreamingMessage 共用的建立流程。"""
        message = params.get("message") or {}
        parts = message.get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text.strip():
            raise A2AError(ERR_CONTENT_TYPE, "only non-empty TextPart is supported")

        context = message.get("contextId") or uuid.uuid4().hex
        if message.get("taskId"):
            # spec MUST:taskId 與 contextId 不匹配就拒絕
            existing = self.registry.get(message["taskId"])
            if not existing or existing.context_id != context:
                raise A2AError(-32602, "taskId does not match contextId")

        meta_in = {**(message.get("metadata") or {}), **(params.get("metadata") or {})}
        sender = self._sanitize(str(meta_in.get("senderName", ""))) or "a2a-client"
        deadline = float(meta_in.get("deadlineSeconds", DEFAULT_DEADLINE_SECONDS))
        deadline = max(MIN_DEADLINE_SECONDS, min(MAX_DEADLINE_SECONDS, deadline))

        task = Task(id=uuid.uuid4().hex, context_id=context, target=agent, deadline_seconds=deadline)
        task.history.append({
            "messageId": message.get("messageId") or uuid.uuid4().hex,  # spec:messageId 由發送方生成
            "role": message.get("role") or "ROLE_USER",
            "parts": [{"text": text}],
            "contextId": context,
            "taskId": task.id,
            "metadata": {"senderName": sender},
        })
        self.registry.add(task)

        # 入流:帶 task_id、把目標 agent 注入 mentions 以觸發喚醒鏈
        feed_msg = await self._ingest(room=context, sender=sender, text=text,
                                      task_id=task.id, extra_mentions=[agent])
        self.registry.bind_feed(task, feed_msg["id"])
        task.deadline_handle = asyncio.get_running_loop().create_task(self._deadline_watch(task))
        return task

    async def _send_message(self, agent: str, params: dict) -> dict:
        task = await self._create_task(agent, params)
        config = params.get("configuration") or {}
        if not config.get("returnImmediately", False):
            # spec MUST:阻塞到 terminal 或 interrupted。deadline 保證有限時間內必有終態,
            # 所以這裡不需要額外逾時(#111 bob:逾時回半熟 Task 違反 MUST)。
            await task.done.wait()
        return task.to_spec(config.get("historyLength"))

    async def _send_streaming_message(self, agent: str, params: dict):
        task = await self._create_task(agent, params)
        return self._stream(task)

    async def _get_task(self, agent: str, params: dict) -> dict:
        task = self._require_task(params)
        return task.to_spec(params.get("historyLength"))

    async def _list_tasks(self, agent: str, params: dict) -> dict:
        context = params.get("contextId")
        tasks = self.registry.in_room(context) if context else \
            [self.registry.get(t) for t in self.registry.all_ids()]
        return {"tasks": [t.to_spec(params.get("historyLength")) for t in tasks]}

    async def _cancel_task(self, agent: str, params: dict) -> dict:
        task = self._require_task(params)
        if task.state in TERMINAL:
            raise A2AError(ERR_TASK_NOT_CANCELABLE, "task already in terminal state")
        self._transition(task, "TASK_STATE_CANCELED")
        return task.to_spec(None)

    async def _subscribe_task(self, agent: str, params: dict):
        return self._stream(self._require_task(params))

    async def _extended_card(self, agent: str, params: dict):
        raise A2AError(ERR_EXTENDED_CARD_NOT_CONFIGURED, "extended agent card not configured")

    def _require_task(self, params: dict) -> Task:
        task = self.registry.get(params.get("id") or "")
        if task is None:
            raise A2AError(ERR_TASK_NOT_FOUND, f"task not found: {params.get('id')}")
        return task

    async def _stream(self, task: Task):
        """SSE 產生器:先送 Task 快照,再逐一送 statusUpdate,終態即收。

        每行 data: 一個裸 StreamResponse(spec §9 範例格式,不包 JSON-RPC)。
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_QUEUE_MAXSIZE)
        task.subscribers.add(queue)
        try:
            yield f"data: {json.dumps({'task': task.to_spec(None)}, ensure_ascii=False)}\n\n"
            while task.state not in TERMINAL:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=STREAM_KEEPALIVE_SECONDS)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            task.subscribers.discard(queue)
