"""把舊的「一個大檔」搬成「一房一資料夾」。**一次性腳本,跑完就可以刪掉。**

    uv run migrate_rooms.py            看它打算做什麼(不動任何東西)
    uv run migrate_rooms.py --apply    真的搬

        hub_data/chat.jsonl        →  hub_data/rooms/<房名>/chat.jsonl
        hub_data/tasks.json        →  hub_data/rooms/<房名>/tasks.json

## 為什麼是獨立腳本,而不是「hub 啟動時自動遷移」

自動遷移的相容碼會**永遠留在程式裡**,而且它在最危險的時刻執行(開機)。
一次性的搬遷就該用一次性的工具 —— 跑完刪掉,程式裡不留任何「以前長不一樣」的痕跡。

★ 對照組:上次 hub_data 集中是「整個檔案換位置」(冪等,自動搬合理);
  這次是「一個檔案的內容逐行拆開」—— 錯了難回頭,複雜度不同級。

## 順序:先複製 → 驗對帳表 → 最後才刪原檔

    ★ 反過來做的話,對帳表發現不對時【已經沒有東西可以回頭比對】。
      所以這支腳本永遠不刪原檔 —— 它只複製,然後印出對帳表,
      由人看過數字之後自己去刪。少一個自動化,多一次確認。
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parent
DATA = BASE / "hub_data"
OLD_CHAT = DATA / "chat.jsonl"
OLD_TASKS = DATA / "tasks.json"
ROOMS = DATA / "rooms"


def read_messages() -> tuple[dict[str, list[str]], int, int]:
    """讀舊 chat.jsonl,按房間分組。回傳(分組、總行數、壞行數)。

    ★ 保留【原始那一行的字串】而不是 parse 後再 dump —— 搬家只該改變
      「資料存在哪裡」,不該改變「資料是什麼」。重新序列化會動到欄位順序、
      空白、Unicode 跳脫,而那些差異在 diff 上看起來像有人改過內容。
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    total = bad = 0
    if not OLD_CHAT.exists():
        return grouped, 0, 0
    for line in OLD_CHAT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        total += 1
        try:
            room = json.loads(stripped)["room"]
        except (json.JSONDecodeError, KeyError):
            bad += 1
            continue
        grouped[room].append(stripped)
    return grouped, total, bad


def read_tasks() -> tuple[dict[str, list[dict]], int]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    if not OLD_TASKS.exists():
        return grouped, 0
    try:
        data = json.loads(OLD_TASKS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ⚠ tasks.json 讀不起來({exc})—— 當作沒有任務繼續", file=sys.stderr)
        return grouped, 0
    for item in data:
        grouped[item.get("context_id", "main")].append(item)
    return grouped, len(data)


def main() -> int:
    apply = "--apply" in sys.argv

    messages, msg_total, msg_bad = read_messages()
    tasks, task_total = read_tasks()

    rooms = sorted(set(messages) | set(tasks))
    if not rooms:
        print("  沒有東西要搬(hub_data/chat.jsonl 與 tasks.json 都不存在或是空的)")
        return 0

    print(f"  來源:{OLD_CHAT.name} {msg_total} 則、{OLD_TASKS.name} {task_total} 個任務")
    if msg_bad:
        print(f"  ⚠ 其中 {msg_bad} 行讀不懂(缺 room 欄位或不是 JSON)—— **不會被搬過去**")
    print(f"  目標:{ROOMS}")
    print()
    for room in rooms:
        print(f"    {room:<20} {len(messages.get(room, [])):>5} 則   "
              f"{len(tasks.get(room, [])):>3} 個任務")
    print()

    if not apply:
        print("  這是預演,什麼都沒動。要真的搬:uv run migrate_rooms.py --apply")
        return 0

    if ROOMS.exists():
        print(f"  ✗ {ROOMS} 已經存在 —— 先確認它是不是上一次搬到一半的殘骸,")
        print("     手動處理掉再跑。這支腳本不覆蓋既有目錄。")
        return 1

    # ── 複製(不刪原檔)──
    for room in rooms:
        room_dir = ROOMS / room
        room_dir.mkdir(parents=True)
        if messages.get(room):
            (room_dir / "chat.jsonl").write_text(
                "\n".join(messages[room]) + "\n", encoding="utf-8")
        if tasks.get(room):
            (room_dir / "tasks.json").write_text(
                json.dumps(tasks[room], ensure_ascii=False), encoding="utf-8")

    # ── 對帳表:數字對不上就把新目錄收掉,原檔一個位元組都沒動過 ──
    print("  ── 對帳 ──")
    ok = True
    moved_msgs = moved_tasks = 0
    for room in rooms:
        chat = ROOMS / room / "chat.jsonl"
        got = len(chat.read_text(encoding="utf-8").splitlines()) if chat.exists() else 0
        want = len(messages.get(room, []))
        moved_msgs += got

        tpath = ROOMS / room / "tasks.json"
        got_t = len(json.loads(tpath.read_text(encoding="utf-8"))) if tpath.exists() else 0
        want_t = len(tasks.get(room, []))
        moved_tasks += got_t

        mark = "✓" if (got == want and got_t == want_t) else "✗"
        if mark == "✗":
            ok = False
        print(f"    {mark} {room:<20} 訊息 {got}/{want}   任務 {got_t}/{want_t}")

    print(f"    ─ 合計:訊息 {moved_msgs}/{msg_total - msg_bad}、任務 {moved_tasks}/{task_total}")
    if moved_msgs != msg_total - msg_bad or moved_tasks != task_total:
        ok = False

    if not ok:
        shutil.rmtree(ROOMS, ignore_errors=True)
        print("\n  ✗ 對帳不符 —— 新目錄已收回,原檔【一個位元組都沒動過】。")
        return 1

    print("\n  ✓ 全部對得上。原檔仍然留在原地:")
    print(f"      {OLD_CHAT}")
    print(f"      {OLD_TASKS}")
    print("    確認 hub 用新結構跑起來、內容都在之後,再自己刪掉它們。")
    print("    ★ 腳本不替你刪:對帳表證明的是「複製正確」,而「新結構真的能跑」")
    print("      要等你把 hub 開起來才知道 —— 那一步之前,原檔是唯一的退路。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
