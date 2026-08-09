"""rule.py(房規工具)—— 它的紅線是「不默默覆蓋別人的判準」。

★ 這支工具跟 say.py 的關係值得先講清楚:兩者都有樂觀鎖,但**擋的東西不同**。

      say.py   擋「我沒讀到最新訊息就發言」—— 保護的是【對話的脈絡】
      rule.py  擋「我根據舊版改寫」—— 保護的是【別人剛寫進去的那一條】

  訊息只增不改,所以撞車最壞是順序不如預期;房規會改寫,撞車就是內容消失,
  而且**寫的人看到成功、讀的人看到一份完整的文件**,沒有人會發現。
"""
from __future__ import annotations

import io
import json

import pytest

import rule as rule_mod


class FakeHTTP:
    """依序回預先排好的資料,並記下每次請求(網址、方法、body)。"""

    def __init__(self, results: list[dict], errors: dict[int, int] | None = None):
        self.results = list(results)
        self.errors = errors or {}          # 第幾次呼叫 → 要拋的 HTTP 狀態碼
        self.calls: list[dict] = []

    def __call__(self, target, *_args, **_kwargs):
        import urllib.error

        url = target if isinstance(target, str) else target.full_url
        method = "GET" if isinstance(target, str) else target.get_method()
        body = None if isinstance(target, str) else target.data
        self.calls.append({"url": url, "method": method, "body": body})

        index = len(self.calls) - 1
        if index in self.errors:
            payload = json.dumps(self.results.pop(0)).encode("utf-8")
            raise urllib.error.HTTPError(url, self.errors[index], "err", {},
                                         io.BytesIO(payload))

        payload = json.dumps(self.results.pop(0)).encode("utf-8")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response(payload)


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    """★ 存根寫在相對路徑 `tmp/` 底下 —— 測試不該去動專案裡那個真的目錄。

    (這也順便證明了存根是**跟著工作目錄走**的:同一台機器、不同專案資料夾,
     各有各的來歷。)
    """
    monkeypatch.chdir(tmp_path)


def run(monkeypatch, http, *argv) -> int:
    monkeypatch.setattr(rule_mod.urllib.request, "urlopen", http)
    monkeypatch.setattr(rule_mod.sys, "argv", ["rule.py", *argv])
    return rule_mod.main()


class TestReading:
    def test_get_把全文送到_stdout_提示走_stderr(self, monkeypatch, capsys):
        """★ 這個分流是為了 `--get > 檔案` 那個用法:導出去的必須是【乾淨的內容】。

        提示如果混進 stdout,存下來的房規開頭就會多一行雜訊 ——
        而下一次 `--put` 會把那行雜訊當成判準的一部分寫回去。
        """
        http = FakeHTTP([{"text": "# 判準\n第一條", "revision": "r1"}])
        assert run(monkeypatch, http, "--name", "alice", "--get") == 0
        captured = capsys.readouterr()
        assert captured.out == "# 判準\n第一條", "stdout 只有內容,連結尾換行都不加"
        assert captured.err == ""

    def test_還沒有判準時說清楚那不是壞掉(self, monkeypatch, capsys):
        http = FakeHTTP([{"text": "", "revision": ""}])
        assert run(monkeypatch, http, "--name", "alice", "--get") == 0
        captured = capsys.readouterr()
        assert captured.out == "", "空的就是空的 —— 導出去的檔案不該有內容"
        assert "還沒有判準" in captured.err


class TestWriting:
    def test_送回去之前自己先拿一次指紋(self, monkeypatch, tmp_path, capsys):
        """★ 指紋不該由人手打:那串 hash 沒有人記得住,要求人抄只會抄錯。

        送的是 `--get` 那一刻記下的存根,**不是 `--put` 當下現拿的**。
        """
        draft = tmp_path / "rule.md"
        draft.write_text("新的判準", encoding="utf-8")

        # 先走一次 --get,讓存根落地
        run(monkeypatch, FakeHTTP([{"text": "舊的判準", "revision": "r1"}]),
            "--name", "alice", "--get")

        http = FakeHTTP([{"ok": True, "room": "main", "revision": "r2"}])
        assert run(monkeypatch, http, "--name", "alice", "--put", str(draft)) == 0

        assert len(http.calls) == 1, "★ 送出時【不再現拿一次】—— 現拿會讓整道鎖失效"
        assert http.calls[0]["method"] == "PUT"
        sent = json.loads(http.calls[0]["body"].decode("utf-8"))
        assert sent["text"] == "新的判準"
        assert sent["expect_revision"] == "r1", "帶的是【--get 那一刻】拿到的版本"
        assert "by=alice" in http.calls[0]["url"], "誰改的要記在帳上"

    def test_現拿指紋等於沒有鎖(self, monkeypatch, tmp_path):
        """★★★ 這條釘住的是一個**改對過的設計**,不是一個功能。

        第一版讓 `--put` 自己先 GET 一次拿指紋 —— 看起來很體貼(不必要人抄 hash),
        **而它讓整道鎖形同虛設**:那樣保證的是「指紋是最新的」,
        不是「手上這份是從最新版改的」。於是 agent 拿著三小時前的內容送出去,
        照樣成功,把中間所有人的修改蓋掉 —— 而雙方都看到 200。

        端到端測試當場抓到:bob 加的那條被 alice 的舊版覆蓋。
        單元測試抓不到它,因為每一支單獨看都「正確」。
        """
        draft = tmp_path / "rule.md"
        draft.write_text("內容", encoding="utf-8")
        # 沒有存根 → 只該送出一個 PUT,而且帶空字串(=「我認為還沒有判準」)
        http = FakeHTTP([{"ok": True, "room": "main", "revision": "r1"}])
        run(monkeypatch, http, "--name", "nobody", "--put", str(draft))
        assert [c["method"] for c in http.calls] == ["PUT"]
        assert json.loads(http.calls[0]["body"].decode("utf-8"))["expect_revision"] == ""

    def test_存根一個名字一份(self, monkeypatch, tmp_path):
        """★ 好幾個 agent 常跑在同一個專案目錄裡。共用一份存根的話,
        bob 拿一次就蓋掉 alice 的來歷,而那正好會讓 alice 覆蓋 bob ——
        **防覆蓋的機制自己被覆蓋。**
        """
        run(monkeypatch, FakeHTTP([{"text": "x", "revision": "ra"}]),
            "--name", "alice", "--get")
        run(monkeypatch, FakeHTTP([{"text": "x", "revision": "rb"}]),
            "--name", "bob", "--get")
        assert rule_mod.read_stamp("main", "alice") == "ra"
        assert rule_mod.read_stamp("main", "bob") == "rb"

    def test_撞車時停手_而且不重試(self, monkeypatch, tmp_path, capsys):
        """★★ 這是整支工具存在的理由。

        不自動重試是刻意的:對方剛加的那條,可能正好讓 agent 想寫的這條
        變得沒必要 —— **要不要改口是判斷,不是工具的動作。**
        """
        draft = tmp_path / "rule.md"
        draft.write_text("我改的版本", encoding="utf-8")
        run(monkeypatch, FakeHTTP([{"text": "舊的", "revision": "r1"}]),
            "--name", "alice", "--get")

        http = FakeHTTP([{"error": "stale_rule", "revision": "r9"}], errors={0: 409})
        assert run(monkeypatch, http, "--name", "alice", "--put", str(draft)) == 2

        assert len(http.calls) == 1, "撞到就停 —— 不能自己再送一次"
        err = capsys.readouterr().err
        assert "改過房規" in err
        assert "--get" in err, "要告訴 agent 怎麼重新開始"

    def test_檔案不在就明講(self, monkeypatch, capsys):
        http = FakeHTTP([])
        assert run(monkeypatch, http, "--name", "alice", "--put", "沒這個檔.md") == 1
        assert "找不到檔案" in capsys.readouterr().err
        assert http.calls == [], "連問都不必問 —— 沒東西可送"


class TestModes:
    def test_get_與_put_二選一(self, monkeypatch):
        http = FakeHTTP([])
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice")
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice", "--get", "--put", "x.md")
