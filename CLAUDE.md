# CLAUDE.md

> 這份是**薄的一層**。真正的內容在根目錄的 `AGENTS.md`,這裡只負責把它接過來。
>
> **為什麼不直接寫在這裡**:Codex 自動載入的是 `AGENTS.md`,Claude Code 自動載入的是
> `CLAUDE.md`。兩邊各寫一份的結果一定是分岔 —— 而**分岔的規則比沒有規則更糟**,
> 因為它會給你一個過期的答案,而你不會知道。
> 所以規則只有一份,這裡只是入口。

@AGENTS.md

---

## 如果上面那行沒有把 AGENTS.md 帶進來

請自己讀一次專案根目錄的 **`AGENTS.md`** —— 那份是這個專案的 agent 說明書,
第一段會告訴你「你是不是這個聊天室的成員」,答案決定你接下來要做什麼。

## 這台機器的兩個地雷(Claude Code 專屬提醒)

1. **沒有系統 Python。** `python` 這個指令是 Windows 的假捷徑,會把你導去市集。
   一律用 `uv run <script.py>`,或直接用 `.venv/Scripts/python.exe`。

2. **主控台預設編碼是 cp950。** 用 Python 印中文或 `✓` 這類符號會丟
   `UnicodeEncodeError`,而且它**常常在最後一行成功訊息才炸** ——
   事情其實做完了,看起來卻像失敗,是最容易誤判的那種。
   寫任何會印中文的腳本,第一行加:

   ```python
   sys.stdout.reconfigure(encoding="utf-8")
   ```

## 想快速上手這個專案

| 想知道 | 看哪裡 |
|---|---|
| 怎麼啟動、怎麼設定、出事怎麼查 | `README.md` |
| 為什麼這樣設計(從零講起,第 0~10 章) | `doc/TUTORIAL.md` |
| 改東西時該怎麼判斷該不該改 | `doc/PLAYBOOK.md` |
| 跟 A2A 官方規格的逐項對映 | `doc/A2A_MAPPING.md` |
