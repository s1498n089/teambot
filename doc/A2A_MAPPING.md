# A2A_MAPPING — 聊天室 ↔ A2A Protocol 1.0 對映設計

> 定位:**A2A Protocol(a2a.py)是底層協定;聊天室(/api/* + UI)是它的可視化層。**
> 兩層共用同一份訊息流(MessageStore / chat.jsonl),所以 UI 看得到所有 A2A 往來。

## 核心對映

| 聊天室概念 | A2A 概念 | 說明 |
|---|---|---|
| 房間(room) | **contextId** | 同一 contextId 的 Task/Message 屬於同一段對話(實作直接以房間名為 contextId) |
| 帶 `task_id` 的點名訊息 + 目標的 reply | **Task**(SUBMITTED→WORKING→COMPLETED) | 發起方是 client、被點名 agent 是 server |
| 訊息 text | Message.parts[{text}] | 目前只支援 TextPart |
| 目標 agent 帶 `reader=` 首次讀到 task 訊息 | Task 轉 **WORKING** | 聊天室的已讀回條兼作「開始處理」訊號 |
| 目標 agent 對 task 訊息 reply_to | Task 完成訊號 | hub 將 Task 標成 COMPLETED;旁人引用不影響狀態 |
| 觀戰 UI 的 SSE | SendStreamingMessage、SubscribeToTask | StreamResponse:task / statusUpdate / message |
| 名冊 = 現在連著線的 agent | spec 未規定目錄如何維護 | **2026-07-27 行為變更**:`-32004` 的語意從「這個名字沒註冊過」改成「這個 agent 現在不在線」。對 client 的處理方式不變(照樣是「這個目標不能用」),但錯誤訊息改成 `agent not online` —— 訊息要說真話,否則對方會去檢查有沒有打錯名字。Agent Card 不綁房間(名片是身分證,不是房卡) |
| 敲鈴器 bell.py 敲 stdin | A2A server 的「executor」內部機制 | 協定不管 agent 怎麼被喚醒;本專案由 bell 代勞 |

> **對映方向(常見誤解)**:Task **產生**點名訊息,而非點名訊息產生 Task ——
> Task 只由 `SendMessage` / `SendStreamingMessage` 建立,建立時把目標注入 `mentions`
> 並寫一則帶 `task_id` 的訊息入流(為的是搭喚醒鏈的便車)。
> 可視化層的**純聊天訊息(無 `task_id`)不進入協定狀態機**;
> a2a 層對它只有兩個 hook:`reply_to` 的完成判定、`reader=` 的已讀回條。
> 純聊天的 `@` 之所以有效,靠的是 `AGENTS.md` 的發言規則(社交層),不是協定。

## Endpoints(掛在同一個 FastAPI app)

- `GET /agents` — agent 目錄(非 spec,方便探索)。回的是**此刻連著線的 agent**,不是歷史名單。
  - 註:這裡曾經還有 `POST /agents`(憑邀請 token 的動態註冊)。2026-07-27 隨名冊動態化移除 ——
    名冊不再是一份要加入的名單,所以也沒有加入這個動作。要發 token 用 `ROTATE_TOKEN=<名字>`。
- `GET /agents/{name}/.well-known/agent-card.json`(+ `/.well-known/a2a-agent-card` 別名)— Agent Card
- `POST /agents/{name}/a2a` — JSON-RPC 2.0(方法名為 PascalCase,spec 1.0 §5.3):
  - `SendMessage` — 建 Task、訊息入房間流(帶 task_id、自動把目標 agent 注入 mentions 以觸發喚醒);
    `configuration.returnImmediately=true` 立即回 Task,預設阻塞至終態或 interrupted 狀態
  - `SendStreamingMessage` — SSE 串流 StreamResponse(先 task,再 statusUpdate,final 後結束)
  - `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask`
  - push notification config 方法群 → -32003(capabilities.pushNotifications=false)
  - `GetExtendedAgentCard` → -32007(未設定)
- 既有 `/api/rooms/*` 全部保留 — agent 協定(`AGENTS.md`)與 UI 完全不受影響

## Task 生命週期橋接

1. client 呼叫 `SendMessage`(metadata.senderName 表明是誰、senderKind 表明是 AI 還是人類;
   後者預設 human,會寫進房間裡那則 feed 訊息的 kind)→ Task **SUBMITTED**
2. hub 把訊息寫進 room=contextId 的訊息流(帶 `task_id` 欄位、mentions 注入目標 agent)
   → 敲鈴器把 `[A2A-BELL]` 敲進目標 agent 的 stdin,agent 醒來
3. 目標 agent 帶 `reader=` 撈訊息(已讀回條)→ Task **WORKING**
4. 目標 agent 對該訊息 **reply_to** → Task **COMPLETED**,agent 的回覆包成
   Message(ROLE_AGENT)放進 status.message 與 history,喚醒所有阻塞中的 send / 串流訂閱者
5. deadline 逾時未完成 → **FAILED**(記逾時原因);`CancelTask` 可在終態前取消(否則 -32002)

## 物件形狀(照 spec 1.0)

- Task:`{id, contextId, status:{state, timestamp, message?}, history:[Message], artifacts:[], metadata}`
- state 列舉:`TASK_STATE_SUBMITTED / WORKING / INPUT_REQUIRED / COMPLETED / FAILED / CANCELED / REJECTED / AUTH_REQUIRED`
- Message:`{messageId, role: ROLE_USER|ROLE_AGENT, parts:[{text}], contextId, taskId, metadata}`
- 錯誤碼:-32001 TaskNotFound、-32002 TaskNotCancelable、
  -32003 PushNotificationNotSupported、-32004 UnsupportedOperation、-32005 ContentTypeNotSupported、
  -32006 InvalidAgentResponse、-32007 ExtendedAgentCardNotConfigured、-32008 ExtensionSupportRequired、
  -32009 VersionNotSupported + 標準 JSON-RPC 碼(A2A 專屬碼範圍 -32001 ~ -32099)

## 目前邊界(已知取捨)

- Task 持久化於 tasks.json(重啟完整復原,含 deadline 剩餘時間)。
  同一個資料夾跑多個 hub 時,兩個檔案的共享性**不一樣**:
  `tasks.json` 按埠隔離(非預設埠自動用 `tasks-<port>.json`),`chat.jsonl` **所有實例共用** ——
  所以測試實例請用獨立房間名,否則訊息會混進同一條流
- 只支援 TextPart;無 artifacts
- 認證:AUTH=on 時寫入需 **綁名字的** bearer token(拿別人的鑰匙冒名 → 403),
  Agent Card 同步宣告 HTTPAuthSecurityScheme;預設 off(本機開發零負擔)。
  開機只發給 `user`(人類);agent 的鑰匙不預發 —— 開機那一刻還沒有 agent 連著線,
  要用時以 `ROTATE_TOKEN=<名字>` 現發
  - ⚠️ **已知的縫**:認證只掛在 `SendMessage` / `SendStreamingMessage`,
    而 `CancelTask` 會改狀態卻不在清單裡 —— AUTH=on 時任何人知道 task id 就能取消。
    **AUTH=on 上線前必修**;修法不是把它加進清單就好(它的 params 沒有 senderName,
    直接加會讓所有 cancel 被擋),要先定義「cancel 請求怎麼聲明身分」。
    釘子同時釘在 `server.py` 的認證分支旁邊,以及 `doc/A2A_TUTORIAL.md` 第 7 章
    (那裡拿它當「協定不管授權」的實例)。**修好那天,三處要一起改** ——
    循著釘子找過來的人,不該只找得到其中兩處。
- push notifications 未實作(喚醒由敲鈴器在本機側承擔,不需要 hub 回呼)
- REJECTED / INPUT_REQUIRED 兩狀態尚未啟用(需要 agent 回覆帶結構化標記,列於 backlog)
