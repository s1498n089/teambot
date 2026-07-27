"""A2A Protocol 1.0 層(JSON-RPC 2.0 binding)— 專案的底層協定。

架構定位:房間 = contextId;聊天室 /api/* 與 UI 是本層之上的可視化。
本模組不做 HTTP,只有純狀態機與方法邏輯 — 路由掛載在 server.create_app()(composition root)。

Task 生命週期:
  feed 入流                        = TASK_STATE_SUBMITTED
  目標 agent 首次帶 reader= 撈到    = TASK_STATE_WORKING(已讀回條的協定化)
  目標 agent 對 task 訊息 reply_to  = TASK_STATE_COMPLETED(旁人引用不動狀態;首回定終態)
  deadline 逾時                    = TASK_STATE_FAILED

設計要點:Task dataclass 合體 spec 欄位與 hub 管理欄位、
TaskRegistry 統一三索引(Repository)、宣告式狀態轉換表(OCP)、deadline timer 終態即取消。
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

# ---------- 協定常數 ----------

A2A_PROTOCOL_VERSION = "1.0"
DEFAULT_DEADLINE_SECONDS = 300.0   # 發起方未指定 metadata.deadlineSeconds 時的預設
MIN_DEADLINE_SECONDS = 5.0
MAX_DEADLINE_SECONDS = 3600.0
STREAM_QUEUE_MAXSIZE = 64          # 單一串流訂閱者的事件緩衝;滿了就丟棄該訂閱者
STREAM_KEEPALIVE_SECONDS = 15

TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
INTERRUPTED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}

# 宣告式狀態轉換表:合法轉換的唯一事實來源。
# backlog 的 REJECTED / INPUT_REQUIRED 上線時,只需在這裡加表項。
# 注意:目前沒有任何通往 INTERRUPTED 的表項,_transition 內的 INTERRUPTED 分支
# 是「刻意預留」不是 dead code— 等 v2 讓 agent 能標記
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

# 不該被任何成員拿去用的名字:人類的預設名、以及基礎設施的代稱。
#
# ★ 這裡是這條規則的【權威定義】,但目前的【執行點只剩前端的取名框】——
#   後端的動態註冊在 2026-07-27 退役,不再有「入冊時檢查名字」這個動作。
#   前端 static/app.js 有一份手動對齊的副本(它少一個 user,理由寫在那邊)。
#   哪天雲端階段把註冊機制撿回來,執行點就會回到這裡。
#
# 留著而不刪的理由:前端那份的註解指名「對齊伺服器的 a2a.py:RESERVED_NAMES」——
# 刪掉它,那句話就變成指向不存在東西的失效引用。
RESERVED_NAMES = {"user", "admin", "system", "hub", "server", "poller"}

# 註:這裡曾經有 SEMANTIC_GREEN(#00ff88)。它是「這個顏色系統獨占,成員不准用」
# 的視覺鐵律,而視覺鐵律的家不該在協定層 —— 已移居 static/util.js 的配色函式旁邊。

# 註:2026-07-27 之前這裡有一份寫死的 SEED_PROFILES(alice/bob/dev),
# 以及一個把註冊者存進 agents.json 的 AgentRegistry。兩者都已退役 ——
# 今天「誰是可以派任務的 agent」由【誰現在連著線】決定,不由任何檔案決定。
# 理由:寫死的名冊會留下永遠不上線的幽靈成員(dev 佔了三個月的位置),
# 而派任務給幽靈只會得到逾時失敗。考古請看 git 歷史。

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
    """一個 A2A Task:spec 欄位 + hub 內部管理欄位合體。

    spec 形狀一律經 to_spec() 輸出,內部欄位(target/feed_mid/訂閱者等)不外洩。
    持久化只序列化 _PERSIST_FIELDS;訂閱者/事件/計時器是 runtime 欄位,
    重啟後由 restore() 重建。
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
    created_ts: str = field(default_factory=now_iso)  # UTC ISO8601(家法);deadline 復原計算用
    subscribers: set[asyncio.Queue] = field(default_factory=set)  # 串流訂閱者
    done: asyncio.Event = field(default_factory=asyncio.Event)    # terminal/interrupted 時 set
    deadline_handle: asyncio.Task | None = None                   # 逾時計時器,終態即取消

    _PERSIST_FIELDS = ("id", "context_id", "target", "deadline_seconds", "state", "state_ts",
                       "state_message", "history", "metadata", "feed_mid", "completed_mid",
                       "created_ts")

    def to_snapshot(self) -> dict:
        """持久化形狀(runtime 欄位除外)。"""
        return {f: getattr(self, f) for f in self._PERSIST_FIELDS}

    @classmethod
    def from_snapshot(cls, data: dict) -> "Task":
        """從快照重建;runtime 欄位(訂閱者/事件/計時器)以全新狀態起始。"""
        return cls(**{f: data[f] for f in cls._PERSIST_FIELDS})

    def to_spec(self, history_length: int | None = None) -> dict:
        """輸出 spec 1.0 形狀的 Task JSON。先截 history 再深拷,省一次全量複製。"""
        history = self.history
        if history_length is not None:
            n = max(0, int(history_length))
            if n:
                history = history[-n:]      # 只留最後 n 筆
            else:
                history = []                # 要 0 筆就是不要
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
    """Task 的唯一儲存與索引(Repository)+ 持久化到 tasks.json。

    三個索引(id、房間、feed 訊息)的同步只發生在這個類別內。
    快照落地的唯二時機(「不可能忘記存」與殭屍防範):
      1. bind_feed 完成後(= task 建立完成的定義;create 中途永不落盤,殭屍無從誕生)
      2. 狀態轉換後(由 A2ALayer._transition 呼叫 checkpoint)
    """

    def __init__(self, path=None):
        self.path = path  # None = 不持久化(隔離測試用)
        self._by_id: dict[str, Task] = {}
        self._by_room: dict[str, list[str]] = defaultdict(list)
        self._by_feed: dict[tuple[str, int], str] = {}  # (room, feed_mid) -> task id
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[tasks] WARN tasks.json 載入失敗,以空名冊啟動:{exc}", file=sys.stderr)
            return
        for item in data:
            try:
                task = Task.from_snapshot(item)
            except (KeyError, TypeError) as exc:
                print(f"[tasks] WARN 跳過壞快照:{exc}", file=sys.stderr)
                continue
            self._by_id[task.id] = task
            self._by_room[task.context_id].append(task.id)
            if task.feed_mid is not None:
                self._by_feed[(task.context_id, task.feed_mid)] = task.id

    def checkpoint(self) -> None:
        """全量快照原子落地。失敗只記 warning 繼續跑 —
        可用性優先於持久性(最壞退回蒸發行為,不炸訊息流)。"""
        if not self.path:
            return
        try:
            snap = [t.to_snapshot() for t in self._by_id.values()]
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, str(self.path))
        except OSError as exc:
            print(f"[tasks] WARN 快照落地失敗(繼續運行):{exc}", file=sys.stderr)

    def add(self, task: Task) -> None:
        self._by_id[task.id] = task
        self._by_room[task.context_id].append(task.id)
        # 注意:這裡刻意不 checkpoint — create 中途落盤會養出殭屍

    def bind_feed(self, task: Task, feed_mid: int) -> None:
        """task 訊息落地後綁定 feed id,建立 O(1) 反查(reply_to 完成判定的熱路徑)。
        這一刻 task 才算「建立完成」,也是第一次落盤的時機。"""
        task.feed_mid = feed_mid
        self._by_feed[(task.context_id, feed_mid)] = task.id
        self.checkpoint()

    def all_tasks(self) -> list[Task]:
        return list(self._by_id.values())

    def get(self, task_id: str) -> Task | None:
        return self._by_id.get(task_id)

    def by_feed(self, room: str, feed_mid: int) -> Task | None:
        tid = self._by_feed.get((room, feed_mid))
        if not tid:
            return None
        return self._by_id.get(tid)

    def in_room(self, room: str) -> list[Task]:
        return [self._by_id[t] for t in self._by_room.get(room, [])]

    def all_ids(self) -> list[str]:
        return list(self._by_id)


class A2ALayer:
    """A2A 狀態機 + JSON-RPC 方法。

    依賴以建構子注入(DIP):
    - ingest:訊息入流的唯一入口(server 提供,含鎖與廣播)
    - sanitize_sender:名字白名單(與可視化層同一套規則)
    """

    def __init__(self, ingest, sanitize_sender, base_url: str,
                 live_agents_fn: Callable[[], set[str]],
                 auth_enabled: bool = False, tasks_path=None):
        self._ingest = ingest
        self._sanitize = sanitize_sender
        self.base_url = base_url.rstrip("/")
        # 「現在有哪些 agent 連著線」——★ 傳進來的是一個【函式】,不是一份名單。
        #
        # 名單是靜態的,函式每次呼叫都給出當下的答案,而 agent 隨時上下線。
        # 這個參數曾經叫 agents、型別註解也曾寫著 AgentRegistry(一個 2026-07-27
        # 已刪除的類別)—— 名字與型別都在說一件與實際相反的事:它從來不是名冊,
        # 現在更不是。改名改型是為了讓讀的人不必先受一次誤導再自己糾正。
        self.live_agents = live_agents_fn
        self.auth_enabled = auth_enabled  # 影響 Agent Card 的 securitySchemes 誠實聲明(③)
        self.registry = TaskRegistry(tasks_path)  # 任務狀態落地,伺服器重開不失憶
        self._handlers = {  # 方法分派表建一次即可,dispatch 熱路徑不重建
            "SendMessage": self._send_message,
            "SendStreamingMessage": self._send_streaming_message,
            "GetTask": self._get_task,
            "ListTasks": self._list_tasks,
            "CancelTask": self._cancel_task,
            "SubscribeToTask": self._subscribe_task,
            "GetExtendedAgentCard": self._extended_card,
        }

    # ---------- Agent Card ----------

    def agent_card(self, name: str) -> dict:
        """spec required 欄位齊備的 Agent Card。

        AUTH 啟用時同步宣告 securitySchemes(誠實聲明做全套,
        標準 A2A client 讀 Card 就知道要帶 bearer)。
        """
        # Agent Card 的內容不再來自寫死的檔案 —— 我們對一個剛連上線的 agent
        # 本來就只知道它的名字。與其編造專長,不如誠實地留白:
        # 真正該宣告能力的是 agent 自己,不是我們替它填。
        card = {
            "name": name,
            "description": f"透過敲鈴器連線的 agent(名字:{name})",
            "supportedInterfaces": [{
                "url": f"{self.base_url}/agents/{name}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }],
            "version": "1.0.0",
            "capabilities": {"streaming": True, "pushNotifications": False, "extendedAgentCard": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            # 空的 skills 是誠實的:agent 沒有向我們宣告過它會什麼。
            # A2A spec 允許空陣列;假造一份專長清單才是真正的違規。
            "skills": [],
        }
        if self.auth_enabled:
            card["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
            card["security"] = [{"bearer": []}]
        return card

    # ---------- 狀態機核心 ----------

    def _transition(self, task: Task, state: str, message: dict | None = None) -> bool:
        """唯一的狀態變更入口:轉換表驗證 → 更新 → 廣播 → 終態收尾。

        不合法的轉換(例如 deadline 在 COMPLETED 之後才觸發)靜默忽略並回 False —
        這正是轉換表取代散落 guard 的價值。
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
                task.deadline_handle.cancel()  # 終態即取消計時器,免空轉
        self.registry.checkpoint()  # 持久化唯二咽喉之二(轉換即落盤,不可能忘)
        return True

    async def _deadline_watch(self, task: Task, seconds: float | None = None) -> None:
        """逾時看門狗:到點仍未終態 → FAILED(漏回的 task 要有收場)。

        seconds 允許覆寫 — 重啟復原時傳「剩餘時間」,deadline 不重新起算
        (否則 deadline 形同橡皮筋)。
        """
        try:
            wait_seconds = seconds
            if wait_seconds is None:
                wait_seconds = task.deadline_seconds
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            return  # 正常完成時被取消
        task.metadata["failureReason"] = f"deadline {task.deadline_seconds}s exceeded"
        self._transition(task, "TASK_STATE_FAILED")

    def restore(self, message_exists) -> None:
        """啟動復原(需在 running event loop 內呼叫):

        1. 殭屍判定:非終態且 feed_mid 為 None 或訊息流查無 → FAILED
        2. 停機期間已逾期 → FAILED(記入 downtime 原因)
        3. 其餘非終態:以「剩餘時間」重掛 deadline 計時器
        """
        for task in self.registry.all_tasks():
            if task.state in TERMINAL:
                continue
            if task.feed_mid is None or not message_exists(task.context_id, task.feed_mid):
                task.metadata["failureReason"] = "orphaned on restore(建立中途遺留的殭屍)"
                self._transition(task, "TASK_STATE_FAILED")
                continue
            elapsed = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(task.created_ts)).total_seconds()
            remaining = task.deadline_seconds - elapsed
            if remaining <= 0:
                task.metadata["failureReason"] = "deadline expired during downtime"
                self._transition(task, "TASK_STATE_FAILED")
            else:
                task.deadline_handle = asyncio.get_running_loop().create_task(
                    self._deadline_watch(task, remaining))

    # ---------- 可視化層 hooks(由 server 在對應時機呼叫)----------

    def on_reader_fetch(self, room: str, reader: str, msgs: list[dict]) -> None:
        """已讀回條 = WORKING:目標 agent 首次撈到自己名下的 task 訊息。"""
        fetched_ids = {m["id"] for m in msgs}
        for task in self.registry.in_room(room):
            if (task.state == "TASK_STATE_SUBMITTED"
                    and task.target == reader and task.feed_mid in fetched_ids):
                self._transition(task, "TASK_STATE_WORKING")

    def on_room_message(self, room: str, msg: dict) -> None:
        """完成橋接:僅目標 agent 對 task 訊息本身的 reply_to 定終態。

        經 feed 索引 O(1) 反查,在每則訊息的熱路徑上不掃全表。
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
        if agent not in self.live_agents():
            # ★ 訊息要說真話:它現在判的是「在不在線」,不是「認不認識」。
            #   寫成 unknown agent 會讓對方去檢查有沒有打錯名字 —— 而真正的問題是
            #   那個 agent 沒開著。錯誤碼不變(對 client 的處理方式沒差),但文字要誠實。
            raise A2AError(ERR_UNSUPPORTED_OPERATION,
                           f"agent not online: {agent}(名冊只收現在連著線的 agent)")
        handlers = self._handlers
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
            # 所以這裡不需要額外逾時(逾時回半熟 Task 違反 spec 的 MUST)。
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

        if context:
            tasks = self.registry.in_room(context)   # 指定房間 → 只要那一間的
        else:
            tasks = self.registry.all_tasks()        # 沒指定 → 全部

        history_length = params.get("historyLength")
        return {"tasks": [t.to_spec(history_length) for t in tasks]}

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
