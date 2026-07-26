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
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """讀一個 .env 檔,回傳鍵值對。

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
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if "=" not in line:
            continue          # 看不懂的行直接略過,不要讓設定檔的小錯誤炸掉整個程式
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
