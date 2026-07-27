"""極簡 .env 檔載入 —— 讓 client 與 server 各自有一份設定檔。

為什麼不用 python-dotenv?
    因為我們只需要它的一小部分。專案的家法是「格式自己寫、協定用庫」:
    .env 是一種【格式】,規則簡單、不會演進,十幾行就寫完;
    而像 HTTP、MCP 那種【協定】會持續改版,追規格不是我們的差異化,才該外包給套件。
    哪天真的需要引號跳脫、多行值、${VAR} 展開,那就是該換 python-dotenv 的時候。

誰讀哪一份:
    client.env  → bell.py(敲鈴器)與 MCP 工具:它們要知道「hub 在哪裡」
    server.env  → server.py(hub 本體):它要知道綁哪個位址、開不開認證……

為什麼要分兩份?因為兩邊關心的東西沒有交集,而且會分別部署到不同機器 ——
遠端那台只有 client,它不該拿到伺服器的邀請碼與認證鑰匙。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def warn(message: str) -> None:
    """印一行警告到 stderr,而且【印不出去也絕不炸掉呼叫者】。

    為什麼要包一層而不直接 print:

    ① **編碼**。這台機器的主控台預設是 cp950,直接印中文會丟 UnicodeEncodeError。
       而這個函式是在程式啟動時被呼叫的 —— 一個警告訊息把整支程式弄到起不來,
       荒謬程度遠超過它想提醒的那件事。
       (bell.py 的 log() 也有同一條規矩:「log 寫不進去不能反過來炸掉轉發」。)

    ② **時機**。bell.py 在【開子行程之前】載入設定檔,所以這時候印 stderr 是安全的;
       等 agent 的 TUI 跑起來之後就不行了 —— 那時 stderr 與子行程共用終端,
       直印會插進畫面甚至斬斷 VT 序列。
       ★ 如果將來有人把 load_env_file 移到子行程啟動之後,這個假設就不成立了。
    """
    try:
        print(f"[env] {message}", file=sys.stderr)
    except (UnicodeEncodeError, OSError):
        pass


def parse_env_file(path: Path) -> dict[str, str]:
    """讀一個 .env 檔,回傳鍵值對。

    ★ 這是純函式:它只讀檔、只回傳結果,【完全不碰環境變數】。
      有副作用的是 load_env_file。分成兩個是刻意的 ——
      解析規則(註解、空白、壞行怎麼處理)因此能被單獨驗證,
      不必每測一條規則就去動 os.environ。正式程式碼只會用 load_env_file,
      直接用這一個的目前只有測試,那不是多餘,是這個切法換來的東西。

    檔案不存在就回空字典 —— 那是正常情況(還沒建設定檔),不是錯誤。

    支援的寫法(刻意只支援這些):
        KEY=value
        KEY = value          # 等號兩邊的空白會去掉
        # 這是註解
        （空行）

    不支援:引號、跳脫、多行值、${VAR} 展開、export 前綴。
    """
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if "=" not in line:
            # 看不懂的這一行略過,不讓它炸掉整支程式 ——
            # 設定檔是給人手改的,打錯字比程式 bug 常見得多,
            # 為了一行 typo 讓伺服器起不來,代價完全不成比例。
            #
            # ★ 但「不炸」不等於「不吭聲」:load_env_file 的說明裡就寫著
            #   「我明明設了卻沒生效」是最難查的問題,而靜默略過正好會製造它。
            #   所以略過歸略過,要讓人看得見。
            warn(f"{path.name} 第 {lineno} 行看不懂(沒有等號),已略過:{line[:30]}")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key:
            result[key] = value
    return result


def load_env_file(path: Path) -> list[str]:
    """把設定檔的內容套進 os.environ,回傳實際套用了哪些鍵。

    ★ 已存在的環境變數【優先】,檔案讓步。

    為什麼是這個方向,而不是檔案覆蓋?因為臨時覆寫必須要有效:

        HOST=127.0.0.1 uv run server.py

    這是最常用的除錯手段。如果設定檔會蓋掉它,這行命令就悄悄失靈了 ——
    而「我明明設了卻沒生效」是最難查的一種問題。

    回傳值是給啟動訊息用的:印出來就能一眼看出設定檔到底有沒有被讀到。
    """
    applied: list[str] = []
    for key, value in parse_env_file(path).items():
        if key in os.environ:
            continue          # 環境變數已經有了 → 尊重它,檔案不插手
        os.environ[key] = value
        applied.append(key)
    return applied
