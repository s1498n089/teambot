"""say.py(發言器)—— 它存在的理由是「不替 agent 做判斷」,所以測的重點是【它不做什麼】。

這支工具最危險的失敗不是崩潰,是**悄悄替 agent 聲明了它沒做過的事**:
推 cursor 說「我讀了」、代填 expect_last_id 說「我看過並判斷過」。
那種錯不會報錯、不會紅、只會讓一則訊息永遠沒人讀到。所以下面每一條
斷言「沒有寫 cursor」的測試,都比斷言「有送出」的那幾條重要。
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import say as say_mod


class FakeHTTP:
    """把 urllib 換掉:GET 回預先排好的資料,POST 記下來(或丟 409)。"""

    def __init__(self, get_results: list[dict], post_result=None):
        self.get_results = list(get_results)
        self.post_result = post_result
        self.posted: list[dict] = []
        self.get_urls: list[str] = []

    def __call__(self, request, *_args, **_kwargs):
        if isinstance(request, str):                      # GET:直接傳網址
            self.get_urls.append(request)
            return self._body(self.get_results.pop(0))
        self.posted.append(json.loads(request.data.decode("utf-8")))
        if isinstance(self.post_result, urllib.error.HTTPError):
            raise self.post_result
        return self._body(self.post_result)

    @staticmethod
    def _body(payload: dict):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        return Response(json.dumps(payload).encode("utf-8"))


def http_409():
    return urllib.error.HTTPError("u", 409, "stale", {},
                                  io.BytesIO(b'{"error":"stale","last_id":9}'))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """把 say.py 的 BASE 指到 tmp,cursor 檔就落在隔離目錄裡。"""
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(say_mod, "BASE", tmp_path)
    return tmp_path


def run(monkeypatch, http, *argv) -> int:
    monkeypatch.setattr(say_mod.urllib.request, "urlopen", http)
    monkeypatch.setattr(say_mod.sys, "argv", ["say.py", *argv])
    return say_mod.main()


def cursor_of(workspace, name="alice") -> str | None:
    path = workspace / "state" / f"cursor-{name}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


# ---------- 有未讀時:停手,而且什麼都不聲明 ----------

class TestUnreadStopsEverything:
    def test_does_not_post(self, workspace, monkeypatch, capsys):
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "等一下"}],
                          "last_id": 9}])
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 1
        assert http.posted == []                      # 一個字都沒送出去
        assert "#9 bob" in capsys.readouterr().out    # 但有攤在眼前

    def test_does_not_touch_cursor(self, workspace, monkeypatch):
        """★ 這條是整份測試最重要的一條。

        「印出來」不等於「讀到了」—— 中間隔著指令返回這個斷窗(session 被換、
        context 壓縮、視窗關掉)。斷在那裡而 cursor 已經推過去,就是永久漏讀,
        而且沒有人會發現。所以有未讀時 cursor 必須一個字都不動。
        """
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "x"}], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert cursor_of(workspace) is None           # 檔案根本沒被建立

    def test_tells_agent_the_next_expect(self, workspace, monkeypatch, capsys):
        """往前走的是 agent 下次給的 --expect —— 所以要告訴它該給哪個數字。"""
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": "x"}], "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert "--expect 9" in capsys.readouterr().out

    def test_prints_full_text_never_truncates(self, workspace, monkeypatch, capsys):
        """★ 擋下發言的這條路【不准截斷】—— 它是 agent 那一刻唯一的閱讀管道。

        截斷會讓 agent 讀到半截,然後下次帶 --expect <新 last_id> 說「我讀了」——
        它是真心的,但它沒做過那件事。**假聲明不會消失,只是換人說**:
        工具寫下的假話讀 code 稽核得到,agent 真心說的假話零行 code 查得到。

        （印一行「有 N 則被截斷」也不夠:那只能讓它知道自己讀不完整,不能讓它讀完。）
        """
        long_text = "字" * 500 + "結尾記號"
        http = FakeHTTP([{"messages": [{"id": 9, "from": "bob", "text": long_text}],
                          "last_id": 9}])
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert "結尾記號" in capsys.readouterr().out      # 最後一個字也要在

    def test_own_messages_do_not_block(self, workspace, monkeypatch):
        """未讀全是自己的話 → 【不擋】,而且 expect 順過去。

        AGENTS.md 步驟 2 明文:「撈回來只有 agent 自己的訊息,是敲鈴器與 cursor
        之間的正常 race,屬於預期內的 no-op」。擋的話,agent 手動發過言、忘了推
        cursor,下次就被自己的話擋在門口 —— 而且它讀完自己說的話還是一樣被擋,
        那不是保護,是死結。

        ★ 那把 expect 推過自己的訊息算不算代寫?不算,而理由不是「無害」:
          那些話是 agent 自己寫的,它必然看過 —— 推過去只是承認一件已經發生的事實。
        """
        http = FakeHTTP([{"messages": [{"id": 9, "from": "alice", "text": "我剛講的"}],
                          "last_id": 9}], {"id": 10})
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 0
        assert http.posted[0]["expect_last_id"] == 9      # 順過自己那則,不是停在 8

    def test_own_messages_mixed_with_others_still_blocks(
            self, workspace, monkeypatch, capsys):
        """★ 邊界:混著別人的訊息就要擋,而且呈現時【自己的也要印】。

        判斷不算自己的訊息,不代表讀的時候要遮住它們 —— 遮掉會讓對話讀起來缺一段。
        """
        http = FakeHTTP([{"messages": [{"id": 9, "from": "alice", "text": "我剛講的"},
                                       {"id": 10, "from": "bob", "text": "等一下"}],
                          "last_id": 10}])
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 1
        assert http.posted == []
        out = capsys.readouterr().out
        assert "我剛講的" in out and "等一下" in out

    def test_get_carries_the_read_receipt(self, workspace, monkeypatch):
        """★ 每一次 GET 都要帶 reader=<名字>,而且【409 之後那次也要】。

        reader= 是 A2A 的已讀回條:hub 靠它把點名這個 agent 的 task 從
        SUBMITTED 轉成 WORKING。掉了不會有任何症狀 —— 直到某天有人問
        「為什麼我派出去的 task 全部 FAILED」。

        而工具化把所有 GET 收攏到這一條路上,等於**把這個回條的存亡集中到一行 code**。
        所以要有一條測試釘住它。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8},
                         {"messages": [], "last_id": 9}], http_409())
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert len(http.get_urls) == 2
        assert all("reader=alice" in url for url in http.get_urls)

    def test_threshold_boundary_both_sides(self, workspace, monkeypatch, capsys):
        """臨界值兩邊都要測 —— 只測一邊的話 off-by-one 會活下來。"""
        n = say_mod.REJOIN_THRESHOLD
        exactly = [{"id": i, "from": "bob", "text": f"第{i}則"} for i in range(1, n + 1)]
        http = FakeHTTP([{"messages": exactly, "last_id": n}])
        run(monkeypatch, http, "--name", "alice", "--expect", "0", "--text", "話")
        assert "第1則" in capsys.readouterr().out           # 剛好 50:印全文

        over = exactly + [{"id": n + 1, "from": "bob", "text": "多一則"}]
        http = FakeHTTP([{"messages": over, "last_id": n + 1}])
        run(monkeypatch, http, "--name", "alice", "--expect", "0", "--text", "話")
        assert "重新加入" in capsys.readouterr().out         # 51:改指路

    def test_huge_backlog_points_to_the_rejoin_flow_instead(
            self, workspace, monkeypatch, capsys):
        """全文有一個邊界:幾百則硬印出來等於洗掉 agent 的 context。

        而那個場景 AGENTS.md 早有處方 —— 那已經不是「發言前對帳」,是「重新加入」。
        門檻跟加入流程的 tail=50 對齊:超過那個量,協定本身就建議跳過中間。
        """
        many = [{"id": i, "from": "bob", "text": f"第{i}則"}
                for i in range(9, 9 + say_mod.REJOIN_THRESHOLD + 1)]
        http = FakeHTTP([{"messages": many, "last_id": many[-1]["id"]}])
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 1
        out = capsys.readouterr().out
        assert "重新加入" in out
        assert "tail=50" in out and "mentioned=alice" in out   # 兩條路都要指
        assert "第9則" not in out                              # 沒有硬印出來


# ---------- 沒有未讀時:送出,而且只在成功之後才推 ----------

class TestCleanSend:
    def test_posts_with_the_agents_own_expect(self, workspace, monkeypatch):
        """expect_last_id 用 agent 自己打的數字,不是工具剛撈到的 last_id。

        差別在語意:那句聲明的內容是「我看過並判斷過」,而判斷不是工具的動作。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 0
        assert http.posted[0]["expect_last_id"] == 8
        assert http.posted[0]["kind"] == "agent"       # 身分自報,漏了會被當人類

    def test_cursor_lands_on_the_returned_id(self, workspace, monkeypatch):
        """★ 正向的那一條:不只要「有推」,還要推到【正確的值】。

        201 是這支工具唯一合法寫 cursor 的地方 —— 也就是唯一可能把 cursor
        推【過頭】的地方,而過頭正是有害的那一邊(漏讀,而且沒有人會發現)。
        推成「房間的 last_id」而不是「回傳的這則 id」,在多人同時發言時
        就會一次跳過好幾則。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 999})
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert cursor_of(workspace) == "999"

    def test_reply_to_is_passed_through(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        run(monkeypatch, http, "--name", "alice", "--expect", "8",
            "--reply-to", "7", "--text", "我的話")
        assert http.posted[0]["reply_to"] == 7


# ---------- 撞車:不重送,也不聲明 ----------

class TestConflict:
    def test_does_not_resend(self, workspace, monkeypatch):
        """409 只送一次就停。

        2026-07-30 實測:某個 agent 撞五次的結果是【一次選擇不發】(對方已講過
        同樣內容)、【兩次改寫】、【零次原文重送】—— 自動重送會把那三次
        全變成重複訊息。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8},
                         {"messages": [{"id": 9, "from": "bob", "text": "搶先"}],
                          "last_id": 9}], http_409())
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--text", "我的話") == 1
        assert len(http.posted) == 1                   # 只試了一次

    def test_refetches_because_409_carries_no_messages(self, workspace, monkeypatch, capsys):
        """409 不夾帶訊息(hub 刻意的),所以要真的再撈一次 —— 不能靠 payload。"""
        http = FakeHTTP([{"messages": [], "last_id": 8},
                         {"messages": [{"id": 9, "from": "bob", "text": "搶先"}],
                          "last_id": 9}], http_409())
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert len(http.get_urls) == 2                 # 對帳一次 + 撞車後再撈一次
        assert "#9 bob" in capsys.readouterr().out

    def test_does_not_touch_cursor(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [], "last_id": 8},
                         {"messages": [{"id": 9, "from": "bob", "text": "x"}], "last_id": 9}],
                        http_409())
        run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "我的話")
        assert cursor_of(workspace) is None


# ---------- 輸入 ----------

class TestInput:
    def test_backtick_in_text_is_rejected(self, workspace, monkeypatch):
        """shell 會把 `foo` 當命令執行掉,而且【不報錯】——對方收到一句缺字的話。

        這條規則原本只是文件裡的提醒,而它在 2026-07-28 一天內被同一個人違反三次。
        提醒守不住的規則就要做進動作的形狀裡。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice", "--expect", "8",
                "--text", "看一下 `SomeClass` 這個")
        assert http.posted == []

    def test_file_path_allows_backticks(self, workspace, monkeypatch, tmp_path):
        """--file 不經過 shell,所以反引號是安全的 —— 這正是它被推薦的理由。"""
        msg = tmp_path / "m.md"
        msg.write_text("看一下 `SomeClass` 這個", encoding="utf-8")
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        assert run(monkeypatch, http, "--name", "alice", "--expect", "8",
                   "--file", str(msg)) == 0
        assert "`SomeClass`" in http.posted[0]["text"]

    def test_expect_is_required(self, workspace, monkeypatch):
        """★ 刻意沒有預設值:那個數字是 agent 親口說的「我讀到這裡了」。

        給預設值(例如讀 cursor 檔)就等於讓工具替它猜,而工具不知道它讀了沒。
        """
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice", "--text", "我的話")

    def test_empty_message_is_rejected(self, workspace, monkeypatch):
        http = FakeHTTP([{"messages": [], "last_id": 8}], {"id": 10})
        with pytest.raises(SystemExit):
            run(monkeypatch, http, "--name", "alice", "--expect", "8", "--text", "   ")
