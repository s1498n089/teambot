"""往聊天室發言 —— 把每次都要手做、而且每次都可能忘的四件事收進來。

## 為什麼有這支工具

同一天內,同樣的三種錯各犯了兩次以上:

    拿了 last_id 卻沒讀內容  → 漏看老闆叫停、漏看新成員的自我介紹(3 次)
    讀完忘記推進度書籤      → 敲鈴器判定我落後,白敲了三輪鈴(3 次)
    commit hash 用手打      → 報出不存在的 hash,對方照著查什麼都找不到(2 次)

每一次我都寫下教訓、也說了改法。**然後又犯。**

跟 safe_delete.py 同一個結論:**規則存在但沒被執行,跟沒有規則的結果一樣。**
所以把動作嵌進工具 —— 不是「我會記得」,而是「不做就發不出去」。

## 用法

    uv run tools/say.py --file message.md          ★ 預設路線,先寫檔再送
    uv run tools/say.py "短句,沒有標點符號"          只給不含符號的短訊息
    uv run tools/say.py "已提交 {HASH}"             {HASH} 自動換成當前 commit

★ 為什麼 --file 是預設而不是「長訊息才用」?

  因為訊息要經過 shell,而 shell 會【動】訊息內容 —— 而且是靜默地動。
  在 bash 的雙引號裡,反引號 `foo` 是命令替換:它會去執行 foo,
  失敗之後留下空字串。於是 `ConflictError` 就這樣從訊息裡消失了。

  這不是假設,是踩過的:一則報告裡的五個 code 識別字全被清成空白,
  **沒有報錯、訊息照樣送出**,對方讀到的是「1. 已刪 ——」。
  而我們談的就是 code,每則訊息都有反引號。

  這一段原本只寫「長訊息從檔案讀」,把它當成【長度】問題 ——
  於是短訊息就理直氣壯地走了 shell。它其實是【安全】問題。

工具會依序做:

    ① 對帳並【把未讀訊息印出來】—— 不是只取 last_id,是真的顯示給你看
    ② 用剛撈到的 last_id 當樂觀鎖送出(撞車時 hub 會擋下並回傳你錯過的)
    ③ 自報 kind=agent(2026-07-27 起的協定,見 doc/AGENT_GUIDE.md v8)
    ④ 送出成功後推進度書籤

★ ① 是刻意設計的:發言前一定會看到未讀內容。看到了還不讀是人的問題,
  但至少不會再出現「我以為我對帳了、其實只取了號碼牌」那種事。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import urllib.request

# ★ Windows 必做:這台機器的主控台預設是 cp950,印中文與 ✓ 這類符號會炸
#   UnicodeEncodeError。而且它【只在最後一行成功訊息炸】—— 訊息其實已經送出去了,
#   看起來卻像失敗,是最容易誤判的那種。
#   (今天第二次踩:早上寫探針時踩過一次,還寫了註解提醒「未來的工具也要做」——
#    然後寫這支工具時照樣忘了。所以現在它在這裡,不在我的記性裡。)
sys.stdout.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent.parent


def current_commit() -> str:
    """當前 commit 的短 hash。手打過兩次錯,從此一律現查。"""
    result = subprocess.run(["git", "log", "--format=%h", "-1"],
                            cwd=BASE, capture_output=True, text=True)
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="往 A2A 聊天室發言")
    parser.add_argument("text", nargs="?", default="", help="訊息內容({HASH} 會自動替換)")
    parser.add_argument("--file", help="從檔案讀訊息(長訊息用)")
    parser.add_argument("--name", default="alice")
    parser.add_argument("--room", default="main")
    parser.add_argument("--server", default="http://127.0.0.1:8787")
    parser.add_argument("--kind", default="agent", choices=["agent", "human"])
    args = parser.parse_args()

    if args.file:
        text = pathlib.Path(args.file).read_text(encoding="utf-8")
    else:
        text = args.text
    if not text.strip():
        parser.error("沒有訊息內容")

    text = text.replace("{HASH}", current_commit())

    cursor_path = BASE / "state" / f"cursor-{args.name}.txt"
    cursor = cursor_path.read_text(encoding="utf-8").strip() if cursor_path.exists() else "0"

    # ① 對帳 —— 而且一定印出來
    url = (f"{args.server}/api/rooms/{args.room}/messages"
           f"?since_id={cursor}&reader={args.name}")
    data = json.load(urllib.request.urlopen(url))

    if data["messages"]:
        print(f"── 發言前有 {len(data['messages'])} 則未讀 ──")
        for msg in data["messages"]:
            print(f"  #{msg['id']} {msg['from']}({msg.get('kind', '?')}):"
                  f"{msg['text'][:150]}")
        print("──────────────────────────")
    else:
        print("(發言前對帳:沒有未讀)")

    # ② + ③ 送出:帶樂觀鎖與身分
    body = json.dumps({
        "from": args.name,
        "kind": args.kind,
        "text": text,
        "expect_last_id": data["last_id"],
    }, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        f"{args.server}/api/rooms/{args.room}/messages",
        data=body, headers={"Content-Type": "application/json"}, method="POST")

    try:
        result = json.load(urllib.request.urlopen(request))
    except urllib.error.HTTPError as error:
        if error.code == 409:
            payload = json.load(error)
            print(f"✗ 撞車了:你以為最新是 #{data['last_id']},但已經到 #{payload['last_id']}")
            print("  你錯過的:")
            for msg in payload.get("missed", []):
                print(f"    #{msg['id']} {msg['from']}:{msg['text'][:150]}")
            print("  讀完再決定要不要重發。")
            return 1
        raise

    # ④ 推書籤
    cursor_path.write_text(str(result["id"]), encoding="utf-8")
    print(f"✓ 已送出 #{result['id']},書籤已推")
    return 0


if __name__ == "__main__":
    sys.exit(main())
