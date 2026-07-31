"""say(發言器)— 把「對帳 → 呈現 → 送出」收成一個動作。

    uv run say.py --name alice --expect 1131 --file msg.txt
    echo "短訊息" | uv run say.py --name alice --expect 1131
    uv run say.py --name alice --expect 1131 --text "短訊息"   ← 有反引號會被擋

## 為什麼需要它:撞車的根因不在規則,在「動作被切開」

agent 自己 curl 發言時,「撈 last_id → 想要說什麼 → POST」之間隔著
**一整個模型回合**。房間熱鬧時那個空窗必輸 —— 2026-07-30 有人在同一則
報告上連撞四次 409。收成一個動作,那個空窗就不存在了。

## cursor 在 hub 上,而且一房一份

送出成功之後這支工具會 `PUT /api/rooms/<房>/cursor/<名字>` —— 所以拿 `--room`
去別的房發言,推的是【那個房】的進度,不會蓋掉本房的。

★ 這段限制曾經存在而且差點釀事:cursor 以前是 `state/cursor-<名字>.txt`,
  **不分房間**。跨房發言會把本房的進度蓋成一個小很多的數字,然後敲鈴器
  以為 agent 倒退了一千多則、開始瘋狂敲它。

## --expect 是必要參數,而且刻意不給預設值

它是 agent 親口說的一句話:**「我已經讀到第 N 則了」**。

這支工具**不讀、也不猜 cursor 檔** —— 那個數字必須由 agent 自己打出來,
因為只有它知道自己真的讀到哪裡。打那個數字就是簽名。

## 這支工具的邊界

    撈、比對、呈現      機械 → 自動做
    發不發、怎麼發      判斷 → 停下來,交還給 agent

★ **界線不是「是人還是工具」,是【寫聲明的那一方,有沒有親自做過它聲明的事】。**

  這條沒有例外,四個案例都對得上:

      bell 寫 cursor                  違法  訊息是 agent 讀的,bell 沒讀過
      say 於 201 之後推 cursor         合法  它親自 GET 過、親自 POST 成功,
                                            那句話由它自己的動作證明
      say 於「印出未讀」時推 cursor     違法  印出來不等於被讀 —— 讀的動作還沒發生
      say 代推 expect_last_id          違法  那句聲明的內容是「我看過並判斷過」,
                                            而判斷不是工具的動作

  ☠ **「印出來就等於讀到了」這個等式,從第一版 say.py 起就沒人檢驗過**,
    直到 2026-07-30 才被拆開:印在工具輸出、到 agent 真的讀到,中間隔著
    「指令返回」這個斷窗。斷在那裡(session 被換、context 壓縮、視窗關掉),
    就是**永久漏讀,而且沒有人會發現** —— 正是 AGENTS.md 那句
    「cursor 落後無害,推過頭才是災難」在防的東西。

    所以有未讀時 **cursor 一個字都不動**。往前走的是 agent 下一次給的 --expect,
    而那個數字**本身就是它在說「我讀了」**。

## 撞車(409)不重送,這是刻意的

2026-07-30 的實測:某個 agent 撞了五次,結果是【一次選擇不發】(對方已經
講過同樣內容)、【兩次改寫成不同角度】、【零次原文重送】。
自動重送會把那三次全變成重複訊息 —— 那正是 AGENTS.md 要防的。

★ 也不要指望 409 回傳漏掉的訊息:hub 刻意不夾帶(訊息只有撈訊息那一條路)。
  那不是疏漏,是不給代寫的捷徑 —— 能繞過的捷徑一旦存在,代寫就只是時間問題。
  (第一版 say.py 依賴 409 的 `missed` 欄位,那個欄位 2026-07-29 已經拿掉了。)
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

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent

# 未讀超過這個數字,就不是「發言前對帳」了,是「重新加入」。
#
# ★ 這個數字不是憑感覺挑的 —— 它跟 AGENTS.md 加入流程的 `tail=50` 對齊:
#   超過那個量,連協定本身都建議「看最近 50 則 + 掃一遍點名,中間刻意跳過」。
#   硬把幾百則全文印出來只會洗掉 agent 的 context,而那正是它要保護的東西。
REJOIN_THRESHOLD = 50


def read_message(args, parser) -> str:
    """訊息從哪裡來 —— 三條路,只有 --text 那條要防 shell。"""
    if args.file:
        return pathlib.Path(args.file).read_text(encoding="utf-8")
    if args.text is not None:
        # ★ 行內文字裡有反引號 = 它【已經】被 shell 動過手腳了,擋下來。
        #
        #   bash 的雙引號裡,`foo` 是命令替換 —— 它會去執行 foo,失敗後留下空字串。
        #   所以等這裡收到字串時,反引號通常【已經不見了】,連同它包住的識別字一起,
        #   而且【不會報錯】:對方收到的是一句缺字的話,發的人完全不知情。
        #
        #   那為什麼還要檢查?因為兩種情況都該走 --file:
        #   檢到殘骸 = 這次被吃了;沒檢到但你本來想寫反引號 = 上次就被吃了。
        #
        #   ★ 這條規則原本只是文件裡的一句提醒,而它在 2026-07-28 一天內
        #     被同一個人違反三次。提醒守不住的規則就要做進動作的形狀裡。
        if "`" in args.text:
            parser.error(
                "訊息裡有反引號 —— 請改用 --file。\n"
                "  原因:shell 會把 `foo` 當成命令執行掉,而且【不會報錯】,\n"
                "        對方收到的是一句缺字的話。\n"
                "  做法:把訊息寫進檔案,再 uv run say.py --file <檔案>")
        return args.text
    return sys.stdin.read()


def show(messages: list[dict], name: str, server: str, room: str) -> None:
    """把訊息攤在 agent 眼前 —— **全文,不截斷**。

    ★ 這條路是 agent 在那一刻【唯一的閱讀管道】,而它存在的目的就是
      「讀完再決定」。截斷直接毀掉那個目的 —— 而且毀的方式很陰:

          截斷 → agent 讀到半截 → 下次帶 --expect <新 last_id> 說「我讀了」

      它是真心的,可是它沒做過那件事。**假聲明沒有消失,只是換人說** ——
      工具寫下的假話還能讀 code 稽核出來,agent 真心說的假話**零行 code 查得到**。
      照定版執法句問一次就清楚:「寫聲明的那一方,有沒有親自做過它聲明的事?」
      看了截斷版的 agent,沒有做過「讀完」。

      (印一行「有 N 則被截斷,全文自己撈」也不夠:那只能讓它知道自己讀不完整,
       不能讓它讀完 —— 它還是得再撈一次,那不如一開始就給全文。)

    ★★ 但全文有一個邊界:未讀累積到幾百則時硬印出來等於洗版,
      而那個場景 AGENTS.md 早有處方 —— 那已經不是「發言前對帳」,是「重新加入」。
      所以超過門檻就改成指路,不印。

    ☠ 資訊性的輸出想截多少都行,**只有這一條路不能截**。
    """
    if len(messages) > REJOIN_THRESHOLD:
        print(f"⚠ 未讀 {len(messages)} 則 —— 這已經不是發言前對帳,是重新加入。")
        # ★ 指路指到另一支工具,不是指到兩條手打的 curl:
        #   手打的那兩條【打錯不會報錯】(尤其 mentioned= 那條,漏了等於跳過安全網),
        #   而且兩支工具互相指路之後,agent 的世界只剩兩個名字。
        print(f"  改跑:uv run read.py --name {name} --rejoin")
        return

    print(f"── {len(messages)} 則未讀(全文)──")
    for msg in messages:
        print(f"\n#{msg['id']} {msg['from']}({msg.get('kind', '?')}):")
        print(msg["text"])
    print("\n──────────────")


def main() -> int:
    parser = argparse.ArgumentParser(description="發言器:對帳、呈現、送出,收成一個動作")
    parser.add_argument("--name", required=True, help="發言者")
    parser.add_argument("--expect", type=int, required=True,
                        help="你聲明自己讀到第幾則(= expect_last_id)——工具不替你猜")
    parser.add_argument("--file", help="訊息檔(長訊息、含反引號一律走這條)")
    parser.add_argument("--text", help="短訊息;含反引號會被擋下")
    parser.add_argument("--reply-to", type=int, help="回覆某一則的 id")
    parser.add_argument("--kind", default="agent", choices=["agent", "human"])
    parser.add_argument("--server", default=os.environ.get("A2A_SERVER", "http://127.0.0.1:8787"))
    parser.add_argument("--room", default=os.environ.get("A2A_ROOM", "main"))
    args = parser.parse_args()

    text = read_message(args, parser)
    if not text.strip():
        parser.error("沒有訊息內容")

    server = args.server.rstrip("/")
    url = (f"{server}/api/rooms/{args.room}/messages"
           f"?since_id={args.expect}&reader={args.name}")

    # ① 對帳 —— 起點是 agent 自己聲明的位置,不是 cursor 檔。
    data = json.load(urllib.request.urlopen(url))

    # ★ 判斷「該不該擋」時要把 agent 自己的訊息剔掉 —— AGENTS.md 步驟 2 明文:
    #   「撈回來只有 agent 自己的訊息,是敲鈴器與 cursor 之間的正常 race,屬於預期內的 no-op」。
    #
    #   不剔掉的話:agent 手動發過言、忘了推 cursor,下次就【被自己的話擋在門口】,
    #   而且它讀完自己說的話還是一樣被擋 —— 那不是保護,是死結。
    #
    #   ★★ 那把 expect 推過自己的訊息,算不算代寫聲明?不算,而且理由不是「無害」:
    #     那些話是 agent【自己寫的】,它必然看過 —— 推過去只是承認一件已經發生的事實,
    #     沒有替任何人聲明它沒做過的事。執法句照樣過得了關。
    others = [msg for msg in data["messages"] if msg["from"] != args.name]

    if others:
        # ★ 有別人的話就停在這裡,而且 cursor 一個字都不動(見檔頭)。
        #   我這段話是基於舊脈絡寫的,新訊息可能讓它變成重複、或者根本不必說 ——
        #   要不要改寫、要不要閉嘴,是判斷,不是機械。
        #
        #   呈現時照印【全部】(含自己的):判斷不算它們,不代表讀的時候要遮住它們。
        show(data["messages"], args.name, server, args.room)
        print(f"✗ 沒有送出。讀完上面這些,下次跑帶 --expect {data['last_id']} "
              f"—— 那個數字就是你在說「我讀了」。")
        return 1

    expect = data["last_id"] if data["messages"] else args.expect
    if data["messages"]:
        print(f"(對帳:{len(data['messages'])} 則未讀全是你自己發的 —— 正常 race,"
              f"expect 順過到 {expect})")
    else:
        print(f"(對帳:--expect {args.expect} 之後沒有未讀)")

    payload = {"from": args.name, "kind": args.kind, "text": text,
               "expect_last_id": expect}
    if args.reply_to:
        payload["reply_to"] = args.reply_to

    request = urllib.request.Request(
        f"{server}/api/rooms/{args.room}/messages",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        result = json.load(urllib.request.urlopen(request))
    except urllib.error.HTTPError as error:
        if error.code != 409:
            raise
        # ★ 撞車:對帳與送出之間有人插隊。不重送,也不動 cursor ——
        #   重新撈回來給 agent 看,決定權還在它手上。
        #   (409 本身不夾帶訊息,所以這裡真的要再撈一次。)
        fresh = json.load(urllib.request.urlopen(url))
        print(f"✗ 撞車了:你聲明讀到 #{args.expect},房間已經到 #{fresh['last_id']}")
        if fresh["messages"]:
            show(fresh["messages"], args.name, server, args.room)
        print(f"  讀完再決定要不要重發、怎麼發,下次帶 --expect {fresh['last_id']}")
        return 1

    # ★ 只有走到這裡才推 cursor:GET 是它親自做的、POST 是它親自成功的,
    #   「讀到這裡」這句話由它自己的動作證明,沒有代任何人聲明。
    #
    # ★★ cursor 住在 hub 上,而且【一房一份】—— 所以拿 --room 去別的房發言,
    #   推的是那個房的進度,不會再蓋掉本房的。
    #   (以前它是 state/cursor-<名字>.txt,不分房間:換房發言會把本房的進度
    #    蓋成一個小很多的數字,然後敲鈴器以為 agent 倒退了一千多則。)
    ack = urllib.request.Request(
        f"{server}/api/rooms/{args.room}/cursor/{urllib.parse.quote(args.name)}"
        f"?last_id={result['id']}",
        method="PUT")
    try:
        urllib.request.urlopen(ack)
        print(f"✓ 已送出 #{result['id']},cursor 已推")
    except urllib.error.HTTPError as error:
        # ★ 訊息【已經送出去了】,推 cursor 是後續動作 —— 這裡失敗不能假裝整件事失敗。
        #   誠實講出兩件事各自的結果,並給出補推的指令:
        #   cursor 沒推的後果只是敲鈴器會再敲一次(無害),而假裝沒送出去
        #   會讓 agent 重寫一遍同樣的話。
        print(f"✓ 已送出 #{result['id']},但 cursor 沒推成({error.code})。補推:")
        print(f"  curl -s -X PUT \"{server}/api/rooms/{args.room}"
              f"/cursor/{args.name}?last_id={result['id']}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
