"""設定檔載入的行為測試。

★ .env 的【解析】交給 python-dotenv,這裡只測**我們自己加的那一層**:

      環境變數優先於檔案、回報套用了哪些鍵、壞行要出聲、警告不能炸掉呼叫者。

  解析規則本身(引號、export 前綴、變數展開……)不在這裡測 —— 那是 python-dotenv 的責任。
  **替第三方套件寫測試不是我們的工作**:它們抓不到我們的 bug,只會在對方改版時發假警報。

  唯一非測不可的是「環境變數必須贏過設定檔」:它是這個模組的靈魂。
  換套件時我實際驗過 dotenv 的預設就是這個方向 ——
  但「現在是對的」跟「以後也是對的」是兩件事,所以留一條測試看著它。
"""
import os
import sys
from pathlib import Path

import pytest

# 專案根目錄 = 這個檔往上兩層(tests/backend/x.py → tests/backend → tests → 根)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from envfile import load_env_file  # noqa: E402


def test_套用_填進環境變數(tmp_path, monkeypatch):
    f = tmp_path / "a.env"
    f.write_text("測試用鍵=來自檔案\n", encoding="utf-8")
    monkeypatch.delenv("測試用鍵", raising=False)

    applied = load_env_file(f)

    assert applied == ["測試用鍵"]
    assert os.environ["測試用鍵"] == "來自檔案"


def test_套用_已存在的環境變數優先(tmp_path, monkeypatch):
    """★ 這條是整個模組最重要的行為:檔案讓步,不覆蓋。

    否則 `HOST=127.0.0.1 uv run server.py` 這種臨時覆寫會被設定檔蓋掉。
    """
    f = tmp_path / "a.env"
    f.write_text("測試用鍵=來自檔案\n", encoding="utf-8")
    monkeypatch.setenv("測試用鍵", "命令列先設好的")

    applied = load_env_file(f)

    assert os.environ["測試用鍵"] == "命令列先設好的", "環境變數必須贏過設定檔"
    assert "測試用鍵" not in applied, "沒套用的鍵不該出現在回報清單裡"


def test_套用_回報清單只含真的套用了的鍵(tmp_path, monkeypatch):
    """回傳值是給啟動訊息用的,要能一眼看出設定檔到底有沒有生效。"""
    f = tmp_path / "a.env"
    f.write_text("有套用=1\n被擋下=2\n", encoding="utf-8")
    monkeypatch.delenv("有套用", raising=False)
    monkeypatch.setenv("被擋下", "環境變數的值")

    assert load_env_file(f) == ["有套用"]


def test_檔案不存在不算錯誤(tmp_path):
    """還沒建設定檔是正常狀態,不是錯誤 —— 回空清單,不丟例外。"""
    assert load_env_file(tmp_path / "根本沒這個檔.env") == []


@pytest.mark.parametrize("name", ["client.env.example", "server.env.example"])
def test_範本檔本身要解析得動(name, monkeypatch):
    """範本是給人複製的,如果連自己都解析不動就太難看了。

    這一條測的是【我們的檔案】,不是 dotenv 的解析能力,所以它留下來。
    """
    path = ROOT / name
    assert path.exists(), f"{name} 應該存在(它是給使用者複製的範本)"

    # 讓範本裡的鍵都算「新的」,否則跑測試時環境裡已經有的會被算成「沒套用」
    for key in ("HOST", "PORT", "PUBLIC_URL", "A2A_SERVER", "A2A_ROOM"):
        monkeypatch.delenv(key, raising=False)

    assert load_env_file(path), f"{name} 應該至少有一個有效設定"


def test_壞行會印警告到_stderr(tmp_path, capsys):
    """★ 略過壞行不能靜悄悄 —— 那正好製造 load_env_file 自己警告過的
    「我明明設了卻沒生效」。不炸、不擋,但要看得見。

    ★ 換成 python-dotenv 之後這條【更重要了】:dotenv 遇到沒有等號的行,
      會把它收成 value=None 而且完全不出聲。出聲是我們這一層加上去的。
    """
    f = tmp_path / "a.env"
    f.write_text("好的=1\n這行沒有等號\n", encoding="utf-8")

    load_env_file(f)

    stderr = capsys.readouterr().err
    assert "已略過" in stderr
    assert "這行沒有等號" in stderr, "警告要指出是哪一行的內容(沒有行號,理由見 envfile)"


def test_警告印不出去也不能炸掉呼叫者(tmp_path, monkeypatch):
    """警告是輔助資訊,絕不該讓程式起不來。

    這裡模擬主控台印不出中文的情況(cp950 那類)—— envfile.warn 必須吞掉它,
    而載入本身要照常完成。
    """
    def exploding_print(*args, **kwargs):
        raise UnicodeEncodeError("cp950", "x", 0, 1, "模擬編碼失敗")

    monkeypatch.setattr("builtins.print", exploding_print)
    monkeypatch.delenv("好的", raising=False)

    f = tmp_path / "a.env"
    f.write_text("好的=1\n壞行\n", encoding="utf-8")

    assert load_env_file(f) == ["好的"]   # 照常載入,沒有炸
