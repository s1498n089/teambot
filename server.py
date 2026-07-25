"""A2A Chatroom hub — composition root 與可視化層。

分層:
  底層協定  a2a.py(A2A Protocol 1.0,純狀態機,不碰 HTTP)
  可視化層  本檔 /api/*(聊天室 REST + SSE)+ static/(UI)
  儲存      MessageStore(chat.jsonl)— 兩層共用同一份訊息流

組裝:create_app() 是唯一的組裝點(composition root)—
store / bus / a2a_layer 都在這裡建構與注入,模組 import 不產生副作用,測試可注入替身。

啟動:`uv run server.py`(本機無系統 Python,一律走 uv)。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from contextlib import asynccontextmanager
import inspect
import json
import os
import re
import secrets
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import a2a as a2a_mod

# ---------- 常數 ----------

BASE = Path(__file__).resolve().parent
DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"  # 預設開放區網(手機觀戰);要只聽本機可設 HOST=127.0.0.1
SENDER_RE = re.compile(r"^[\w一-鿿-]{1,32}$")   # 名字白名單:擋空白與 @,防 parse 怪象
SSE_KEEPALIVE_SECONDS = 15
SSE_REPLAY_LIMIT = 10_000                        # 重連回放的上限
SUBSCRIBER_QUEUE_MAXSIZE = 256                   # 慢客戶端的 backpressure 界線


now_iso = a2a_mod.now_iso  # 時戳格式的單一定義在 a2a.py(UTC RFC3339 帶時區),此處僅取別名


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


class ConflictError(ApiError):
    """資源衝突(如註冊重名)。"""
    status = 409


class UnauthorizedError(ApiError):
    """未帶或無效的 token(roadmap ③)。"""
    status = 401


class ForbiddenError(ApiError):
    """token 有效但身分不符(冒名,roadmap ③)。"""
    status = 403


class RateLimitError(ApiError):
    """寫入頻率超限(roadmap ③),payload 附 retryAfter 秒數。"""
    status = 429


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
      成熟房間只認已知名字 — 避免 @media 這類術語誤判
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
    - _ids / _known 為增量索引:exists()/known() O(1),
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
        """訊息數(補齊封裝,路由不再直摸內部 dict)。"""
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


# ---------- TokenStore 與 RateLimiter(roadmap ③)----------

class TokenStore:
    """per-agent bearer token。明文只在生成當下出現一次,落地只存 sha256。

    - 比對用 hmac.compare_digest(防時序側信道)
    - tokens.json 原子落地(temp+rename),gitignore
    - 「user」也是持鑰者 — 人類不在名冊,但不能被鎖在門外
    """

    def __init__(self, path: Path):
        self.path = path
        self.hashes: dict[str, str] = {}
        if path.exists():
            try:
                self.hashes = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                print(f"[auth] WARN tokens.json 載入失敗:{exc}", file=sys.stderr)

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _persist(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(self.hashes, f, indent=2)
        os.replace(tmp, str(self.path))

    def issue(self, name: str) -> str:
        """生成(或重生)某人的 token,回傳明文 — 呼叫端負責讓使用者看到這唯一一次。"""
        token = secrets.token_urlsafe(24)
        self.hashes[name] = self._digest(token)
        self._persist()
        return token

    def ensure(self, names: list[str]) -> dict[str, str]:
        """為還沒有 token 的名字補發;回傳 {名字: 新明文} 供啟動時列印。"""
        fresh = {}
        for name in names:
            if name not in self.hashes:
                fresh[name] = self.issue(name)
        return fresh

    def verify(self, name: str, token: str) -> bool:
        stored = self.hashes.get(name)
        return bool(stored) and hmac.compare_digest(self._digest(token), stored)

    def owner_of(self, token: str) -> str | None:
        digest = self._digest(token)
        for name, stored in self.hashes.items():
            if hmac.compare_digest(digest, stored):
                return name
        return None


class RateLimiter:
    """寫入限流:每個名字 10 秒滑動窗最多 10 則(與 409 重試流程相容)。"""

    WINDOW_SECONDS = 10.0
    LIMIT = 10

    def __init__(self):
        self.hits: dict[str, deque] = {}

    def check(self, name: str) -> None:
        """超限時 raise RateLimitError(附 retryAfter);未超限記錄本次。"""
        now = time.monotonic()
        window = self.hits.setdefault(name, deque())
        while window and now - window[0] > self.WINDOW_SECONDS:
            window.popleft()
        if len(window) >= self.LIMIT:
            retry_after = round(self.WINDOW_SECONDS - (now - window[0]), 1)
            raise RateLimitError({"error": "rate_limited",
                                  "detail": f"{self.WINDOW_SECONDS:.0f} 秒內最多 {self.LIMIT} 則寫入",
                                  "retryAfter": max(retry_after, 0.1)})
        window.append(now)


# ---------- EventBus ----------

@dataclass(eq=False)  # eq=False 保留預設身分 hash — 訂閱者要放進 set,且本來就該以身分區分
class Subscription:
    """一個 SSE 訂閱者(取代對 asyncio.Queue 的 monkey-patch)。

    watcher = 訂閱者自報的身分(agent 的 bell、瀏覽器的使用者);None 為匿名。
    這是 presence 的第一手事實:連線開著 = 這個人在場,不必拿「最近有沒有發言」去猜。
    """
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAXSIZE))
    dead: bool = False   # backpressure:queue 滿了標記,產生器見狀自行收尾
    watcher: str | None = None


class EventBus:
    """房間層 SSE 廣播(Observer)。慢客戶端斷線後靠 Last-Event-ID 重連補齊。"""

    def __init__(self):
        self.subs: dict[str, set[Subscription]] = {}

    def subscribe(self, room: str, watcher: str | None = None) -> Subscription:
        sub = Subscription(watcher=watcher)
        self.subs.setdefault(room, set()).add(sub)
        return sub

    def watchers(self, room: str) -> set[str]:
        """當前在場者(具名且連線未死)— presence 的唯一事實來源。"""
        return {s.watcher for s in self.subs.get(room, set()) if s.watcher and not s.dead}

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


class RegisterAgent(BaseModel):
    """roadmap ② 動態註冊:新 agent 憑邀請 token 自報。欄位上限防灌書。"""
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=300)
    skills: list = Field(default_factory=list)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    inviteToken: str = Field(min_length=1, max_length=128)


# ---------- Composition Root ----------

def create_app(port: int | None = None, host: str | None = None,
               public_url: str | None = None) -> FastAPI:
    """組裝一切:store / bus / a2a_layer 在這裡建構、耦合點在這裡宣告。

    ingest 是「訊息入流」的唯一入口(REST 發言與 A2A SendMessage 共用):
    鎖 → 樂觀鎖驗證 → reply_to 驗證 → 解析 mentions → 落地 → 廣播 → 通知 A2A 層。
    A2A 完成橋接以 callback 顯式注入 — 一行誠實的呼叫,宣告在組裝處。

    遠端化(roadmap ①):HOST 控制綁定位址(預設只聽本機);PUBLIC_URL 決定
    Agent Card 對外宣告的位址 — 開放綁定卻沒設它時,遠端 client 會拿到
    對它無效的 127.0.0.1,故啟動時印警告。
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

    agents = a2a_mod.AgentRegistry(BASE / "agents.json")  # 名冊:seed + 註冊者(roadmap ②)
    invite_token = os.environ.get("INVITE_TOKEN", "")     # 未設 = 註冊關閉(安全預設)

    # ── 認證與限流(roadmap ③)──
    auth_enabled = os.environ.get("AUTH", "").lower() in ("on", "1", "true")
    token_store = TokenStore(BASE / "tokens.json")
    rate_limiter = RateLimiter()
    if auth_enabled:
        # 名冊每人 + user(人類)確保持鑰;新發的印 console 讓使用者分發
        fresh = token_store.ensure(agents.names() + ["user"])
        for name, token in fresh.items():
            print(f"[auth] {name} 的 token(僅此一次,請抄下分發):{token}", file=sys.stderr)
        rotate = os.environ.get("ROTATE_TOKEN", "")
        if rotate:  # 丟鑰匙換鎖:ROTATE_TOKEN=<名字> 重生該人 token
            print(f"[auth] {rotate} 的新 token(舊的已失效):{token_store.issue(rotate)}",
                  file=sys.stderr)

    def check_writer(name: str, request: Request) -> None:
        """AUTH=on 時的寫入守門:Bearer 必須存在、有效、且與聲稱身分綁定。"""
        if not auth_enabled:
            return
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise UnauthorizedError({"error": "no_token",
                                     "detail": "AUTH 已啟用,寫入需 Authorization: Bearer <token>"})
        token = header[7:].strip()
        if token_store.verify(name, token):
            return
        owner = token_store.owner_of(token)
        if owner:  # 鑰匙是真的,但開的不是自己的門 = 冒名
            raise ForbiddenError({"error": "wrong_identity",
                                  "detail": f"這把 token 屬於「{owner}」,不能以「{name}」發言"})
        raise UnauthorizedError({"error": "bad_token", "detail": "無效的 token"})

    # task 快照路徑:非預設 PORT 的實例自動用獨立檔 — 全量快照是「最後寫者贏」,
    # 多實例共用同一檔會互洗 task 狀態(實測過的營運風險);TASKS_PATH 可覆寫
    tasks_path = Path(os.environ.get("TASKS_PATH") or
                      BASE / ("tasks.json" if port == DEFAULT_PORT else f"tasks-{port}.json"))
    a2a_layer = a2a_mod.A2ALayer(ingest=ingest, sanitize_sender=sanitize_sender,
                                 base_url=base_url, agents=agents, auth_enabled=auth_enabled,
                                 tasks_path=tasks_path)  # roadmap ④:task 持久化

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 復原 in-flight task(roadmap ④):殭屍判定/停機逾期/剩餘時間重掛,
        # 必須在 event loop 起來後執行,故掛在 lifespan 而非 create_app 本體
        a2a_layer.restore(store.exists)
        yield

    app = FastAPI(title="A2A Chatroom Hub", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.payload)

    # ---------- 可視化層 REST(協定見 AGENT_GUIDE.md)----------

    @app.get("/")
    async def index():
        # 這一頁不准快取。原因:index.html 決定要載入哪些 js 檔,瀏覽器若拿到
        # 舊版的它,就會少載新加的檔案 —— 畫面會整個壞掉,而且使用者按重整
        # 也救不回來(要按 Ctrl+F5 才行)。每次只有一份小小的 HTML,不值得為
        # 它冒這個險;js 與 css 本身仍可照常被快取。
        return FileResponse(
            BASE / "static" / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/api/config")
    async def get_config():
        """前端開機設定:mention 規則與 agent 色相的單一事實來源。
        名冊動態化後,新註冊者的色相在「重新整理後」生效(已知時效行為)。"""
        return {
            "mentionPattern": MentionParser.JS_SOURCE,
            "a2aVersion": a2a_mod.A2A_PROTOCOL_VERSION,
            "authEnabled": auth_enabled,  # UI 據此顯示/隱藏 token 欄
            "agents": {name: {"color": p.get("color", "#c9d1d9")}
                       for name, p in agents.profiles.items()},
        }

    @app.get("/api/rooms")
    async def rooms_index():
        return {"rooms": store.rooms_index()}

    @app.get("/api/rooms/{room}/presence")
    async def get_presence(room: str):
        """在場名單:誰的 SSE 連線正開著(bell 或瀏覽器)。"""
        return {"room": room, "present": sorted(bus.watchers(room))}

    @app.get("/api/rooms/{room}/members")
    async def get_members(room: str):
        return {"members": store.members(room)}

    @app.get("/api/rooms/{room}/state")
    async def get_state(room: str):
        """給 poller 的極輕量狀態:只有 cursor,不含訊息內容。"""
        return {"room": room, "last_id": store.last_id(room), "count": store.count(room)}

    @app.get("/api/rooms/{room}/messages")
    async def get_messages(room: str, request: Request, since_id: int = 0, limit: int = 500,
                           mentioned: str | None = None, before_id: int | None = None,
                           tail: int | None = None, reader: str | None = None):
        sel, last = store.query(room, since_id, limit, mentioned, before_id, tail)
        if reader:
            name = sanitize_sender(reader)
            # reader= 是隱藏的寫入(觸發 task 轉 WORKING)— AUTH=on 時必須驗身分,
            # 驗不過就優雅降級:GET 本體照常回,只是不觸發已讀
            authorized = True
            if auth_enabled and name:
                try:
                    check_writer(name, request)
                except ApiError:
                    authorized = False
            if name and authorized:
                a2a_layer.on_reader_fetch(room, name, sel)  # 已讀回條 = WORKING
        return {"messages": sel, "last_id": last}

    @app.post("/api/rooms/{room}/messages", status_code=201)
    async def post_message(room: str, body: PostMessage, request: Request):
        sender = sanitize_sender(body.sender)
        if sender is None:
            raise BadSenderError({"error": "bad_sender",
                                  "detail": "名字限 1-32 字的中英數與 - _,不含空白與 @"})
        check_writer(sender, request)   # 認證(AUTH=on 時)
        rate_limiter.check(sender)      # 限流(永遠啟用)
        msg = await ingest(room, sender, body.text, reply_to=body.reply_to,
                           expect_last_id=body.expect_last_id)
        return {"id": msg["id"]}

    @app.get("/api/rooms/{room}/tasks")
    async def get_room_tasks(room: str):
        return {"tasks": a2a_layer.tasks_for_room(room)}

    # /wait long-poll 端點已移除(喚醒改走 bell 敲鈴器,watch 機制留作備援,
    # curl 等待路線退場)— 需要考古的話看 git 歷史 🚀 fa0c64b 前後。

    @app.get("/api/rooms/{room}/stream")
    async def stream(room: str, request: Request, since_id: int = 0,
                     watcher: str | None = None):
        """觀戰 SSE。送 id: 欄位,瀏覽器斷線重連自帶 Last-Event-ID 無縫補齊。

        watcher=<名字> 讓訂閱者自報身分以計入 presence;AUTH=on 時驗不過就
        降級為匿名(串流照給,只是不計入在場名單)— 與 reader= 同一套模式,
        否則任何人都能假裝別人在線。
        """
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            since_id = int(last_event_id)
        name = sanitize_sender(watcher) if watcher else None
        if name and auth_enabled:
            try:
                check_writer(name, request)
            except ApiError:
                name = None  # 冒名者只當匿名觀眾
        sub = bus.subscribe(room, watcher=name)

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
                           for n in agents.names()]}

    @app.post("/agents", status_code=201)
    async def register_agent(body: RegisterAgent):
        """roadmap ② 動態註冊:憑邀請 token 入冊,回 Agent Card。

        驗證順序:註冊開關 → token → 名字消毒 → 保留名單 → 語意綠黑名單 →
        (持鎖)不分大小寫重名 → 入冊原子落地。
        """
        if not invite_token:
            return JSONResponse(status_code=403, content={
                "error": "registration_closed",
                "detail": "hub 未設定 INVITE_TOKEN,註冊功能關閉"})
        if body.inviteToken != invite_token:
            return JSONResponse(status_code=403, content={"error": "bad_invite_token"})
        name = sanitize_sender(body.name)
        if name is None:
            raise BadSenderError({"error": "bad_name",
                                  "detail": "名字限 1-32 字的中英數與 - _,不含空白與 @"})
        if name.lower() in a2a_mod.RESERVED_NAMES:
            raise BadSenderError({"error": "reserved_name",
                                  "detail": f"「{name}」是保留名(人類/基礎設施專用)"})
        if body.color.lower() == a2a_mod.SEMANTIC_GREEN:
            raise BadSenderError({"error": "semantic_color",
                                  "detail": "語意綠 #00ff88 為系統獨占(點名/連線/NEW),sender 禁用"})
        if len(json.dumps(body.skills, ensure_ascii=False)) > 2000 or len(body.skills) > 10:
            raise BadSenderError({"error": "skills_too_large", "detail": "skills 最多 10 項、總長 2000 字"})
        async with post_lock:  # 併發決勝:同名同時註冊只有一人成功
            if agents.taken_ci(name):
                raise ConflictError({"error": "name_taken",
                                     "detail": f"名字「{name}」已被使用(不分大小寫)"})
            agents.register(name, {"color": body.color.lower(),
                                   "description": body.description,
                                   "skills": body.skills})
        # AUTH=on 時隨註冊發一次性 token(明文僅此一次;勿貼進聊天室)
        token = token_store.issue(name) if auth_enabled else None
        return {"agentCard": a2a_layer.agent_card(name), "token": token}

    @app.get("/agents/{name}/.well-known/agent-card.json")
    @app.get("/agents/{name}/.well-known/a2a-agent-card")  # 常見路徑別名
    async def agent_card(name: str):
        if agents.get(name) is None:
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
        params = body.get("params") or {}
        if method in ("SendMessage", "SendStreamingMessage"):
            # 寫入方法:AUTH=on 時驗 senderName 的 Bearer 綁定 + 限流(spec 將認證放在 HTTP 層)
            meta = {**((params.get("message") or {}).get("metadata") or {}),
                    **(params.get("metadata") or {})}
            sender = sanitize_sender(str(meta.get("senderName", ""))) or "a2a-client"
            try:
                check_writer(sender, request)
                rate_limiter.check(sender)
            except ApiError as exc:
                return JSONResponse(status_code=exc.status, content=exc.payload)
        try:
            result = await a2a_layer.dispatch(name, method, params)
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
