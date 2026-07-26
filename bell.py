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
- 鈴聲固定單行:BELL_TEXT + BELL_SUBMIT(ConPTY 的送出鍵是 \r,不是 \n — T2.5 生死關卡)
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

BASE = Path(__file__).resolve().parent

BELL_TEXT = "[A2A-BELL] cursor updated"
BELL_SUBMIT = "\r"          # ConPTY/TUI 的送出鍵(T2.5:對真 CLI 驗證會自動成為 prompt)
RE_RING_SECONDS = 90        # 敲後多久 cursor 仍未推進就重敲
MAX_RINGS = 3               # 同一段落後最多敲幾次,之後改印警告(不騷擾設計)
SSE_READ_TIMEOUT = 60       # server 每 15 秒有 keep-alive,60 秒沒動靜視為死連線
RECONNECT_MAX_BACKOFF = 30


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


def guarded_write(write_fn, payload, lock) -> bool:
    """所有「寫進子行程」的唯一閘門(打字與鈴聲共用一把鎖,互不插隊)。

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

    def __init__(self, cursor_path: Path, ring_fn):
        self.cursor_path = cursor_path
        self.ring_fn = ring_fn
        self.known_last_id = 0
        self.rings_this_gap = 0
        self.last_ring_at = 0.0
        self.warned = False
        self.lock = threading.Lock()

    def read_cursor(self) -> int:
        try:
            return int(self.cursor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def on_message(self, msg_id: int) -> None:
        """SSE 每收到一則訊息呼叫一次。"""
        with self.lock:
            self.known_last_id = max(self.known_last_id, msg_id)
        self.evaluate()

    def evaluate(self) -> None:
        """核心判斷:落後才敲;敲過就交給重敲節奏;追上就歸零。"""
        with self.lock:
            cursor = self.read_cursor()
            if self.known_last_id <= cursor:
                if self.rings_this_gap:
                    log(f"cursor 已追上(={cursor}),鈴聲歸位")
                self.rings_this_gap = 0
                self.warned = False
                return
            now = time.monotonic()
            if self.rings_this_gap >= MAX_RINGS:
                if not self.warned:
                    log(f"WARN 已敲 {MAX_RINGS} 次仍未見 cursor 推進"
                        f"(room={self.known_last_id} cursor={cursor})— agent 可能卡住,請人類看一眼")
                    self.warned = True
                return
            if self.rings_this_gap and now - self.last_ring_at < RE_RING_SECONDS:
                return  # 敲過了,還在等待窗內,不騷擾
            delivered = self.ring_fn()
            self.rings_this_gap += 1  # 送不進去也計數:pty 已死時才不會無限重敲刷 log
            self.last_ring_at = now
            if delivered is False:  # 只認明確的 False;回 None 的 ring_fn 視為沒回報
                log(f"WARN 鈴聲沒送進子行程 #{self.rings_this_gap}"
                    f"(room={self.known_last_id} cursor={cursor})— pty 可能已關,靠重敲兜底")
            else:
                log(f"叮咚 #{self.rings_this_gap}(room={self.known_last_id} cursor={cursor})")


def sse_watch(server: str, room: str, state: BellState, child_alive) -> None:
    """眼睛:掛 hub 的 SSE 直播(既有 UI 端點,零新增),斷線指數退避重連。
    重連自帶 since_id=cursor — 斷線期間漏的事件靠這裡補,對帳鐵則 wrapper 版。"""
    backoff = 1
    while child_alive():
        url = (f"{server}/api/rooms/{room}/stream?since_id={state.read_cursor()}"
               f"&watcher={state.name}")  # 報上身分 → hub 據此判定 agent 在場(presence)
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT) as resp:
                log(f"SSE 已連線 {server} #{room}")
                backoff = 1
                for raw in resp:
                    if not child_alive():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: "):
                        try:
                            state.on_message(int(json.loads(line[6:])["id"]))
                        except (json.JSONDecodeError, KeyError, ValueError):
                            pass  # keep-alive 或非訊息 payload,略過
        except OSError as exc:
            log(f"SSE 斷線({exc}),{backoff}s 後重連")
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)


def re_ring_loop(state: BellState, child_alive) -> None:
    """重敲節拍器:每 5 秒 evaluate 一次 — 落後未回應者到點重敲(SSE 靜默時也會跑)。"""
    while child_alive():
        time.sleep(5)
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
    # 這些 import 刻意寫在函式裡面,不放檔案頂端 —— 理由見 run_posix 那邊的說明。
    import ctypes
    import msvcrt
    from ctypes import wintypes
    from winpty import PtyProcess

    kernel32 = ctypes.windll.kernel32
    # 輸出開 VT 處理(讓子行程的 ANSI 畫面原樣呈現)
    hout = kernel32.GetStdHandle(-11)
    out_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(hout, ctypes.byref(out_mode))
    kernel32.SetConsoleMode(hout, out_mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

    # 輸入開 VT 模式(getwch 只回「字元」,Shift+Tab 與 Tab 同為 \t,
    # 修飾鍵資訊全丟 — TUI 認的 Shift+Tab 是 ESC[Z)。VT 輸入模式下 Windows 會把
    # 組合鍵翻成標準 VT 序列,按鍵保真交給作業系統。失敗(舊系統)則退回 getwch 路徑。
    hin = kernel32.GetStdHandle(-10)
    in_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(hin, ctypes.byref(in_mode))
    raw_mode = (in_mode.value | 0x0200) & ~(0x0001 | 0x0002 | 0x0004)  # +VT_INPUT -PROCESSED -LINE -ECHO
    vt_input = bool(kernel32.SetConsoleMode(hin, raw_mode))
    old_in_cp = kernel32.GetConsoleCP()
    if vt_input:
        kernel32.SetConsoleCP(65001)  # 輸入位元組走 UTF-8,中文 IME 不亂碼

    def restore_terminal() -> None:
        """離場清潔工:借來的終端機狀態全部歸還 —— 少了這段,退出後鍵盤會吐亂碼。"""
        try:
            sys.stdout.write(TERM_RESTORE)
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        kernel32.SetConsoleMode(hin, in_mode.value)    # 先送序列再還原,順序不能顛倒
        kernel32.SetConsoleMode(hout, out_mode.value)
        kernel32.SetConsoleCP(old_in_cp)

    cols, rows = shutil.get_terminal_size()
    proc = PtyProcess.spawn(cmd, dimensions=(rows, cols), cwd=str(BASE))
    write_lock = threading.Lock()  # 鈴聲(SSE 執行緒)與打字(輸入執行緒)不互插

    def safe_write(text: str) -> bool:
        return guarded_write(proc.write, text, write_lock)  # 回傳透傳給鈴聲,失敗才有案可查

    state: BellState = state_factory(safe_write)

    def alive() -> bool:
        return proc.isalive()

    def pump_output() -> None:
        """子行程畫面 → 我們的終端機(TUI 保真的另一半)。"""
        while True:
            try:
                data = proc.read(4096)
            except EOFError:
                break
            if data:
                sys.stdout.write(data)
                sys.stdout.flush()

    # 傳統鍵碼 → VT 序列(getwch 退路用:對特殊鍵回傳 \x00/\xe0 前綴 + 第二碼)
    VT_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D",
               "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~", "R": "\x1b[2~",
               "I": "\x1b[5~", "Q": "\x1b[6~"}

    def pump_input() -> None:
        """鍵盤 → 子行程。主路徑:VT 輸入模式的原始位元組(Shift+Tab=ESC[Z、
        修飾鍵組合全保真);退路:getwch 逐鍵(寬字元 IME OK,但修飾鍵資訊有限)。"""
        if vt_input:
            # incremental decoder:os.read 可能把多位元組中文切在 1024 邊界,
            # 殘餘位元組要跨次保留拼接,否則貼上大段中文會出 � 亂碼
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while proc.isalive():
                try:
                    data = os.read(0, 1024)
                except OSError:
                    break
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    safe_write(text)
            return
        while proc.isalive():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                safe_write(VT_KEYS.get(msvcrt.getwch(), ""))
            else:
                safe_write(ch)

    def watch_resize() -> None:
        nonlocal cols, rows
        while proc.isalive():
            time.sleep(1)
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

    try:
        for fn in (pump_input, watch_resize, watch_room, keep_ringing):
            threading.Thread(target=fn, daemon=True).start()
        pump_output()  # 主執行緒守輸出;子行程退出即結束
        return proc.exitstatus or 0
    finally:
        restore_terminal()


# ---------- POSIX(Mac/Linux):std lib pty ----------
#
# Unix 這邊不需要任何外部套件:偽終端是作業系統的原生設施,Python 標準庫直接有 pty。
# 這正是「Windows 遲到了幾十年」的另一面。

def run_posix(cmd: list[str], state_factory) -> int:
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

    # pty.fork():開一條偽終端,然後把行程一分為二。
    # 回傳的 pid 若為 0 代表「我是子行程」,此時它的 stdin/stdout 已經接在
    # 偽終端的從屬端上;execvp 把自己整個換成要跑的 agent CLI(取代,不是啟動另一個)。
    # 父行程拿到的 master 就是主控端 —— 往它寫字 = 假裝有人在鍵盤上打字。
    pid, master = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)

    write_lock = threading.Lock()  # 同 Windows:鈴聲與打字不互插

    def write_to_child(payload: bytes) -> None:
        os.write(master, payload)

    def safe_write(data: bytes) -> bool:
        return guarded_write(write_to_child, data, write_lock)

    def ring(text: str) -> bool:
        """鈴聲是字串,但 pty 收的是位元組,所以在這裡轉一次。"""
        return safe_write(text.encode("utf-8"))

    def alive() -> bool:
        """POSIX 這邊不查子行程狀態,改以「讀到 EOF」判定結束(見下方讀迴圈),
        所以這裡永遠回 True —— 執行緒都是 daemon,主迴圈收工就一起走。"""
        return True

    state: BellState = state_factory(ring)

    def watch_room() -> None:
        """背景執行緒:盯著 hub 的直播,有新訊息就敲鈴。"""
        sse_watch(state.server, state.room, state, alive)

    def keep_ringing() -> None:
        """背景執行緒:敲了沒反應就再敲(節拍器)。"""
        re_ring_loop(state, alive)

    threading.Thread(target=watch_room, daemon=True).start()
    threading.Thread(target=keep_ringing, daemon=True).start()

    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())
    try:
        while True:
            r, _, _ = select.select([sys.stdin, master], [], [])
            if sys.stdin in r:
                data = os.read(sys.stdin.fileno(), 1024)
                if not data:
                    break
                safe_write(data)
            if master in r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
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
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A2A 敲鈴器:包住 agent CLI,新訊息時往其 stdin 敲鈴")
    parser.add_argument("--name", required=True, help="agent 名字(對應 state/cursor-<名字>.txt)")
    parser.add_argument("--server", default="http://127.0.0.1:8787", help="hub 位址(遠端機器指向遠端 hub)")
    parser.add_argument("--room", default="main")
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="-- 之後接要包的指令,如:-- claude --resume")
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        parser.error("缺少要包的指令,例:uv run bell.py --name alice -- claude --resume")

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
        def ring_the_bell() -> bool:
            """真正的「敲鈴」動作:往子行程送一行固定暗號 + 送出鍵。"""
            return write_fn(BELL_TEXT + BELL_SUBMIT)

        state = BellState(cursor_path, ring_the_bell)
        state.name = args.name
        state.server = args.server.rstrip("/")
        state.room = args.room
        return state

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
