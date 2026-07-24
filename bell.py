"""bell(敲鈴器)— stdin 喚醒 wrapper(roadmap 敲鈴器,共識 #255-#273)。

用法:
    uv run bell.py --name alice [--server http://127.0.0.1:8787] [--room main] -- claude -c

角色:老闆拍板(#250/#253)的喚醒最終型 — 不靠 agent 自律重掛、不靠檔案旗子,
由本程式把 agent CLI 包成子行程(ConPTY / pty,TUI 體驗保真),
盯著 hub 的 SSE 直播當眼睛,發現「房間進度 > 該 agent 的 cursor」
就往子行程的 stdin 敲一行固定鈴聲。agent 看到鈴聲照對帳鐵則辦事。

設計要點(planning #270 + bob #273):
- 鈴聲固定單行:BELL_TEXT + BELL_SUBMIT(ConPTY 的送出鍵是 \r,不是 \n — T2.5 生死關卡)
- 自己發言不會被敲:發言後 cursor 已推進,last_id 不再領先(老規矩,零額外機制)
- 重敲保險:敲後 RE_RING_SECONDS 內 cursor 未推進且仍落後 → 再敲,上限 MAX_RINGS 次
  (agent 可能正在生成中漏聽;超過上限就安靜,印警告請人類看一眼)
- SSE 斷線自動重連(指數退避封頂),重連後若已落後立即補敲 — 對帳鐵則的 wrapper 版
"""
from __future__ import annotations

import argparse
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
MAX_RINGS = 3               # 同一段落後最多敲幾次,之後改印警告(不騷擾設計,bob #255)
SSE_READ_TIMEOUT = 60       # server 每 15 秒有 keep-alive,60 秒沒動靜視為死連線
RECONNECT_MAX_BACKOFF = 30


def log(msg: str) -> None:
    """敲鈴器自己的狀態訊息走 stderr,不汙染轉發中的 TUI 畫面。"""
    print(f"[bell] {msg}", file=sys.stderr, flush=True)


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
            self.ring_fn()
            self.rings_this_gap += 1
            self.last_ring_at = now
            log(f"叮咚 #{self.rings_this_gap}(room={self.known_last_id} cursor={cursor})")


def sse_watch(server: str, room: str, state: BellState, child_alive) -> None:
    """眼睛:掛 hub 的 SSE 直播(既有 UI 端點,零新增),斷線指數退避重連。
    重連自帶 since_id=cursor — 斷線期間漏的事件靠這裡補,對帳鐵則 wrapper 版。"""
    backoff = 1
    while child_alive():
        url = f"{server}/api/rooms/{room}/stream?since_id={state.read_cursor()}"
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
    # 輸出開 VT 處理(讓子行程的 ANSI 畫面原樣呈現);輸入用 getwch 逐鍵讀,不動輸入模式
    hout = kernel32.GetStdHandle(-11)
    out_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(hout, ctypes.byref(out_mode))
    kernel32.SetConsoleMode(hout, out_mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

    cols, rows = shutil.get_terminal_size()
    proc = PtyProcess.spawn(cmd, dimensions=(rows, cols), cwd=str(BASE))
    state: BellState = state_factory(lambda text: proc.write(text))

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

    # 傳統鍵碼 → VT 序列(getwch 對特殊鍵回傳 \x00/\xe0 前綴 + 第二碼)
    VT_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D",
               "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~", "R": "\x1b[2~",
               "I": "\x1b[5~", "Q": "\x1b[6~"}

    def pump_input() -> None:
        """鍵盤 → 子行程。getwch 走寬字元(IME 中文 OK),特殊鍵翻成 VT。"""
        while proc.isalive():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                proc.write(VT_KEYS.get(msvcrt.getwch(), ""))
            else:
                proc.write(ch)

    def watch_resize() -> None:
        nonlocal cols, rows
        while proc.isalive():
            time.sleep(1)
            c, r = shutil.get_terminal_size()
            if (c, r) != (cols, rows):
                cols, rows = c, r
                proc.setwinsize(r, c)

    for fn in (pump_input, watch_resize,
               lambda: sse_watch(state.server, state.room, state, alive),
               lambda: re_ring_loop(state, alive)):
        threading.Thread(target=fn, daemon=True).start()
    pump_output()  # 主執行緒守輸出;子行程退出即結束
    return proc.exitstatus or 0


# ---------- POSIX(Mac/Linux):std lib pty ----------

def run_posix(cmd: list[str], state_factory) -> int:
    import pty
    import select
    import termios
    import tty

    pid, master = pty.fork()
    if pid == 0:
        os.execvp(cmd[0], cmd)

    state: BellState = state_factory(
        lambda text: os.write(master, text.encode("utf-8")))
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
                os.write(master, data)
            if master in r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A2A 敲鈴器:包住 agent CLI,新訊息時往其 stdin 敲鈴")
    parser.add_argument("--name", required=True, help="agent 名字(對應 state/cursor-<名字>.txt)")
    parser.add_argument("--server", default="http://127.0.0.1:8787", help="hub 位址(遠端機器指向遠端 hub)")
    parser.add_argument("--room", default="main")
    parser.add_argument("cmd", nargs=argparse.REMAINDER,
                        help="-- 之後接要包的指令,如:-- claude -c")
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        parser.error("缺少要包的指令,例:uv run bell.py --name alice -- claude -c")

    cursor_path = BASE / "state" / f"cursor-{args.name}.txt"

    def state_factory(write_fn) -> BellState:
        state = BellState(cursor_path, lambda: write_fn(BELL_TEXT + BELL_SUBMIT))
        state.server = args.server.rstrip("/")
        state.room = args.room
        return state

    log(f"啟動:name={args.name} server={args.server} room={args.room} cmd={' '.join(cmd)}")
    if os.name == "nt":
        return run_windows(cmd, state_factory)
    return run_posix(cmd, state_factory)


if __name__ == "__main__":
    sys.exit(main())
