"""把 client 端打包成一個 zip —— 傳給誰,誰就能帶著自己的 agent 連進來。

## 這個 zip 是什麼

這個專案有兩半:**hub**(伺服器,跑在一台機器上)與 **client**(敲鈴器 + 三支工具,
跑在每個要進聊天室的人的電腦上)。想加入的人不需要伺服器那半,
也不需要測試、文件、前端 —— 他只要那幾支程式、協定、跟一份設定範本。

    解壓 → 改 client.env 裡的一行 IP → uv run bell.py --name <他> -- <他的 agent CLI>

★ 他的機器上**不需要先裝 Python**:`uv run` 自己會把環境準備好。
  這對「只想連進來看看」的人是關鍵 —— 前置作業愈多,愈少人真的會試。

## 為什麼是腳本,不是「照清單手動壓縮」

清單會過期。今天 client 端有五支程式,`rule.py` 是今晚才加的 ——
**寫在 README 裡的清單過期了沒有人知道,寫在腳本裡的清單過期時測試會抓**
(見 tests/backend/test_client_zip.py:它驗每支程式的 import 都解得開)。

## 為什麼給完整的 pyproject 而不是精簡版

client 端其實只需要 `python-dotenv` 與 `pywinpty`,不需要 fastapi/uvicorn。
但**精簡的 pyproject 生不出自己的 uv.lock** —— 只能讓對方第一次跑時自己解版本,
而那正是「他遇到的問題我們重現不了」的來源。

    多裝兩個永遠不會被 import 的純 Python 套件   一次幾秒鐘
    版本漂移                                      除錯時每次都要付

**用一次的下載,換一直都在的可重現。**
"""
from __future__ import annotations

import datetime
import pathlib
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

BASE = pathlib.Path(__file__).resolve().parent

# ── 這就是那份清單 ──────────────────────────────────────────
#
# ★ 每一項後面都寫了「為什麼它在這裡」—— 因為刪東西的人會來看這份清單,
#   而「這個好像沒用到」是清單被砍出洞的第一句話。
FILES = [
    ("bell.py",            "敲鈴器:包住 agent 的 CLI,有新訊息就敲醒它"),
    ("read.py",            "讀信器:對帳、重新加入"),
    ("say.py",             "發言:對帳 → 送出,收成一個動作"),
    ("rule.py",            "房規:讀這個房的判準、改它(擋覆蓋)"),
    ("envfile.py",         "設定載入 —— 上面四支都靠它讀 client.env"),
    ("AGENTS.md",          "協定:agent 的行為守則"),
    ("CLAUDE.md",          "把 AGENTS.md 接給 Claude Code —— 只有一行,漏了整套規矩不生效"),
    ("doc/A2A_MAPPING.md", "AGENTS.md 說「詳見這份」—— 沒帶的話那句話是死路"),
    ("client.env.example", "設定範本:唯一要動手改的東西"),
    ("pyproject.toml",     "uv 環境(對方連 Python 都不用先裝)"),
    ("uv.lock",            "鎖住版本 —— 他遇到的問題要能在我們這邊重現"),
]

# 空目錄:say.py / rule.py 把草稿寫在這裡
DIRS = ["tmp"]


def git_commit() -> str:
    """打包當下的 commit —— 這張出生證明是為了**遠端除錯**。

    ★ zip 散出去之後不會自己更新。對方回報「連不上」時,
      第一句話可以是「你的 VERSION 是哪一版」,而不是猜。
      **遠端除錯最貴的成本是「不知道對方在跑什麼」。**
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=BASE, capture_output=True, text=True, timeout=10)
        commit = out.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               cwd=BASE, capture_output=True, text=True, timeout=10)
        # ★ 有未提交的改動就標出來:那種包裝出去的東西【在 git 裡找不到】,
        #   而不標的話,對方的 VERSION 會指向一個內容不符的 commit —— 比沒有還糟。
        return f"{commit}-dirty" if dirty.stdout.strip() else commit
    except Exception:
        return "unknown"


def version_note() -> str:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"A2A client 包\n"
            f"打包時間: {stamp}\n"
            f"專案版本: {git_commit()}\n"
            f"\n"
            f"回報問題時請附上這三行 —— 這個包不會自己更新,\n"
            f"hub 升級之後要請對方重新拿一份。\n")


def build(out_dir: pathlib.Path) -> pathlib.Path:
    missing = [name for name, _ in FILES if not (BASE / name).exists()]
    if missing:
        raise SystemExit(f"✗ 這些檔案不在:{missing}\n"
                         f"  (清單在 make_client_zip.py 的 FILES,改過名字就要一起改)")

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    target = out_dir / f"a2a-client-{stamp}.zip"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, _why in FILES:
            bundle.write(BASE / name, arcname=f"a2a-client/{name}")
        for folder in DIRS:
            # zip 存不了空目錄,塞一個佔位檔 —— 解壓後那個資料夾才會在
            bundle.writestr(f"a2a-client/{folder}/.gitkeep", "")
        bundle.writestr("a2a-client/VERSION", version_note())
        bundle.writestr("a2a-client/README.txt", readme_text())

    return target


def readme_text() -> str:
    """★ 這份說明只講三件事:改什麼、怎麼開、出事看哪裡。

    收到 zip 的人不需要知道這個專案怎麼設計的 —— 他要的是「怎麼進去」。
    (真的想知道的話,AGENTS.md 就在旁邊。)
    """
    return (
        "A2A 聊天室 —— client 包\n"
        "================================\n"
        "\n"
        "1. 改一行設定\n"
        "   把 client.env.example 複製成 client.env,\n"
        "   把 A2A_SERVER 改成 hub 那台機器的位址,例如:\n"
        "       A2A_SERVER=http://192.168.1.50:8787\n"
        "\n"
        "2. 帶著你的 agent 進去\n"
        "       uv run bell.py --name <你的名字> -- <你的 agent CLI 啟動指令>\n"
        "   例如:\n"
        "       uv run bell.py --name kevin -- claude\n"
        "\n"
        "   這台機器不需要先裝 Python,uv 會自己準備環境。\n"
        "   (沒有 uv 的話:https://docs.astral.sh/uv/ )\n"
        "\n"
        "3. 然後就不用管了\n"
        "   有人在聊天室叫你的 agent,它會被自動叫醒。\n"
        "   agent 該怎麼表現寫在 AGENTS.md 裡,它自己會讀。\n"
        "\n"
        "--------------------------------\n"
        "連不上的時候:\n"
        "  - 確認 hub 那台在跑,而且你們在同一個區網\n"
        "  - 確認 client.env 的位址沒打錯(注意 http:// 開頭)\n"
        "  - 回報問題時附上 VERSION 這個檔案的內容\n"
        "\n"
        "這個包不會自己更新。hub 升級之後,跟對方要一份新的。\n"
    )


def main() -> int:
    target = build(BASE / "dist")
    size_kb = target.stat().st_size / 1024
    print(f"✓ {target.name}({size_kb:.0f} KB)")
    print(f"  位置:{target}")
    print(f"  版本:{git_commit()}")
    print()
    print("  傳給對方,他解壓之後改 client.env 裡的一行 IP 就能用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
