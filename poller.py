"""poller — 笨眼睛(備援喚醒 option 2 專用;預設喚醒是敲鈴器 bell.py,用不到本程式)。

每隔幾秒拉一次 server 的 /state,last_id 有變就原子性地改寫本地狀態檔。
agent(Claude Code 的 Monitor、或任何 blocking shell 迴圈)只盯這個檔案,
網路的斷線重試全部由這裡吞掉,agent 永遠不會看到連線錯誤。
只用標準函式庫,不需要安裝任何東西。
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def fetch_state(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def write_atomic(path: str, content: str) -> None:
    """寫 temp 檔再 rename,讀的人永遠不會看到寫一半的內容。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def read_current(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="A2A chatroom state poller")
    parser.add_argument("--server", default="http://127.0.0.1:8787", help="chat server base URL(可指向遠端)")
    parser.add_argument("--room", default="main")
    parser.add_argument("--state-file", default=os.path.join(BASE, "state", "last_id.txt"))
    parser.add_argument("--interval", type=float, default=2.0, help="輪詢間隔秒數")
    parser.add_argument("--once", action="store_true", help="只拉一次就結束(測試用)")
    args = parser.parse_args()

    url = f"{args.server.rstrip('/')}/api/rooms/{args.room}/state"
    known = read_current(args.state_file)
    failures = 0  # 連續失敗計數:驅動指數退避
    print(f"[poller] watching {url} -> {args.state_file} (interval={args.interval}s)", flush=True)

    while True:
        try:
            state = fetch_state(url)
            last_id = str(state["last_id"])
            failures = 0
            if last_id != known:
                write_atomic(args.state_file, last_id)
                known = last_id
                print(f"[poller] last_id -> {last_id}", flush=True)
        except Exception as exc:  # 網路錯誤只記錄、下一輪重試,不外洩給 agent
            failures += 1
            print(f"[poller] WARN {type(exc).__name__}: {exc} (連續失敗 {failures})", flush=True)
        if args.once:
            return 0
        # 指數退避:server 掛掉時別用力敲,恢復後第一次成功即回正常節奏
        delay = min(args.interval * (2 ** failures), 60.0) if failures else args.interval
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
