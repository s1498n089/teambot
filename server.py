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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import a2a as a2a_mod

# ---------- 主控台編碼 ----------
#
# Windows 主控台預設不是 UTF-8,而這支程式印中文警告(還有 SystemExit 的錯誤訊息)。
# 不處理的話,最需要被讀懂的那幾行會變成亂碼 —— 而它們正好都是「你設定寫錯了」那種訊息。
#
# hasattr 那道保護是給 pytest 的:測試會把 stdout 換成自己的擷取物件,
# 那個物件不一定有 reconfigure。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# ---------- 常數 ----------

BASE = Path(__file__).resolve().parent

# 伺服器的所有資料檔都放這裡:訊息、任務、成員名冊、認證鑰匙。
#
# 為什麼要有這個目錄:這些檔案原本散在專案根目錄,跟程式碼混在一起 ——
# 看起來亂,而且清理或備份時容易誤傷。
#
# 為什麼叫 hub_data 而不是 data:「data」太通用,遲早撞名;
# 而 hub 是這個專案從 README 到教學一直在用的詞,一看就知道是伺服器的東西。
#
# ★ 它跟 state/ 的分工要記住:
#       hub_data/  伺服器的資料(這裡)
#       state/     客戶端的狀態(每個 agent 的進度書籤、敲鈴器紀錄)
#   遠端機器只跑客戶端,所以它只會有 state/,不該有 hub_data/ ——
#   跟 server.env / client.env 是同一條分界線。
# ★ 這裡只放「目錄名字」,不是算好的完整路徑 —— 理由很實際:
#   測試靠 monkeypatch 換掉 BASE 來隔離,但那換不掉一個【在 import 時就算完】的常數。
#   如果這裡寫成 DATA_DIR = BASE / "hub_data",測試會跑去動真實專案目錄的資料。
#   所以完整路徑一律等到 Hub 建立時才算(見 Hub.__init__)。
DATA_DIR_NAME = "hub_data"

DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"  # 預設開放區網(手機觀戰);要只聽本機可設 HOST=127.0.0.1
SENDER_RE = re.compile(r"^[\w一-鿿-]{1,32}$")   # 名字白名單:擋空白與 @,防 parse 怪象

SSE_KEEPALIVE_SECONDS = 15
SUBSCRIBER_QUEUE_MAXSIZE = 256                   # 慢客戶端的 backpressure 界線


now_iso = a2a_mod.now_iso  # 時戳格式的單一定義在 a2a.py(UTC RFC3339 帶時區),此處僅取別名


def sanitize_sender(raw: str) -> str | None:
    """名字消毒:strip 後過白名單,不合法回 None(呼叫端決定 422 或 fallback)。"""
    name = (raw or "").strip()
    if SENDER_RE.match(name):
        return name
    return None


def clean_public_host(raw: str) -> str:
    """PUBLIC_HOST 只吃主機名或 IP —— 帶了 scheme 或 port 就當場擋下。

    為什麼是擋下、而不是默默剝掉:
        設定寫錯的後果是【遠端拿到一張打不通的名片】,而那個症狀會在
        很遠的地方才浮出來 —— 對方派的 task 逾時變 FAILED,沒有人會聯想到
        是 hub 這一行設定。開機就炸、訊息直接寫出正確寫法,比事後追那條線便宜太多。
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):    # IPv6 字面值,例如 [::1]
        return value
    if "://" in value or "/" in value or ":" in value:
        raise SystemExit(
            f"[hub] PUBLIC_HOST 只放主機名或 IP,不要帶 http:// 或 port —— 現在是 {value!r}。\n"
            f"      port 請用 PORT 那一行設,scheme 固定 http。\n"
            f"      正確寫法:PUBLIC_HOST=10.199.20.151")
    return value


# ---------- 自訂例外 ----------
#
# 每個都自帶「該回什麼 HTTP 狀態碼」與「該說什麼」,路由裡只管 raise,
# 統一由 create_app 的 handle_api_error 轉成回應 —— 錯誤格式因此只有一種寫法。

class ApiError(Exception):
    """業務錯誤基底:handler 統一轉成 JSONResponse,路由裡只管 raise。"""
    status = 400

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(str(payload))


class StaleCursorError(ApiError):
    """樂觀鎖失敗(有人搶先發言)。

    payload 只說「房間現在到哪」,【不夾帶訊息】—— 訊息只有一條取得路徑
    (撈訊息那個端點)。順便夾一份等於開第二條路,而兩條路要各自維護正確性。
    """
    status = 409


class UnauthorizedError(ApiError):
    """未帶或無效的 token(AUTH=on 時)。"""
    status = 401


class ForbiddenError(ApiError):
    """token 有效但身分不符 —— 拿別人的鑰匙開自己的門。"""
    status = 403


class RateLimitError(ApiError):
    """寫入頻率超限,payload 附 retryAfter 秒數。"""
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
    - `@all` 是廣播名字:所有人都算被點到(見 BROADCAST)
    """

    BROADCAST = "all"
    """`@all` = 點名所有人。

    ★ 實作是「把它當成一個永遠存在的成員」,而不是另寫一條規則 ——
      於是既有的規則全部免費沿用:

          必須帶 @        `all` 三個字單獨出現只是普通句子(TOKEN_RE 要求 @)
          中文可以緊貼    `@all呢` 認得出來
          不會誤判        `@allen` 不算 all(黏著規則:下一個字元是 ASCII 就不算)

      另寫一套判斷 `@all` 的邏輯也能動,但那一套遲早跟本尊分岔。
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
        known = known | {cls.BROADCAST}   # @all 當成永遠在場的成員(見 BROADCAST)
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
        """load 與 append 共用的索引維護 — 三個結構同步只在這裡發生。

        ★ 這裡【不修補任何格式】。檔案裡是什麼樣子,記憶體就是什麼樣子。

          若在這裡補格式(例如「舊訊息沒有 kind 就當場補上」),結果會是
          記憶體有、檔案沒有 —— 而那個分岔不會寫在任何地方,
          下一個直接讀檔案的人(備份腳本、資料分析)會拿到不一樣的東西。

          正確的處理位置是【資料本身】:要補就補進 chat.jsonl,
          讓讀取端不必知道有過舊格式。
        """
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
        """這個房間最新一則訊息的編號。空房間回 0。"""
        msgs = self.rooms.get(room, [])
        if not msgs:
            return 0
        return msgs[-1]["id"]

    def count(self, room: str) -> int:
        """訊息數(補齊封裝,路由不再直摸內部 dict)。"""
        return len(self.rooms.get(room, []))

    def known(self, room: str) -> set[str]:
        return self._known.get(room, set())

    def exists(self, room: str, mid: int) -> bool:
        return mid in self._ids.get(room, set())

    def query(self, room: str, since_id: int = 0,
              mentioned: str | None = None, before_id: int | None = None,
              tail: int | None = None) -> tuple[list[dict], int]:
        """三種查詢模式。回傳 (訊息, 房間最新 id)。

        ★ 這裡【沒有筆數上限】,要多少給多少。

          上限原本是為了兩種一次要撈幾百則的情況:**初次加入**、**太久沒回來**。
          那兩種現在由 AGENTS.md 的加入流程處理掉了 —— 讀最近 50 則 + 掃一遍點名,
          中間那段刻意跳過。**沒有人會再要求撈一大段,上限也就沒有存在的理由。**

          ★ 為什麼不留一個「以防萬一」的上限:因為切一半的回應跟完整的回應
            【長得一模一樣】,照文件把回傳的 id 寫進 cursor 就會靜默漏讀。
            要防它就得多回一個 id 讓呼叫端自己比對 —— 那是一整套呼叫端要記得做的事。
            **不切,那整套就都不需要。**

        三種模式互斥,而且【判斷順序就是下面的順序】—— before_id 最優先。
        每一種的實際網址長這樣(都是真的有人在打的,不是舉例):

        ┌─ 模式一:往上捲載入更舊的 ──────────────────────────────────────
        │  誰在用   觀戰 UI,使用者把訊息列往上拉到頂的時候
        │  網址     GET /api/rooms/main/messages?before_id=700&tail=50
        │  意思     「比 #700 更舊的,給我最近的 50 則」
        │  來源     static/app.js 的 loadOlder()
        │
        │  ★ 為什麼是「最近的 N 則」而不是「最舊的 N 則」:
        │    使用者要的是接在畫面頂端上面那一段,也就是【緊鄰 #700 之前】的內容。
        │    給他最舊的那幾則會跳到三個月前,中間整段是空的。
        │
        ├─ 模式二:開頁 ─────────────────────────────────────────────────
        │  誰在用   觀戰 UI,剛打開網頁
        │  網址     GET /api/rooms/main/messages?tail=50
        │  意思     「最新的 50 則」
        │  來源     static/app.js 的 loadFirstPage()(則數是那邊的 PAGE 常數)
        │
        ├─ 模式三:agent 對帳 ───────────────────────────────────────────
        │  誰在用   agent 被鈴聲叫醒之後
        │  網址     GET /api/rooms/main/messages?since_id=731&reader=alice
        │  意思     「#731 之後的全部給我」(reader= 順便回報已讀,見路由層)
        │  來源     AGENTS.md 教的 curl
        │
        │  變體     ?since_id=0&mentioned=alice
        │           只要「有點名 alice」的那些 —— 加入時掃一遍整段歷史,
        │           確認跳過舊訊息不會漏掉找他的人(見 AGENTS.md 加入流程)。
        └─────────────────────────────────────────────────────────────────

        ★ 為什麼 UI 用 tail、agent 用 since_id:兩者要的東西不一樣。
          UI 要的是「畫面上該顯示什麼」——它不在乎錯過了什麼,捲上去就補得到。
          agent 要的是「我不在的時候發生了什麼」——它【一則都不能漏】,
          所以必須用自己的游標當起點,而不是用「最近幾則」。
        """
        msgs = self.rooms.get(room, [])
        last = self.last_id(room)

        # 模式一:UI 往上捲,要「某則之前」的那一批
        if before_id is not None:
            older = [m for m in msgs if m["id"] < before_id]
            page_size = tail if tail is not None else 100
            return older[-page_size:], last   # 取最後 N 則 = 離 before_id 最近的 N 則

        # 模式二:UI 開頁,要最新的 N 則
        if tail is not None:
            return msgs[-tail:], last

        # 模式三:agent 對帳,要「我的游標之後」的所有訊息(有多少給多少)
        sel = [m for m in msgs if m["id"] > since_id]
        if mentioned is not None:
            sel = [m for m in sel if mentioned in m.get("mentions", [])]
        return sel, last

    def append(self, room: str, sender: str, text: str, mentions: list[str],
               reply_to: int | None = None, task_id: str | None = None,
               kind: str = "human") -> dict:
        """把一則訊息寫進記事本。

        ★ kind 是「說這句話的是 AI 還是人類」,由發送方自己聲明,寫下去就不再改變。

          為什麼要寫進訊息裡,而不是查「這個名字是不是 agent」?
          因為那是兩種壽命不同的事實:

              「這句話是誰說的」  → 歷史,永遠不變
              「他現在在不在線」  → 當下,隨時在變

          如果徽章去查後者,alice 一斷線,她三個月前的訊息就會從 AI 變成人類 ——
          歷史跟著網路連線閃爍。記在訊息裡,它就跟著那句話一起被凍結。
        """
        # 正規化收在這裡 —— 這是訊息落地的唯一入口,在這裡擋住就不會有第二種寫法進檔案。
        # (刻意不在每一層都防一次:重複的防禦會讓後人以為某一層可以省略。)
        msg = {
            "id": self.last_id(room) + 1,
            "room": room,
            "from": sender,
            "kind": "agent" if kind == "agent" else "human",
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
        """所有房間的一覽:名字、訊息數、最新編號(給 UI 的房間下拉選單用)。"""
        index = []
        for name, msgs in sorted(self.rooms.items()):
            index.append({"name": name, "count": len(msgs), "last_id": self.last_id(name)})
        return index

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


# ---------- TokenStore 與 RateLimiter(認證與限流)----------

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
    # 這條連線背後是不是 AI?由連線方自己宣告(敲鈴器會說,瀏覽器不會)。
    # 它決定「現在能不能派任務給這個名字」,不決定「這個名字是不是 AI」——
    # 後者寫在每則訊息裡,見 MessageStore.append 的 kind。
    is_agent: bool = False


class EventBus:
    """房間層 SSE 廣播(Observer)。慢客戶端斷線後靠 Last-Event-ID 重連補齊。"""

    def __init__(self):
        self.subs: dict[str, set[Subscription]] = {}

    def subscribe(self, room: str, watcher: str | None = None,
                  is_agent: bool = False) -> Subscription:
        """掛一條新的直播連線。

        is_agent 由連線方自己宣告(敲鈴器會說「我包的是 agent」,瀏覽器不會說)。
        ★ 這是「現在誰能接任務」的唯一來源 —— 沒有名冊檔案、沒有註冊手續,
          連著線就算在,線一斷就不算。
        """
        sub = Subscription(watcher=watcher, is_agent=is_agent)
        self.subs.setdefault(room, set()).add(sub)
        return sub

    def watchers(self, room: str) -> set[str]:
        """當前在場者(具名且連線未死)— presence 的唯一事實來源。"""
        return {s.watcher for s in self.subs.get(room, set()) if s.watcher and not s.dead}

    def all_live_agents(self) -> set[str]:
        """任何房間裡連著線的 agent。

        ★ 為什麼需要「不分房間」的版本:agent 的身分不屬於某個房間。
          A2A 的 Agent Card 是它的身分證,而任務可以在任何房間(contextId)派給它 ——
          用單一房間的名單去判斷「這個 agent 存不存在」,會讓一個待在別房的 agent
          看起來像不存在。房間層的 live_agents 只該用在「這個房間的選單要列誰」。
        """
        live = set()
        for room in self.subs:
            live |= self.live_agents(room)
        return live

    def live_agents(self, room: str) -> set[str]:
        """現在連著線、而且自稱是 agent 的那些名字。

        ★ 它回答的是【可用性】:現在能不能派任務給這個名字。
          它【不】回答「這個名字是不是 AI」—— 那是身分問題,答案寫在每則訊息的 kind 欄位裡,
          不會因為誰斷線而改變。兩個問題分開,是因為它們的答案有不同的壽命。
        """
        live = set()
        for sub in self.subs.get(room, set()):
            if sub.watcher and sub.is_agent and not sub.dead:
                live.add(sub.watcher)
        return live

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
    # 說這句話的是誰:agent 或 human。由發送方自己聲明,沒說就當人類。
    # ★ 為什麼不由伺服器判斷「這個名字有沒有 agent 連線」?
    #   因為發言走這條路、連線走另一條路 —— 敲鈴器斷線重連的那兩秒裡,
    #   agent 發的話會被誤判成人類,而訊息只增不改,錯了就永遠錯了。
    #   讓聲明跟著訊息一起來,就沒有這個時間差。
    kind: str = "human"
    model_config = {"populate_by_name": True}



# ═══════════════════════════════════════════════════════════════════════════
#  Hub —— 資料與規則
# ═══════════════════════════════════════════════════════════════════════════

class Hub:
    """聊天室的中樞:所有元件與共用規則都放在這裡。

    為什麼要有這個類別:
        這些東西原本全塞在 create_app 裡面,那個函式長到三百多行,
        中間還巢狀了十九個路由函式 —— 想看懂任何一個路由,得先穿過那三百行。

        現在切成三層,各做一件事:

            Hub          管資料與規則(這個類別)
            register_*   管「哪個網址對應到哪個動作」
            create_app   只負責把上面兩者組裝起來

        好處是:想改規則就看 Hub,想加網址就看 register_*,兩邊互不干擾。
    """

    def __init__(self, port: int | None = None, host: str | None = None,
                 public_host: str | None = None):
        self.port = port or int(os.environ.get("PORT", str(DEFAULT_PORT)))
        self.host = host or os.environ.get("HOST", DEFAULT_HOST)
        self.public_host = clean_public_host(public_host or os.environ.get("PUBLIC_HOST", ""))
        # 對外網址只有【主機】那一段要設定,scheme 與 port 由這裡接上 ——
        # port 只有 PORT 一個來源,不會出現「PORT 改了但網址還寫舊的」這種對不起來的設定。
        #
        # 誠實標一個取捨:這樣就固定是 http、固定用 PORT,
        # **反向代理後面的 https://a2a.example.com(沒有 port)表達不出來**。
        # 現階段是區網直連,設定直覺比那個彈性值錢;哪天真要上反向代理,
        # 這行就是要改的地方(那時候該回到「整條網址」的形式)。
        self.base_url = f"http://{self.public_host or '127.0.0.1'}:{self.port}"
        self.warn_if_exposed_without_public_host()

        # 資料目錄在這裡才算完整路徑(不是模組層級的常數)——
        # 這樣測試 monkeypatch 掉 BASE 之後,資料自然落在它的隔離目錄裡。
        self.data_dir = BASE / DATA_DIR_NAME
        self.data_dir.mkdir(exist_ok=True)

        # ── 訊息與廣播 ──
        self.store = MessageStore(self.data_dir / "chat.jsonl")
        self.bus = EventBus()
        # 這把鎖讓「寫進聊天室」這件事一次只有一個人在做。
        # 沒有它的話,兩個人同時發言會讓樂觀鎖失效 —— 兩邊都以為自己是最新的。
        self.post_lock = asyncio.Lock()

        # ── 認證、限流 ──
        # 名冊是動態的(誰連著線就是誰),所以這裡沒有註冊機制、也沒有邀請碼。
        # 雲端階段若要控管誰能進房,那是另一道題(認證那道)。
        self.auth_enabled = os.environ.get("AUTH", "").lower() in ("on", "1", "true")
        self.token_store = TokenStore(self.data_dir / "tokens.json")
        self.rate_limiter = RateLimiter()
        if self.auth_enabled:
            self.issue_startup_tokens()

        # ── A2A 協定層 ──
        # 注意這裡傳的是 self.ingest,一個已經綁在這個 Hub 上的方法。
        # 協定層收到訊息時會呼叫它,而它裡面又會回頭呼叫協定層 —— 兩邊互相需要。
        # 放在同一個類別裡,這種互相需要就自然解決了。
        self.a2a_layer = a2a_mod.A2ALayer(
            ingest=self.ingest,
            sanitize_sender=sanitize_sender,
            base_url=self.base_url,
            # 不分房間:agent 的身分不屬於某個房間,任務也可以在任何房間派給它。
            #
            # 誠實標一個取捨:一個只掛在 A 房的 agent,理論上可以被派 B 房的任務,
            # 而它根本收不到那個房間的訊息 —— 結果會是逾時失敗。
            # 我們選擇不擋,因為擋的話要把「目標房間」一路傳進協定層,
            # 而這個情境至今沒發生過(通常只有一個房間),逾時機制也已經兜住後果。
            # 哪天真的多房間常態運作,這裡就是要改的第一個地方。
            live_agents_fn=self.bus.all_live_agents,
            auth_enabled=self.auth_enabled,
            tasks_path=self.pick_tasks_path(),
        )

    # ---------- 開機時的準備工作 ----------

    def warn_if_exposed_without_public_host(self) -> None:
        """對外開放卻沒設對外主機時,印一行警告。

        為什麼要警告:遠端的 client 會來問「你的名片在哪」,
        我們若回答 127.0.0.1,對它來說指的是【它自己那台機器】,永遠連不到我們。
        """
        if self.host in ("127.0.0.1", "localhost"):
            return
        if self.public_host:
            return
        print(f"[hub] WARN HOST={self.host}(對外開放)但未設 PUBLIC_HOST — "
              f"遠端 client 取得的 Agent Card url 會指向對它無效的 {self.base_url};"
              f"建議啟動時設 PUBLIC_HOST=<你的區網IP>(port 會自動接上 {self.port})",
              file=sys.stderr)

    def issue_startup_tokens(self) -> None:
        """開了認證時,確保名冊上每個人(加上人類 user)都有一把鑰匙。

        新發的鑰匙印在畫面上,而且【只印這一次】—— 檔案裡存的是指紋不是明文,
        所以沒抄到就只能重發。設 ROTATE_TOKEN=<名字> 可以幫某人重發、舊的作廢。
        """
        # ★ 只替人類(user)準備鑰匙。agent 的名冊現在是動態的 ——
        #   開機這一刻還沒有任何 agent 連上線,無從預發。
        #   要給某個 agent 鑰匙,用 ROTATE_TOKEN=<名字> 重啟一次即可。
        fresh = self.token_store.ensure(["user"])
        for name, token in fresh.items():
            print(f"[auth] {name} 的 token(僅此一次,請抄下分發):{token}", file=sys.stderr)
        rotate = os.environ.get("ROTATE_TOKEN", "")
        if rotate:
            new_token = self.token_store.issue(rotate)
            print(f"[auth] {rotate} 的新 token(舊的已失效):{new_token}", file=sys.stderr)

    def pick_tasks_path(self) -> Path:
        """決定任務狀態要存到哪個檔案。

        為什麼不固定一個檔名:任務檔是【整包蓋回去】的寫法,
        兩個 hub 共用同一個檔案會互相把對方的狀態洗掉(實際踩過)。
        所以用非預設埠號跑的實例,自動改用自己的檔名。TASKS_PATH 可以手動指定。
        """
        custom = os.environ.get("TASKS_PATH")
        if custom:
            return Path(custom)
        if self.port == DEFAULT_PORT:
            return self.data_dir / "tasks.json"
        return self.data_dir / f"tasks-{self.port}.json"

    async def restore_tasks(self) -> None:
        """伺服器重開時,把還沒做完的任務接回來。

        這件事必須等 event loop 起來才能做(裡面要重新掛計時器),
        所以由 lifespan 呼叫,不能寫在 __init__ 裡。
        """
        self.a2a_layer.restore(self.store.exists)

    # ---------- 共用規則 ----------

    async def ingest(self, room: str, sender: str, text: str, reply_to: int | None = None,
                     task_id: str | None = None, extra_mentions: list[str] | None = None,
                     expect_last_id: int | None = None, kind: str = "human") -> dict:
        """所有訊息進入聊天室的唯一入口。

        網頁發言走這裡,A2A 派任務也走這裡 —— 只有一個入口,規則才不會有兩套。

        流程固定六步:上鎖 → 樂觀鎖檢查 → 引用檢查 → 解析點名 → 寫檔 → 廣播
        """
        async with self.post_lock:
            current = self.store.last_id(room)

            # 樂觀鎖:發言者聲明「我以為現在最新是第 N 則」。
            # 對不上代表有人搶先發言了 —— 擋下來,並把他錯過的內容一起回給他。
            if expect_last_id is not None and expect_last_id != current:
                # 只告訴他房間到哪,不夾帶訊息 —— 他重新對帳一次就拿得到,
                # 而那條路本來就是取得訊息的唯一路徑。
                raise StaleCursorError({"error": "stale", "last_id": current})

            # 引用檢查:不能引用一則不存在的訊息。
            if reply_to is not None and not self.store.exists(room, reply_to):
                raise BadReplyToError({"error": "bad_reply_to",
                                       "detail": f"訊息 #{reply_to} 不存在於 {room}"})

            mentions = MentionParser.parse(self.store.known(room), text)
            # 派任務時要強制點名目標,否則對方不會被叫醒。
            for extra in extra_mentions or []:
                if extra not in mentions:
                    mentions.append(extra)

            msg = self.store.append(room, sender, text, mentions,
                                    reply_to=reply_to, task_id=task_id, kind=kind)

        # 鎖放掉之後才做這兩件事 —— 它們不碰檔案,不需要排隊。
        self.bus.publish(room, msg)                 # 通知所有正在看的人
        self.a2a_layer.on_room_message(room, msg)   # 通知協定層(任務狀態可能要變)
        return msg

    def check_writer(self, name: str, request: Request) -> None:
        """寫入前的身分檢查。沒開認證就直接放行。

        三種失敗分開講,因為它們的意思完全不同:
            沒帶鑰匙     → 你需要先拿一把
            鑰匙是別人的 → 你在冒名(這種最該講清楚)
            鑰匙不存在   → 這把是假的
        """
        if not self.auth_enabled:
            return

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise UnauthorizedError({"error": "no_token",
                                     "detail": "AUTH 已啟用,寫入需 Authorization: Bearer <token>"})

        token = header[7:].strip()
        if self.token_store.verify(name, token):
            return

        owner = self.token_store.owner_of(token)
        if owner:
            raise ForbiddenError({"error": "wrong_identity",
                                  "detail": f"這把 token 屬於「{owner}」,不能以「{name}」發言"})
        raise UnauthorizedError({"error": "bad_token", "detail": "無效的 token"})

    def identify_optional_reader(self, name: str | None, request: Request) -> str | None:
        """認一下「順便報上名字」的人是誰,認不出來就當匿名。

        用在兩個地方:讀訊息時的 reader=、看直播時的 watcher=。
        這兩個都不是正式的寫入動作,所以驗不過【不擋人】,只是不算數 ——
        照樣讓你讀、讓你看,只是不觸發已讀、不列進在場名單。

        為什麼還是要驗:不驗的話,任何人都能假裝別人已讀、假裝別人在線上。
        """
        if not name:
            return None
        clean = sanitize_sender(name)
        if clean is None:
            return None
        if not self.auth_enabled:
            return clean
        try:
            self.check_writer(clean, request)
        except ApiError:
            return None      # 冒名者當匿名處理
        return clean


# ═══════════════════════════════════════════════════════════════════════════
#  路由註冊 —— 哪個網址對應到哪個動作
# ═══════════════════════════════════════════════════════════════════════════
#
# 每一組獨立一個函式。想找某個端點,直接看對應的那一組就好,不必翻整個檔案。

STATIC_REF_RE = re.compile(r'"/static/([^"?]+\.(?:js|css))"')


def stamp_static_urls(html: str) -> str:
    """把 index.html 裡的 js / css 網址接上「這個檔案的修改時間」。

    為什麼要這樣做:瀏覽器會把 js 與 css 快取起來,所以改了樣式或程式碼之後,
    使用者按重整**仍然看到舊的**,要按 Ctrl+F5 才會更新 —— 這種「明明改好了
    對方卻看不到」的狀況我們已經踩過兩次。

    接上修改時間之後,檔案一改網址就變(/static/app.js?v=1753...),
    瀏覽器認得那是新網址,自然會重新下載;檔案沒改時網址不變,快取照樣有效。
    兩全其美,而且不需要任何打包工具。
    """
    def add_version(match: re.Match) -> str:
        filename = match.group(1)
        file_path = BASE / "static" / filename
        version = 0
        if file_path.exists():
            version = int(file_path.stat().st_mtime)
        return f'"/static/{filename}?v={version}"'

    return STATIC_REF_RE.sub(add_version, html)


def register_page_routes(app: FastAPI, hub: Hub) -> None:
    """網頁本體與前端開機設定。"""

    @app.get("/")
    async def index():
        # 這一頁不准快取。原因:index.html 決定要載入哪些 js 檔,瀏覽器若拿到
        # 舊版的它,就會少載新加的檔案 —— 畫面會整個壞掉,而且使用者按重整
        # 也救不回來(要按 Ctrl+F5 才行)。每次只有一份小小的 HTML,不值得為
        # 它冒這個險;js 與 css 由 stamp_static_urls 負責換網址。
        html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            stamp_static_urls(html),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/api/config")
    async def get_config():
        """前端開機時要知道的兩件事:點名規則、以及伺服器有沒有開認證。

        點名規則(@某人 怎麼解析)由這裡下發,前後端因此共用同一套規則 ——
        那是它非在這裡不可的理由:規則若各寫一份,遲早會對不起來。

        ★ 這裡【不下發 agent 的顏色】。伺服器開機時並不知道會有誰連進來,
          所以顏色由前端從名字算(同名同色)—— 少一個要同步的東西,
          而且新成員第一次出現就有顏色,不必等重整。
        """
        return {
            "mentionPattern": MentionParser.JS_SOURCE,
            "a2aVersion": a2a_mod.A2A_PROTOCOL_VERSION,
            "authEnabled": hub.auth_enabled,   # 前端據此決定要不要顯示 token 欄位
        }


def register_room_routes(app: FastAPI, hub: Hub) -> None:
    """房間的讀寫:清單、在場名單、訊息、發言、任務。"""

    @app.get("/api/rooms")
    async def rooms_index():
        return {"rooms": hub.store.rooms_index()}

    @app.get("/api/rooms/{room}/presence")
    async def get_presence(room: str):
        """在場名單:誰的直播連線正開著(敲鈴器或瀏覽器都算)。"""
        return {"room": room, "present": sorted(hub.bus.watchers(room))}

    @app.get("/api/rooms/{room}/members")
    async def get_members(room: str):
        return {"members": hub.store.members(room)}

    @app.get("/api/rooms/{room}/state")
    async def get_state(room: str):
        """極輕量的狀態查詢:只回「最新第幾則」,不含訊息內容。"""
        return {"room": room,
                "last_id": hub.store.last_id(room),
                "count": hub.store.count(room)}

    @app.get("/api/rooms/{room}/messages")
    async def get_messages(room: str, request: Request, since_id: int = 0,
                           mentioned: str | None = None, before_id: int | None = None,
                           tail: int | None = None, reader: str | None = None):
        """撈訊息。

        reader=<名字> 是一個「順便」的動作:代表這個人真的把訊息看過了,
        所以派給他的任務要從「已送出」變成「處理中」—— 等於已讀回條。
        認不出身分時照樣把訊息給他,只是不算已讀(見 identify_optional_reader)。

        ★ 回應永遠是 {messages, last_id},last_id = 房間的最後一則。
          這裡沒有筆數上限,所以「拿到的」就是「那段區間的全部」,
          last_id 可以直接寫進 cursor。

          ★ 例外是 mentioned= 過濾模式:拿到的是子集,last_id 仍是房間的尾。
            那個模式只有一個用途 —— AGENTS.md 加入流程的「掃一遍有沒有人叫過我」,
            而在那裡把 cursor 推到房間尾正是【刻意跳過舊訊息】的決定。
            拿它做別的事之前,先想清楚被濾掉的那些你是不是需要。
        """
        selected, last = hub.store.query(room, since_id, mentioned, before_id, tail)
        who = hub.identify_optional_reader(reader, request)
        if who:
            hub.a2a_layer.on_reader_fetch(room, who, selected)

        return {"messages": selected, "last_id": last}

    @app.post("/api/rooms/{room}/messages", status_code=201)
    async def post_message(room: str, body: PostMessage, request: Request):
        sender = sanitize_sender(body.sender)
        if sender is None:
            raise BadSenderError({"error": "bad_sender",
                                  "detail": "名字限 1-32 字的中英數與 - _,不含空白與 @"})
        hub.check_writer(sender, request)    # 認證(只在 AUTH=on 時真的檢查)
        hub.rate_limiter.check(sender)       # 限流(永遠啟用)
        msg = await hub.ingest(room, sender, body.text,
                               reply_to=body.reply_to,
                               expect_last_id=body.expect_last_id,
                               kind=body.kind)
        return {"id": msg["id"]}

    @app.post("/api/rooms/{room}/ring/{name}")
    async def force_ring(room: str, name: str):
        """人類的「喂,醒醒」—— 強制敲某個 agent 的鈴,不管它的不騷擾計數。

        ★ 為什麼要有這個端點:

          敲鈴器有一條「連敲三次沒反應就安靜」的不騷擾規則,而觸發它的
          不一定是「卡住」,也可能只是「正在忙」。一旦安靜下來,要重新開始敲
          得等該 agent 的 cursor 追上 —— 而追上需要被敲醒。**那是死結**,
          從使用者的角度看就是「我在聊天室講話,這個 agent 完全沒反應」。

          伺服器沒辦法直接戳敲鈴器(它是別台機器上的另一個行程),
          但敲鈴器一直掛在這個房間的 SSE 上 —— 所以往那條線上丟一則
          指名的事件就夠了。**現成的通道,不需要新的連線。**

        回傳 online 讓 UI 分得出「敲了但對方沒開敲鈴器」與「敲了對方沒反應」。
        """
        hub.bus.publish(room, {"type": "ring", "target": name})
        return {"ok": True, "target": name,
                "online": name in hub.bus.live_agents(room)}

    @app.get("/api/rooms/{room}/tasks")
    async def get_room_tasks(room: str):
        return {"tasks": hub.a2a_layer.tasks_for_room(room)}


def register_stream_route(app: FastAPI, hub: Hub) -> None:
    """觀戰直播。獨立一組,因為它是唯一一個「連線會一直開著」的端點。"""

    @app.get("/api/rooms/{room}/stream")
    async def stream(room: str, request: Request, since_id: int = 0,
                     watcher: str | None = None, kind: str = "human"):
        """把新訊息即時推給對方,連線一直開著不關。

        watcher=<名字> 讓訂閱者報上身分,才會被算進在場名單;
        認不出來就當匿名觀眾(照樣看得到,只是不列名)。

        斷線重連:瀏覽器會自動帶上 Last-Event-ID 這個標頭告訴我們「我看到第幾則」,
        我們就從那裡繼續送 —— 斷線期間的訊息不會漏掉。
        """
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            since_id = int(last_event_id)

        who = hub.identify_optional_reader(watcher, request)
        # 這條 SSE 連線【誰都會建】—— 瀏覽器靠它收即時訊息,敲鈴器靠它知道何時該敲鈴。
        # 差別只在網址帶不帶 kind=agent:
        #
        #     敲鈴器   /stream?since_id=N&watcher=alice&kind=agent
        #     瀏覽器   /stream?since_id=N&watcher=allen              ← 沒有 kind
        #
        # 帶了的意思是「這條線後面是 AI,可以派任務給它」,所以它會進派任務的選單;
        # 沒帶的照樣算「在場」(頭像會出現在標題列),只是不能被派任務 ——
        # 人類接不了 A2A 任務,所以人類永遠不會出現在那個選單裡。
        subscription = hub.bus.subscribe(room, watcher=who, is_agent=(kind == "agent"))

        def to_sse(event: dict) -> str:
            """把一則事件包成直播的格式。

            ★ 只有【訊息】會帶 `id:` 那一行 —— 它是瀏覽器斷線續傳的游標
              (Last-Event-ID)。這條直播上還有不是訊息的東西(例如強制敲鈴),
              給它們一個 id 會讓續傳點跳掉;而假設「每一則都有 id」則會
              直接 KeyError —— 那不只是這一則送不出去,是**整條直播當場結束**,
              房間裡每一個訂閱者(含瀏覽器)一起被踢掉。

              SSE 規格本來就允許沒有 id 的事件,所以沒有就不寫那一行。
            """
            payload = json.dumps(event, ensure_ascii=False)
            if "id" not in event:
                return f"data: {payload}\n\n"
            return f"id: {event['id']}\ndata: {payload}\n\n"

        async def event_stream():
            try:
                yield "retry: 2000\n\n"     # 告訴瀏覽器:斷線後 2 秒再重連

                # 先補上他錯過的,再開始等新的。
                # 順序是「先訂閱、後回放」,所以交界處可能重複送一兩則 ——
                # 沒關係,對方會用 id 去掉重複的。反過來做則會漏訊息。
                replay, _ = hub.store.query(room, since_id=since_id)
                for msg in replay:
                    yield to_sse(msg)

                while True:
                    try:
                        msg = await asyncio.wait_for(subscription.queue.get(),
                                                     timeout=SSE_KEEPALIVE_SECONDS)
                        yield to_sse(msg)
                    except asyncio.TimeoutError:
                        # 一段時間沒訊息了。先看看這條連線是不是已經被判定塞車,
                        # 是的話就結束,讓對方重連(重連會自動補齊)。
                        if subscription.dead:
                            break
                        yield ": keep-alive\n\n"   # 送個空訊號,免得中間的路由器把連線切掉
            finally:
                hub.bus.unsubscribe(room, subscription)

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


def register_agent_routes(app: FastAPI, hub: Hub) -> None:
    """成員名冊:列出現在有誰在線、查看某個 agent 的名片。"""

    @app.get("/agents")
    async def agents_index(room: str = "main"):
        """現在可以接任務的 agent —— 也就是【此刻連著線且自稱 agent】的那些。

        ★ 這裡回答的是「現在能派給誰」,不是「歷史上有誰」。
          一個 agent 關掉視窗就會從這裡消失,重開又回來 —— 這正是我們要的:
          派任務給一個沒在跑的 agent,結果只會是逾時失敗,不如一開始就不讓你選。
        """
        listing = []
        for name in sorted(hub.bus.live_agents(room)):
            listing.append({"name": name,
                            "card": f"/agents/{name}/.well-known/agent-card.json"})
        return {"agents": listing}

    @app.get("/agents/{name}/.well-known/agent-card.json")
    @app.get("/agents/{name}/.well-known/a2a-agent-card")   # 常見的路徑別名,一併支援
    async def agent_card(name: str):
        # 「這個 agent 存在嗎」= 「它現在連著線嗎」(不分房間 —— 名片是身分證,不是房卡)。
        # 離線的 agent 沒有名片,因為你就算拿到名片也派不了任務給它。
        if name not in hub.bus.all_live_agents():
            return JSONResponse(status_code=404, content={"error": "unknown agent"})
        return hub.a2a_layer.agent_card(name)


def extract_sender_name(params: dict) -> str:
    """從 A2A 請求裡找出「發送者自稱是誰」。

    這個資訊可能出現在兩個地方(訊息裡面、或請求外層),外層優先。
    找不到就給一個預設名字,讓後續流程照樣走得下去。

    ★ 同一套「兩層 metadata 合併」的規則在 a2a.py 的 _create_task 也有一份
      (那邊還要順便取 deadlineSeconds)。兩處是刻意的重複:
      要合併就得把目標房間一路傳進協定層,漣漪比這幾行大得多。
      **但改的時候要一起改** —— 只改一邊,同一個請求會在兩處被解讀成不同的人。
    """
    message = params.get("message") or {}
    metadata = {}
    metadata.update(message.get("metadata") or {})
    metadata.update(params.get("metadata") or {})
    raw_name = str(metadata.get("senderName", ""))
    return sanitize_sender(raw_name) or "a2a-client"


def register_a2a_route(app: FastAPI, hub: Hub) -> None:
    """A2A 協定端點:別的 agent 用這個網址跟我們對話。"""

    @app.post("/agents/{name}/a2a")
    async def a2a_rpc(name: str, request: Request):
        """JSON-RPC 2.0 端點。

        JSON-RPC 的規矩是:不管成功或失敗,回應都長同一個樣子 ——
        有 jsonrpc、有 id,然後 result 或 error 二選一。
        """
        try:
            body = await request.json()
        except Exception:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"}}

        request_id = body.get("id")
        method = body.get("method")
        if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32600, "message": "invalid request"}}

        params = body.get("params") or {}

        # 會寫入的方法要先過認證與限流。
        # (協定本身把認證交給 HTTP 層處理,所以檢查放在這裡,而不是放進協定層。)
        #
        # ⚠️ 已知的縫:CancelTask 會改狀態(把任務轉成 CANCELED),但【不在這份清單裡】。
        #    也就是說 AUTH=on 時,任何人只要知道一個 task id,不帶 token 就能取消它。
        #
        #    現在沒出事,是因為 AUTH 預設關著、而且只在區網跑。★ 但 AUTH=on 上線前必修。
        #
        #    為什麼不是現在順手加進清單:CancelTask 的 params 裡【沒有 senderName】,
        #    extract_sender_name 會 fallback 成 "a2a-client" —— 直接加進去的結果是
        #    AUTH=on 時所有 cancel 全部被擋。要修得先定「cancel 請求怎麼聲明身分」,
        #    那是認證那一輪的設計題,不是一行改動。
        #
        #    GetTask / ListTasks / SubscribeToTask 不進清單是對的:它們不改狀態。
        #
        #    ★ 這個洞記在三個地方,修好那天【三處要一起改】:
        #      這裡、doc/A2A_MAPPING.md 的「已知取捨」、
        #      以及 doc/A2A_TUTORIAL.md 第 7 章(那裡拿它當「協定不管授權」的實例)。
        if method in ("SendMessage", "SendStreamingMessage"):
            sender = extract_sender_name(params)
            try:
                hub.check_writer(sender, request)
                hub.rate_limiter.check(sender)
            except ApiError as exc:
                return JSONResponse(status_code=exc.status, content=exc.payload)

        try:
            result = await hub.a2a_layer.dispatch(name, method, params)
        except a2a_mod.A2AError as exc:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": exc.code, "message": exc.message}}

        # 串流類的方法回傳的是「會一直吐東西的東西」,要用直播的方式送。
        if inspect.isasyncgen(result):
            return StreamingResponse(result, media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


# ═══════════════════════════════════════════════════════════════════════════
#  組裝 —— 把上面兩層接起來
# ═══════════════════════════════════════════════════════════════════════════

def create_app(port: int | None = None, host: str | None = None,
               public_host: str | None = None) -> FastAPI:
    """把整個 hub 組起來,回傳一個可以跑的網頁應用。

    這個函式刻意保持很短 —— 它只做組裝,不做決定。
    想知道規則怎麼定的就看 Hub;想知道有哪些網址就看 register_* 那幾個函式。

    關於綁定位址:HOST 決定要聽哪個網路介面,**預設是 0.0.0.0,也就是開放區網**
    (見 DEFAULT_HOST;想只聽本機請設 HOST=127.0.0.1)。
    """
    hub = Hub(port, host, public_host)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 伺服器起來之後、開始服務之前,先把沒做完的任務接回來。
        await hub.restore_tasks()
        yield

    app = FastAPI(title="A2A Chatroom Hub", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    async def handle_api_error(request: Request, exc: ApiError):
        """我們自訂的錯誤都帶著「該回什麼狀態碼、該說什麼」,這裡統一轉成回應。"""
        return JSONResponse(status_code=exc.status, content=exc.payload)

    app.add_exception_handler(ApiError, handle_api_error)

    # 把 hub 掛在 app 上:測試(以及任何需要「從 app 取得內部狀態」的工具)
    # 才有一個正式的入口,不必去猜閉包裡有什麼。
    app.state.hub = hub

    register_page_routes(app, hub)
    register_room_routes(app, hub)
    register_stream_route(app, hub)
    register_agent_routes(app, hub)
    register_a2a_route(app, hub)
    return app


if __name__ == "__main__":
    import uvicorn

    from envfile import load_env_file

    # 設定檔要在讀任何環境變數【之前】載入,否則下面幾行拿到的還是舊值。
    # 已存在的環境變數優先,所以 `HOST=127.0.0.1 uv run server.py` 這種臨時覆寫仍然有效。
    _applied = load_env_file(BASE / "server.env")
    if _applied:
        print(f"[hub] 已套用 server.env:{', '.join(_applied)}", file=sys.stderr)

    _port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    _host = os.environ.get("HOST", DEFAULT_HOST)
    uvicorn.run(create_app(_port, _host), host=_host, port=_port)
