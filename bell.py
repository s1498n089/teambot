"""bell(敲鈴器)— stdin 喚醒 wrapper。

用法:
    uv run bell.py --name alice [--server http://127.0.0.1:8787] [--room main] -- claude --resume

角色:本專案的預設喚醒機制 — 不靠 agent 自律重掛、不靠檔案旗子,
由本程式把 agent CLI 包成子行程(ConPTY / pty,TUI 體驗保真),
    ※ TUI = Text User Interface(文字使用者介面):像 BIOS 設定畫面或 menuconfig 那樣,
      在終端機裡用文字字元畫出框線、選單、顏色的介面。Claude Code 就是這種程式。
      這也正是本檔非用偽終端不可的原因 —— 純 CLI 用管線就夠了,是 TUI 逼出來的。
盯著 hub 的 SSE 直播當眼睛,發現「房間進度 > 該 agent 的 cursor」
就往子行程的 stdin 敲一行固定鈴聲。agent 看到鈴聲照對帳鐵則辦事。

設計要點:
- 鈴聲固定單行:BELL_PREFIX 開頭 + BELL_SUBMIT。**送出鍵一定要用 \r,不能用 \n** ——
  實測過:對真正的 CLI 送 \n,鈴聲只會躺在輸入框裡不送出,agent 永遠不會醒
- 自己發言不會被敲:發言後 cursor 已推進,last_id 不再領先(老規矩,零額外機制)
- 重敲保險:敲後 RE_RING_SECONDS 內 cursor 未推進且仍落後 → 再敲,上限 MAX_RINGS 次
  (agent 可能正在生成中漏聽;超過上限就安靜,印警告請人類看一眼)
- SSE 斷線自動重連(指數退避封頂),重連後若已落後立即補敲 — 對帳鐵則的 wrapper 版


═══ 這個檔案怎麼分層(改 code 前先看這張圖)═══

    ┌─ main() ──────────── 讀參數、決定要跑哪個平台
    │
    ├─ BellState ───────── 【決策層】什麼時候該敲?
    │                      只認得兩個數字:房間進度 vs 我的 cursor。
    │                      不知道自己活在 Windows 還是 Linux,
    │                      也不知道「敲」實際上是在做什麼。
    │
    ├─ sse_watch ───────── 【眼睛】盯 hub 直播,把房間進度餵給決策層
    ├─ re_ring_loop ────── 【節拍器】定時催決策層再想一次(SSE 靜默時的保險)
    │
    └─ run_windows / run_posix ── 【平台層】真的把 agent CLI 開起來,
                                  並提供「怎麼往它 stdin 寫字」這個能力

分層的意義,就是韌體的 HAL(硬體抽象層):
上層只認得一個 `ring_fn()`,呼叫它就會響 —— 底下是 ConPTY 還是 pty,上層一無所知。
要支援第三種終端,只要再寫一個 run_xxx 提供 write 能力,BellState 一行都不用改。

**兩層之間唯一的連結是一個函式**,這件事在 main() 的 state_factory 那裡有更完整的說明
(包括「為什麼不能在 main 裡直接把 BellState 建好」)。

另外有一條規矩貫穿全檔,違反了會出大事:
    **敲鈴器只『讀』cursor,永遠不寫。**
寫 cursor 的是 agent 自己(它讀完訊息後自己更新)。
如果敲鈴器也能寫,它就能把「你已經讀了」這件事偽造出來 ——
等於自己把叫醒你的證據銷毀掉。所以整個檔案裡找不到任何一行寫 cursor。


═══ 這條技術鏈的源頭:一個產品需求 ═══

會走到偽終端這麼底層的東西,不是技術品味,是被一句需求逼出來的:

    「不要 headless(無畫面模式)—— 過程必須看得見」
        ↓ 所以要跑真正的 TUI,不能用只吐文字的批次模式
        ↓ 所以要給子行程一條真的 tty
        ↓ 所以要用偽終端(Windows 的 ConPTY / Unix 的 pty)
        ↓ 所以有了這個檔案

這個需求的理由:這套系統要當「虛擬 AI 團隊」,人類得能旁觀、能中途插話、
能一眼看出誰卡住了。headless 做不到「看著它工作」,出事只能事後翻 log。

★ 反過來說,**這整條鏈是可以一起拆掉的**。
  哪天需求變成「跑在雲端,沒人看畫面」,那 ConPTY、pty、VT 模式、
  終端機還原(TERM_RESTORE)……全部都可以刪 —— 它們全部只為「看得見」而存在。
  要拆就整串拆,別東挑一塊西挑一塊,那會拆出一個半殘的東西。


═══ 偽終端(pseudo-terminal / PTY)是什麼,為什麼非用它不可 ═══

先講「終端機」這個字的來歷:早年那真的是一台機器 —— 一台打字機接上主機,
你敲鍵盤、它印紙。那台東西叫 teletype,縮寫 tty,這個字一路活到今天。
程式從 tty 讀輸入、往 tty 寫畫面,還可以反過來問它:你多寬?幾行?支援顏色嗎?

今天當然沒有那台機器了。你用的 Windows Terminal / iTerm 是「終端機模擬器」——
一個視窗程式,假裝自己是那台老機器。

**偽終端就是作業系統提供的一條「假的線」**,兩端各給一個人拿:

    子行程(claude)拿【從屬端】 ← 它看到的是一個正常的 tty,完全不知道對面是誰
    bell.py 拿【主控端】        ← 誰拿這端,誰就在扮演「坐在鍵盤前的人類」

所以 claude 以為自己接在真終端上、以為對面是人在打字;實際上對面是我們。
(這兩端在 code 裡的原文是 master / slave,Python 的 pty 模組仍沿用這組字。)

**為什麼不能用一般的管線(pipe)就好?** 因為程式會發現自己不是接在 tty 上,
接著會發生四件事,每一件都足以毀掉體驗:

    1. 它會關掉顏色、關掉整個互動式介面,改用「給程式讀的」純文字批次輸出
    2. 它問不到視窗大小 → 不知道畫面幾行幾列 → 沒辦法畫框、沒辦法排版
    3. Ctrl+C 不再變成中斷訊號、方向鍵不再被解讀成方向鍵
    4. 輸出從「一有東西就吐」改成「攢一大塊才吐」→ 畫面一卡一卡

一句話:**要讓畫面跟你自己開 claude 一模一樣,就必須給它一條真的 tty,
而偽終端是唯一能造出這種東西的辦法。**

給韌體背景的對照:這幾乎就是 **USB-to-UART 橋接晶片**。
MCU 那端看到標準 UART(有 TX/RX、有 baud rate),PC 那端看到一個 COM port,
中間根本沒有真的串列線 —— 但兩邊的驅動都不必改一行,因為兩邊都感覺不出來。
偽終端就是作業系統內建的這顆橋接晶片。
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path

from envfile import load_env_file

BASE = Path(__file__).resolve().parent

BELL_PREFIX = "[A2A-BELL]"      # 文件教的是「凡見這個前綴一律對帳」,不能動
# 送出鍵。★ 一定是 \r 不是 \n:實測對真正的 CLI 送 \n,那行字會躺在輸入框裡
# 不送出,agent 就永遠不會醒 —— 而且畫面上看起來「鈴有敲到」,最難查的那種。
BELL_SUBMIT = "\r"
BELL_SUBMIT_GAP = 0.1       # 鈴聲的字與送出鍵之間隔多久(見 guarded_write)
RE_RING_SECONDS = 90        # 【同一批】敲後多久 cursor 仍未推進就重敲
PATROL_SECONDS = 5          # 節拍器一圈;也是【有新訊息時】的最短敲鈴間隔
MAX_RINGS = 3               # 同一段落後最多敲幾次,之後改印警告(不騷擾設計)
SSE_READ_TIMEOUT = 60       # server 每 15 秒有 keep-alive,60 秒沒動靜視為死連線
RECONNECT_MAX_BACKOFF = 30


def bell_line(name: str, room_last_id: int) -> str:
    """一般鈴聲:房間往前了。

    ★ 為什麼帶名字:**那是 agent 唯一查得到自己是誰的地方。**
      敲鈴器用偽終端把 agent 包起來,被包住的行程看不見自己是被誰、用什麼名字啟動的
      (`--name` 只在敲鈴器自己的 argv 裡)。名字錯了,agent 會去讀別人的進度、
      用別人的身分發言。而鈴聲每次重講一次,對話被壓縮之後也救得回來。

    ★ 為什麼是「房間到 #N」而不是「cursor updated」:**後者是假話。**
      敲鈴器【只讀 cursor,永遠不寫】(見檔頭的鐵則)——
      沒有人更新過 agent 的 cursor,更新它是 agent 自己的事。
      句子講的必須是這支程式真的做過的事。

    ★ 這個數字過時是無害的:它是【下限】。敲出去的瞬間房間可能又前進了,
      但 agent 拿自己的 cursor 去對帳,會拿到 ≥ 這個數字的全部。
      它講的是「房間在哪」,不是「你讀到哪」—— 不可能被誤讀成已經追平。
    """
    return f"{BELL_PREFIX} 房間到 #{room_last_id}(你是 {name})"


def force_bell_line(name: str) -> str:
    """人類按下的強制敲鈴 —— 刻意跟一般鈴聲說不一樣的話。

    ★ 為什麼不能共用同一句:**強制敲鈴最常見的情況是「對帳為空」。**

      而 AGENTS.md 教「撈回來是空的就是正常 race,不用疑惑也不用回報」——
      於是 agent 會醒來、撈到空、【安靜回去睡】。
      但按下按鈕的人想講的是「你好像沒反應」,結果它變得更沒反應。

      **文字不同,agent 才知道這一下是人按的**,
      而 AGENTS.md 對這一種另有規定:就算對帳是空的也要回一句。
    """
    return f"{BELL_PREFIX} 使用者強制敲鈴(你是 {name})"


LOG_PATH: Path | None = None  # main() 依 --name 指定;None 時退回 stderr(僅啟動失敗前)

# 離場時把終端機交還乾淨。TUI 子行程開了一堆終端機私有模式,若在它(或我們)退出時
# 沒關,狀態會留給下一個程式 —— 最明顯的災情是 win32-input-mode 沒關,退回 PowerShell 後
# 每個按鍵都被編成 ESC[...;0;1_ 印成亂碼,鍵盤等同壞掉。
# 送出順序有講究:序列要在還原 console mode 之前送(那時 VT 輸出處理還開著),否則會被當字面印出來。
TERM_RESTORE = (
    "\x1b[?9001l"                                    # win32-input-mode off(亂碼元凶)
    "\x1b[?1004l"                                    # focus reporting off(會吐 ESC[I / ESC[O)
    "\x1b[?2004l"                                    # bracketed paste off
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"   # mouse tracking off
    "\x1b[?1049l"                                    # 離開 alternate screen
    "\x1b[?25h"                                      # 游標顯示(TUI 常藏起來)
    "\x1b[0m"                                        # 顏色與屬性歸零
)


def log(msg: str) -> None:
    """敲鈴器狀態訊息寫檔(state/bell-<名字>.log)— stderr 與子行程 TUI 共用終端,
    直印會插進畫面甚至斬斷 VT 序列造成花屏,故一律落檔。"""
    line = f"[bell {time.strftime('%H:%M:%S')}] {msg}\n"
    if LOG_PATH is None:
        sys.stderr.write(line)
        return
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # log 寫不進去不能反過來炸掉轉發


def guarded_write(write_fn, *payloads, lock, gap: float = 0.0) -> bool:
    """所有「寫進子行程」的唯一閘門(打字與鈴聲共用一把鎖,互不插隊)。

    payloads 是【依序寫入的幾段】,段與段之間隔 gap 秒。

    ★ 為什麼要有「分段」這個能力:有些 TUI(Codex 就是)會把「一大串字瞬間湧入、
      尾巴夾著 \r」判定成【貼上】,而貼上裡的換行是換行、不是送出 ——
      於是鈴聲整行躺在輸入框裡,agent 永遠不會醒。中間隔 0.1 秒就打破那個判定。

    ★★ 為什麼分段一定要在【這裡】、而不是呼叫端連呼叫兩次:
      鎖會在兩次呼叫之間放開,使用者打到一半的字就插進「鈴聲」與「Enter」中間 ——
      送出去的會是一行殘缺的鈴聲加半句人話。分段必須是一次持鎖內的原子動作。

      代價誠實寫在這:多段時持鎖從「瞬間」變成 gap 秒,使用者那 0.1 秒打的字會排隊。
      鈴聲不常敲、0.1 秒也感覺不到,拿它換「絕不交錯」划算。

    ★ 用可變參數而不是「收一個序列」是刻意的:序列版傳字串進來會被【逐字迭代】,
      靜默變成一個字寫一次 —— 那種 bug 不會報錯,只會讓人看到奇怪的輸入。

    吞掉寫入失敗是刻意的:子行程一退出,pty/master 隨即關閉,而此刻仍可能有東西要寫 ——
    退出瞬間殘留在終端機緩衝的按鍵、或剛好撞上的鈴聲。isalive() 擋不住這個空檔
    (檢查與寫入之間子行程就死了),所以退出時噴 EOFError('Pty is closed') 堆疊
    其實是「正常終局被當成錯誤」。回傳是否真的寫進去了。

    但「吞」的安全性有前提,講清楚免得後人誤以為吞本身無害(bob 驗收時點破的因果):
    子行程還活著時的罕見暫時性寫失敗也會被吞,那一次鈴聲就此蒸發。之所以不會變成
    「永久聾掉」,是因為 BellState 的重敲機制兜底 —— 90 秒內 cursor 沒推進就再敲、
    三次未果就喊人。最壞情況是鈴晚到一輪,不是叫不醒。
    換句話說:要動重敲機制之前,先回來看這段 —— 拆了它,吞就不再安全。
    """
    with lock:
        try:
            for index, payload in enumerate(payloads):
                if index:
                    time.sleep(gap)
                write_fn(payload)
            return True
        except (EOFError, OSError, ValueError):
            return False  # ValueError:POSIX 端 master fd 已被關閉


class BellState:
    """鈴聲狀態機:知道房間進度(SSE 餵)與 agent 進度(cursor 檔),決定何時敲。

    這是整個檔案的【決策層】,也是最該保持乾淨的一塊。它只認得兩個數字:

        known_last_id  房間現在最新是第幾則(眼睛 sse_watch 餵進來)
        cursor         這個 agent 讀到第幾則(從檔案讀)

    前者比後者大 = 有沒看過的訊息 = 該敲。就這麼簡單,沒有別的條件。

    它刻意不知道的事(這些「不知道」就是分層的價值):
    - 不知道自己跑在 Windows 還是 Linux
    - 不知道「敲」實際上是把字寫進 ConPTY、pty、還是別的什麼東西
    - 不知道 agent 是 Claude 還是 Codex

    它只知道「手上有個 ring_fn,呼叫它就會響」—— 這正是韌體的 HAL:
    上層邏輯只認得 send(),不管底下是 UART 還是 SPI。

    ring_fn 是從外面【傳進來】的,不是自己建的(這叫依賴注入),
    因為只有平台層才知道怎麼往那個特定的子行程寫字。建構的時機問題見 main() 裡的說明。

    ★ 這裡永遠只讀 cursor,不寫。寫是 agent 自己的事。
      理由:能寫就能偽造「你已經讀過了」,等於自己銷毀叫醒你的證據。
      (這個「一邊寫、另一邊讀、彼此不協調」的結構,來歷見 doc/TUTORIAL.md 第 3 章)
    """

    def __init__(self, cursor_path: Path, ring_fn, name: str, server: str, room: str):
        self.cursor_path = cursor_path
        self.ring_fn = ring_fn

        # 這三個是「我是誰、要盯哪台的哪個房間」——sse_watch 會用到。
        # ★ 它們收在建構式裡,不是建好之後才從外面塞(state.name = ...)。
        #   從外面塞也能跑,但讀這個類別的定義看不出它有這些欄位,
        #   而且漏塞其中一個,錯誤要等到 sse_watch 連線時才炸。
        #   收進建構式之後,「少給一個就建不起來」變成結構保證,不必靠記性。
        self.name = name
        self.server = server
        self.room = room

        self.known_last_id = 0
        self.rings_this_gap = 0
        self.last_ring_at = 0.0
        self.rang_for_id = 0        # 上次是為了哪一則敲的 —— 分辨「同一批」與「新內容」
        self.warned = False
        self.lock = threading.Lock()

    def read_cursor(self) -> int:
        """讀這個 agent 的進度書籤。讀不到就回 0。

        ★ 回 0 的意思是「當作他一則都沒讀過」,所以他會被叫醒。這是刻意選的方向:

            回 0        → 最壞情況是白醒一次(無害,他對帳後發現沒新訊息就回去睡)
            回最新編號  → 最壞情況是【永遠不叫他】,而且安靜到沒有人會發現

        兩種錯的代價差很多,所以寧可吵也不要漏 —— 檔案讀不到(還沒建立、
        權限問題、內容壞掉)本來就是異常狀態,異常時要往「會被注意到」的方向倒。
        別把它「優化」成安靜的那一邊。
        """
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def on_message(self, msg_id: int) -> None:
        """SSE 每收到一則訊息呼叫一次。

        ★ 這裡【不看訊息內容】,只看 id。誰被點名、講了什麼,決策層一概不知道 ——
          它只比兩個數字:房間到哪、這個 agent 讀到哪。
        """
        with self.lock:
            self.known_last_id = max(self.known_last_id, msg_id)
        self.evaluate()

    def evaluate(self) -> None:
        """判斷「現在該不該敲」。

        ★ 這個方法【被呼叫得很頻繁】,但真正敲下去的次數很少。
          呼叫它的有兩邊:收到新訊息時(sse_watch)、以及每 5 秒一次(re_ring_loop)。

        會敲下去,得先通過三道閘門 —— 任何一道擋下都直接返回:

            1. 你追上了嗎?    cursor 已經不落後 → 不敲,順便把計數歸零
            2. 敲滿三次了嗎?  已經敲了 MAX_RINGS 次 → 不敲,只印一次警告就閉嘴
            3. 才剛敲過嗎?    距離上次太近 → 不敲

        ★★ 閘門二與三都先問同一件事:**從上次敲到現在,房間有沒有前進?**

               有新內容  額度重新計算(閘二歸零),最短間隔只要一圈巡邏
               同一批    維持原本的不騷擾:三次封頂、90 秒才重敲

          為什麼非這樣不可 —— 綁錯對象會壞掉兩次:

            閘二綁「落後」 → 敲滿三次就【永久】安靜,而解除需要 cursor 追上,
                             追上又需要被敲醒。死結,從外面看就是這個 agent 死了。
            閘三綁「時間」 → 敲完之後 90 秒內【新來的訊息】也被當成舊 backlog,
                             使用者連發四則會一聲都沒有(2026-07-29 實地重現)。

          兩個 bug 同一個根:**「要不要吵你」看的該是「有沒有新東西」,
          不是「你落後多久」。**

        所以實際的節奏是這樣的:

            發現落後 → 立刻敲第 1 次
                    → 90 秒後還沒動 → 敲第 2 次
                    → 再 90 秒     → 敲第 3 次
                    → 再過去       → 安靜,印一行警告請人類看一眼

        中間那幾十次每 5 秒的檢查,全部在第三道閘門就返回了 ——
        **每 5 秒是「檢查」,不是「敲」。**
        """
        with self.lock:
            cursor = self.read_cursor()

            # ── 閘門一:追上了就沒事,順便把狀態歸零,下次落後才能重新從第 1 次算起 ──
            if self.known_last_id <= cursor:
                if self.rings_this_gap:
                    log(f"cursor 已追上(={cursor}),鈴聲歸位")
                self.rings_this_gap = 0
                self.warned = False
                self.rang_for_id = self.known_last_id
                return

            now = time.monotonic()
            fresh = self.known_last_id > self.rang_for_id   # 上次敲之後又有新訊息

            # ── 閘門二:敲滿了就不再騷擾,但要留一行紀錄讓人類知道有人卡住 ──
            #    warned 這個旗標是為了「只印一次」—— 否則每 5 秒就會刷一行同樣的警告。
            #    ★ 有新內容就把額度還回來:新訊息是【新的敲鈴理由】,
            #      不該被上一批的額度綁住(否則永久噤聲的死結解不開)。
            #
            #    ★★ 這個修法【拿掉了什麼】,要一起記住:
            #      額度歸零的同時,下面那行 WARN 在【活躍的房間裡就永遠不會印】——
            #      訊息一直來,額度一直歸零,到不了 MAX_RINGS。
            #      所以它只在安靜的房間有效;熱鬧的房間要判斷「敲了但沒醒」,
            #      得手動看 log:【「叮咚」有記、cursor 卻不動】就是敲進去沒送出。
            #      不要以為有自動警報可以靠。
            if fresh:
                self.rings_this_gap = 0
                self.warned = False
            if self.rings_this_gap >= MAX_RINGS:
                if not self.warned:
                    log(f"WARN 已敲 {MAX_RINGS} 次仍未見 cursor 推進"
                        f"(room={self.known_last_id} cursor={cursor})— agent 可能卡住,請人類看一眼")
                    self.warned = True
                return

            # ── 閘門三:才剛敲過 —— 這道擋掉了絕大多數的呼叫 ──
            #    (agent 可能正在生成中,給它時間反應,不要連珠炮)
            #    ★ 間隔看的是「有沒有新東西」:
            #        同一批   90 秒(等它回應那一批)
            #        新內容   一圈巡邏就好 —— 使用者剛講的話不該等一分半
            #      連發多則仍然只敲一次:它們在同一圈巡邏內到達,
            #      而 PATROL_SECONDS 的間隔把它們併成一次。
            gap = PATROL_SECONDS if fresh else RE_RING_SECONDS
            if self.last_ring_at and now - self.last_ring_at < gap:
                return

            # ── 三道都過了,真的敲下去 ──
            delivered = self.ring_fn(bell_line(self.name, self.known_last_id))
            self.rang_for_id = self.known_last_id
            # ★ 送不進去也照樣計數。這樣子行程已經死掉時,才不會變成無限重敲狂刷紀錄檔。
            self.rings_this_gap += 1
            self.last_ring_at = now
            if delivered is False:  # 只認明確的 False;回 None 的 ring_fn 視為沒回報
                log(f"WARN 鈴聲沒送進子行程 #{self.rings_this_gap}"
                    f"(room={self.known_last_id} cursor={cursor})— pty 可能已關,靠重敲兜底")
            else:
                log(f"叮咚 #{self.rings_this_gap}(room={self.known_last_id} cursor={cursor})")


    def force_ring(self) -> None:
        """人類從觀戰 UI 按下的「強制敲鈴」—— 繞過三道閘,並把不騷擾的計數歸零。

        ★ 為什麼要有一條繞過去的路:

          那三道閘是為了「不吵一個正在生成中的 agent」而設的,而觸發閘門二
          (連敲三次沒反應就安靜)的不一定是「卡住」,也可能只是「正在忙」。
          一旦安靜下來,要重新開始敲得等 cursor 追上 —— 而追上需要被敲醒。
          **那是一個死結,而且從外面看起來就是「這個 agent 對整個聊天室沒反應」。**

          人類看得見畫面,他比計數器清楚該不該吵。所以這條路把判斷權交給他。
        """
        with self.lock:
            self.rings_this_gap = 0      # 歸零:這一下不算在不騷擾額度裡
            self.warned = False
            self.last_ring_at = time.monotonic()
            self.rang_for_id = self.known_last_id
            delivered = self.ring_fn(force_bell_line(self.name))
        log(f"叮咚(強制,人類要求){'' if delivered is not False else ' —— WARN 沒送進子行程'}")


def sse_watch(server: str, room: str, state: BellState, child_alive) -> None:
    """眼睛:掛在 hub 的直播上,一有新訊息就通知決策層。

    ═══════════ 這個函式是怎麼跑的 ═══════════

    先講最重要的一件事,因為它最違反直覺:

        **這裡有兩層迴圈,而程式幾乎整天都待在【內層】。**

        外層 while  → 負責「重連」,只在出事時才轉一圈
        內層 for    → 負責「讀訊息」,★ 它會停在那裡等,一等可能好幾小時

    所以外層那個 while 雖然寫在外面、看起來像主迴圈,其實它跑得最少。

    ── 一天的作息長這樣 ──

        08:00  啟動 → 進 while → 連上 → 進 for
        08:00  ┐
               │ 一直待在 for 裡面(收訊息、收心跳、收訊息……)
        18:00  ┘
        18:00  網路斷了 → 拋錯 → 進 except → 等 1 秒
        18:00  回到 while → 重連 → 又進 for → 再待到下次出事

    ── 逐步流程(下面的程式碼有對應的編號)──

        ① 外層迴圈:只要子行程還活著,就一直重複「連線 → 讀 → 斷了再連」
        ② 組網址,帶上兩個關鍵參數:
             since_id = 我讀到哪  ← 斷線期間漏掉的,伺服器會從這裡補給我
             watcher  = 我是誰    ← 讓 hub 知道我在線上(網頁的在場名單靠這個)
        ③ 連上去,並把等待時間歸零(因為連得上,代表對方活著)
        ④ ★ 讀訊息的迴圈 —— 它會【卡在這裡等】,不是讀完就結束
        ⑤ 每讀到一行先確認 claude 還在,不在就整個收工(用 return 不是 break,理由見下)
        ⑥ 只有 "data: " 開頭的才是真訊息,其餘(心跳)略過
        ⑦ 通知決策層「房間到第幾則了」,該不該敲鈴由它判斷
        ⑧ 連線出事了 —— 被關掉、網路斷、或太久沒動靜,都會掉到這裡
        ⑨ 等一下再重連,而且每次等更久(理由見下)

    ── 怎樣才會離開內層那個 for?只有兩條路 ──

        拋錯     → 進 except → 等一下 → 回到 while,重連
        return   → 直接離開整個函式,這條執行緒收工

        **for 自己永遠不會跑完。** 伺服器不關連線,它就一直掛著。

        ★ 第二條用 return 而不是 break 是刻意的:claude 都關掉了,
          重連沒有任何意義,直接走比掉回 while 再判斷一次乾淨。

    ── 為什麼「太久沒動靜」算出事?伺服器不是沒訊息就不送嗎? ──

        不是。hub 每 15 秒會送一個空的心跳訊號,就算沒人講話也照送
        (那是為了避免中間的路由器把安靜的連線當成死的切掉)。

        所以這條線幾乎不會安靜超過 15 秒。
        而我們的逾時設 60 秒 —— 那不是「總共只能連 60 秒」,
        是「**兩次收到資料之間**最多等 60 秒」。等於容忍連續漏掉三次心跳,
        網路偶爾卡一下不會誤判斷線。

    ── 為什麼等待時間要越等越久(指數退避)? ──

        如果 hub 掛了,你每秒重連一次,等於在對方最脆弱的時候一直敲門。
        所以等待時間是 1 → 2 → 4 → 8 → 16 → 30 → 30…(封頂 30 秒)。
        封頂是另一個考量:不能無限加倍,否則對方復活了你還要等好幾分鐘才發現。

    ── 一個平台差異,讀這裡的人容易漏掉 ──

        `child_alive` 在 Windows 是真的去問子行程還在不在;
        **但在 POSIX 端它永遠回 True**(見 run_posix 裡 alive() 的說明)。

        也就是說:**POSIX 上這個函式的外層迴圈永遠不會自己結束**,
        連 ⑤ 那個 return 也不會發生。它靠的是執行緒被標記為 daemon ——
        主迴圈(讀子行程輸出那個)收工時,整個程式退出,這條執行緒跟著被回收。

        這不是疏漏,是兩邊各自選了最省事的做法。但如果你在改結束邏輯,
        要記得「有一邊根本不看 child_alive」。

    ── 最後,這裡藏著整個專案的鐵則 ──

        重連時帶的是 since_id=**cursor**(我真正讀到哪),
        不是「我上次連線時看到哪」。所以就算斷線期間漏了一百則,
        一連上就全部補回來 —— 這就是「通知可以漏,資料不會丟」的 wrapper 版。
    """
    backoff = 1
    while child_alive():                                            # ①
        # kind=agent 是這條連線的自我宣告:「我後面包的是一個 AI」。
        # hub 的名冊就是這樣長出來的 —— 沒有註冊手續、沒有名單檔案,
        # 連著線就算在,線一斷就不算。人類用瀏覽器連進來時不會帶這個參數。
        url = (f"{server}/api/rooms/{room}/stream?since_id={state.read_cursor()}"
               f"&watcher={state.name}&kind=agent")                 # ②
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT) as resp:
                log(f"SSE 已連線 {server} #{room}")                  # ③
                backoff = 1
                for raw in resp:                                    # ④ ★ 會卡在這裡等
                    if not child_alive():
                        return                                      # ⑤
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):                   # ⑥
                        try:
                            payload = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue                       # keep-alive 之類,略過
                        # 人類按的「強制敲鈴」:只認指名自己的那一則
                        if payload.get("type") == "ring":
                            if payload.get("target") == state.name:
                                state.force_ring()
                            continue
                        try:
                            state.on_message(int(payload["id"]))    # ⑦
                        except (KeyError, ValueError, TypeError):
                            pass  # 非訊息 payload,略過
        except OSError as exc:                                      # ⑧
            # 被關掉(ConnectionResetError)、網路斷、太久沒動靜(TimeoutError)——
            # 這些全都是 OSError 的子類,所以一句就接得住。
            # 對我們來說它們是同一件事:這條線不能用了,重連一次。
            log(f"SSE 斷線({exc}),{backoff}s 後重連")
            time.sleep(backoff)                                     # ⑨
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)


def re_ring_loop(state: BellState, child_alive) -> None:
    """重敲節拍器:每 5 秒 evaluate 一次 — 落後未回應者到點重敲(SSE 靜默時也會跑)。"""
    while child_alive():
        time.sleep(PATROL_SECONDS)
        state.evaluate()


# ---------- Windows(主戰場):ConPTY via pywinpty ----------
#
# ConPTY = Windows 版的偽終端(概念見檔案頂端)。它很年輕:
# Unix 有偽終端幾十年了,**Windows 直到 2018 年秋的 Windows 10 更新才第一次有**。
#
# 在那之前,Windows 上的第三方終端機必須做一件荒謬的事 —— 微軟自己這樣描述:
#     「被迫開一個螢幕外的 Console,把使用者輸入送進去,再把它的畫面『刮』出來,
#       重畫到自己的視窗上。」
# 微軟列出的後果是:不穩定、崩潰、資料損毀、格式全丟。
#
# 換句話說:**這個專案能在 Windows 上成立,是因為 2018 年那次更新。**
# 再早幾年,「包住一個 TUI 程式又保持畫面原樣」在 Windows 上根本做不到。
#
# 我們沒有直接呼叫 ConPTY 的 Win32 API,而是透過 pywinpty 這個套件(winpty 3.0.5)。

def run_windows(cmd: list[str], state_factory) -> int:
    """Windows 版:開起 agent CLI,並在它和使用者之間當中間人。

    ═══ 這個函式怎麼讀(裡面有很多是照抄的樣板,別平均用力)═══

    【要懂的】這五件是設計,你改東西時會踩到:
      1. 為什麼要開 VT —— 不開的話,子行程畫的畫面會變成滿螢幕垃圾字
      2. 為什麼先記舊設定再還原 —— 不還原的話,你退出後鍵盤會壞掉(真的發生過)
      3. 為什麼要關掉「等 Enter 才送出」—— 不關的話 TUI 完全不能用
      4. 那把鎖為什麼存在 —— 打字和敲鈴是兩個執行緒,同時寫會打架
      5. 最後啟動的四個背景執行緒各做什麼 —— 那才是這個函式真正的骨架

    【知道有這回事就好】不必記細節:
      · ctypes + kernel32 = Python 在直接請 Windows 本人做事
      · handle = Windows 發給資源的號碼牌
      · `|` 是打開某個開關,`& ~` 是關掉某個開關

    【完全不用記】Windows 的規矩,照抄即可:
      · -11 / -10 這些編號、0x0004 / 0x0200 這些常數值
      · byref、DWORD 這些 C 語言慣例
      · VT_KEYS 那張鍵碼對照表的內容
    """
    # 這些 import 刻意寫在函式裡面,不放檔案頂端 —— 理由見 run_posix 那邊的說明。
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from winpty import PtyProcess

    # kernel32.dll 是 Windows 最核心的系統函式庫。以下每一個 kernel32.XXX
    # 都是在請 Windows 本人做事,不是 Python 的功能。(照抄)
    kernel32 = ctypes.windll.kernel32

    # ══ 第一件事:借用終端機,並記住原本的樣子 ══
    #
    # 我們接下來要改主控台的行為(讓它看懂 VT 暗號、讓按鍵原始傳遞)。
    # ★ 但那是【跟使用者借的】,離場時必須原封不動還回去 ——
    #   所以每改一個設定之前,都要先把舊值讀出來存著。
    #   不還的話,你退出後鍵盤會吐亂碼,等同壞掉。(restore_terminal 就是在還)

    # ── 輸出方向:讓主控台看懂 VT 暗號 ──
    # 不開的話,子行程送出的 ESC[31m(把字變紅)會被當普通文字印出來,
    # 你會看到滿螢幕的 ←[31m 垃圾。
    hout = kernel32.GetStdHandle(-11)                       # 拿輸出的號碼牌(-11 照抄)
    out_mode = wintypes.DWORD()                             # 準備一個空盒子接答案(照抄)
    kernel32.GetConsoleMode(hout, ctypes.byref(out_mode))   # ★ 先讀舊值,離場要還
    # `|` = 在原有開關上多開一個。直接寫 =0x0004 會把使用者其他設定全關掉。
    kernel32.SetConsoleMode(hout, out_mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

    # ── 輸入方向:讓按鍵原封不動傳給子行程 ──
    #
    # 這裡打開一個、關掉三個。三個都要關,少關一個就會壞掉:
    #   關 LINE(0x0002)  → 不然 Windows 會【等你按 Enter】才把整行交出去,TUI 完全不能用
    #   關 ECHO(0x0004)  → 不然你的字會顯示兩次(Windows 印一次、claude 自己再印一次)
    #   關 PROCESSED(0x0001)→ 不然 Ctrl+C 會被 Windows 攔走,claude 根本收不到
    # 打開 VT_INPUT(0x0200)→ Windows 會把組合鍵翻成標準暗號再送出。
    #   舊做法 getwch 只回「字元」,Shift+Tab 和 Tab 一樣都是 \t,修飾鍵資訊整個丟失;
    #   而 TUI 要的 Shift+Tab 是 ESC[Z —— 開了這個才拿得到。
    hin = kernel32.GetStdHandle(-10)                        # 拿輸入的號碼牌(-10 照抄)
    in_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(hin, ctypes.byref(in_mode))     # ★ 一樣先讀舊值
    # `& ~(...)` = 把括號裡那幾個開關關掉;`| 0x0200` = 把 VT 輸入打開。
    raw_mode = (in_mode.value | 0x0200) & ~(0x0001 | 0x0002 | 0x0004)  # +VT_INPUT -PROCESSED -LINE -ECHO
    # SetConsoleMode 會回報成功或失敗。太舊的 Windows 不支援 VT 輸入 → 失敗 →
    # 下面 pump_input 會自動改走 getwch 那條退路。
    vt_input = bool(kernel32.SetConsoleMode(hin, raw_mode))
    old_in_cp = kernel32.GetConsoleCP()                     # ★ 字碼頁也是借的,一樣要記
    if vt_input:
        # 65001 是 UTF-8 的字碼頁編號。這台機器預設是 cp950(繁中),
        # 不改的話中文輸入法打的字會亂碼。
        kernel32.SetConsoleCP(65001)

    def restore_terminal() -> None:
        """離場清潔工:把上面借的三樣東西全部還回去。

        少了這段,你退出後每個按鍵都會被印成 ESC[...;0;1_ 這種序列,鍵盤等同壞掉。
        """
        try:
            sys.stdout.write(TERM_RESTORE)   # 先送「關掉各種模式」的暗號
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        # ★ 順序不能顛倒:上面那些暗號要在【還原設定之前】送出。
        #   一旦把設定還原了,VT 輸出處理就關掉了,那些暗號會被當普通文字印在螢幕上。
        kernel32.SetConsoleMode(hin, in_mode.value)         # 還輸入設定
        kernel32.SetConsoleMode(hout, out_mode.value)       # 還輸出設定
        kernel32.SetConsoleCP(old_in_cp)                    # 還字碼頁

    # ══ 第二件事:開一條偽終端,把 agent CLI 放進去 ══

    cols, rows = shutil.get_terminal_size()                 # 問「這個視窗現在幾行幾列」
    # 開偽終端 + 啟動子行程。注意 dimensions 是 (rows, cols),跟上一行順序相反。
    # cwd 設成專案目錄,子行程才找得到 state/、chat.jsonl 這些檔案。
    proc = PtyProcess.spawn(cmd, dimensions=(rows, cols), cwd=str(BASE))

    # ★ 這把鎖是必要的:等一下會有【兩個執行緒】同時想往子行程寫字 ——
    #   一個是你打字(pump_input),一個是敲鈴(SSE 執行緒)。
    #   沒有鎖的話,鈴聲可能插進你打到一半的字中間,兩邊都變成亂碼。
    write_lock = threading.Lock()

    def safe_write(*parts: str) -> bool:
        """所有「寫進子行程」都要走這裡 —— 上鎖 + 吞掉子行程已死的錯誤。

        收多段是給鈴聲用的(字一段、送出鍵一段);打字只傳一段,行為跟以前一模一樣。
        """
        return guarded_write(proc.write, *parts,           # 回傳透傳給鈴聲,失敗才有案可查
                             lock=write_lock, gap=BELL_SUBMIT_GAP)

    # 到這裡才建 BellState,因為現在才有 safe_write 可以交給它。
    # (為什麼不能在 main() 就建好,見 main() 裡 state_factory 的說明)
    state: BellState = state_factory(safe_write)

    def alive() -> bool:
        """子行程還活著嗎?下面每個迴圈都靠它決定要不要繼續。"""
        return proc.isalive()

    # ══ 第三件事:定義五個「幫浦」,等一下讓它們各自跑起來 ══

    def pump_output() -> None:
        """子行程的畫面 → 你的終端機。(TUI 保真的一半)

        它讀出來的是子行程畫的原始內容(含 VT 暗號),原封不動印出去 ——
        因為前面已經請主控台看懂那些暗號了,所以畫面會正確呈現。
        """
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                break                      # 子行程結束了,這個迴圈就該收工
            if data:
                sys.stdout.write(data)
                sys.stdout.flush()         # 立刻吐出去,不然畫面會一卡一卡

    # 舊路徑專用的鍵碼對照表(照抄,不用記內容):
    # getwch 遇到方向鍵這類特殊鍵時,會回傳兩個字元 —— 先給 \x00 或 \xe0 當前綴,
    # 再給第二碼。這張表就是把第二碼翻成 TUI 認得的 VT 暗號。
    VT_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D",
               "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~", "R": "\x1b[2~",
               "I": "\x1b[5~", "Q": "\x1b[6~"}

    def pump_input() -> None:
        """你的鍵盤 → 子行程。(TUI 保真的另一半)

        有兩條路,取決於前面 VT 輸入模式有沒有開成功:
          主路徑:直接讀原始位元組,Shift+Tab、Ctrl+方向鍵這些組合鍵全部保真
          退路  :舊的 getwch 逐鍵讀,中文輸入沒問題,但修飾鍵資訊有限
        """
        if vt_input:
            # ★ 這個 decoder 是為中文而存在的:
            #   一個中文字佔 3 個位元組,而 os.read 一次最多讀 1024 個 ——
            #   如果某個字剛好被切在邊界上,直接解碼會變成 � 亂碼。
            #   incremental decoder 會把「還沒湊齊的殘餘位元組」留著,等下一次讀進來再拼。
            #   （貼上一大段中文時就會踩到這個。）
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while proc.isalive():
                try:
                    data = os.read(0, 1024)    # 0 = 標準輸入的檔案編號(照抄)
                except OSError:
                    break
                if not data:
                    break
                text = decoder.decode(data)
                if text:                       # 可能整批都是殘餘位元組,還拼不成字
                    safe_write(text)
            return

        # 退路:一次讀一個字元
        while proc.isalive():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):         # 前綴 → 代表這是特殊鍵,還有第二碼
                safe_write(VT_KEYS.get(msvcrt.getwch(), ""))
            else:
                safe_write(ch)

    def watch_resize() -> None:
        """你把視窗拉大縮小時,通知子行程重新排版。

        沒有這段的話,你拉大視窗後 claude 還以為自己只有原本那麼寬,畫面會歪掉。
        """
        nonlocal cols, rows
        while proc.isalive():
            time.sleep(1)                              # 每秒看一次就夠,不必更頻繁
            c, r = shutil.get_terminal_size()
            if (c, r) != (cols, rows):
                cols, rows = c, r
                try:
                    proc.setwinsize(r, c)
                except (EOFError, OSError):
                    return  # 同 guarded_write:子行程剛走,調整視窗已無意義

    def watch_room() -> None:
        """背景執行緒:盯著 hub 的直播,有新訊息就敲鈴。"""
        sse_watch(state.server, state.room, state, alive)

    def keep_ringing() -> None:
        """背景執行緒:敲了沒反應就再敲(節拍器)。"""
        re_ring_loop(state, alive)

    # ══ 第四件事:把五個幫浦跑起來 ══
    #
    # ★ 這幾行是整個函式的骨架,其他都是準備工作:
    #     pump_input   你打的字   → 子行程
    #     watch_resize 視窗大小變 → 子行程
    #     watch_room   hub 有新訊息 → 敲鈴
    #     keep_ringing 敲了沒反應 → 再敲
    #     pump_output  子行程畫面 → 你的螢幕     ← 這個留在主執行緒
    #
    # 為什麼 pump_output 不也開一條執行緒?因為主執行緒總得有事做,
    # 而「子行程畫面沒東西了」正好就是「該收工了」——用它當結束訊號最自然。
    #
    # daemon=True 的意思是「主人走了就跟著走」:主執行緒一結束,
    # 這四條背景執行緒會自動消失,不必一個個去叫它們停。
    try:
        for fn in (pump_input, watch_resize, watch_room, keep_ringing):
            threading.Thread(target=fn, daemon=True).start()
        pump_output()  # 主執行緒守輸出;子行程退出即結束
        return proc.exitstatus or 0
    finally:
        # ★ finally 保證這行一定會跑到 —— 就算上面爆炸、就算使用者按 Ctrl+C。
        #   終端機是跟使用者借的,無論如何都要還。
        restore_terminal()


# ---------- POSIX(Mac/Linux):std lib pty ----------
#
# Unix 這邊不需要任何外部套件:偽終端是作業系統的原生設施,Python 標準庫直接有 pty。
# 這正是「Windows 遲到了幾十年」的另一面。

def run_posix(cmd: list[str], state_factory) -> int:
    """Mac / Linux 版:做的事跟 run_windows 完全一樣,只是工具不同。

    ═══ 這個函式怎麼讀 ═══

    【要懂的】跟 Windows 版對照著看最快:
      1. 為什麼 import 寫在函式裡 —— 放頂端會讓整個程式在 Windows 上開不起來
      2. pty.fork() 那三行在做什麼 —— 一個行程變兩個,一個當人一個當 CLI
      3. 為什麼要先記舊設定再還原 —— 跟 Windows 版同一個理由:終端機是借來的
      4. select 那個迴圈 —— 它一個人做完 Windows 版兩條執行緒的事

    【知道有這回事就好】:
      · termios / tty = Unix 版的「主控台設定」,對應 Windows 的 SetConsoleMode
      · select = 「這幾個來源,誰有資料就叫我」,不必為每個來源各開一條執行緒

    【兩邊的差異,不是遺漏】:
      · 這裡不必開 VT —— Unix 終端機天生就懂那些暗號(Windows 2018 年才有)
      · 這裡不必設 UTF-8 —— Unix 預設就是
      · 這裡不必裝套件 —— 偽終端是作業系統原生設施,標準庫直接有

    ⚠️ 已知缺口:這裡【沒有】對應 Windows 版的 watch_resize ——
       在 Mac/Linux 上把視窗拉大縮小,子行程不會知道,畫面會歪掉。
       正解是接 SIGWINCH 訊號。尚未實作,因為手邊沒有 POSIX 環境長期驗證。
    """
    # ★ 為什麼這幾個 import 寫在函式裡,不放檔案頂端?
    #   因為在 Windows 上 `import pty` 會【當場失敗】——
    #   它連鎖 import tty → termios,而 termios 是 POSIX 專屬,Windows 根本沒有。
    #   放在檔案頂端的話,這個檔案在 Windows 上連載入都做不到,整個程式開不起來。
    #   (實測過:ModuleNotFoundError: No module named 'termios')
    #   所以兩邊的平台專屬 import 都關在各自的函式裡,誰被呼叫誰才載入 ——
    #   這叫延遲載入(lazy import),是跨平台程式的常見手法,不是隨手亂放。
    import pty
    import select
    import termios
    import tty

    # ══ 第一件事:開一條偽終端,並把自己一分為二 ══
    #
    # pty.fork() 做兩件事:開偽終端,然後複製出一個一模一樣的行程。
    # 複製完之後,兩個行程都從這一行繼續執行,靠回傳值分辨自己是誰:
    #     pid == 0  → 我是子行程,我的鍵盤與螢幕已經接在偽終端的從屬端上
    #     pid  > 0  → 我是父行程,master 就是主控端(往它寫字 = 假裝有人在打字)
    #
    # execvp 是「把自己整個換掉」,不是「啟動另一個程式」——
    # 換成功之後,這個行程就【變成】 claude 了,下面的程式碼一行都不會執行。
    # (萬一換失敗,例如指令不存在,Python 會拋例外讓子行程直接結束 —— 實測確認過。)
    pid, master = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)

    # ══ 第二件事:準備「怎麼寫字進去」 ══

    # 跟 Windows 版同一個理由:等一下會有兩個執行緒同時想寫,不上鎖會交錯成亂碼。
    write_lock = threading.Lock()

    def write_to_child(payload: bytes) -> None:
        """往主控端寫 = 假裝使用者敲了這些鍵。"""
        os.write(master, payload)

    def safe_write(*parts: bytes) -> bool:
        """所有寫入的唯一閘門:上鎖 + 吞掉子行程已死的錯誤。"""
        return guarded_write(write_to_child, *parts,
                             lock=write_lock, gap=BELL_SUBMIT_GAP)

    def ring(*parts: str) -> bool:
        """鈴聲是字串,但偽終端只收位元組,所以在這裡轉一次。

        ★ 這就是分層的意義:BellState 只知道「呼叫 ring 就會響」,
          完全不知道 POSIX 這邊多了一道編碼手續。Windows 那邊則沒有這道。
        """
        return safe_write(*(part.encode("utf-8") for part in parts))

    def alive() -> bool:
        """子行程還活著嗎?

        ★ 這裡永遠回 True,跟 Windows 版【故意不一樣】:
          Windows 有現成的 isalive() 可問,POSIX 這邊要問得多花一次系統呼叫,
          而我們其實不需要 —— 下面那個讀迴圈讀到 EOF 就會自己結束,
          兩條背景執行緒都是 daemon,主迴圈一收工它們就跟著走。
        """
        return True

    # 到這裡才建 BellState,因為現在才有 ring 可以交給它。
    state: BellState = state_factory(ring)

    def watch_room() -> None:
        """背景執行緒:盯著 hub 的直播,有新訊息就敲鈴。"""
        sse_watch(state.server, state.room, state, alive)

    def keep_ringing() -> None:
        """背景執行緒:敲了沒反應就再敲(節拍器)。"""
        re_ring_loop(state, alive)

    threading.Thread(target=watch_room, daemon=True).start()
    threading.Thread(target=keep_ringing, daemon=True).start()

    # ══ 第三件事:借用終端機,跑主迴圈,最後還回去 ══

    old_attrs = termios.tcgetattr(sys.stdin)   # ★ 先記舊設定,離場要原封不動還回去
    tty.setraw(sys.stdin.fileno())             # 切成原始模式:按鍵一按就轉交,不等 Enter、不回顯
    try:
        while True:
            # select 的意思是「這幾個來源,誰有資料就叫我」。
            # ★ Windows 版為了同樣的效果開了兩條執行緒(pump_input + pump_output),
            #   POSIX 這邊一個迴圈就夠 —— 這是兩邊最大的結構差異。
            r, _, _ = select.select([sys.stdin, master], [], [])

            if sys.stdin in r:                 # 你打了字 → 轉交給子行程
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                safe_write(data)

            if master in r:                    # 子行程畫了畫面 → 原樣印到螢幕
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break                      # 子行程結束了
                if not data:
                    break                      # 讀到 EOF,同樣代表結束
                os.write(sys.stdout.fileno(), data)
    finally:
        # 與 Windows 端對稱:termios 只還原「我們」動過的,子行程留下的終端機私有模式
        # (alternate screen、mouse、focus reporting…)得另外關,否則同樣髒給下一個程式
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
        try:
            sys.stdout.write(TERM_RESTORE)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    # 等子行程真的結束,把它的結束碼原樣轉交出去(讓外面知道 claude 是正常退出還是出錯)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main() -> int:
    # 設定檔要在建立 parser 之【前】載入,因為下面幾個 default 會去讀環境變數,
    # 而這一行正是把 client.env 的內容填進環境變數的那一步。順序顛倒的話設定檔會失效。
    load_env_file(BASE / "client.env")

    parser = argparse.ArgumentParser(
        description="A2A 敲鈴器:包住 agent CLI,新訊息時往其 stdin 敲鈴")
    parser.add_argument("--name", required=True, help="agent 名字(對應 state/cursor-<名字>.txt)")
    parser.add_argument("--server", default=os.environ.get("A2A_SERVER", "http://127.0.0.1:8787"),
                        help="hub 位址(遠端機器指向遠端 hub);預設值可寫在 client.env 的 A2A_SERVER")
    parser.add_argument("--room", default=os.environ.get("A2A_ROOM", "main"))
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="-- 之後接要包的指令,如:-- claude --resume")
    args = parser.parse_args()

    # ★ 把【解析後】的位址與房間寫回環境變數,子行程(agent)才查得到。
    #
    #   AGENTS.md 教的指令長這樣:${A2A_SERVER:-http://127.0.0.1:8787}
    #   —— 沒設定就落回預設值。而設定有兩條路進來:
    #
    #       client.env      load_env_file 會填進 os.environ  ✔ agent 繼承得到
    #       --server 參數   只在這裡的 argv 裡                ✘ agent 看不到
    #
    #   第二條路上,agent 會安靜地落回 127.0.0.1 去連【自己這台】——
    #   而遠端接入時那裡根本沒有 hub。所以在這裡補齊,兩條路合而為一。
    os.environ["A2A_SERVER"] = args.server
    os.environ["A2A_ROOM"] = args.room

    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        parser.error("缺少要包的指令,例:uv run bell.py --name alice -- claude --resume")

    # ★ 必須在真的終端機裡執行,否則後面會炸。
    #   這個程式的工作是「站在使用者的鍵盤與 agent 之間轉交按鍵」——
    #   如果 stdin 被接到管線或排程系統上,根本沒有按鍵可以轉,底層借用終端機設定
    #   的動作也會失敗(POSIX 端會丟出 Inappropriate ioctl for device 這種天書)。
    #   與其讓它在深處爆炸,不如在這裡先講清楚。
    #
    #   哪些用法會被這道檢查擋下來(常有人問,所以列清楚):
    #       ✗ 接管線      echo something | bell.py
    #       ✗ 排程 / CI   沒有終端機可用
    #       ✗ 打包成「無視窗」的執行檔(PyInstaller 的 --windowed)—— 那會拿掉主控台
    #       ✓ 在終端機裡執行、雙擊 .bat、打包成「有主控台」的執行檔(--console)
    #
    #   換句話說:**想包成 .bat 或 .exe 都沒問題**,只要別把主控台拿掉。
    #   .bat 雙擊時 Windows 會開一個 console 給它,那就是真終端機。
    #   (打包成 exe 另有一個坑:pywinpty 帶原生 DLL,打包工具常漏抓,要手動指定收進去。)
    if not sys.stdin.isatty():
        print("bell.py 必須在終端機裡執行(它要轉交你的按鍵)。\n"
              "偵測到 stdin 不是終端機 —— 常見原因是接了管線、放進排程、或在 CI 裡跑。",
              file=sys.stderr)
        return 1

    cursor_path = BASE / "state" / f"cursor-{args.name}.txt"
    global LOG_PATH
    LOG_PATH = BASE / "state" / f"bell-{args.name}.log"
    LOG_PATH.parent.mkdir(exist_ok=True)

    def state_factory(write_fn) -> BellState:
        """把「怎麼建 BellState」打包成一份食譜,交給平台層在對的時機自己煮。

        為什麼不在這裡直接建好就好?因為有個雞生蛋的環:

            BellState 要能敲鈴  → 需要「往子行程寫字」的能力
            那個能力            → 要先有子行程才存在
            子行程              → 在 run_windows / run_posix 裡面才誕生
            而 run_*            → 又需要 BellState

        在 main() 這個時間點,子行程根本還沒開,所以建不出來。
        解法是把建構往後延:main 只交食譜,平台層開好子行程、湊齊材料後
        才呼叫這個函式,拿到一個綁定了「這個平台的寫入方式」的 BellState。

        這個技巧叫【延遲建構】,傳進來的 write_fn 叫【依賴注入】。
        它是「工廠函式」(一個回傳新物件的函式),但**不是** GoF 的工廠模式 ——
        那個模式的重點是靠多型決定要建立哪個類別,這裡永遠只建 BellState 一種。

        兩個平台傳進來的東西其實不一樣(Windows 傳吃字串的、POSIX 傳要轉 bytes 的),
        而 BellState 完全不知道這件事 —— 那個「不知道」就是分層要換來的東西。
        """
        def ring_the_bell(text: str) -> bool:
            """真正的「敲鈴」動作:把決策層給的那行字 + 送出鍵寫進子行程。

            ★ 「要說什麼」由 BellState 決定,這裡只管「怎麼寫進去」——
              一般鈴與強制鈴說不一樣的話,而這一層完全不需要知道有兩種。
            """
            # ★ 兩段【分開送】,不是 text + BELL_SUBMIT ——
            #   合成一段的話,Codex 那種會偵測貼上的 TUI 只會換行、不會送出。
            #   兩段之間的間隔與「不准有人插隊」都由 write_fn 底下那把鎖負責。
            #
            #   誠實標一個新的失敗模式:第一段寫進去、第二段失敗的話,
            #   字會躺在輸入框裡沒送出,而這裡回 False。以前一次 write 沒有這個中間態。
            #   兜底還是重敲機制(90 秒後再敲一次),代價是輸入框裡會多一行殘留。
            return write_fn(text, BELL_SUBMIT)

        return BellState(cursor_path, ring_the_bell,
                         name=args.name,
                         server=args.server.rstrip("/"),
                         room=args.room)

    log(f"啟動:name={args.name} server={args.server} room={args.room} cmd={' '.join(cmd)}")
    # 為什麼是 "nt" 不是 "windows"?os.name 只有 'posix' 與 'nt' 兩個值(官方原話),
    # 它問的是「系統 API 是哪一家的」,不是商品名。nt 來自 Windows NT ——
    # 1993 年那條跟 DOS 分家的核心血脈,今天的 Win10/11 都是它的後代
    # (你在 Windows 11 上查系統版本會看到 10.0.xxxxx,那個 10.0 就是 NT 版本號)。
    # 這裡用 os.name 而不是 platform.system()=="Windows",是因為我們要分的那條線
    # 剛好就是它那條線:run_posix 用 POSIX 的 pty,run_windows 用 NT 的 ConPTY。
    #
    # 註:編輯器(Pylance)可能會把下面兩行的其中一行標成「永遠不會執行」。
    # 那不是錯誤 —— 它知道你現在這台是什麼系統,就把另一條路判成走不到。
    # 換一台機器打開,它會反過來標另一行。
    if os.name == "nt":
        return run_windows(cmd, state_factory)
    return run_posix(cmd, state_factory)


if __name__ == "__main__":
    sys.exit(main())
