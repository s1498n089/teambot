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
    KNOWN = {"alice", "bob", "dev"}

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
        since, _ = store.query("r", since_id=3)
        assert [m["id"] for m in since] == [4, 5]
        tail, _ = store.query("r", tail=2)
        assert [m["id"] for m in tail] == [4, 5]
        older, _ = store.query("r", before_id=3, tail=10)
        assert [m["id"] for m in older] == [1, 2]

    def test_mentioned_filter(self, tmp_path):
        store = MessageStore(tmp_path / "chat.jsonl")
        store.append("r", "a", "hi @bob", ["bob"])
        store.append("r", "a", "plain", [])
        sel, _ = store.query("r", mentioned="bob")
        assert len(sel) == 1 and sel[0]["mentions"] == ["bob"]

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
        assert bell_mod.BELL_TEXT == "[A2A-BELL] cursor updated"
        assert bell_mod.BELL_SUBMIT == chr(13)  # ConPTY 送出鍵,換行符會讓鈴聲躺在輸入框

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
        assert bell_mod.guarded_write(got.append, "hi", threading.Lock()) is True
        assert got == ["hi"]

    @pytest.mark.parametrize("exc", [EOFError("Pty is closed"),   # pywinpty 實際丟的
                                     OSError(5, "Input/output error"),  # POSIX master 已關
                                     ValueError("I/O operation on closed file")])
    def test_closed_child_swallowed(self, exc):
        def boom(_):
            raise exc
        assert bell_mod.guarded_write(boom, "x", threading.Lock()) is False  # 不拋,誠實回報沒寫進去

    def test_lock_is_held_during_write(self):
        """打字與鈴聲共用一把鎖 —— 沒鎖住就會互相插隊,鈴聲被剖成兩半。"""
        lock, seen = threading.Lock(), []
        assert bell_mod.guarded_write(lambda _: seen.append(lock.locked()), "x", lock) is True
        assert seen == [True]

    def test_lock_released_after_failure(self):
        """吞例外不能連鎖也一起吞掉:失敗後鎖沒放,下一次寫入就永久卡死。"""
        lock = threading.Lock()

        def boom(_):
            raise EOFError("Pty is closed")

        bell_mod.guarded_write(boom, "x", lock)
        assert lock.locked() is False


# ---------- BellState ----------

class TestBellState:
    def _make(self, tmp_path, cursor: int):
        cursor_file = tmp_path / "cursor-x.txt"
        cursor_file.write_text(str(cursor), encoding="utf-8")
        rings = []
        state = bell_mod.BellState(cursor_file, lambda: rings.append(1))
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

    def test_catch_up_resets(self, tmp_path):
        state, rings, cursor_file = self._make(tmp_path, cursor=0)
        state.on_message(1)
        cursor_file.write_text("1", encoding="utf-8")
        state.evaluate()
        assert state.rings_this_gap == 0  # 追上歸零
        state.on_message(2)
        assert len(rings) == 2  # 新落後配額重來

    def test_max_rings_then_warn_once(self, tmp_path, monkeypatch):
        state, rings, _ = self._make(tmp_path, cursor=0)
        monkeypatch.setattr(bell_mod, "RE_RING_SECONDS", 0)  # 讓重敲窗立即過期
        for _ in range(10):
            state.evaluate() if rings else state.on_message(1)
        assert len(rings) == bell_mod.MAX_RINGS  # 封頂
        assert state.warned is True
