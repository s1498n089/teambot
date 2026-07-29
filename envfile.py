"""設定檔載入 —— 薄薄一層包在 python-dotenv 外面。

## 為什麼用 python-dotenv,而不自己寫一個

自己寫一個 `.env` 解析器只要十幾行,但它的成本不在行數:

  - 十幾行的版本只支援 `KEY=value`。引號、`export` 前綴、`${VAR}` 展開一律不支援 ——
    而使用者不會知道,他只會發現「我寫的設定沒生效」。
  - 遇到問題時,搜尋「python dotenv 怎麼寫引號」有一百篇答案;
    搜尋「這個專案的 envfile.py 怎麼寫引號」有零篇。

★ **「查得到答案」本身就是一種功能** —— 而那是自己寫的東西永遠給不了的。
  能不要自己寫就不要自己寫:裝一個套件對開發者無感,現成好用就好。

用 python-dotenv 的話,上面那三種寫法全部自動支援(實測過)。

## 這一層還留著什麼

python-dotenv 沒做、而我們需要的兩件事:

  ① **回報「實際套用了哪些鍵」** —— 給啟動訊息用,讓使用者一眼看出設定檔有沒有被讀到。
     `load_dotenv` 只回傳 True/False。
  ② **壞行要出聲** —— 理由見下方 load_env_file。

誰讀哪一份:
    client.env  → bell.py(敲鈴器):它要知道「hub 在哪裡」
    server.env  → server.py(hub 本體):它要知道綁哪個位址、開不開認證……

為什麼要分兩份?因為兩邊關心的東西沒有交集,而且會分別部署到不同機器 ——
遠端那台只有 client,它不該拿到伺服器的認證鑰匙。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def warn(message: str) -> None:
    """印一行警告到 stderr,而且【印不出去也絕不炸掉呼叫者】。

    為什麼要包一層而不直接 print:

    ① **編碼**。有些 Windows 主控台的預設編碼不是 UTF-8,直接印中文會丟
       UnicodeEncodeError。而這個函式是在程式啟動時被呼叫的 ——
       一個警告訊息把整支程式弄到起不來,荒謬程度遠超過它想提醒的那件事。
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


def load_env_file(path: Path) -> list[str]:
    """把設定檔的內容套進 os.environ,回傳實際套用了哪些鍵。

    ★ 已存在的環境變數【優先】,檔案讓步。
      (這是 `load_dotenv` 的預設行為 `override=False`,換套件時實測確認過 ——
       這條是整個模組的靈魂,不能靠「應該是這樣吧」。)

    為什麼是這個方向,而不是檔案覆蓋?因為臨時覆寫必須要有效:

        HOST=127.0.0.1 uv run server.py

    這是最常用的除錯手段。如果設定檔會蓋掉它,這行命令就悄悄失靈了 ——
    而「我明明設了卻沒生效」是最難查的一種問題。

    ★ 壞行(沒有等號的那種)dotenv 會收成 `value=None`,而且【完全不出聲】。
      我們在這裡替它出聲 —— 因為靜默略過正好會製造上面那句「我明明設了卻沒生效」。

      代價:警告裡**沒有行號**(dotenv 的公開 API 不給,要拿得深入它的內部解析器)。
      自己寫的舊版有行號。拿行號換掉整個解析器的維護,划算 ——
      但那是個真實的退步,寫在這裡免得日後被當成「本來就沒有」。

    回傳值是給啟動訊息用的:印出來就能一眼看出設定檔到底有沒有被讀到。
    """
    values = dotenv_values(path)          # 檔案不存在時回空 dict,不是錯誤

    applied: list[str] = []
    for key, value in values.items():
        if value is None:
            warn(f"{path.name} 有一行看不懂(沒有等號),已略過:{key[:30]}")
            continue
        if key in os.environ:
            continue                      # 環境變數已經有了 → 尊重它,檔案不插手
        applied.append(key)

    load_dotenv(path)                     # override=False:檔案不蓋掉既有的環境變數
    return applied
