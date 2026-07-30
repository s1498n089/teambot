"""單元/邊界層:純函式與小類別,毫秒級,不建 app。"""
from __future__ import annotations

import threading
import time

import pytest

import a2a as a2a_mod
import bell as bell_mod
import server as server_mod
from server import MentionParser, MessageStore, RateLimiter, TokenStore


# ---------- MentionParser ----------

class TestMentionParser:
    # ★ 這是【測試資料】不是名冊 —— 名字刻意用不存在的成員(carol),
    #   免得讀的人以為它在描述誰是這個聊天室的成員。
    KNOWN = {"alice", "bob", "carol"}

    def test_basic_mention(self):
        assert MentionParser.parse(self.KNOWN, "hi @bob") == ["bob"]

    def test_sticky_cjk_suffix(self):
        """已知名字後緊黏中文要能剝出名字(@bob呢 → bob)。"""
        assert MentionParser.parse(self.KNOWN, "@bob呢?") == ["bob"]

    def test_ascii_lookbehind_not_mention(self):
        """@ 前貼著 ASCII(alice@main、email)不算點名。"""
        assert MentionParser.parse(self.KNOWN, "位址是 alice@main 這樣") == []

    def test_mature_room_rejects_unknown_ascii(self):
        """成熟房間(≥2 名成員)不吃冷啟動規則:@media 這類術語不誤判。"""
        assert MentionParser.parse(self.KNOWN, "用 @media 查詢") == []

    def test_coldstart_allows_unknown_ascii(self):
        """新房間(<2 名成員)第一句 @alice 要叫得到人。"""
        assert MentionParser.parse(set(), "@alice 在嗎") == ["alice"]

    def test_coldstart_rejects_pure_cjk_token(self):
        """冷啟動也只認 ASCII 開頭的未知名——純中文 @ 是裝飾字。"""
        assert MentionParser.parse(set(), "@白名單 檢查") == []

    def test_dedup_keeps_order(self):
        assert MentionParser.parse(self.KNOWN, "@bob @alice @bob") == ["bob", "alice"]

    def test_longest_prefix_wins(self):
        known = {"al", "alice"}
        assert MentionParser.parse(known, "@alice好") == ["alice"]


# ---------- sanitize_sender ----------

class TestSanitizeSender:
    def test_valid_names(self):
        assert server_mod.sanitize_sender("alice") == "alice"
        assert server_mod.sanitize_sender("  bob  ") == "bob"
        assert server_mod.sanitize_sender("小明") == "小明"
        assert server_mod.sanitize_sender("ci-bot_2") == "ci-bot_2"

    def test_rejects_empty_and_whitespace(self):
        assert server_mod.sanitize_sender("") is None
        assert server_mod.sanitize_sender("   ") is None

    def test_rejects_at_and_inner_space(self):
        assert server_mod.sanitize_sender("a b") is None
        assert server_mod.sanitize_sender("@bob") is None

    def test_length_boundary_32(self):
        assert server_mod.sanitize_sender("a" * 32) == "a" * 32
        assert server_mod.sanitize_sender("a" * 33) is None


# ---------- MessageStore ----------

class TestPublicHost:
    """PUBLIC_HOST 只放主機/IP,port 由 PORT 接上 —— 設定裡 port 只有一個地方寫。"""

    def test_bare_host_passes(self):
        assert server_mod.clean_public_host("10.199.20.151") == "10.199.20.151"
        assert server_mod.clean_public_host(" hub.local ") == "hub.local"

    def test_blank_means_not_set(self):
        assert server_mod.clean_public_host("") == ""
        assert server_mod.clean_public_host("   ") == ""

    def test_ipv6_literal_passes(self):
        assert server_mod.clean_public_host("[::1]") == "[::1]"

    @pytest.mark.parametrize("bad", [
        "http://10.199.20.151:8787",   # 舊格式:整條網址
        "10.199.20.151:8787",          # 帶了 port
        "10.199.20.151/",              # 帶了路徑
    ])
    def test_url_shaped_values_are_rejected_loudly(self, bad):
        """寫成網址就開機炸掉。

        默默剝掉也做得到,但那樣錯誤會【沉下去】:hub 照跑,名片卻是壞的,
        症狀要等到對方派的 task 逾時才浮出來,而那時沒有人會聯想到這一行設定。
        """
        with pytest.raises(SystemExit) as excinfo:
            server_mod.clean_public_host(bad)
        assert "PUBLIC_HOST" in str(excinfo.value)

    def test_base_url_follows_port(self, isolated_base, monkeypatch):
        """改 PORT,對外網址要跟著動 —— 這是把 port 從設定值裡拿掉的全部理由。"""
        monkeypatch.setenv("PUBLIC_HOST", "10.199.20.151")
        monkeypatch.setenv("PORT", "9999")
        assert server_mod.Hub().base_url == "http://10.199.20.151:9999"

    def test_falls_back_to_loopback(self, isolated_base):
        assert server_mod.Hub(port=8787).base_url == "http://127.0.0.1:8787"


class TestMessageStore:
    def test_per_room_independent_ids(self, tmp_path):
        store = MessageStore(tmp_path / "chat.jsonl")
        a1 = store.append("roomA", "alice", "hi", [])
        store.append("roomB", "bob", "yo", [])
        a2 = store.append("roomA", "alice", "again", [])
        assert (a1["id"], a2["id"]) == (1, 2)  # roomB 的流量不影響 roomA

    def test_empty_room_last_id_zero(self, tmp_path):
        store = MessageStore(tmp_path / "chat.jsonl")
        assert store.last_id("nowhere") == 0
        assert store.query("nowhere")[0] == []

    def test_bad_line_skipped_on_load(self, tmp_path):
        p = tmp_path / "chat.jsonl"
        p.write_text('{"id":1,"room":"r","from":"a","text":"ok","mentions":[],"ts":"t"}\n'
                     "THIS IS NOT JSON\n"
                     '{"id":2,"room":"r","from":"a","text":"ok2","mentions":[],"ts":"t"}\n',
                     encoding="utf-8")
        store = MessageStore(p)
        assert store.count("r") == 2  # 壞行跳過,不炸啟動

    def test_query_three_modes(self, tmp_path):
        store = MessageStore(tmp_path / "chat.jsonl")
        for i in range(5):
            store.append("r", "a", f"m{i}", [])
        since, last = store.query("r", since_id=3)
        assert [m["id"] for m in since] == [4, 5] and last == 5
        tail, last = store.query("r", tail=2)
        assert [m["id"] for m in tail] == [4, 5] and last == 5
        older, last = store.query("r", before_id=3, tail=10)
        # ★ 第二個值【永遠是房間的尾】,跟這批撈到什麼無關 —— 往上捲也一樣
        assert [m["id"] for m in older] == [1, 2] and last == 5

    def test_mentioned_filter(self, tmp_path):
        store = MessageStore(tmp_path / "chat.jsonl")
        store.append("r", "a", "hi @bob", ["bob"])
        store.append("r", "a", "plain", [])
        sel, last = store.query("r", mentioned="bob")
        assert len(sel) == 1 and sel[0]["mentions"] == ["bob"]
        assert last == 2                              # 房間的尾,不是這批的

    def test_reload_restores_indexes(self, tmp_path):
        p = tmp_path / "chat.jsonl"
        MessageStore(p).append("r", "alice", "hi", [])
        store2 = MessageStore(p)  # 模擬重啟
        assert store2.exists("r", 1) and "alice" in store2.known("r")


# ---------- TokenStore ----------

class TestTokenStore:
    def test_issue_verify_reject(self, tmp_path):
        ts = TokenStore(tmp_path / "tokens.json")
        token = ts.issue("alice")
        assert ts.verify("alice", token)
        assert not ts.verify("alice", "wrong")
        assert not ts.verify("bob", token)  # 鑰匙綁名字

    def test_owner_of(self, tmp_path):
        ts = TokenStore(tmp_path / "tokens.json")
        token = ts.issue("bob")
        assert ts.owner_of(token) == "bob"
        assert ts.owner_of("nope") is None

    def test_rotate_invalidates_old(self, tmp_path):
        ts = TokenStore(tmp_path / "tokens.json")
        old = ts.issue("alice")
        new = ts.issue("alice")  # rotate = 重新 issue
        assert not ts.verify("alice", old)
        assert ts.verify("alice", new)

    def test_persist_across_reload(self, tmp_path):
        p = tmp_path / "tokens.json"
        token = TokenStore(p).issue("alice")
        assert TokenStore(p).verify("alice", token)  # 只存 hash 也能驗

    def test_ensure_only_fills_missing(self, tmp_path):
        p = tmp_path / "tokens.json"
        ts = TokenStore(p)
        first = ts.ensure(["a", "b"])
        assert set(first) == {"a", "b"}
        again = ts.ensure(["a", "b", "c"])
        assert set(again) == {"c"}  # 已有的不重發

    def test_bad_file_tolerated(self, tmp_path):
        p = tmp_path / "tokens.json"
        p.write_text("not json", encoding="utf-8")
        ts = TokenStore(p)  # 不炸
        assert ts.verify("x", "y") is False


# ---------- RateLimiter ----------

class TestRateLimiter:
    def test_eleventh_hit_rejected(self):
        rl = RateLimiter()
        for _ in range(rl.LIMIT):
            rl.check("alice")
        with pytest.raises(server_mod.RateLimitError) as exc:
            rl.check("alice")
        assert exc.value.payload["retryAfter"] >= 0.1  # 下限保護

    def test_names_isolated(self):
        rl = RateLimiter()
        for _ in range(rl.LIMIT):
            rl.check("alice")
        rl.check("bob")  # 別人不受影響

    def test_window_slides(self, monkeypatch):
        rl = RateLimiter()
        base = time.monotonic()
        fake = [base]
        monkeypatch.setattr(time, "monotonic", lambda: fake[0])
        for _ in range(rl.LIMIT):
            rl.check("a")
        fake[0] = base + rl.WINDOW_SECONDS + 0.1  # 窗滑過
        rl.check("a")  # 恢復


# ---------- 狀態轉換表 ----------

class TestTransitions:
    def test_legal_matrix(self):
        legal = a2a_mod.ALLOWED_TRANSITIONS
        assert ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING") in legal
        assert ("TASK_STATE_SUBMITTED", "TASK_STATE_COMPLETED") in legal
        assert ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED") in legal
        assert ("TASK_STATE_SUBMITTED", "TASK_STATE_FAILED") in legal
        assert ("TASK_STATE_WORKING", "TASK_STATE_CANCELED") in legal

    def test_terminal_states_have_no_exit(self):
        legal = a2a_mod.ALLOWED_TRANSITIONS
        for terminal in a2a_mod.TERMINAL:
            assert not [t for t in legal if t[0] == terminal], f"{terminal} 不可再轉出"


class TestSpecConstants:
    """規格值鎖定(bob 突變抽查的教訓):行為測試多用相對寫法(range(LIMIT)),
    常數改了測試會跟著過 — 規格數字本身需要專人看守,改動必須是「有意識的」
    (改這裡的斷言 = 明示改規格)。"""

    def test_rate_limit_contract(self):
        assert RateLimiter.LIMIT == 10
        assert RateLimiter.WINDOW_SECONDS == 10.0

    def test_deadline_clamp_contract(self):
        assert a2a_mod.MIN_DEADLINE_SECONDS == 5.0
        assert a2a_mod.MAX_DEADLINE_SECONDS == 3600.0

    def test_bell_contract(self):
        assert bell_mod.RE_RING_SECONDS == 90
        assert bell_mod.MAX_RINGS == 3
        assert bell_mod.BELL_PREFIX == "[A2A-BELL]"
        assert bell_mod.BELL_SUBMIT == chr(13)  # ConPTY 送出鍵,換行符會讓鈴聲躺在輸入框

    def test_bell_line_carries_name_and_room(self):
        """一般鈴聲要帶名字與房間位置,而且前綴不能動。

        名字:agent 沒有別的地方查得到自己是誰(pty 包住的行程看不見 argv)。
        房間位置:給盯著畫面的人看的進度;對 agent 是【下限】,過時無害。
        前綴:文件教「凡見 [A2A-BELL] 一律對帳」,前綴變了那句話就失效。
        """
        line = bell_mod.bell_line("alice", 955)
        assert "alice" in line and "955" in line
        assert line.startswith(bell_mod.BELL_PREFIX)

    def test_force_bell_line_says_something_different(self):
        """強制鈴【必須】跟一般鈴說不一樣的話。

        ★ 這條不是美觀問題:強制敲鈴最常見的情況是「對帳為空」,
          而 AGENTS.md 教「撈到空的不用回報」—— 兩種鈴長得一樣的話,
          agent 會醒來、看一眼、安靜回去睡,而按按鈕的人正是為了打破那個安靜。
        """
        forced = bell_mod.force_bell_line("alice")
        assert forced.startswith(bell_mod.BELL_PREFIX) and "alice" in forced
        assert forced != bell_mod.bell_line("alice", 955)
        assert "強制" in forced

    def test_term_restore_contract(self):
        """離場清潔序列缺一不可:少了 9001l,退出後 PowerShell 的每個按鍵
        都會被印成 ESC[...;0;1_ 亂碼(實地災情,不是假想)。"""
        for seq, why in [("\x1b[?9001l", "win32-input-mode"), ("\x1b[?1004l", "focus reporting"),
                         ("\x1b[?2004l", "bracketed paste"), ("\x1b[?1049l", "alternate screen"),
                         ("\x1b[?25h", "游標"), ("\x1b[0m", "SGR")]:
            assert seq in bell_mod.TERM_RESTORE, f"{why} 沒關,終端機會髒給下一個程式"


# ---------- guarded_write(退出競態)----------

class TestGuardedWrite:
    """子行程退出的瞬間仍可能有東西要寫(殘留按鍵、剛好撞上的鈴聲)。
    那是正常終局,不是錯誤 —— 不該讓使用者看到 EOFError 堆疊。"""

    def test_normal_write_passes_through(self):
        got = []
        assert bell_mod.guarded_write(got.append, "hi", lock=threading.Lock()) is True
        assert got == ["hi"]

    @pytest.mark.parametrize("exc", [EOFError("Pty is closed"),   # pywinpty 實際丟的
                                     OSError(5, "Input/output error"),  # POSIX master 已關
                                     ValueError("I/O operation on closed file")])
    def test_closed_child_swallowed(self, exc):
        def boom(_):
            raise exc
        assert bell_mod.guarded_write(boom, "x", lock=threading.Lock()) is False  # 不拋,誠實回報沒寫進去

    def test_lock_is_held_during_write(self):
        """打字與鈴聲共用一把鎖 —— 沒鎖住就會互相插隊,鈴聲被剖成兩半。"""
        lock, seen = threading.Lock(), []
        assert bell_mod.guarded_write(lambda _: seen.append(lock.locked()), "x", lock=lock) is True
        assert seen == [True]

    def test_lock_released_after_failure(self):
        """吞例外不能連鎖也一起吞掉:失敗後鎖沒放,下一次寫入就永久卡死。"""
        lock = threading.Lock()

        def boom(_):
            raise EOFError("Pty is closed")

        bell_mod.guarded_write(boom, "x", lock=lock)
        assert lock.locked() is False

    # ── 分段寫入(鈴聲的字與送出鍵要分開送)──

    def test_parts_are_written_in_order(self):
        got = []
        assert bell_mod.guarded_write(got.append, "hello", "\r",
                                      lock=threading.Lock(), gap=0) is True
        assert got == ["hello", "\r"]

    def test_all_parts_share_one_lock_hold(self):
        """整串必須在【同一次持鎖】內寫完。

        ★ 這條是這次改動最關鍵的一條。分兩次呼叫 safe_write 也能達到「拆開送」,
          但鎖會在中間放開 —— 使用者打到一半的字就插進「鈴聲」與「Enter」之間,
          送出去的會是一行殘缺的鈴聲加半句人話。而那種錯誤只在有人剛好同時打字時出現,
          測不到、重現不了,只會偶爾看到一則沒頭沒尾的訊息。
        """
        lock, seen = threading.Lock(), []
        bell_mod.guarded_write(lambda part: seen.append((part, lock.locked())),
                               "hello", "\r", lock=lock, gap=0)
        assert seen == [("hello", True), ("\r", True)]

    def test_gap_only_between_parts(self):
        """段與段之間要真的等,但【只有一段時完全不等】—— 打字走的是單段路徑。"""
        stamps = []
        bell_mod.guarded_write(lambda _: stamps.append(time.monotonic()), "a", "b",
                               lock=threading.Lock(), gap=0.05)
        assert stamps[1] - stamps[0] >= 0.05

        start = time.monotonic()
        bell_mod.guarded_write(lambda _: None, "solo", lock=threading.Lock(), gap=10)
        assert time.monotonic() - start < 1      # gap=10 也不該等:只有一段就沒有「之間」

    def test_string_is_one_part_not_iterated(self):
        """字串是【一整段】,不是可迭代的序列。

        如果哪天有人把介面改回「收一個序列」,傳字串進來會被逐字迭代成
        一個字一次 write —— 不報錯,只是輸入框裡出現奇怪的輸入。這條擋住那個回頭路。
        """
        got = []
        bell_mod.guarded_write(got.append, "hi", lock=threading.Lock())
        assert got == ["hi"]                     # 不是 ["h", "i"]


# ---------- BellState ----------

class TestBellState:
    def _make(self, tmp_path, cursor: int):
        cursor_file = tmp_path / "cursor-x.txt"
        cursor_file.write_text(str(cursor), encoding="utf-8")
        rings = []
        # name/server/room 是建構需求 ——
        # 這幾個測試不碰它們,但少給就建不起來,那正是把它們收進來的目的。
        state = bell_mod.BellState(cursor_file, lambda text: rings.append(text),
                                   name="x", server="http://test", room="main")
        return state, rings, cursor_file

    def test_ring_only_when_behind(self, tmp_path):
        state, rings, _ = self._make(tmp_path, cursor=5)
        state.on_message(5)
        assert rings == []  # 沒落後不敲
        state.on_message(6)
        assert len(rings) == 1

    def test_no_rering_within_window(self, tmp_path):
        state, rings, _ = self._make(tmp_path, cursor=0)
        state.on_message(1)
        state.on_message(2)
        state.on_message(3)
        assert len(rings) == 1  # 連發只敲一次(90 秒窗內不重敲)

    def test_force_ring_bypasses_the_no_nag_cap(self, tmp_path):
        """強制敲鈴要繞過【閘門二】(敲滿三次就安靜)—— 那是它存在的唯一理由。

        死結長這樣:計數只有在 cursor 追上時才歸零,而追上需要被敲醒;
        敲滿之後就再也不敲了。從使用者的角度看,就是這個 agent 對整個聊天室沒反應。
        """
        state, rings, _ = self._make(tmp_path, cursor=0)
        state.rings_this_gap = bell_mod.MAX_RINGS      # 已經敲滿
        state.rang_for_id = 99                         # 而且【沒有新內容】—— 額度才擋得住
        state.on_message(99)
        assert rings == []                             # 閘門二確實擋住了

        state.force_ring()
        assert len(rings) == 1                         # 強制那一下穿過去了
        assert state.rings_this_gap == 0               # 而且把額度還回來
        assert state.warned is False

    def test_force_ring_works_even_when_caught_up(self, tmp_path):
        """就算沒落後也照敲 —— 判斷權在按按鈕的人身上,不在計數器。"""
        state, rings, _ = self._make(tmp_path, cursor=5)
        state.on_message(5)
        assert rings == []                             # 沒落後,正常路徑不敲
        state.force_ring()
        assert len(rings) == 1

    def test_catch_up_resets(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bell_mod, "PATROL_SECONDS", 0)   # 拿掉最短間隔的地板
        state, rings, cursor_file = self._make(tmp_path, cursor=0)
        state.on_message(1)
        cursor_file.write_text("1", encoding="utf-8")
        state.evaluate()
        assert state.rings_this_gap == 0  # 追上歸零
        state.on_message(2)
        assert len(rings) == 2  # 新落後配額重來

    def test_new_message_is_not_muted_by_the_re_ring_window(self, tmp_path, monkeypatch):
        """敲過一次之後【新來的】訊息不該被 90 秒窗擋住。

        ★ 這是 2026-07-29 實地重現的 bug:使用者連發四則,鈴一聲都沒有 ——
          因為「不要連珠炮」被寫成一個時間窗,於是新內容被當成舊 backlog。
          意圖是「同一批不要吵很多次」,不是「90 秒內誰來都不理」。
        """
        monkeypatch.setattr(bell_mod, "PATROL_SECONDS", 0)
        state, rings, cursor_file = self._make(tmp_path, cursor=0)

        state.on_message(1009)
        assert len(rings) == 1
        cursor_file.write_text("1009", encoding="utf-8")   # 讀完了

        state.on_message(1010)          # 全新的一則,距上次遠不到 90 秒
        assert len(rings) == 2, "新訊息被 90 秒窗吃掉了 —— 那正是這個 bug"

    def test_same_batch_still_rings_once(self, tmp_path):
        """同一批連發多則仍然只敲一次 —— 修 bug 不能把原本要防的東西也拆掉。"""
        state, rings, _ = self._make(tmp_path, cursor=0)
        state.on_message(1)
        state.on_message(2)
        state.on_message(3)
        assert len(rings) == 1          # 五秒地板把它們併成一次

    def _log_after_ring(self, tmp_path, monkeypatch, ring_fn) -> str:
        """把 log 導到 tmp,敲一次鈴,回傳落檔內容。"""
        monkeypatch.setattr(bell_mod, "LOG_PATH", tmp_path / "bell.log")
        cursor_file = tmp_path / "cursor-x.txt"
        cursor_file.write_text("0", encoding="utf-8")
        bell_mod.BellState(cursor_file, ring_fn,
                           name="x", server="http://test", room="main").on_message(1)
        return (tmp_path / "bell.log").read_text(encoding="utf-8")

    def test_failed_ring_is_logged(self, tmp_path, monkeypatch):
        """鈴送不進去要留下案底 —— 否則「agent 沒反應」時分不清是沒敲還是沒聽。"""
        assert "WARN" in self._log_after_ring(tmp_path, monkeypatch, lambda text: False)

    def test_silent_ring_fn_is_not_a_failure(self, tmp_path, monkeypatch):
        """只有明確的 False 算失敗:不回報的 ring_fn(回 None)不該被誣告。"""
        assert "WARN" not in self._log_after_ring(tmp_path, monkeypatch, lambda text: None)

    def test_failed_ring_still_counts_toward_cap(self, tmp_path, monkeypatch):
        """送不進去也計數 —— pty 已死時若不計數就會無限重敲刷 log。"""
        monkeypatch.setattr(bell_mod, "LOG_PATH", tmp_path / "bell.log")
        monkeypatch.setattr(bell_mod, "RE_RING_SECONDS", 0)
        cursor_file = tmp_path / "cursor-x.txt"
        cursor_file.write_text("0", encoding="utf-8")
        state = bell_mod.BellState(cursor_file, lambda text: False,
                                   name="x", server="http://test", room="main")
        for _ in range(10):
            state.on_message(1)
        assert state.rings_this_gap == bell_mod.MAX_RINGS  # 照樣封頂

    def test_max_rings_then_warn_once(self, tmp_path, monkeypatch):
        state, rings, _ = self._make(tmp_path, cursor=0)
        monkeypatch.setattr(bell_mod, "RE_RING_SECONDS", 0)  # 讓重敲窗立即過期
        for _ in range(10):
            state.evaluate() if rings else state.on_message(1)
        assert len(rings) == bell_mod.MAX_RINGS  # 封頂
        assert state.warned is True
