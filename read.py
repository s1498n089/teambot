"""read(讀信器)— 把訊息好好呈現出來,然後把筆遞過去。

    uv run read.py --name alice --expect 1186     對帳:看 1186 之後有什麼
    uv run read.py --name alice --rejoin          剛上線 / 關太久回來

## 它做什麼

    組網址、撈、排版、印全文、算好下一步的指令    ← 機械,自動做
    要不要發言、什麼時候把書籤推過去              ← 判斷,交還給 agent

## ★ 紅線:它一次都不寫 cursor 檔

今天(2026-07-30)立過的原則:**印出來 ≠ 讀到**。
工具把字印在螢幕上,到 agent 真的讀進去之間隔著「指令返回」這個斷窗 ——
斷在那裡(session 被換、context 壓縮、視窗關掉)而 cursor 已經推過去,
就是**永久漏讀,而且沒有人會發現**。

那 say.py 為什麼推得了?因為它**親自 GET 過、親自 POST 成功** ——
「我讀到這裡」那句話由它自己的動作證明。read 沒有任何動作能證明「讀」發生了。

    say 推得了   因為它做過那件事
    read 推不了  因為印出來不是讀到

同一條執法句(「寫聲明的那一方,有沒有親自做過它聲明的事」),兩個相反的結論。

★ 所以尾行印的是**可以直接貼的指令**,不是幫你執行 ——
  **把簽名的筆遞過去,但不代簽。**

## 它真正省掉什麼(不是省打字)

    reader= 漏掉不會報錯   只會讓別人派給你的 task 靜靜逾時 FAILED
    JSON 擠成一整行        中文夾在裡面,每次臨時湊命令列 parse 都要重踩編碼坑
    mentioned= 那條打錯    等於跳過加入流程的安全網,而且不會有人知道

★ 這三個的共同點:**錯了都不會報錯**。工具裡的那條路只要走對一次就永遠對。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent

# 加入流程要看最近幾則。
#
# ★ 這個數字跟 AGENTS.md 加入流程寫的一樣 —— 那裡的理由是「這跟人進聊天室的習慣一樣:
#   往上滑看一下前後文就開口,沒有人會把幾個月的訊息從頭讀完」。
#   (say.py 的 REJOIN_THRESHOLD 也是 50,但那是【另一個語意】:超過多少則就不硬印。
#    兩個數字碰巧相同,不要合併 —— 合併之後改其中一個意圖會連帶動到另一個。)
REJOIN_TAIL = 50

# 點名掃描要回顧最近幾則、每則印多長。
#
# ★ 這裡【可以】截斷,而上面那兩條路不行 —— 差別在「它會不會變成一句聲明」:
#
#     --expect 那條   agent 讀完就要說「我讀到 N 了」→ 讀到半截等於假聲明,不准截
#     點名掃描        產出是「有沒有人找過我、在哪幾則」,不是「我讀完了這些」
#                     agent 看到可疑的就回頭撈那則的前後文 —— AGENTS.md 就是這樣教的
#
#   而且不截的代價是真的:實測某個名字被點名 236 次,全文印出來會直接洗掉
#   agent 的 context —— 那正是這整套設計要保護的東西。
CALLED_TAIL = 20
CALLED_PREVIEW = 200


def fetch(server: str, room: str, query: dict) -> dict:
    """撈訊息。**reader 一定會帶**,這是這支工具存在的理由之一。

    ★ reader=<名字> 是 A2A 的已讀回條:hub 靠它把點名這個 agent 的 task 從
      SUBMITTED 轉成 WORKING,發起方才知道 agent 動工了。
      漏掉不會有任何症狀 —— 直到某天有人問「為什麼我派出去的 task 全部 FAILED」。
      把它收進工具,就不會再有人手打時漏掉。
    """
    url = f"{server}/api/rooms/{room}/messages?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show(messages: list[dict], title: str) -> None:
    """把訊息攤開 —— **全文,不截斷**。

    ★ 截斷會製造一種很陰的錯:agent 讀到半截,然後真心地聲明「我讀了」。
      工具寫下的假話還能讀 code 稽核出來,agent 真心說的假話零行 code 查得到。
      這條路的存在目的就是「讀完」,截斷直接毀掉目的本身。
    """
    if not messages:
        print(f"── {title}:沒有 ──")
        return
    print(f"── {title}({len(messages)} 則,全文)──")
    for msg in messages:
        mentions = msg.get("mentions") or []
        tag = f"  → @{' @'.join(mentions)}" if mentions else ""
        reply = f"  ↩ #{msg['reply_to']}" if msg.get("reply_to") else ""
        print(f"\n#{msg['id']} {msg['from']}({msg.get('kind', '?')}){tag}{reply}")
        print(msg["text"])
    print("\n──────────────")


def show_called(messages: list[dict], name: str, server: str, room: str) -> None:
    """點名掃描的結果 —— 這條路【可以】截斷,理由見 CALLED_TAIL 旁邊那段。

    它回答的問題是「有沒有人找過我、在哪幾則」,不是「我讀完了這些」。
    看到可疑的就回頭撈那則的前後文 —— AGENTS.md 的加入流程就是這樣教的。
    """
    if not messages:
        print(f"── 整段歷史裡沒有人點名 {name} ──")
        return

    total = len(messages)
    recent = messages[-CALLED_TAIL:]
    print(f"── 點名 {name} 的訊息:共 {total} 則,"
          f"以下是最近 {len(recent)} 則(每則截 {CALLED_PREVIEW} 字)──")
    for msg in recent:
        head = msg["text"].replace("\n", " ")[:CALLED_PREVIEW]
        print(f"  #{msg['id']} {msg['from']}:{head}")
    if total > len(recent):
        print(f"\n  還有 {total - len(recent)} 則更早的沒印出來。")
    print(f"  ★ 這是【掃描】不是【讀完】—— 有哪則看起來要緊,回頭撈它的前後文:")
    print(f'     curl -s "{server}/api/rooms/{room}/messages?since_id=<那則id-3>&reader={name}"')
    print("──────────────")


def cursor_command(server: str, room: str, name: str, last_id: int) -> str:
    """印出「把書籤推到這裡」的指令。

    ★ cursor 住在 hub 上、而且一房一份,所以這行必須帶著房間與名字 ——
      以前它是往本地檔案 printf 一個數字,而那個檔案【不分房間】:
      換房之後推進去,蓋掉的是別的房的進度。

    ★★ 這支工具仍然【不執行】這行指令,只把它印出來。
      印出來不等於讀到 —— 中間隔著「指令返回」那個斷窗,
      而 read 沒有任何動作可以證明「讀」發生了。**遞筆,不代簽。**
    """
    return (f'curl -s -X PUT "{server}/api/rooms/{room}'
            f'/cursor/{name}?last_id={last_id}"')


def main() -> int:
    parser = argparse.ArgumentParser(description="讀信器:撈、排版、印全文,然後把筆遞給你")
    parser.add_argument("--name", required=True, help="你的名字(會帶進 reader= 已讀回條)")
    parser.add_argument("--expect", type=int,
                        help="從第幾則之後開始看(= 你目前讀到哪)")
    parser.add_argument("--rejoin", action="store_true",
                        help="剛上線/關太久:看最近 %d 則 + 掃整段歷史的點名" % REJOIN_TAIL)
    parser.add_argument("--server", default=os.environ.get("A2A_SERVER", "http://127.0.0.1:8787"))
    parser.add_argument("--room", default=os.environ.get("A2A_ROOM", "main"))
    args = parser.parse_args()

    if bool(args.expect is not None) == bool(args.rejoin):
        parser.error("--expect 與 --rejoin 二選一(前者是日常對帳,後者是重新加入)")

    server = args.server.rstrip("/")

    if args.rejoin:
        # ── 重新加入:看最近的 + 掃一遍點名 ──
        #
        # ★ 兩步都要做,而第二步是安全網:可以不讀全部歷史,但不能漏掉找你的人。
        #   AGENTS.md 把它列為加入流程的第 2 步,理由是「跳過歷史」必須有補償。
        recent = fetch(server, args.room, {"tail": REJOIN_TAIL, "reader": args.name})
        show(recent["messages"], f"最近 {REJOIN_TAIL} 則")

        called = fetch(server, args.room,
                       {"since_id": 0, "mentioned": args.name, "reader": args.name})
        show_called(called["messages"], args.name, server, args.room)

        last_id = recent["last_id"]
        print(f"\n讀完之後,把書籤設到房間目前的位置(這是【刻意跳過】中間那段的決定):")
        print(f"  {cursor_command(server, args.room, args.name, last_id)}")
        return 0

    # ── 日常對帳 ──
    data = fetch(server, args.room, {"since_id": args.expect, "reader": args.name})

    if len(data["messages"]) > REJOIN_TAIL:
        # ★ 落後太多就不是「對帳」了,是「重新加入」—— 硬印幾百則只會洗掉 context,
        #   而那正是這整套設計要保護的東西。
        print(f"⚠ {args.expect} 之後有 {len(data['messages'])} 則 —— "
              f"這已經不是對帳,是重新加入。")
        print(f"  改跑:uv run read.py --name {args.name} --rejoin")
        return 1

    show(data["messages"], f"#{args.expect} 之後的未讀")

    last_id = data["last_id"]
    if not data["messages"]:
        # 空的:敲鈴器與 cursor 之間的正常 race,AGENTS.md 說是預期內的 no-op。
        print("(這是敲鈴器與書籤之間的正常 race,不用疑惑也不用回報)")
        return 0

    print("\n讀完之後 —— 這兩行都是【可以直接貼】的,選一條:")
    print(f"  要發言   uv run say.py --name {args.name} --expect {last_id} "
          f"--file tmp/msg-{args.name}.md")
    print(f"  不發言   {cursor_command(server, args.room, args.name, last_id)}")
    print("\n★ 這兩個數字是【你的聲明】:「我讀到這裡了」。"
          "工具不替你推,因為它不知道你有沒有真的讀進去。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
