"""read.py(讀信器)—— 它的紅線是「一次都不寫 cursor」,所以測的重點也是【它不做什麼】。

這支工具最危險的失敗不是崩潰,是**替 agent 聲明了它沒做過的事**:
把字印在螢幕上、然後順手推 cursor 說「讀完了」。那種錯不報錯、不變紅,
只會讓一則訊息永遠沒人讀到 —— 而且沒有人會發現。
"""
from __future__ import annotations

import io
import json

import pytest

import read as read_mod


class FakeHTTP:
    """把 urllib 換掉:依序回預先排好的資料,並記下每次請求的網址。"""

    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.urls: list[str] = []

    def __call__(self, url, *_args, **_kwargs):
        self.urls.append(url)
        payload = json.dumps(self.results.pop(0)).encode("utf-8")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response(payload)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(read_mod, "BASE", tmp_path)
    return tmp_path


def run(monkeypatch, http, *argv) -> int:
    monkeypatch.setattr(read_mod.urllib.request, "urlopen", http)
    monkeypatch.setattr(read_mod.sys, "argv", ["read.py", *argv])
    return read_mod.main()


def cursor_exists(workspace, name="alice") -> bool:
    return (workspace / "state" / f"cursor-{name}.txt").exists()


# ---------- 紅線:一次都不寫 cursor ----------

class TestNeverWritesCursor:
    """★ 這一族是整份測試的核心。

    「印出來」到「agent 真的讀到」之間隔著【指令返回】這個斷窗 ——
    斷在那裡(session 被換、context 壓縮、視窗關掉)而 cursor 已經推過去,
    就是永久漏讀且無人發現。

    say.py 能在 201 之後推,是因為它親自 GET 過、親自 POST 成功;
    read 沒有任何動作能證明「讀」發生了。**同一條執法句,兩個相反的結論。**
    """

    def test_normal_read_does_not_write_cursor(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "hi"}], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        assert not cursor_exists(workspace)

    def test_rejoin_does_not_write_cursor(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "hi"}], "last_id": 9},
                         {"messages": [], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--rejoin")
        assert not cursor_exists(workspace)

    def test_empty_read_does_not_write_cursor(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [], "last_id": 8}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        assert not cursor_exists(workspace)


# ---------- 把筆遞過去:尾行的兩條路 ----------

class TestHandsOverThePen:
    def test_offers_both_paths(self, workspace, monkeypatch, capsys):
        """讀完之後有兩條路,而【不發言】那條是最常走的。

        只給「接 say.py」的話,最常走的那條反而沒指引 ——
        agent 醒來看一眼、發現沒自己的事、回去睡,那條路的收尾是推 cursor。
        """
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "hi"}], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        out = capsys.readouterr().out
        assert "say.py --name alice --expect 9" in out      # 要發言
        assert "printf '%s' 9 >" in out                     # 不發言

    def test_cursor_command_is_absolute(self, workspace, monkeypatch, capsys):
        """★ 印絕對路徑,不是 state/cursor-alice.txt。

        agent 的工作目錄不一定在專案根,而相對路徑寫到錯的地方是**安靜失敗**:
        檔案建出來了、指令也沒報錯,只是 bell 讀的還是舊的那個 ——
        於是它一直敲,而 agent 一直以為自己追上了。
        """
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "hi"}], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        assert str(workspace / "state" / "cursor-alice.txt") in capsys.readouterr().out

    def test_rejoin_offers_the_init_command(self, workspace, monkeypatch, capsys):
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "hi"}], "last_id": 42},
                         {"messages": [], "last_id": 42}])
        run(monkeypatch, http, "--name", "alice", "--rejoin")
        assert "printf '%s' 42 >" in capsys.readouterr().out


# ---------- 撈:reader= 與兩段式 rejoin ----------

class TestFetching:
    def test_every_request_carries_the_read_receipt(self, workspace, monkeypatch):
        """★ reader=<名字> 是 A2A 的已讀回條,漏掉不會有任何症狀 ——
        直到有人問「為什麼我派出去的 task 全部 FAILED」。

        把 GET 收進工具,等於把這個回條的存亡集中到一行 code,所以要釘住它。
        """
        http = FakeHTTP([{"messages": [], "last_id": 9},
                         {"messages": [], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--rejoin")
        assert len(http.urls) == 2
        assert all("reader=alice" in url for url in http.urls)

    def test_rejoin_does_both_steps(self, workspace, monkeypatch):
        """加入流程是兩步,第二步是安全網:可以跳過歷史,但不能漏掉找你的人。"""
        http = FakeHTTP([{"messages": [], "last_id": 9},
                         {"messages": [], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--rejoin")
        assert f"tail={read_mod.REJOIN_TAIL}" in http.urls[0]
        assert "mentioned=alice" in http.urls[1] and "since_id=0" in http.urls[1]

    def test_huge_backlog_points_to_rejoin(self, workspace, monkeypatch, capsys):
        """落後太多就不是對帳了 —— 硬印幾百則會洗掉 agent 的 context。"""
        many = [{"id": i, "from": "bob", "text": f"第{i}則"}
                for i in range(1, read_mod.REJOIN_TAIL + 2)]
        http = FakeHTTP([{"messages": many, "last_id": many[-1]["id"]}])
        assert run(monkeypatch, http, "--name", "alice", "--expect", "0") == 1
        out = capsys.readouterr().out
        assert "--rejoin" in out
        assert "第1則" not in out


# ---------- 呈現 ----------

class TestPresentation:
    def test_prints_full_text(self, workspace, monkeypatch, capsys):
        """★ 不截斷 —— 截斷會讓 agent 讀到半截,然後真心地聲明「我讀了」。

        工具寫下的假話還能讀 code 稽核出來,agent 真心說的假話零行 code 查得到。
        """
        long_text = "字" * 800 + "結尾記號"
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": long_text}],
                          "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        assert "結尾記號" in capsys.readouterr().out

    def test_shows_mentions_and_reply(self, workspace, monkeypatch, capsys):
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "看這個",
                                        "mentions": ["alice"], "reply_to": 7}],
                          "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8")
        out = capsys.readouterr().out
        assert "@alice" in out and "#7" in out

    def test_called_scan_is_capped_and_says_so(self, workspace, monkeypatch, capsys):
        """★ 點名掃描【可以】截斷,而且必須截 —— 但要說清楚截了多少。

        差別在「它會不會變成一句聲明」:
            --expect 那條   讀完就要說「我讀到 N 了」→ 讀到半截等於假聲明,不准截
            點名掃描        產出是「有沒有人找過我、在哪幾則」,不是「我讀完了」

        而不截的代價是實測出來的:某個名字被點名 236 次,全文印出來會直接洗掉
        agent 的 context —— 那正是這整套設計要保護的東西。
        """
        many = [{"id": i, "from": "bob", "text": f"點名{i} " + "字" * 500}
                for i in range(1, 101)]
        http = FakeHTTP([{"messages": [], "last_id": 200},
                         {"messages": many, "last_id": 200}])
        run(monkeypatch, http, "--name", "alice", "--rejoin")
        out = capsys.readouterr().out

        assert "共 100 則" in out                       # 總數要誠實講
        assert f"還有 {100 - read_mod.CALLED_TAIL} 則更早的" in out
        assert "點名1 " not in out                      # 最早那些沒印
        assert "點名100" in out                         # 最近的有印
        assert "掃描" in out and "讀完" in out           # 說明它不是「讀完」
        assert "curl" in out                            # 給回頭撈脈絡的方法

    def test_empty_says_it_is_a_normal_race(self, workspace, monkeypatch, capsys):
        """撈到空的是敲鈴器與 cursor 之間的正常 race —— 說清楚,免得 agent 疑惑。"""
        http = FakeHTTP([{"messages": [], "last_id": 8}])
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8") == 0
        assert "race" in capsys.readouterr().out


# ---------- 參數 ----------

class TestArgs:
    def test_expect_and_rejoin_are_exclusive(self, workspace, monkeypatch):
        """兩個模式的語意不同(日常對帳 vs 重新加入),同時給就是沒想清楚要做什麼。"""
        http = FakeHTTP([])
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice", "--expect", "8", "--rejoin")

    def test_one_of_them_is_required(self, workspace, monkeypatch):
        http = FakeHTTP([])
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice")
