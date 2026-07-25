"""bell(敲鈴器)— stdin 喚醒 wrapper。

用法:
    uv run bell.py --name alice [--server http://127.0.0.1:8787] [--room main] -- claude --resume

角色:本專案的預設喚醒機制 — 不靠 agent 自律重掛、不靠檔案旗子,
由本程式把 agent CLI 包成子行程(ConPTY / pty,TUI 體驗保真),
盯著 hub 的 SSE 直播當眼睛,發現「房間進度 > 該 agent 的 cursor」
就往子行程的 stdin 敲一行固定鈴聲。agent 看到鈴聲照對帳鐵則辦事。

設計要點:
- 鈴聲固定單行:BELL_TEXT + BELL_SUBMIT(ConPTY 的送出鍵是 \r,不是 \n — T2.5 生死關卡)
- 自己發言不會被敲:發言後 cursor 已推進,last_id 不再領先(老規矩,零額外機制)
- 重敲保險:敲後 RE_RING_SECONDS 內 cursor 未推進且仍落後 → 再敲,上限 MAX_RINGS 次
  (agent 可能正在生成中漏聽;超過上限就安靜,印警告請人類看一眼)
- SSE 斷線自動重連(指數退避封頂),重連後若已落後立即補敲 — 對帳鐵則的 wrapper 版
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
    """鈴聲狀態機:知道房間進度(SSE 餵)與 agent 進度(cursor 檔),決定何時敲。"""

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

def run_windows(cmd: list[str], state_factory) -> int:
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

    try:
        for fn in (pump_input, watch_resize,
                   lambda: sse_watch(state.server, state.room, state, alive),
                   lambda: re_ring_loop(state, alive)):
            threading.Thread(target=fn, daemon=True).start()
        pump_output()  # 主執行緒守輸出;子行程退出即結束
        return proc.exitstatus or 0
    finally:
        restore_terminal()


# ---------- POSIX(Mac/Linux):std lib pty ----------

def run_posix(cmd: list[str], state_factory) -> int:
    import pty
    import select
    import termios
    import tty

    pid, master = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)

    write_lock = threading.Lock()  # 同 Windows:鈴聲與打字不互插

    def safe_write(data: bytes) -> bool:
        return guarded_write(lambda payload: os.write(master, payload), data, write_lock)

    state: BellState = state_factory(lambda text: safe_write(text.encode("utf-8")))
    alive = lambda: True  # 以 EOF 判終,見下方讀迴圈

    threading.Thread(target=lambda: sse_watch(state.server, state.room, state, alive),
                     daemon=True).start()
    threading.Thread(target=lambda: re_ring_loop(state, alive), daemon=True).start()

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
        state = BellState(cursor_path, lambda: write_fn(BELL_TEXT + BELL_SUBMIT))
        state.name = args.name
        state.server = args.server.rstrip("/")
        state.room = args.room
        return state

    log(f"啟動:name={args.name} server={args.server} room={args.room} cmd={' '.join(cmd)}")
    if os.name == "nt":
        return run_windows(cmd, state_factory)
    return run_posix(cmd, state_factory)


if __name__ == "__main__":
    sys.exit(main())
