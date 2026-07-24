"""A2A Chatroom hub — composition root 與可視化層。

分層:
  底層協定  a2a.py(A2A Protocol 1.0,純狀態機,不碰 HTTP)
  可視化層  本檔 /api/*(聊天室 REST + SSE)+ static/(UI)
  儲存      MessageStore(chat.jsonl)— 兩層共用同一份訊息流

組裝:create_app() 是唯一的組裝點(composition root,review #151-3)—
store / bus / a2a_layer 都在這裡建構與注入,模組 import 不產生副作用,測試可注入替身。

啟動:`uv run server.py`(本機無系統 Python,一律走 uv)。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import a2a as a2a_mod

# ---------- 常數 ----------

BASE = Path(__file__).resolve().parent
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"  # 安全預設:只聽本機;要開放區網以 HOST=0.0.0.0 顯式 opt-in(共識 #216/#217)
SENDER_RE = re.compile(r"^[\w一-鿿-]{1,32}$")   # 名字白名單:擋空白與 @,防 parse 怪象
SSE_KEEPALIVE_SECONDS = 15
SSE_REPLAY_LIMIT = 10_000                        # 重連回放的上限(review #151-8:魔數常數化)
SUBSCRIBER_QUEUE_MAXSIZE = 256                   # 慢客戶端的 backpressure 界線
LONGPOLL_MAX_SECONDS = 50.0                      # /wait 上限:各層 infra 常在 60s 砍連線(共識 #185)


def now_iso() -> str:
    """UTC RFC3339(帶時區)— 與 a2a.now_iso 同格式,前端負責在地化顯示。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_sender(raw: str) -> str | None:
    """名字消毒:strip 後過白名單,不合法回 None(呼叫端決定 422 或 fallback)。"""
    name = (raw or "").strip()
    return name if SENDER_RE.match(name) else None


# ---------- 自訂例外(409/422 例外化,B 清單)----------

class ApiError(Exception):
    """業務錯誤基底:handler 統一轉成 JSONResponse,路由裡只管 raise。"""
    status = 400

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(str(payload))


class StaleCursorError(ApiError):
    """樂觀鎖失敗(有人搶先發言),payload 附 missed 讓 agent 一次補齊。"""
    status = 409


class BadReplyToError(ApiError):
    status = 422


class BadSenderError(ApiError):
    status = 422


# ---------- MentionParser ----------

class MentionParser:
    """@點名解析,規則演進史全在這裡:

    - lookbehind:@ 前貼著 ASCII(alice@main、email)不算點名;中文緊貼 @ 放行
    - 已知成員最長前綴優先(@bob呢 → bob)
    - 冷啟動規則(未知 ASCII 名保留)只在房間 <2 名成員時啟用,
      成熟房間只認已知名字 — 避免 @media 這類術語誤判(review 時代 #123)
    - JS_SOURCE 經 /api/config 下發,前後端同一套規則(單一事實來源)
    """

    JS_SOURCE = r"(?<![A-Za-z0-9_@.-])(@[\w一-鿿-]+)"
    TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_@.-])@([\w一-鿿-]+)")
    ASCII_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+")
    COLDSTART_THRESHOLD = 2

    @staticmethod
    def _is_sticky_prefix(name: str, token: str) -> bool:
        """token 是「名字後面緊黏著中文」(如 @bob呢)才算指到 name。"""
        if token == name:
            return True
        if not token.startswith(name):
            return False
        return not token[len(name)].isascii()

    @classmethod
    def parse(cls, known: set[str], text: str) -> list[str]:
        result = []
        allow_coldstart = len(known) < cls.COLDSTART_THRESHOLD
        for token in cls.TOKEN_RE.findall(text):
            matched = [n for n in known if cls._is_sticky_prefix(n, token)]
            if matched:
                result.append(max(matched, key=len))
                continue
            ascii_prefix = cls.ASCII_NAME_RE.match(token)
            if ascii_prefix and allow_coldstart:
                result.append(ascii_prefix.group(0))
        return list(dict.fromkeys(result))  # 去重保序


# ---------- MessageStore ----------

class MessageStore:
    """訊息的儲存與查詢(Repository)。

    - id 為房間內獨立遞增(別房流量不造成本房跳號)
    - chat.jsonl 逐行落地,重啟自動載回;壞行跳過記 warning,不讓一行毀掉啟動
    - _ids / _known 為增量索引(review #151-6):exists()/known() O(1),
      維護只發生在 _index() 一處
    """

    def __init__(self, path: Path):
        self.path = path
        self.rooms: dict[str, list[dict]] = {}
        self._ids: dict[str, set[int]] = {}
        self._known: dict[str, set[str]] = {}
        self._load()

    def _index(self, msg: dict) -> None:
        """load 與 append 共用的索引維護 — 三個結構同步只在這裡發生。"""
        room = msg["room"]
        self.rooms.setdefault(room, []).append(msg)
        self._ids.setdefault(room, set()).add(msg["id"])
        self._known.setdefault(room, set()).add(msg["from"])

    def _load(self) -> None:
        if not self.path.exists():
            return
        for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                self._index(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                print(f"[store] WARN skip bad line {lineno}: {exc}", file=sys.stderr)

    def last_id(self, room: str) -> int:
        msgs = self.rooms.get(room, [])
        return msgs[-1]["id"] if msgs else 0

    def count(self, room: str) -> int:
        """訊息數(bob 複審 #155:補齊封裝,路由不再直摸內部 dict)。"""
        return len(self.rooms.get(room, []))

    def known(self, room: str) -> set[str]:
        return self._known.get(room, set())

    def exists(self, room: str, mid: int) -> bool:
        return mid in self._ids.get(room, set())

    def query(self, room: str, since_id: int = 0, limit: int = 500,
              mentioned: str | None = None, before_id: int | None = None,
              tail: int | None = None) -> tuple[list[dict], int]:
        """三種查詢模式:agent 用 since_id(+mentioned 輕量預檢)向前撈;
        UI 用 tail=N 撈最近、before_id+tail 向上懶載。回傳 (訊息, 房間最新 id)。"""
        msgs = self.rooms.get(room, [])
        last = msgs[-1]["id"] if msgs else 0
        if before_id is not None:
            sel = [m for m in msgs if m["id"] < before_id][-(tail or 100):]
        elif tail is not None:
            sel = msgs[-tail:]
        else:
            sel = [m for m in msgs if m["id"] > since_id]
            if mentioned is not None:
                sel = [m for m in sel if mentioned in m.get("mentions", [])]
            sel = sel[:limit]
        return sel, last

    def append(self, room: str, sender: str, text: str, mentions: list[str],
               reply_to: int | None = None, task_id: str | None = None) -> dict:
        msg = {
            "id": self.last_id(room) + 1,
            "room": room,
            "from": sender,
            "text": text,
            "mentions": mentions,
            "ts": now_iso(),
        }
        if reply_to is not None:
            msg["reply_to"] = reply_to
        if task_id is not None:
            msg["task_id"] = task_id
        self._index(msg)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return msg

    def rooms_index(self) -> list[dict]:
        return [{"name": r, "count": len(msgs), "last_id": msgs[-1]["id"] if msgs else 0}
                for r, msgs in sorted(self.rooms.items())]

    def members(self, room: str) -> list[dict]:
        """成員統計,全從歷史推導、零新狀態。lastSeen 只在「發言」時更新,
        僅被點名過的名字 lastSeen 為 None。低頻查詢,O(n) 可接受。"""
        stats: dict[str, dict] = {}

        def entry(name: str, ts: str) -> dict:
            return stats.setdefault(name, {"name": name, "messageCount": 0,
                                           "mentionedCount": 0, "firstSeen": ts, "lastSeen": None})

        for m in self.rooms.get(room, []):
            s = entry(m["from"], m["ts"])
            s["messageCount"] += 1
            s["lastSeen"] = m["ts"]
            for name in m.get("mentions", []):
                entry(name, m["ts"])["mentionedCount"] += 1
        return list(stats.values())


# ---------- EventBus ----------

@dataclass(eq=False)  # eq=False 保留預設身分 hash — 訂閱者要放進 set,且本來就該以身分區分
class Subscription:
    """一個 SSE 訂閱者(review #151-4:取代對 asyncio.Queue 的 monkey-patch)。"""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE))
    dead: bool = False   # backpressure:queue 滿了標記,產生器見狀自行收尾


class EventBus:
    """房間層 SSE 廣播(Observer)。慢客戶端斷線後靠 Last-Event-ID 重連補齊。"""

    def __init__(self):
        self.subs: dict[str, set[Subscription]] = {}

    def subscribe(self, room: str) -> Subscription:
        sub = Subscription()
        self.subs.setdefault(room, set()).add(sub)
        return sub

    def unsubscribe(self, room: str, sub: Subscription) -> None:
        self.subs.get(room, set()).discard(sub)

    def publish(self, room: str, msg: dict) -> None:
        for sub in list(self.subs.get(room, set())):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                sub.dead = True
                self.subs[room].discard(sub)


# ---------- 請求模型 ----------
# 注意:必須定義在模組層級 — 本檔用了 `from __future__ import annotations`(PEP 563),
# FastAPI 以字串解析端點註解,函式內的區域類別會解析不到(重構時踩過的雷)。

class PostMessage(BaseModel):
    sender: str = Field(alias="from", min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=8000)
    expect_last_id: int | None = None   # 樂觀鎖:發言時聲明「我以為的最新 id」
    reply_to: int | None = None         # 引用;若指向 task 訊息且我是目標 → 完成該 task
    model_config = {"populate_by_name": True}


# ---------- Composition Root ----------

def create_app(port: int | None = None, host: str | None = None,
               public_url: str | None = None) -> FastAPI:
    """組裝一切:store / bus / a2a_layer 在這裡建構、耦合點在這裡宣告。

    ingest 是「訊息入流」的唯一入口(REST 發言與 A2A SendMessage 共用):
    鎖 → 樂觀鎖驗證 → reply_to 驗證 → 解析 mentions → 落地 → 廣播 → 通知 A2A 層。
    A2A 完成橋接以 callback 顯式注入 — 一行誠實的呼叫,宣告在組裝處(#151-5 折衷)。

    遠端化(roadmap ①):HOST 控制綁定位址(預設只聽本機);PUBLIC_URL 決定
    Agent Card 對外宣告的位址 — 開放綁定卻沒設它時,遠端 client 會拿到
    對它無效的 127.0.0.1,故啟動時印警告(bob #217)。
    """
    port = port or int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = host or os.environ.get("HOST", DEFAULT_HOST)
    public_url = (public_url or os.environ.get("PUBLIC_URL", "")).rstrip("/")
    base_url = public_url or f"http://127.0.0.1:{port}"
    if host not in ("127.0.0.1", "localhost") and not public_url:
        print(f"[hub] WARN HOST={host}(對外開放)但未設 PUBLIC_URL — "
              f"遠端 client 取得的 Agent Card url 會指向對它無效的 {base_url};"
              f"建議啟動時設 PUBLIC_URL=http://<你的區網IP>:{port}", file=sys.stderr)
    store = MessageStore(BASE / "chat.jsonl")
    bus = EventBus()
    post_lock = asyncio.Lock()

    async def ingest(room: str, sender: str, text: str, reply_to: int | None = None,
                     task_id: str | None = None, extra_mentions: list[str] | None = None,
                     expect_last_id: int | None = None) -> dict:
        async with post_lock:
            current = store.last_id(room)
            if expect_last_id is not None and expect_last_id != current:
                missed, _ = store.query(room, since_id=expect_last_id)
                raise StaleCursorError({"error": "stale", "last_id": current, "missed": missed})
            if reply_to is not None and not store.exists(room, reply_to):
                raise BadReplyToError({"error": "bad_reply_to",
                                       "detail": f"訊息 #{reply_to} 不存在於 {room}"})
            mentions = MentionParser.parse(store.known(room), text)
            for extra in extra_mentions or []:
                if extra not in mentions:
                    mentions.append(extra)  # task 訊息強制點名目標 agent,確保喚醒
            msg = store.append(room, sender, text, mentions, reply_to=reply_to, task_id=task_id)
        bus.publish(room, msg)
        a2a_layer.on_room_message(room, msg)  # 完成橋接(耦合點,見 docstring)
        return msg

    a2a_layer = a2a_mod.A2ALayer(ingest=ingest, sanitize_sender=sanitize_sender,
                                 base_url=base_url)

    app = FastAPI(title="A2A Chatroom Hub")
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.payload)

    # ---------- 可視化層 REST(協定見 AGENT_GUIDE.md)----------

    @app.get("/")
    async def index():
        return FileResponse(BASE / "static" / "index.html")

    @app.get("/api/config")
    async def get_config():
        """前端開機設定:mention 規則與 agent 色相的單一事實來源(#151-7)。"""
        return {
            "mentionPattern": MentionParser.JS_SOURCE,
            "a2aVersion": a2a_mod.A2A_PROTOCOL_VERSION,
            "agents": {name: {"color": p["color"]} for name, p in a2a_mod.AGENT_PROFILES.items()},
        }

    @app.get("/api/rooms")
    async def rooms_index():
        return {"rooms": store.rooms_index()}

    @app.get("/api/rooms/{room}/members")
    async def get_members(room: str):
        return {"members": store.members(room)}

    @app.get("/api/rooms/{room}/state")
    async def get_state(room: str):
        """給 poller 的極輕量狀態:只有 cursor,不含訊息內容。"""
        return {"room": room, "last_id": store.last_id(room), "count": store.count(room)}

    @app.get("/api/rooms/{room}/messages")
    async def get_messages(room: str, since_id: int = 0, limit: int = 500,
                           mentioned: str | None = None, before_id: int | None = None,
                           tail: int | None = None, reader: str | None = None):
        sel, last = store.query(room, since_id, limit, mentioned, before_id, tail)
        if reader:
            name = sanitize_sender(reader)
            if name:
                a2a_layer.on_reader_fetch(room, name, sel)  # 已讀回條 = WORKING
        return {"messages": sel, "last_id": last}

    @app.post("/api/rooms/{room}/messages", status_code=201)
    async def post_message(room: str, body: PostMessage):
        sender = sanitize_sender(body.sender)
        if sender is None:
            raise BadSenderError({"error": "bad_sender",
                                  "detail": "名字限 1-32 字的中英數與 - _,不含空白與 @"})
        msg = await ingest(room, sender, body.text, reply_to=body.reply_to,
                           expect_last_id=body.expect_last_id)
        return {"id": msg["id"]}

    @app.get("/api/rooms/{room}/tasks")
    async def get_room_tasks(room: str):
        return {"tasks": a2a_layer.tasks_for_room(room)}

    @app.get("/api/rooms/{room}/wait")
    async def wait_for_change(room: str, since_id: int = 0, timeout: float = LONGPOLL_MAX_SECONDS):
        """平台中立喚醒(共識 #186):long-poll 阻塞到「房間最新 id > since_id」或逾時。

        任何會 curl 的 agent 一行即可等待,不依賴 Monitor / poller / 門鈴檔。
        回傳刻意極簡 — 「返回不是資訊來源,cursor 對帳才是」(bob #185):
        呼叫方無論拿到 changed=true/false 還是網路錯誤,一律回 GET since_id=cursor 對帳再重掛。
        """
        timeout = max(1.0, min(LONGPOLL_MAX_SECONDS, timeout))
        sub = bus.subscribe(room)  # 先訂閱再檢查,堵住「檢查與訂閱之間來訊」的縫
        try:
            if store.last_id(room) > since_id:
                return {"changed": True, "last_id": store.last_id(room)}
            try:
                await asyncio.wait_for(sub.queue.get(), timeout=timeout)
                return {"changed": True, "last_id": store.last_id(room)}
            except asyncio.TimeoutError:
                return {"changed": False, "last_id": store.last_id(room)}
        finally:
            bus.unsubscribe(room, sub)

    @app.get("/api/rooms/{room}/stream")
    async def stream(room: str, request: Request, since_id: int = 0):
        """觀戰 SSE。送 id: 欄位,瀏覽器斷線重連自帶 Last-Event-ID 無縫補齊。"""
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            since_id = int(last_event_id)
        sub = bus.subscribe(room)

        def sse(msg: dict) -> str:
            return f"id: {msg['id']}\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"

        async def gen():
            try:
                yield "retry: 2000\n\n"
                replay, _ = store.query(room, since_id=since_id, limit=SSE_REPLAY_LIMIT)
                for m in replay:  # 先訂閱再回放,交界重複由 client 以 id 去重
                    yield sse(m)
                while True:
                    try:
                        m = await asyncio.wait_for(sub.queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
                        yield sse(m)
                    except asyncio.TimeoutError:
                        if sub.dead:
                            break  # backpressure 斷線,client 重連補齊
                        yield ": keep-alive\n\n"
            finally:
                bus.unsubscribe(room, sub)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------- A2A Protocol 層(底層協定,對映見 A2A_MAPPING.md)----------

    @app.get("/agents")
    async def agents_index():
        return {"agents": [{"name": n, "card": f"/agents/{n}/.well-known/agent-card.json"}
                           for n in a2a_mod.AGENT_PROFILES]}

    @app.get("/agents/{name}/.well-known/agent-card.json")
    @app.get("/agents/{name}/.well-known/a2a-agent-card")  # 常見路徑別名
    async def agent_card(name: str):
        if name not in a2a_mod.AGENT_PROFILES:
            return JSONResponse(status_code=404, content={"error": "unknown agent"})
        return a2a_layer.agent_card(name)

    @app.post("/agents/{name}/a2a")
    async def a2a_rpc(name: str, request: Request):
        """JSON-RPC 2.0 端點。串流方法回 SSE,其餘回標準 JSON-RPC 信封。"""
        try:
            body = await request.json()
        except Exception:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        rid = body.get("id")
        method = body.get("method")
        if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "invalid request"}}
        try:
            result = await a2a_layer.dispatch(name, method, body.get("params") or {})
        except a2a_mod.A2AError as exc:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": exc.code, "message": exc.message}}
        if inspect.isasyncgen(result):  # SendStreamingMessage / SubscribeToTask
            return StreamingResponse(result, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    return app


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    _host = os.environ.get("HOST", DEFAULT_HOST)
    uvicorn.run(create_app(_port, _host), host=_host, port=_port)
