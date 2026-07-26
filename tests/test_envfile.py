"""設定檔載入的行為測試。

這裡鎖住的不只是「能不能讀檔」,更重要的是【優先順序】:
真正的環境變數必須贏過設定檔,否則 `HOST=127.0.0.1 uv run server.py` 這類
臨時覆寫會悄悄失效 —— 而「我明明設了卻沒生效」是最難查的一種問題。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from envfile import load_env_file, parse_env_file  # noqa: E402


def test_解析_基本鍵值(tmp_path):
    f = tmp_path / "a.env"
    f.write_text("A2A_SERVER=http://127.0.0.1:8787\n", encoding="utf-8")
    assert parse_env_file(f) == {"A2A_SERVER": "http://127.0.0.1:8787"}


def test_解析_忽略註解與空行(tmp_path):
    f = tmp_path / "a.env"
    f.write_text("# 這是註解\n\nKEY=值\n\n# 又一個註解\n", encoding="utf-8")
    assert parse_env_file(f) == {"KEY": "值"}


def test_解析_去掉等號兩邊的空白(tmp_path):
    f = tmp_path / "a.env"
    f.write_text("  KEY  =  值有空白  \n", encoding="utf-8")
    assert parse_env_file(f) == {"KEY": "值有空白"}


def test_解析_壞掉的行不會炸掉整份檔案(tmp_path):
    """設定檔打錯一行,不該讓整個程式起不來 —— 略過它,其餘照讀。"""
    f = tmp_path / "a.env"
    f.write_text("好的=1\n這行沒有等號\n也好的=2\n", encoding="utf-8")
    assert parse_env_file(f) == {"好的": "1", "也好的": "2"}


def test_解析_值可以是空的(tmp_path):
    f = tmp_path / "a.env"
    f.write_text("空值=\n", encoding="utf-8")
    assert parse_env_file(f) == {"空值": ""}


def test_解析_檔案不存在回空字典而不是報錯(tmp_path):
    """還沒建設定檔是正常狀態,不是錯誤。"""
    assert parse_env_file(tmp_path / "根本沒這個檔.env") == {}


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


@pytest.mark.parametrize("name", ["client.env.example", "server.env.example"])
def test_範本檔本身要解析得動(name):
    """範本是給人複製的,如果連自己都解析不動就太難看了。"""
    path = Path(__file__).resolve().parent.parent / name
    assert path.exists(), f"{name} 應該存在(它是給使用者複製的範本)"
    parsed = parse_env_file(path)
    assert parsed, f"{name} 應該至少有一個有效設定"
