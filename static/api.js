/* ═══════════════════════════════════════════════════════════════════════════
   api.js — 跟伺服器說話的唯一窗口

   畫面上所有需要「問伺服器」或「告訴伺服器」的事,都從這裡出去:
   拿訊息、送訊息、拿成員名單、派任務……

   為什麼要有這個檔案:如果每個元件各自去發請求,錯誤處理、認證標頭、
   逾時規則就會散落在十幾個地方,改一次要改十幾處。集中在這裡之後,
   「跟伺服器溝通」這件事就只有一個入口。
   ═══════════════════════════════════════════════════════════════════════ */

/* ═══════════ ChatApi(Repository:HTTP 唯一出入口)═══════════ */

function createApi(notify) {
  let failStreak = 0;

  /** 通用 GET/RPC:失敗 console.warn,連續失敗才 toast(誠實原則)。

      quiet=true 給「背景輪詢」用:失敗不計入 failStreak、不彈 toast —
      次要功能壞掉不該蓋住使用者的畫面,而「連線真的斷了」有 header 的
      SERVER 燈負責通報,分工清楚。錯誤帶 status 供呼叫端做 404 降級。 */
  async function request(url, options, quiet) {
    try {
      const res = await fetch(url, options);
      if (!res.ok) {
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        throw err;
      }
      if (!quiet) failStreak = 0;
      return await res.json();
    } catch (err) {
      console.warn("[api]", url, err.message || err);
      if (!quiet && ++failStreak >= API_FAIL_TOAST_THRESHOLD) {
        notify(`>> API 連續失敗:${url}`, false);
      }
      throw err;
    }
  }

  return {
    config: () => request("/api/config"),
    rooms: () => request("/api/rooms", undefined, true),
    agents: () => request("/agents", undefined, true),
    members: (room) => request(`/api/rooms/${room}/members`, undefined, true),
    tasks: (room) => request(`/api/rooms/${room}/tasks`, undefined, true),
    presence: (room) => request(`/api/rooms/${room}/presence`, undefined, true),
    messages: (room, qs) => request(`/api/rooms/${room}/messages?${qs}`),
    /** 發言例外:4xx 的 detail 是要給使用者看的,不走 throw,回 {ok, status, data}。
        token 有值時附 Authorization(AUTH=on 的寫入守門,roadmap ③)。 */
    async send(room, body, token) {
      const headers = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`/api/rooms/${room}/messages`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, data };
    },
    /** 發任務:走 A2A 正門而非聊天門。UI 一律 returnImmediately —
        阻塞版會讓畫面卡到對方回覆或逾時。 */
    async sendTask(target, { room, text, sender, deadlineSeconds, token }) {
      const headers = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch(`/agents/${target}/a2a`, {
        method: "POST", headers,
        body: JSON.stringify({
          jsonrpc: "2.0", id: 1, method: "SendMessage",
          params: {
            message: { role: "ROLE_USER", parts: [{ text }],
                       messageId: crypto.randomUUID(), contextId: room },  // = 當前房間
            configuration: { returnImmediately: true },
            metadata: { senderName: sender, deadlineSeconds },
          },
        }),
      });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok && !data.error, data };
    },
    rpc: (agent, method, params) => request(`/agents/${agent}/a2a`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    }),
  };
}
