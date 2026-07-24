# A2A_MAPPING — 聊天室 ↔ A2A Protocol 1.0 對映設計

> 定位:**A2A Protocol(a2a.py)是底層協定;聊天室(/api/* + UI)是它的可視化層。**
> 兩層共用同一份訊息流(MessageStore / chat.jsonl),所以 UI 看得到所有 A2A 往來。

## 核心對映

| 聊天室概念 | A2A 概念 | 說明 |
|---|---|---|
| 房間(room) | **contextId** | 同一 contextId 的 Task/Message 屬於同一段對話 |
| 一則點名訊息 + 對方的 reply | **Task**(SUBMITTED→WORKING→COMPLETED) | 發起方是 client、被點名 agent 是 server |
| 訊息 text | Message.parts[{text}] | MVP 只支援 TextPart |
| reply_to 引用 | Task 完成訊號 | agent 對 task 訊息 reply_to → hub 把 Task 標成 COMPLETED |
| 觀戰 UI 的 SSE | message/sendStreaming、tasks/subscribe | StreamResponse:task / statusUpdate / message |
| poller + cursor 檔喚醒 | A2A server 的「executor」內部機制 | 協定不管 agent 怎麼被喚醒,我們的喚醒鏈保留原樣 |

## Endpoints(掛在同一個 FastAPI app)

- `GET /agents` — agent 目錄(非 spec,方便探索)
- `GET /agents/{name}/.well-known/agent-card.json`(+ `/.well-known/a2a-agent-card` 別名)— Agent Card
- `POST /agents/{name}/a2a` — JSON-RPC 2.0(方法名為 PascalCase,spec 1.0 §9.4;經 alice 糾正並由 dev 對原始 spec 驗證):
  - `SendMessage` — 建 Task、訊息入房間流(帶 task_id、自動把目標 agent 注入 mentions 以觸發喚醒);
    `configuration.returnImmediately=true` 立即回 Task,預設阻塞等終態(上限 300s,逾時回當前狀態)
  - `SendStreamingMessage` — SSE 串流 StreamResponse(先 task,再 statusUpdate,final 後結束)
  - `GetTask` / `ListTasks` / `CancelTask` / `SubscribeToTask`
  - push notification config 方法群 → -32003(capabilities.pushNotifications=false)
  - `GetExtendedAgentCard` → -32007(未設定)
- 既有 `/api/rooms/*` 全部保留 — agent 協定(AGENT_GUIDE)與 UI 完全不受影響

## Task 生命週期橋接

1. client 呼叫 `message/send`(metadata.senderName 表明身分)→ Task SUBMITTED
2. hub 把訊息寫進 room=contextId 的訊息流(帶 `task_id` 欄位、mentions 注入目標 agent)→ WORKING
3. poller → cursor 檔 → agent 喚醒(既有機制,零改動)
4. agent 依 guide 對該訊息 **reply_to** → hub 偵測到 → Task COMPLETED,agent 的回覆包成
   Message(ROLE_AGENT)放進 status.message 與 history,喚醒所有阻塞中的 send / 串流訂閱者
5. `tasks/cancel` 可在終態前取消(否則 -32002)

## 物件形狀(照 spec 1.0)

- Task:`{id, contextId, status:{state, timestamp, message?}, history:[Message], artifacts:[], metadata}`
- state 列舉:`TASK_STATE_SUBMITTED / WORKING / INPUT_REQUIRED / COMPLETED / FAILED / CANCELED / REJECTED / AUTH_REQUIRED`
- Message:`{messageId, role: ROLE_USER|ROLE_AGENT, parts:[{text}], contextId, taskId, metadata}`
- 錯誤碼(已對原始 spec 驗證):-32001 TaskNotFound、-32002 TaskNotCancelable、
  -32003 PushNotificationNotSupported、-32004 UnsupportedOperation、-32005 ContentTypeNotSupported、
  -32006 InvalidAgentResponse、**-32007 ExtendedAgentCardNotConfigured**、-32008 ExtensionSupportRequired、
  **-32009 VersionNotSupported** + 標準 JSON-RPC 碼(A2A 專屬碼範圍 -32001 ~ -32099)

## MVP 邊界(已知取捨)

- Task 存在記憶體(重啟即失;訊息本體仍在 chat.jsonl 不會丟)— 之後可落地 tasks.jsonl
- 只支援 TextPart;無 artifacts;無簽章/security schemes(本機信任環境)
- push notifications 未實作(webhook 喚醒鏈已由 poller 承擔)
