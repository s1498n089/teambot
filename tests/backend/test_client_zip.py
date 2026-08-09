"""client 包 —— 守的是「清單會不會過期」,不是「zip 壓得對不對」。

★ 這份測試存在的理由:**清單寫在腳本裡,而腳本會過期。**

  今天有人給 `say.py` 加一個 `import requests`,或把某段抽成新的 `helper.py` ——
  打包腳本不會知道,zip 照樣產得出來,而對方解壓之後才發現跑不動。
  **那個失敗發生在別人的電腦上、幾天之後、而且我們重現不了。**

  所以這裡驗的是:**包裡每一支程式的 import,都在包裡或在依賴裡找得到。**
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import tomllib
import zipfile

import pytest

import make_client_zip as pack


BASE = pathlib.Path(pack.__file__).resolve().parent


def imported_modules(source: str) -> set[str]:
    """這支程式用到哪些**頂層**模組名。

    ★ 只看頂層(`urllib.request` → `urllib`):我們要問的是「這個套件在不在」,
      不是「這個子模組叫什麼」。
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def declared_dependencies() -> set[str]:
    """pyproject 宣告了哪些第三方套件(轉成 import 時的名字)。"""
    with (BASE / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    names = set()
    for spec in data["project"]["dependencies"]:
        # "python-dotenv>=1.2.2" → "python-dotenv"
        raw = spec.split(">")[0].split("=")[0].split("<")[0].strip()
        names.add(raw)
    # 套件名 ≠ import 名的那幾個,明列出來(而不是猜規則)
    alias = {"python-dotenv": "dotenv", "pywinpty": "winpty"}
    return {alias.get(name, name.replace("-", "_")) for name in names}


class TestBundleIsSelfContained:
    def test_每支程式的_import_都解得開(self):
        """★ 這條是整份測試的核心。

        凡是「不是標準庫、不在依賴清單、也不在包裡」的 import,就是一個洞 ——
        而那個洞要等到對方解壓、跑起來、噴 ModuleNotFoundError 才會被發現。
        """
        packed = {name for name, _ in pack.FILES}
        packed_modules = {name[:-3] for name in packed if name.endswith(".py")}
        allowed = set(sys.stdlib_module_names) | declared_dependencies() | packed_modules

        problems = []
        for name in sorted(packed_modules):
            source = (BASE / f"{name}.py").read_text(encoding="utf-8")
            for module in sorted(imported_modules(source)):
                if module not in allowed:
                    problems.append(f"{name}.py 用了 {module},但它不在包裡也不在依賴裡")

        assert not problems, "\n".join(problems)

    def test_清單裡的檔案都真的存在(self):
        """★ 改檔名時最容易漏掉這份清單 —— 而 zip 會照樣產出,只是少一個檔。"""
        missing = [name for name, _ in pack.FILES if not (BASE / name).exists()]
        assert not missing, f"清單指向不存在的檔案:{missing}"

    def test_每一項都寫了為什麼在這裡(self):
        """★ 清單被砍出洞的第一句話是「這個好像沒用到」。

        每一項旁邊的那句說明就是在回答它 —— 空的說明等於沒有防線。
        """
        blank = [name for name, why in pack.FILES if not why.strip()]
        assert not blank, f"這些沒寫理由:{blank}"

    def test_協定兩份都要在(self):
        """★★ `CLAUDE.md` 只有一行內容(把 AGENTS.md 接過來),**所以最容易被當成可有可無**。

        少了它,Claude Code 在對方機器上不會自動讀到協定 —— agent 進來了,
        但不知道規矩:不對帳、不推 cursor、可能還亂發言。
        而這個失敗**沒有錯誤訊息**,只有「那個 agent 怎麼怪怪的」。
        """
        names = {name for name, _ in pack.FILES}
        assert "AGENTS.md" in names and "CLAUDE.md" in names

    def test_協定裡指到的檔案都要在包裡(self):
        """★★★ 這條比「補上某個漏掉的檔」值錢:**它把整類缺永遠擋住。**

        `AGENTS.md` 是對方的 agent 唯一會讀的協定,而它裡面每一句「詳見 X」
        都是一條路。X 沒進包的話那條路通往空氣 —— 而且**不會有任何錯誤訊息**,
        只有一個 agent 找不到它需要的說明,然後照自己的想像做事。

        (這一條是被實際咬到才寫的:`doc/A2A_MAPPING.md` 第一版沒進包。)
        """
        agents = (BASE / "AGENTS.md").read_text(encoding="utf-8")
        packed = {name for name, _ in pack.FILES}
        # 只看有副檔名的路徑,而且排除自我引用
        referenced = {
            token for token in re.findall(r"[A-Za-z0-9_/.-]+\.md", agents)
            if token not in ("AGENTS.md", ".md")
        }
        missing = sorted(ref for ref in referenced if ref not in packed)
        assert not missing, (
            f"AGENTS.md 指到這些檔案,但它們沒進包:{missing}\n"
            f"  對方照著找會找不到 —— 要嘛加進 FILES,要嘛把那句引用拿掉"
        )

    def test_不該把伺服器那半包進去(self):
        """★ 給人 client 包的前提是「他不需要跑伺服器」。

        包進 server.py 不會壞掉,但會讓收到的人以為自己要跑一個 ——
        **多給的東西也是一種誤導。**
        """
        names = {name for name, _ in pack.FILES}
        for forbidden in ("server.py", "a2a.py", "hub.bat", "server.env.example"):
            assert forbidden not in names, f"{forbidden} 是 hub 那半的東西"


class TestBundleContents:
    def test_打出來的包含_VERSION_與說明(self, tmp_path):
        target = pack.build(tmp_path)
        with zipfile.ZipFile(target) as bundle:
            names = bundle.namelist()
            assert "a2a-client/VERSION" in names, "沒有出生證明就無法遠端除錯"
            assert "a2a-client/README.txt" in names
            assert "a2a-client/tmp/.gitkeep" in names, "tmp/ 要存在,工具會往裡面寫草稿"

            version = bundle.read("a2a-client/VERSION").decode("utf-8")
            assert "專案版本" in version
            readme = bundle.read("a2a-client/README.txt").decode("utf-8")
            assert "A2A_SERVER" in readme, "說明要講出那唯一要改的一行"

    def test_每個檔案都放在同一個資料夾裡(self, tmp_path):
        """★ 解壓之後要是一個資料夾,不是散一地檔案。

        (在下載資料夾解壓縮一包散檔,是那種一次就讓人放棄的體驗。)
        """
        target = pack.build(tmp_path)
        with zipfile.ZipFile(target) as bundle:
            assert all(n.startswith("a2a-client/") for n in bundle.namelist())

    def test_髒工作區要標在版本上(self, tmp_path, monkeypatch):
        """★★ 有未提交的改動時打包出去,那份 code 在 git 裡【找不到】。

        不標的話,對方的 VERSION 會指向一個內容不符的 commit —— 比沒有版本更糟:
        它讓我們拿著錯的東西去重現。
        """
        class FakeRun:
            def __init__(self, out):
                self.stdout = out

        def fake(cmd, **_kwargs):
            if "rev-parse" in cmd:
                return FakeRun("abc1234\n")
            return FakeRun(" M server.py\n")     # 工作區有改動

        monkeypatch.setattr(pack.subprocess, "run", fake)
        assert pack.git_commit().endswith("-dirty")
