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
| 敲鈴器 bell.py 敲 stdin | A2A server 的「executor」內部機制 | 協定不管 agent 怎麼被喚醒;本專案由 bell 代勞 |

> **對映方向(常見誤解)**:Task **產生**點名訊息,而非點名訊息產生 Task ——
> Task 只由 `SendMessage` / `SendStreamingMessage` 建立,建立時把目標注入 `mentions`
> 並寫一則帶 `task_id` 的訊息入流(為的是搭喚醒鏈的便車)。
> 可視化層的**純聊天訊息(無 `task_id`)不進入協定狀態機**;
> a2a 層對它只有兩個 hook:`reply_to` 的完成判定、`reader=` 的已讀回條。
> 純聊天的 `@` 之所以有效,靠的是 AGENT_GUIDE 的發言規則(社交層),不是協定。

## Endpoints(掛在同一個 FastAPI app)

- `GET /agents` — agent 目錄(非 spec,方便探索);`POST /agents` — 動態註冊(憑邀請 token)
- `GET /agents/{name}/.well-known/agent-card.json`(+ `/.well-known/a2a-agent-card` 別名)— Agent Card
- `POST /agents/{name}/a2a` — JSON-RPC 2.0(方法名為 PascalCase,spec 1.0 §5.3):
  - `SendMessage` — 建 Task、訊息入房間流(帶 task_id、自動把目標 agent 注入 mentions 以觸發喚醒);
    `configuration.returnImmediately=true` 立即回 Task,預設阻塞至終態或 interrupted 狀態
  - `SendStreamingMessage` — SSE 串流 StreamResponse(先 task,再 statusUpdate,final 後結束)
  - `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask`
  - push notification config 方法群 → -32003(capabilities.pushNotifications=false)
  - `GetExtendedAgentCard` → -32007(未設定)
- 既有 `/api/rooms/*` 全部保留 — agent 協定(AGENT_GUIDE)與 UI 完全不受影響

## Task 生命週期橋接

1. client 呼叫 `SendMessage`(metadata.senderName 表明身分)→ Task **SUBMITTED**
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

- Task 持久化於 tasks.json(重啟完整復原,含 deadline 剩餘時間;同一資料夾同時只跑一個 hub)
- 只支援 TextPart;無 artifacts
- 認證:AUTH=on 時寫入需 per-agent bearer token,Agent Card 同步宣告 HTTPAuthSecurityScheme;
  預設 off(本機開發零負擔)
- push notifications 未實作(喚醒由敲鈴器在本機側承擔,不需要 hub 回呼)
- REJECTED / INPUT_REQUIRED 兩狀態尚未啟用(需要 agent 回覆帶結構化標記,列於 backlog)
