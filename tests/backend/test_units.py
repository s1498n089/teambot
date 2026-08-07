"""單元/邊界層:純函式與小類別,毫秒級,不建 app。"""
from __future__ import annotations

import io
import threading
import time

import pytest

import a2a as a2a_mod
import bell as bell_mod
import server as server_mod
from server import MentionParser, MessageStore, RateLimiter


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
        store = MessageStore(tmp_path / "rooms")
        a1 = store.append("roomA", "alice", "hi", [])
        store.append("roomB", "bob", "yo", [])
        a2 = store.append("roomA", "alice", "again", [])
        assert (a1["id"], a2["id"]) == (1, 2)  # roomB 的流量不影響 roomA

    def test_empty_room_last_id_zero(self, tmp_path):
        store = MessageStore(tmp_path / "rooms")
        assert store.last_id("nowhere") == 0
        assert store.query("nowhere")[0] == []

    def test_bad_line_skipped_on_load(self, tmp_path):
        room = tmp_path / "rooms" / "r"
        room.mkdir(parents=True)
        (room / "chat.jsonl").write_text(
            '{"id":1,"room":"r","from":"a","text":"ok","mentions":[],"ts":"t"}\n'
            "THIS IS NOT JSON\n"
            '{"id":2,"room":"r","from":"a","text":"ok2","mentions":[],"ts":"t"}\n',
            encoding="utf-8")
        store = MessageStore(tmp_path / "rooms")
        assert store.count("r") == 2  # 壞行跳過,不炸啟動

    def test_room_name_comes_from_the_directory(self, tmp_path):
        """★ 目錄與行內的 room 欄位不一致時,以【目錄】為準。

        目錄是這則訊息現在住在哪裡,欄位是它被寫下來時記的 ——
        兩者衝突通常代表有人手動搬過檔案,而那時該相信看得見的那個。
        """
        room = tmp_path / "rooms" / "moved"
        room.mkdir(parents=True)
        (room / "chat.jsonl").write_text(
            '{"id":1,"room":"old-name","from":"a","text":"x","mentions":[],"ts":"t"}\n',
            encoding="utf-8")
        store = MessageStore(tmp_path / "rooms")
        assert store.count("moved") == 1
        assert store.count("old-name") == 0

    def test_query_three_modes(self, tmp_path):
        store = MessageStore(tmp_path / "rooms")
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
        store = MessageStore(tmp_path / "rooms")
        store.append("r", "a", "hi @bob", ["bob"])
        store.append("r", "a", "plain", [])
        sel, last = store.query("r", mentioned="bob")
        assert len(sel) == 1 and sel[0]["mentions"] == ["bob"]
        assert last == 2                              # 房間的尾,不是這批的

    def test_reload_restores_indexes(self, tmp_path):
        rooms = tmp_path / "rooms"
        MessageStore(rooms).append("r", "alice", "hi", [])
        store2 = MessageStore(rooms)  # 模擬重啟
        assert store2.exists("r", 1) and "alice" in store2.known("r")

    def test_rooms_live_in_separate_files(self, tmp_path):
        """一房一資料夾 —— 刪一個房不必碰別人的檔案,這是整批重構的理由。"""
        rooms = tmp_path / "rooms"
        store = MessageStore(rooms)
        store.append("a", "alice", "在 a", [])
        store.append("b", "bob", "在 b", [])
        assert (rooms / "a" / "chat.jsonl").exists()
        assert (rooms / "b" / "chat.jsonl").exists()
        assert "在 b" not in (rooms / "a" / "chat.jsonl").read_text(encoding="utf-8")

    def test_dropping_a_room_removes_its_directory(self, tmp_path):
        rooms = tmp_path / "rooms"
        store = MessageStore(rooms)
        store.append("doomed", "alice", "x", [])
        store.append("keep", "bob", "y", [])
        assert store.drop_room("doomed") == 1
        assert not (rooms / "doomed").exists()
        assert (rooms / "keep" / "chat.jsonl").exists()

    @pytest.mark.parametrize("reserved", ["CON", "con", "NUL", "COM1", "LPT9", "AUX", "PRN"])
    def test_windows_device_names_are_rejected(self, tmp_path, reserved):
        """★ Windows 的保留裝置名不能當資料夾 —— 而且兩種失敗長得不一樣:

            CON/PRN/AUX/COM1/LPT1   mkdir 當場失敗(WinError 267)
            NUL                     mkdir【成功】,但往裡面寫檔案時才失敗

        NUL 那個比較陰:房間看起來建起來了、清單上有它,訊息卻永遠寫不進去 ——
        而使用者只會看到「我發的話不見了」。所以不能靠「mkdir 失敗就知道」,
        要在名字這一關攔。

        ★★ 這跟路徑穿越同源:**都是「房間名變成路徑」那天才長出來的**。
          sanitize_sender 沒有這個問題,因為名字不會變成路徑。
        """
        store = MessageStore(tmp_path / "rooms")
        with pytest.raises(server_mod.BadRoomError):
            store.room_dir(reserved)

    @pytest.mark.parametrize("evil", ["..", "../..", "a/b", "a\\b", ""])
    def test_path_traversal_is_rejected(self, tmp_path, evil):
        """★★ 房間名分資料夾之後【變成路徑的一段】,所以必須過白名單。

        沒有這道關的話,`DELETE /api/rooms/..` 會 rmtree 掉整個 hub_data ——
        房間名以前只是 dict 的 key(怎麼寫都安全),而那個安全性是隨著
        「改成資料夾」一起消失的,**消失得毫無聲息**。
        """
        store = MessageStore(tmp_path / "rooms")
        with pytest.raises(server_mod.BadRoomError):
            store.room_dir(evil)


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

    def test_submit_gap_clears_codex_paste_window(self):
        """★★ BELL_SUBMIT_GAP 必須大於 Codex 把 Enter 當成換行的那段時間。

        這是唯一守著那個常數的東西 —— 沒有它,誰都可以把 1.0 調回 0.1
        而全部測試照樣綠,然後 Codex 又開始「鈴敲了但不會醒」。

        下限來自 Codex TUI 的兩個常數(paste_burst.rs,2026-08-05 查):

            PASTE_BURST_ACTIVE_IDLE_TIMEOUT   60 ms(Windows)  靜這麼久才 flush
            PASTE_ENTER_SUPPRESS_WINDOW      120 ms           flush 後 Enter 仍插換行

        合計 180 ms。在那之前送 \\r,Codex 會把它當成「貼上內容裡的換行」——
        那是它刻意的設計(多行貼上時 Enter 本來就該換行),不會被上游修掉。

        ★ 斷言用 0.18 而不是 1.0:守的是【那個下限的理由】,不是現在這個值。
          有人為了別的理由把 1.0 調成 0.5 是合理的,調成 0.1 不是。
        """
        CODEX_PASTE_WINDOW = 0.06 + 0.12
        assert bell_mod.BELL_SUBMIT_GAP > CODEX_PASTE_WINDOW, (
            f"gap={bell_mod.BELL_SUBMIT_GAP}s 落在 Codex 的 Enter 抑制窗內"
            f"({CODEX_PASTE_WINDOW}s)—— 鈴聲會躺在輸入框裡不送出")

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
    def _bare(self, ring_fn=lambda text: True):
        return bell_mod.BellState(ring_fn, name="x", server="http://test", room="main")

    # ── read_cursor:它現在問 hub,而「問不到」只有一個正確方向 ──

    def test_read_cursor_asks_the_hub(self, monkeypatch):
        """cursor 從本地檔案搬到 hub 之後,這裡要打對房間與名字。"""
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fake_urlopen(req, *_a, **_kw):
            seen.append(req.full_url)
            return Resp(b'{"room":"main","name":"x","last_id":42}')

        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", fake_urlopen)
        assert self._bare().read_cursor() == 42
        assert seen == ["http://test/api/rooms/main/cursor/x"]

    @pytest.mark.parametrize("failure", [
        OSError("hub 沒開"),
        ValueError("回了不是數字的東西"),
    ])
    def test_read_cursor_falls_back_to_zero(self, monkeypatch, failure):
        """★ 問不到一律回 0 —— 這個方向不能因為搬到網路上就改。

            回 0        最壞是白醒一次(無害,對帳後發現沒新訊息就回去睡)
            回最新編號  最壞是【永遠不叫他】,而且安靜到沒有人會發現

        兩種錯的代價差很多。異常時要往「會被注意到」的方向倒 ——
        別把它「優化」成安靜的那一邊。
        """
        def boom(*_a, **_kw):
            raise failure

        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", boom)
        assert self._bare().read_cursor() == 0

    def test_bell_never_writes_cursor(self):
        """★★ 貫穿全檔的鐵則:**敲鈴器只讀 cursor,永遠不寫。**

        能寫就能偽造「你已經讀了」—— 等於自己把叫醒你的證據銷毀掉。
        搬到 API 之後這條更要釘住:寫變成了一個 PUT,而 PUT 就在隔壁。

        ★ 斷言要精確到「HTTP 方法」這一層。我第一版寫的是 `"PUT" not in source`,
          結果它抓到 `VT_INPUT` 與 `PROCESSED` 裡的那三個字母 —— **假陽性**。
          粗糙的字串比對在這種測試裡特別危險:紅了還好(我就發現了),
          萬一它剛好綠,我會以為自己驗過了。
        """
        source = (bell_mod.BASE / "bell.py").read_text(encoding="utf-8")
        assert 'method="PUT"' not in source
        assert "write_cursor" not in source
        # 唯一碰 cursor 端點的地方必須是讀:整支程式只有一次 cursor/,而它在 GET 裡
        # ★ 這裡比對的是 `cursor/` 而不是 `/cursor/`:端點路徑後來被拆成
        #   「共用前綴 + 尾段」,前導斜線落在共用那半邊了。少一個字元反而更嚴 ——
        #   比對變寬鬆,而斷言是「恰好一次」,多出任何一處都會紅。
        assert source.count("cursor/") == 1

    # ── sync_room_head:訂閱起點取自「房間到哪」,不是「他讀到哪」 ──

    def test_subscribe_starts_at_room_head_not_cursor(self, monkeypatch):
        """★★ 2026-07-31 的事故:訂閱起點拿 cursor 去帶,把整本記錄重播了一遍。

        當時 cursor 是 0(一房一資料夾剛搬完,還沒有人推過進度),於是
        `since_id=0` 讓 hub 從第 1 則開始回放 1231 則,而每一則都會經過
        on_message → evaluate → read_cursor 一個 HTTP 請求 —— 19 秒 1231 個,
        兩個敲鈴器一起約 2500 個,log 洗版、鈴一路狂敲。

        根子是搞混了誰該補課:**bell 收下訊息之後只取 id,內容整包丟掉**,
        它為了知道「房間到 1231」這一個數字搬了 1231 則訊息。真正要把那些
        訊息追回來的是 agent 的對帳,那條路本來就該由 agent 自己走。

        所以起點必須是房間 head —— cursor 是 0 也照樣只訂閱往後的。
        """
        rings = []
        st = self._bare(ring_fn=lambda text: rings.append(text) or True)
        monkeypatch.setattr(st, "read_room_last_id", lambda: 1231)
        monkeypatch.setattr(st, "read_cursor", lambda: 0)      # 落後一千多則

        assert st.sync_room_head() == 1231, "訂閱起點必須是房間 head,不是 cursor"
        assert st.known_last_id == 1231
        assert len(rings) == 1, "落後就該敲 —— 而且不必先收下任何一則訊息"

    def test_sync_room_head_keeps_old_value_when_hub_unreachable(self, monkeypatch):
        """★ 問不到房間 head 時沿用舊值,**不能退回 0**。

        這裡跟 read_cursor 的失敗方向【故意相反】,兩個都是往安全倒:

            read_cursor 問不到 → 0    當作沒讀過 → 會被吵醒(吵是無害的)
            room head   問不到 → 舊值  退回 0 會讓訂閱重播整本記錄(正是要修的)

        沿用舊值最壞是漏掉斷線期間的幾聲通知,而那個由 agent 對帳補得回來。
        """
        st = self._bare()
        st.known_last_id = 900
        monkeypatch.setattr(st, "read_room_last_id", lambda: None)   # hub 不通
        monkeypatch.setattr(st, "read_cursor", lambda: 900)

        assert st.sync_room_head() == 900
        assert st.known_last_id == 900

    def test_read_room_last_id_returns_none_on_failure(self, monkeypatch):
        """★ 失敗回 None 而不是 0 —— 「讀不到」與「房間是空的」不能混為一談。"""
        def boom(*_a, **_kw):
            raise OSError("hub 沒開")

        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", boom)
        assert self._bare().read_room_last_id() is None

    def test_read_room_last_id_asks_the_state_endpoint(self, monkeypatch):
        """問的是 /state —— 那支 API 只回一個數字,不含任何訊息內容。"""
        seen = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def fake_urlopen(req, *_a, **_kw):
            seen.append(req.full_url)
            return Resp(b'{"room":"main","last_id":1231,"count":1231}')

        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", fake_urlopen)
        assert self._bare().read_room_last_id() == 1231
        assert seen == ["http://test/api/rooms/main/state"]

    def test_sse_subscribes_from_room_head_not_cursor(self, monkeypatch):
        """★★★ 這個測試盯的是【真的組出來的那條 url】,不是中間那層方法。

        上面幾個測試都在測 sync_room_head 自己,而事故是在 sse_watch 裡發生的 ——
        只要有人把 `since_id=` 後面換回 `state.read_cursor()`,那幾個測試【全部照樣綠】。
        所以這裡真的跑一圈迴圈,把 urlopen 攔下來看 url 長什麼樣。

        場景就是 2026-07-31 當天:cursor=0、房間到 1231。
        """
        seen, rounds = [], {"n": 0}

        def child_alive():
            rounds["n"] += 1
            return rounds["n"] == 1          # 只讓外層迴圈跑一圈

        def fake_urlopen(req, *_a, **_kw):
            seen.append(req.full_url)
            raise OSError("測試到此為止 —— 連上之後的事這裡不管")

        st = self._bare()
        monkeypatch.setattr(st, "read_room_last_id", lambda: 1231)
        monkeypatch.setattr(st, "read_cursor", lambda: 0)
        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(bell_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(bell_mod, "log", lambda *_a, **_kw: None)

        bell_mod.sse_watch("http://test", "main", st, child_alive)

        assert len(seen) == 1, "外層迴圈應該只跑一圈"
        assert "since_id=1231" in seen[0], f"訂閱起點必須是房間 head:{seen[0]}"
        assert "since_id=0" not in seen[0], "帶 cursor 去訂閱 = 要 hub 重播整本記錄"

    # ── 開機那一聲:不敲。敲鈴的意義是「你睡著時有人講話了」,不是「你落後很多」 ──

    def test_startup_is_silent_even_when_far_behind(self, monkeypatch):
        """★★ 開機時落後 1234 則也不敲 —— 而且【節拍器那一圈也不敲】。

        使用者常用 `claude -r` 啟動,那會先跳出「選哪段聊天紀錄」的選單。
        鈴聲是把字打進輸入框的,人還在選單裡的時候敲下去,字會落在選單上。

        ★ 這裡連 evaluate() 都直接叫一次 —— 那是節拍器每 5 秒在做的事。
          只讓「啟動時跳過敲」是不夠的:5 秒後節拍器就會替你敲下去,
          所以這條線必須擋在 evaluate 裡面,不能只擋在啟動路徑上。
        """
        rings, cursor_asks = [], {"n": 0}
        st = self._bare(ring_fn=lambda text: rings.append(text) or True)
        monkeypatch.setattr(st, "read_room_last_id", lambda: 1234)

        def counting_read_cursor():
            cursor_asks["n"] += 1
            return 0                      # 落後 1234 則
        monkeypatch.setattr(st, "read_cursor", counting_read_cursor)

        st.sync_room_head(initial=True)
        assert rings == [], "開機不該敲"
        assert cursor_asks["n"] == 0, "開機時連 cursor 都不必問 —— 閘門零排在它前面"

        for _ in range(5):                # 節拍器連跑五圈
            st.evaluate()
        assert rings == [], "節拍器也必須安靜,否則 5 秒後照樣敲下去"

    def test_rings_once_someone_actually_speaks(self, monkeypatch):
        """開機後【有人講話】就要敲 —— 這是安靜的代價不能付過頭的地方。"""
        rings = []
        st = self._bare(ring_fn=lambda text: rings.append(text) or True)
        monkeypatch.setattr(st, "read_room_last_id", lambda: 1234)
        monkeypatch.setattr(st, "read_cursor", lambda: 0)

        st.sync_room_head(initial=True)
        assert rings == []

        st.on_message(1235)               # 有人講了一句
        assert len(rings) == 1, "開機之後的新訊息必須敲得出來"

    def test_reconnect_does_not_move_the_silence_line(self, monkeypatch):
        """★ 重連【不能】重設那條線 —— 否則斷線期間別人講的話會被永久吃掉。

        一次網路抖動就讓 agent 從此聽不見那段對話,而且沒有任何錯誤訊息 ——
        這種安靜的失敗比吵鬧的失敗難查得多。
        """
        rings = []
        st = self._bare(ring_fn=lambda text: rings.append(text) or True)
        monkeypatch.setattr(st, "read_cursor", lambda: 0)

        monkeypatch.setattr(st, "read_room_last_id", lambda: 1234)
        st.sync_room_head(initial=True)               # 開機:線釘在 1234
        assert rings == []

        monkeypatch.setattr(st, "read_room_last_id", lambda: 1240)
        st.sync_room_head()                           # 重連:斷線期間多了 6 則
        assert len(rings) == 1, "斷線期間的發言必須敲得出來"
        assert st.start_id == 1234, "那條線不該跟著重連往前移"

    def test_only_the_first_round_arms_the_silence_line(self, monkeypatch):
        """盯 sse_watch 真的只在第一圈傳 initial=True —— 後面幾圈都不能傳。

        跟上面那個測試的差別:那個測 BellState 自己,這個測【呼叫它的那一方】。
        兩個都要有,否則把 `initial=first_round` 寫死成 `initial=True` 不會紅。
        """
        seen_initial, rounds = [], {"n": 0}

        def child_alive():
            rounds["n"] += 1
            return rounds["n"] <= 3          # 讓外層迴圈跑三圈

        st = self._bare()
        monkeypatch.setattr(st, "sync_room_head",
                            lambda initial=False: seen_initial.append(initial) or 1234)

        def boom(*_a, **_kw):
            raise OSError("連不上,直接進重連")
        monkeypatch.setattr(bell_mod.urllib.request, "urlopen", boom)
        monkeypatch.setattr(bell_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(bell_mod, "log", lambda *_a, **_kw: None)

        bell_mod.sse_watch("http://test", "main", st, child_alive)

        assert seen_initial == [True, False, False], f"實際:{seen_initial}"

    def _make(self, tmp_path, cursor: int):
        """建一個 BellState,並把「他讀到哪」換成測試可以直接改的一個值。

        ★ cursor 現在住在 hub 上(GET /api/rooms/<房>/cursor/<名字>)——
          這一族測試要驗的是【決策層怎麼判斷】,不是【怎麼問 hub】,
          所以把那個問法換掉。read_cursor 自己的行為由 TestReadCursor 驗。
        """
        rings = []
        # name/server/room 是建構需求 ——
        # 這幾個測試不碰它們,但少給就建不起來,那正是把它們收進來的目的。
        state = bell_mod.BellState(lambda text: rings.append(text),
                                   name="x", server="http://test", room="main")
        cursor_box = {"value": cursor}
        state.read_cursor = lambda: cursor_box["value"]
        return state, rings, cursor_box

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
        state, rings, cursor = self._make(tmp_path, cursor=0)
        state.on_message(1)
        cursor["value"] = 1
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
        state, rings, cursor = self._make(tmp_path, cursor=0)

        state.on_message(1009)
        assert len(rings) == 1
        cursor["value"] = 1009                             # 讀完了

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
        state_for_log = bell_mod.BellState(ring_fn,
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
        state = bell_mod.BellState(lambda text: False,
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
