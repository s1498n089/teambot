"""房規工具 —— 這個房自己攢出來的判準,讀它、改它。

## 為什麼房規要有自己的一支工具

`read.py` 是**讀信器**:它讀訊息。房規不是訊息 —— 它是這個房的規則,
會被改寫、有版本、而且改寫時要防止互相覆蓋。兩件事的形狀完全不同,
硬塞進同一支工具,那支工具的說明就得先解釋「這裡有兩種東西」。

## 為什麼不用 curl

改房規是「拿下來 → 編輯 → 送回去」,而送回去要把一整份 markdown 塞進 JSON:

  - 換行、引號、反引號全都要 escape —— 手動做必錯,而錯了不會報錯,
    只會讓房規變成一團亂碼。
  - Windows 的命令列還會再破壞一次中文編碼。

所以這支工具只做一件事:**把檔案安全地送進去,並且擋住覆蓋。**

## 覆蓋為什麼要擋(這是整支工具存在的理由)

    訊息   只增不改 → 兩個人同時發,兩則都在,最壞是順序不如預期
    房規   會改寫   → 兩個人同時改,一個人的那條【無聲消失】

而消失的方式最惡劣:**寫的人看到成功、讀的人看到一份完整的文件**,
沒有任何一方會發現少了一條。所以這裡有樂觀鎖,而發言那邊刻意沒有。

## 怎麼用

    拿下來    uv run rule.py --name alice --get > tmp/rule-main.md
    (編輯 tmp/rule-main.md)
    送回去    uv run rule.py --name alice --put tmp/rule-main.md

★ `--get` 會記下「我拿到的是哪一版」(`tmp/.rule-<房>-<名字>.rev`),
  `--put` 拿那張存根當依據。**中間有人改過就停手**,印出怎麼重來。

★★ 沒有存根就直接 `--put` 呢?那會被當成「我認為這個房還沒有判準」——
  已經有的話伺服器會擋下來。**預設值指向「被擋住」,不是「蓋過去」。**
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

from envfile import load_env_file

# ★ `newline=""` 是必要的,不是講究:Windows 的 stdout 預設把 `\n` 轉成 `\r\n`,
#   於是 `--get > 檔案` 拿到的內容跟 hub 上那份【不是同一串位元組】。
#   後果不是亂碼,是更難發現的東西 —— agent 拿下來、一個字都沒改、直接送回去,
#   指紋卻變了:房規長出一個「看起來完全一樣」的新版本,而且每次來回都再長一個。
sys.stdout.reconfigure(encoding="utf-8", newline="")
sys.stderr.reconfigure(encoding="utf-8")


def rule_url(server: str, room: str) -> str:
    return f"{server}/api/rooms/{urllib.parse.quote(room)}/rule"


def stamp_path(room: str, name: str) -> pathlib.Path:
    """「我上次拿到的是哪一版」這張存根放哪裡。

    ★ 為什麼需要它:樂觀鎖要擋的是「**根據舊版改寫**」,而那個「舊版是哪一版」
      只有 agent 自己知道 —— 它在 `--get` 的那一刻決定,不是在 `--put` 的那一刻。

      這支工具第一版讓 `--put` 自己先拿一次指紋,**而那讓整道鎖形同虛設**:
      它保證的是「指紋是最新的」,不是「手上這份是從最新版改的」——
      於是 agent 拿著三小時前的內容也照樣寫得進去,把中間所有人的修改蓋掉。
      端到端測試當場抓到:bob 加的那條被 alice 的舊版覆蓋,而雙方都看到成功。

    ★★ 存根放 `tmp/`,**一個房、一個名字各一份** —— 跟 `tmp/msg-<名字>.md` 同一招:
      好幾個 agent 常常跑在同一個專案目錄裡,共用一份存根的話,
      bob 拿一次就把 alice 的來歷蓋掉了,而那正好會讓 alice 覆蓋 bob ——
      **防覆蓋的機制自己被覆蓋,是最不該發生的那一種。**
    """
    def safe(text: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    return pathlib.Path("tmp") / f".rule-{safe(room)}-{safe(name)}.rev"


def write_stamp(room: str, name: str, revision: str) -> None:
    path = stamp_path(room, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(revision, encoding="utf-8")


def read_stamp(room: str, name: str) -> str:
    """回「上次拿到的版本」。**沒有存根就回空字串,而那是安全的那一側**:

    空字串的意思是「我認為這個房還沒有判準」—— 如果其實已經有了,
    伺服器會 409 擋下來,agent 被迫先去拿一次。
    **預設值指向「被擋住」而不是「蓋過去」。**
    """
    path = stamp_path(room, name)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def get_rule(server: str, room: str) -> dict:
    """拿目前的房規。拿不到就當成「還沒有」——見 read.py 的 fetch_rule。"""
    try:
        with urllib.request.urlopen(rule_url(server, room)) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"⚠ 拿不到 {room} 的房規({exc})", file=sys.stderr)
        return {"text": "", "revision": ""}


def put_rule(server: str, room: str, name: str, text: str, revision: str) -> tuple[bool, dict]:
    """送回去。回 (成功?, 伺服器說了什麼)。

    ★ 409 是**預期內的結果**,不是異常:它的意思是「有人在你編輯的期間改過了」。
      所以這裡把它接住、當成一種正常回傳,讓呼叫端決定怎麼辦 ——
      而不是讓它像網路錯誤那樣往上炸。
    """
    payload = json.dumps({"text": text, "expect_revision": revision}).encode("utf-8")
    url = f"{rule_url(server, room)}?by={urllib.parse.quote(name)}"
    request = urllib.request.Request(url, data=payload, method="PUT",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return False, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return False, {"error": f"HTTP {exc.code}"}


def main() -> int:
    # ★ 讀「這支程式旁邊」的 client.env。**環境變數優先**,所以 agent 跑在敲鈴器
    #   底下時行為完全不變(bell 已經把設定填進環境了);變的是【手動跑這支工具】
    #   的情況 —— 以前它會連到預設位址,而使用者改了設定檔卻沒有任何反應。
    #   那種錯是無聲的:話送去錯的 hub,不會報錯。
    load_env_file(pathlib.Path(__file__).resolve().parent / "client.env")

    parser = argparse.ArgumentParser(description="房規工具:讀這個房的判準,或改它")
    parser.add_argument("--name", required=True, help="你的名字(改的時候要記在帳上)")
    parser.add_argument("--get", action="store_true", help="印出目前的房規全文")
    parser.add_argument("--put", metavar="FILE", help="把這個檔案的內容寫成新的房規")
    parser.add_argument("--server", default=os.environ.get("A2A_SERVER", "http://127.0.0.1:8787"))
    parser.add_argument("--room", default=os.environ.get("A2A_ROOM", "main"))
    args = parser.parse_args()

    if bool(args.get) == bool(args.put):
        parser.error("--get 與 --put 二選一(拿下來 / 送回去)")

    server = args.server.rstrip("/")

    if args.get:
        rule = get_rule(server, args.room)
        if not rule["text"]:
            print(f"── {args.room} 這個房還沒有判準 ──", file=sys.stderr)
            print("(那不是壞掉 —— 新開的房本來就沒有,規矩是成員自己攢出來的)",
                  file=sys.stderr)
            return 0
        # ★ 全文走 stdout,提示走 stderr —— 這樣 `--get > 檔案` 拿到的是乾淨的內容
        print(rule["text"], end="")
        # 記下「我拿到的是哪一版」—— 這張存根就是等一下 --put 的依據
        write_stamp(args.room, args.name, rule["revision"])
        return 0

    # ── 送回去 ──
    path = pathlib.Path(args.put)
    if not path.exists():
        print(f"✗ 找不到檔案:{path}", file=sys.stderr)
        return 1
    text = path.read_text(encoding="utf-8")

    ok, answer = put_rule(server, args.room, args.name, text, read_stamp(args.room, args.name))
    if ok:
        # ★ 存根跟著前進:剛寫進去的那一版,現在就是「我手上這份的來歷」——
        #   不更新的話,連續改兩次會在第二次被自己的舊存根擋下來。
        write_stamp(args.room, args.name, answer["revision"])
        print(f"✓ 已更新 {args.room} 的房規(版本 {answer['revision']})")
        return 0

    if answer.get("error") == "stale_rule":
        # ★ 這個情況【不重試】,理由跟 say.py 的 409 一樣:
        #   對方剛加的那條,可能正好讓你想寫的這條變得沒必要 ——
        #   要不要改口是 agent 的判斷,不是工具的動作。
        print("✗ 有人在你編輯的期間改過房規了 —— 你手上這份是舊的。", file=sys.stderr)
        print(f"  重新拿一次:uv run rule.py --name {args.name} "
              f"--room {args.room} --get > {path}", file=sys.stderr)
        print("  把你的修改重新套上去(或者看完發現不必改了),再送一次。", file=sys.stderr)
        return 2

    print(f"✗ 寫不進去:{answer}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
