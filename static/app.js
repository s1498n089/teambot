/* A2A Chatroom — 前端邏輯層。
   分層:
     ChatApi(Repository)   — HTTP 唯一出入口 + 統一錯誤策略
     composables            — useTasks / useUnread / useStream(SRP,root 瘦身)
     元件                    — HoloModal(彈窗皮)/ ChatHeader / MessageItem / ChatComposer
     root                    — 只做編排;SSE 進訊息走具名 handler 鏈
   零建置:純 CDN Vue 3,無打包器。 */

const { createApp, reactive, computed } = Vue;

/* ═══════════ 常數(魔數集中地)═══════════ */
const PAGE = 100;                    // 初載與懶載的每頁訊息數
const GROUP_WINDOW_MS = 300000;      // 同人連續發言的 grouping 視窗(5 分鐘)
const ACTIVE_WINDOW_MS = 600000;     // 近期發言窗:在場者再細分「活躍/待命」(10 分鐘)
const PRESENCE_POLL_MS = 15000;      // 在場名單輪詢間隔(輕量,只回名字陣列)
const TOAST_MS = 3500;
const FLASH_MS = 2000;               // 跳轉脈衝動畫的 class 存留時間
const API_FAIL_TOAST_THRESHOLD = 3;  // 連續失敗達此數才吵使用者
const DEFAULT_DEADLINE_SECONDS = 300;  // 任務逾時預設值(與 hub 同步)

/* 執行期設定:開機從 /api/config 灌入 — reactive 讓 tokens/顏色 computed 真正依賴它
   (regex 熱替換必須觸發重算,不能靠呼叫順序保命)。 */
const rt = reactive({
  mentionPattern: "(?<![A-Za-z0-9_@.-])(@[\\w一-鿿-]+)", // fallback,與 server 同步
  palette: { user: "#c9d1d9" },                          // 人類底色;agent 色相由 config 下發
  authEnabled: false,                                    // AUTH=on 時 UI 顯示 token 欄(roadmap ③)
});

/* toast 的延遲繫結:api 在 root mount 前就建好,先把通知丟進這個殼 */
const toastBus = { show: null };

/* ═══════════ 純函式 helpers ═══════════ */

/** FNV-1a:名字 → 穩定 hash(頭像與未註冊名字的色相種子)。 */
function hashName(name) {
  let h = 2166136261;
  for (const c of name) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

/** 名字 → 色相:已註冊走 config 下發的 palette,未知名字用 hash 產生穩定 HSL。 */
function colorHexOf(name) {
  if (rt.palette[name]) return rt.palette[name];
  return `hsl(${hashName(name) % 360} 60% 62%)`;
}

/* 頭像:名字 hash → 5x5 鏡射像素 identicon(SVG data-uri,零外部依賴)。
   cache key 帶色相 — config 晚到會改變 agent 顏色,舊快取不能沿用。 */
const avatarCache = {};
function avatarOf(name) {
  const color = colorHexOf(name);
  const key = `${name}|${color}`;
  if (avatarCache[key]) return avatarCache[key];
  let h = hashName(name) || 1;
  const rnd = () => { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; return (h >>> 0) / 4294967296; };
  let rects = "";
  for (let y = 0; y < 5; y++) for (let x = 0; x < 3; x++) {
    if (rnd() > 0.48) {
      rects += `<rect x="${x}" y="${y}" width="1" height="1"/>`;
      if (x < 2) rects += `<rect x="${4 - x}" y="${y}" width="1" height="1"/>`; // 左右鏡射
    }
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-0.7 -0.7 6.4 6.4" shape-rendering="crispEdges">` +
    `<rect x="-0.7" y="-0.7" width="6.4" height="6.4" fill="#0d0d14"/><g fill="${color}">${rects}</g></svg>`;
  return (avatarCache[key] = "data:image/svg+xml," + encodeURIComponent(svg));
}

/* 斷詞:URL 只吃 ASCII 字元(中文與全形標點自然截斷),結尾 ASCII 標點剝回文字;
   mention 規則從 rt 讀(reactive 依賴)。regex 依 pattern 字串快取,不必每次重編。 */
const URL_RE = /(https?:\/\/[A-Za-z0-9\-._~:/?#@!$&*+;=%()\[\]]+)/g;
const URL_TRAIL_RE = /^(.*?)([.,;:!?)\]]*)$/;
let _mentionCache = { src: null, rx: null };
function mentionRegex() {
  if (_mentionCache.src !== rt.mentionPattern) {
    _mentionCache = { src: rt.mentionPattern, rx: new RegExp(rt.mentionPattern, "g") };
  }
  return _mentionCache.rx;
}
/** 裸文字 → link / mention / text(markdown 語法之外的最後一關,永遠是它收尾)。
    mods 是「繼承下來的字體修飾」,見 inlineTokens 的扁平化說明。 */
function plainTokens(text, mods, out) {
  for (const chunk of text.split(URL_RE)) {
    if (!chunk) continue;
    if (/^https?:\/\//.test(chunk)) {
      const m = chunk.match(URL_TRAIL_RE);
      out.push({ t: "link", v: m[1], href: m[1], ...mods });
      if (m[2]) out.push({ t: "text", v: m[2], ...mods });
      continue;
    }
    for (const p of chunk.split(mentionRegex())) {
      if (!p) continue;
      out.push({ t: p.startsWith("@") && p.length > 1 ? "mention" : "text", v: p, ...mods });
    }
  }
}

/* ── Markdown(受限子集)──────────────────────────────────────────────
   為什麼自己寫而不用 marked + DOMPurify:渲染 markdown 等於把「別人發的文字」
   變成「在我瀏覽器裡跑的 HTML」,而 localStorage 裡放著 token。這裡的 renderer
   只吐**資料結構**,模板全程走 Vue 插值({{ }} 自動 escape、絕不 v-html)——
   於是 XSS 不是「被過濾掉」,而是**沒有產生 HTML 的路徑可走**。
   代價是功能只做子集;parser 有 bug 最慘是排版跑掉,不會變成執行別人的 script。
   附帶好處:零外部依賴,沒有外網的區網機器照樣渲染得出來。 */

// 行內語法,左優先:code 先吃(其內不再解析)、strong 先於 em(避免 ** 被拆成兩個 *)。
// strong/em 兩端都要求非空白字元,"5 * 3 * 2" 這種算式才不會被誤判成斜體。
const INLINE_RE = /(`[^`\n]+`)|(\*\*\S(?:[^*]*\S)?\*\*)|(\*\S(?:[^*\n]*\S)?\*)|(!?\[[^\]\n]*\]\([^)\s]+\))/;
// 圖片語法 ![alt](url) 一併吃下,但**只渲染成連結、絕不載圖**。兩個理由:
// (1) 自動載外圖 = 把每個觀戰者的 IP 送給第三方,圖片 URL 可以是追蹤像素;
// (2) <img src> 對「不是圖片」的 URL 一樣會發出 GET —— 等於一個無聲的跨站請求
//     發射器,誰貼一則訊息就能讓全房的瀏覽器去打某個會改狀態的端點(bob 補的)。
// 想看圖的人自己點連結。
const MD_LINK_RE = /^!?\[([^\]\n]*)\]\(([^)\s]+)\)$/;
const SAFE_HREF_RE = /^https?:\/\//i;  // scheme 白名單:擋 javascript: / data:

/** 行內解析。刻意輸出**扁平**陣列而非巢狀樹:粗體/斜體改用 token 上的 b/i 旗標表示,
    於是「**粗體裡有 `code`**」也只是一顆帶 b 的 code token。扁平換來三件事 ——
    模板不必遞迴、沒有深度炸彈、渲染分支一眼看完。 */
function inlineTokens(text, mods) {
  mods = mods || {};
  const out = [];
  let rest = text, m;
  while ((m = INLINE_RE.exec(rest))) {
    if (m.index) plainTokens(rest.slice(0, m.index), mods, out);
    const raw = m[0];
    if (m[1]) {
      out.push({ t: "code", v: raw.slice(1, -1), ...mods });          // `code`:內容原樣,不再解析
    } else if (m[2]) {
      out.push(...inlineTokens(raw.slice(2, -2), { ...mods, b: true }));
    } else if (m[3]) {
      out.push(...inlineTokens(raw.slice(1, -1), { ...mods, i: true }));
    } else {
      const md = raw.match(MD_LINK_RE);
      if (md && SAFE_HREF_RE.test(md[2])) {
        out.push({ t: "link", v: md[1] || md[2], href: md[2], ...mods });
      } else {
        plainTokens(raw, mods, out);  // scheme 不合白名單 → 當普通文字,不生連結
      }
    }
    rest = rest.slice(m.index + raw.length);
  }
  if (rest) plainTokens(rest, mods, out);
  return out;
}

const FENCE_RE = /^\s*```(.*)$/;
const LIST_RE = /^\s*(?:[-*+]|\d+\.)\s+(.*)$/;
const QUOTE_RE = /^\s*>\s?(.*)$/;
const HEAD_RE = /^\s*(#{1,6})\s+(.*)$/;
const ROW_RE = /^\s*\|(.*)\|\s*$/;              // 表格列:前後都要有 |
const SEP_RE = /^\s*\|[\s:|-]+\|\s*$/;          // 分隔列:只由 | - : 空白組成

/** "| a | b |" → ["a", "b"](前後的空欄去掉,不支援跳脫的 \| ,子集) */
function tableCells(line) {
  return line.match(ROW_RE)[1].split("|").map((c) => inlineTokens(c.trim()));
}

/** 塊解析。
    標題:原本刻意不做(顧慮聊天室裡的 "# xxx" 多半是 shell 註解,變超大字幫倒忙),
    但老闆實際用了之後回報「缺標題」——**使用者的實際體驗勝過我們的事前推測**,故補上。
    顧慮用兩件事化解:``` 區塊與行內 `code` 內不解析任何語法(shell 註解通常就在那裡),
    且標題字級刻意克制(最大只到本文的 1.25 倍)——泡泡不是文件,不需要巨無霸大字。
    段落內的單換行**就是換行**(rows 陣列):標準 markdown 會把它吃掉,
    但聊天訊息裡按 Enter 就是要換行,照標準做反而是老闆說的「黏在一起」。 */
function parseBlocks(text) {
  const lines = String(text).split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE_RE);
    if (fence) {                                   // ``` 圍籬:內容原樣,不解析任何語法
      const lang = fence[1].trim();
      const body = [];
      i++;
      while (i < lines.length && !FENCE_RE.test(lines[i])) body.push(lines[i++]);
      i++;                                         // 吃掉收尾的 ```(沒有就是讀到結尾,一樣收工)
      blocks.push({ t: "code", lang, v: body.join("\n") });
    } else if (HEAD_RE.test(line)) {
      const h = line.match(HEAD_RE);
      blocks.push({ t: "h", level: h[1].length, inline: inlineTokens(h[2]) });
      i++;
    } else if (ROW_RE.test(line) && i + 1 < lines.length && SEP_RE.test(lines[i + 1])) {
      const head = tableCells(line);                // 表格必須有表頭 + 分隔列才算數,
      i += 2;                                       // 否則單獨一行 |a|b| 只是普通文字
      const rows = [];
      while (i < lines.length && ROW_RE.test(lines[i])) rows.push(tableCells(lines[i++]));
      blocks.push({ t: "table", head, rows });
    } else if (LIST_RE.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && LIST_RE.test(lines[i])) {
        items.push(inlineTokens(lines[i].match(LIST_RE)[1]));
        i++;
      }
      blocks.push({ t: "list", ordered, items });
    } else if (QUOTE_RE.test(line) && line.trim().startsWith(">")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        rows.push(inlineTokens(lines[i].match(QUOTE_RE)[1]));
        i++;
      }
      blocks.push({ t: "quote", rows });
    } else if (!line.trim()) {
      i++;                                         // 空行只負責分段,不留痕跡
    } else {
      const rows = [];
      // 段落一路吃到「空行或另一種塊開頭」為止 —— 每加一種塊型,這裡就要讓一次路,
      // 否則新塊會被段落吞掉(表格判斷含下一行,故一併看 i+1)
      while (i < lines.length && lines[i].trim()
             && !FENCE_RE.test(lines[i]) && !LIST_RE.test(lines[i])
             && !HEAD_RE.test(lines[i]) && !lines[i].trim().startsWith(">")
             && !(ROW_RE.test(lines[i]) && i + 1 < lines.length && SEP_RE.test(lines[i + 1]))) {
        rows.push(inlineTokens(lines[i]));
        i++;
      }
      blocks.push({ t: "p", rows });
    }
  }
  return blocks;
}

function fmtTime(ts) { return new Date(ts).toLocaleTimeString("en-GB", { hour12: false }); }
function fmtFull(ts) { return new Date(ts).toLocaleString("en-GB", { hour12: false }); }
function dayOf(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* task 狀態的單一事實來源:CSS class 與縮寫都查這張表,
   REJECTED / INPUT_REQUIRED 上線時只加表項。null(蒸發 task)→ 誠實的灰。 */
const STATE_META = {
  TASK_STATE_SUBMITTED: { cls: "open", short: "SUBMITTED" },
  TASK_STATE_WORKING: { cls: "open", short: "WORKING" },
  TASK_STATE_COMPLETED: { cls: "done", short: "COMPLETED" },
  TASK_STATE_FAILED: { cls: "failed", short: "FAILED" },
  TASK_STATE_REJECTED: { cls: "failed", short: "REJECTED" },
  TASK_STATE_CANCELED: { cls: "canceled", short: "CANCELED" },
};
const STATE_GONE = { cls: "gone", short: "EVAPORATED" };
function stateMeta(state) { return STATE_META[state] || STATE_GONE; }

/* 鏡頭(FOCUS):決定「這面牆以誰為第一人稱」——靠右的那位。
   預設 FOCUS_ME:跟著名字欄動(你報什麼身分,就以誰為主角);點頭像把鏡頭
   交給別人(以他人視角回顧),再點一次交回自己。沒有「關閉」狀態 ——
   鏡頭永遠有主角,少一個狀態少一份心智負擔。 */
const FOCUS_ME = "@me";

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

/* ═══════════ Composables ═══════════ */

/** task 摘要的載入與查詢:UI 徽章、header 計數、彈窗摘要都吃這份。 */
function useTasks(api, room) {
  const tasks = reactive({
    map: {},
    active: computed(() =>
      Object.values(tasks.map).filter(
        (t) => t.state === "TASK_STATE_SUBMITTED" || t.state === "TASK_STATE_WORKING").length),
    async load() {
      try {
        const data = await api.tasks(room);
        tasks.map = Object.fromEntries(data.tasks.map((t) => [t.id, t]));
      } catch (e) { /* request 已記錄;徽章維持上次狀態 */ }
    },
  });
  return tasks;
}

/** 未讀與分頁標題:── NEW ── 分隔線位置、title (n) 未讀數、已讀水位持久化。 */
function useUnread(room) {
  const key = "a2a-read-" + room;
  const unread = reactive({
    afterId: parseInt(localStorage.getItem(key) || "0", 10), // 上次離開時的已讀水位
    unseen: 0,
    markRead(lastId) {
      localStorage.setItem(key, String(lastId));
      unread.unseen = 0;
      unread.syncTitle();
    },
    syncTitle() {
      document.title = unread.unseen > 0 ? `(${unread.unseen}) A2A Chatroom` : "A2A Chatroom";
    },
  });
  return unread;
}

/** SSE 連線的薄封裝:root 只提供具名 handlers,不碰 EventSource 細節。 */
function useStream({ url, onMessage, onOpen, onError }) {
  let es = null;
  return {
    connect() {
      es = new EventSource(url);
      es.onopen = onOpen;
      es.onerror = onError;
      es.onmessage = (e) => onMessage(JSON.parse(e.data));
    },
    close() { if (es) es.close(); },
  };
}

/* ═══════════ 粒子背景 ═══════════
   密度依視口面積封頂、DPR≤1.5、分頁隱藏暫停、游標吸引;
   prefers-reduced-motion 全關(只留 CSS 靜態 circuit grid)。 */
function startParticles() {
  const canvas = document.getElementById("particles");
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { canvas.remove(); return; }
  const ctx = canvas.getContext("2d");
  const dpr = Math.min(devicePixelRatio || 1, 1.5);
  let W, H, pts = [], raf = null;
  const mouse = { x: -1e4, y: -1e4 };
  const newPt = () => ({ x: Math.random() * W, y: Math.random() * H,
                         vx: (Math.random() - 0.5) * 0.35, vy: (Math.random() - 0.5) * 0.35 });
  function resize() {
    W = innerWidth; H = innerHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    let target = Math.min(80, Math.floor((W * H) / 18000));
    if (W < 560) target = Math.floor(target / 2); // 手機減半;resize 時重判
    while (pts.length < target) pts.push(newPt());
    pts.length = target;
  }
  function step() {
    ctx.clearRect(0, 0, W, H);
    for (const p of pts) {
      const dx = mouse.x - p.x, dy = mouse.y - p.y, d2 = dx * dx + dy * dy;
      if (d2 < 22500 && d2 > 1) { const d = Math.sqrt(d2); p.vx += (dx / d) * 0.015; p.vy += (dy / d) * 0.015; }
      p.vx = Math.max(-0.6, Math.min(0.6, p.vx)); p.vy = Math.max(-0.6, Math.min(0.6, p.vy));
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > W) p.vx *= -1;
      if (p.y < 0 || p.y > H) p.vy *= -1;
      ctx.fillStyle = "rgba(0, 255, 136, 0.5)";
      ctx.fillRect(p.x - 1, p.y - 1, 2, 2);
    }
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
      const a = pts[i], b = pts[j];
      const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy;
      if (d2 < 14400) { // 連線距離 <120px,透明度隨距離衰減
        ctx.strokeStyle = `rgba(0, 212, 255, ${(1 - Math.sqrt(d2) / 120) * 0.22})`;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
    }
    raf = requestAnimationFrame(step);
  }
  addEventListener("resize", resize);
  addEventListener("mousemove", (e) => { mouse.x = e.clientX; mouse.y = e.clientY; });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = null; }
    else if (!raf) step();
  });
  resize(); step();
}
startParticles();

/* ═══════════ 元件 ═══════════ */

/** Holographic 彈窗的皮:overlay/四角/close 只寫一次,內容走 slot。 */
const HoloModal = {
  emits: ["close"],
  template: `
  <div class="overlay" @click.self="$emit('close')">
    <div class="modal">
      <span class="corner tl"></span><span class="corner tr"></span>
      <span class="corner bl"></span><span class="corner br"></span>
      <button class="modal-close mono" @click="$emit('close')">✕</button>
      <div class="modal-head"><slot name="head"></slot></div>
      <div class="modal-body"><slot name="body"></slot></div>
    </div>
  </div>`,
};

const ChatHeader = {
  data() { return { FOCUS_ME }; },  // 樣板要讀得到鏡頭常數
  props: ["room", "rooms", "status", "activeTasks", "onlineMembers",
          "focusTarget", "focusName"],
  emits: ["switch-room", "open-member", "set-focus", "adjust-font"],
  computed: {
    /** 這顆燈講的是「本頁與 server 的連線」— 主詞寫明,別跟成員在場狀態混淆。 */
    statusText() { return { connecting: "SERVER…", online: "SERVER", reconnecting: "SERVER ✕" }[this.status]; },
  },
  methods: { avatarOf, colorHexOf },
  template: `
  <header>
    <span class="hdr-title mono">&gt;&gt; ROOM_{{ room.toUpperCase() }}</span>
    <select v-if="rooms.length > 1" class="room-switch mono" :value="room"
            @change="$emit('switch-room', $event.target.value)">
      <option v-for="r in rooms" :key="r.name" :value="r.name">#{{ r.name }} ({{ r.count }})</option>
    </select>
    <span class="lamp" :class="status"></span>
    <span class="lamp-text mono" :class="{ glitching: status === 'reconnecting' }">{{ statusText }}</span>
    <span class="online-row">
      <img v-for="m in onlineMembers" :key="m.name" :src="avatarOf(m.name)"
           :title="'以 ' + m.name + ' 的視角檢視(再點一次交回自己)'"
           :class="{ focused: m.name === focusName }"
           :style="m.name === focusName ? { borderColor: colorHexOf(m.name) } : {}"
           @click="$emit('set-focus', focusTarget === m.name ? FOCUS_ME : m.name)">
    </span>
    <span class="spacer"></span>
    <span class="font-ctl" title="聊天字級">
      <button class="mono" @click="$emit('adjust-font', -2)">A-</button>
      <button class="mono" @click="$emit('adjust-font', 0)">A</button>
      <button class="mono" @click="$emit('adjust-font', 2)">A+</button>
    </span>
    <span class="meta mono" :class="{ 'task-counter': activeTasks, zero: !activeTasks }">TASKS:{{ activeTasks }}</span>
  </header>`,
};

/** 行內 token 的渲染器。因為 inlineTokens 吐的是扁平陣列,這裡不需要遞迴自我引用 ——
    粗體/斜體是 token 上的旗標,不是巢狀結構。每個分支都用 {{ }} 插值(Vue 自動 escape)。 */
const MdInline = {
  props: ["ts"],
  template: `<template v-for="(p, i) in ts" :key="i"><span v-if="p.t === 'mention'" class="mention" :class="{ 'md-b': p.b, 'md-i': p.i }">{{ p.v }}</span><a v-else-if="p.t === 'link'" :href="p.href" target="_blank" rel="noopener noreferrer" :class="{ 'md-b': p.b, 'md-i': p.i }">{{ p.v }}</a><code v-else-if="p.t === 'code'" class="md-code-inline" :class="{ 'md-b': p.b, 'md-i': p.i }">{{ p.v }}</code><span v-else :class="{ 'md-b': p.b, 'md-i': p.i }">{{ p.v }}</span></template>`,
};

const MessageItem = {
  components: { "md-inline": MdInline },
  props: ["m", "grouped", "me", "focusName", "taskInfo"],
  emits: ["reply", "jump", "open-member", "open-task", "copy", "anchor"],
  computed: {
    /** 靠右的是「鏡頭主角」而非固定的自己 — focusName 由 root 解析(ME/具名/null)。 */
    isFocused() { return !!this.focusName && this.m.from === this.focusName; },
    /** 內部讀 rt.mentionPattern(reactive)→ config 熱替換會觸發重算 */
    blocks() { return parseBlocks(this.m.text); },
    senderColor() { return colorHexOf(this.m.from); },
    registered() { return this.m.from in rt.palette; },
    /** 綠邊永遠關於「我」:警示不因換鏡頭而失效(語意色獨占鐵律)。 */
    pingMe() { return (this.m.mentions || []).includes(this.me); },
    /** 主角被點名:用主角自己的色相標記,不搶語意綠 — 兩個資訊同框不打架。 */
    pingFocus() {
      return !!this.focusName && this.focusName !== this.me
        && (this.m.mentions || []).includes(this.focusName);
    },
    focusColor() { return this.focusName ? colorHexOf(this.focusName) : "transparent"; },
    badge() { return stateMeta(this.taskInfo ? this.taskInfo.state : null); },
  },
  methods: { avatarOf, fmtTime, fmtFull },
  template: `
  <div class="msg" :id="'msg-' + m.id"
       :class="{ grouped, own: isFocused, 'ping-me': pingMe, 'ping-focus': pingFocus }"
       :style="{ '--sender': senderColor, '--focus-color': focusColor }">
    <div class="gutter">
      <img v-if="!grouped" class="avatar" :src="avatarOf(m.from)" :style="{ borderColor: senderColor }"
           :title="m.from" @click="$emit('open-member', m.from)">
    </div>
    <div class="msg-body">
      <div v-if="!grouped" class="msg-head mono">
        <span class="pill" :class="{ unreg: !registered }"
              :style="{ color: senderColor, borderColor: senderColor }">{{ m.from.toUpperCase() }}</span>
        <span v-if="!registered" class="unreg-tag">UNREGISTERED</span>
        <span v-if="m.task_id" class="task-badge" :class="badge.cls"
              :title="taskInfo ? taskInfo.state : 'EVAPORATED(server 重啟前的 task,狀態已失)'"
              @click="$emit('open-task', m.task_id)">{{ taskInfo ? 'TASK' : 'TASK⌀' }}</span>
        <span v-if="m.reply_to" class="reply-chip" @click="$emit('jump', m.reply_to)">&gt;&gt; #{{ m.reply_to }}</span>
        <span class="time" :title="fmtFull(m.ts)" @click="$emit('anchor', m.id)">#{{ m.id }} · {{ fmtTime(m.ts) }}</span>
        <span class="head-spacer"></span>
        <button class="hover-btn mono" @click="$emit('copy', m.text)">⧉ COPY</button>
        <button class="hover-btn mono" @click="$emit('reply', m.id)">⟲ REPLY</button>
      </div>
      <div class="bubble chamfer-sm"><template v-for="(b, bi) in blocks" :key="bi"><pre v-if="b.t === 'code'" class="md-code"><code>{{ b.v }}</code></pre><div v-else-if="b.t === 'h'" class="md-h" :class="'md-h' + b.level"><md-inline :ts="b.inline"></md-inline></div><div v-else-if="b.t === 'table'" class="md-table-wrap"><table class="md-table"><thead><tr><th v-for="(c, ci) in b.head" :key="ci"><md-inline :ts="c"></md-inline></th></tr></thead><tbody><tr v-for="(r, ri) in b.rows" :key="ri"><td v-for="(c, ci) in r" :key="ci"><md-inline :ts="c"></md-inline></td></tr></tbody></table></div><component v-else-if="b.t === 'list'" :is="b.ordered ? 'ol' : 'ul'" class="md-list"><li v-for="(it, ii) in b.items" :key="ii"><md-inline :ts="it"></md-inline></li></component><blockquote v-else-if="b.t === 'quote'" class="md-quote"><template v-for="(r, ri) in b.rows" :key="ri"><br v-if="ri"><md-inline :ts="r"></md-inline></template></blockquote><p v-else class="md-p"><template v-for="(r, ri) in b.rows" :key="ri"><br v-if="ri"><md-inline :ts="r"></md-inline></template></p></template></div>
    </div>
  </div>`,
};

const ChatComposer = {
  props: ["name", "room", "replyTo", "authEnabled", "token", "targets"],
  emits: ["update:name", "update:token", "send", "send-task", "cancel-reply"],
  data() {
    return {
      draft: "",
      mode: "msg",                             // msg = 聊天門、task = 協定門
      target: "",                              // 交辦給誰(不從文字的 @ 自動帶入 —
                                               // 目標是協定欄位、@ 是社交語法,不讓兩者黏回去)
      deadline: DEFAULT_DEADLINE_SECONDS,
    };
  },
  computed: {
    nameWidth() { return Math.max(3, (this.name || "").length + 1) + "ch"; }, // 名字欄自適應不截斷
    isTask() { return this.mode === "task"; },
    canFire() { return !!this.draft.trim() && (!this.isTask || !!this.target); },
  },
  methods: {
    fire() {
      const t = this.draft.trim();
      if (!t) return;
      if (!this.isTask) return this.$emit("send", t);
      if (!this.target) return;                // 沒選對象就不送(鈕已 disabled,雙保險)
      this.$emit("send-task", { text: t, target: this.target,
                                deadlineSeconds: Number(this.deadline) || DEFAULT_DEADLINE_SECONDS });
    },
    clear() { this.draft = ""; },       // 父層在「送出成功」後才呼叫 — 失敗保留草稿
    focusBox() { this.$refs.box.focus(); },
  },
  template: `
  <footer>
    <div v-if="replyTo" class="reply-bar mono">RE #{{ replyTo }}
      <span class="x" @click="$emit('cancel-reply')">✕</span><span class="hint">[ESC]</span>
    </div>
    <div class="task-bar mono" v-if="isTask">
      <span class="task-tag">TASK</span>
      <label>TO
        <select v-model="target">
          <option value="">選一位…</option>
          <option v-for="t in targets" :key="t.name" :value="t.name">
            {{ t.name }} {{ t.present ? "●" : "○" }}
          </option>
        </select>
      </label>
      <label>逾時
        <input class="deadline" type="number" min="5" max="3600" v-model="deadline"> 秒
      </label>
      <span class="hint">送出後對方會收到帶 TASK 徽章的交辦,狀態全程可追蹤</span>
    </div>
    <div class="input-row">
      <span class="mode-ctl">
        <button class="mono" :class="{ on: !isTask }" @click="mode = 'msg'">MSG</button>
        <button class="mono" :class="{ on: isTask }" @click="mode = 'task'">TASK</button>
      </span>
      <span class="prompt mono">
        <input class="name" :value="name" :style="{ width: nameWidth }"
               @input="$emit('update:name', $event.target.value)">@{{ room }} &gt;_
      </span>
      <input v-if="authEnabled" class="token mono" type="password" :value="token"
             placeholder="token" title="AUTH 已啟用:發言需要你的 bearer token"
             @input="$emit('update:token', $event.target.value)">
      <textarea ref="box" v-model="draft"
                :placeholder="isTask ? '任務內容,Enter 送出(Shift+Enter 換行)'
                                     : '輸入訊息,Enter 送出(Shift+Enter 換行)'"
                @keydown.enter.exact.prevent="fire"
                @keydown.esc="$emit('cancel-reply')"></textarea>
      <button class="send" :class="{ task: isTask }" :disabled="!canFire" @click="fire">
        {{ isTask ? 'SEND TASK' : 'SEND' }}</button>
    </div>
  </footer>`,
};

/* ═══════════ Root:只做編排 ═══════════ */

createApp({
  components: {
    "holo-modal": HoloModal,
    "chat-header": ChatHeader,
    "message-item": MessageItem,
    "chat-composer": ChatComposer,
  },
  setup() {
    // composition root:api 與 composables 在這裡建構、注入樣板
    const room = new URLSearchParams(location.search).get("room") || "main";
    const api = createApi((msg, ok) => toastBus.show && toastBus.show(msg, ok));
    return { room, api, tasks: useTasks(api, room), unread: useUnread(room) };
  },
  data() {
    return {
      messages: [],
      members: [],
      agents: [],     // A2A 名冊 — 只有註冊 agent 能被指派 task
      present: [],   // 在場名單(SSE 連線開著的人)— presence 的事實來源
      presenceSupported: true,  // 舊版 hub 沒有 /presence:404 後自動停用並退回舊判定
      rooms: [],
      lastId: 0,
      name: localStorage.getItem("a2a-name") || "user",
      token: localStorage.getItem("a2a-token") || "",  // AUTH=on 時的個人鑰匙(roadmap ③)
      status: "connecting",
      replyTo: null,
      newBelow: 0,
      hasMore: false,
      loadingOlder: false,
      toast: null,
      toastOk: false,
      modal: null,   // { type: 'member'|'task', ... }
      // 鏡頭目標:預設 ME(自己靠右,聊天慣例);舊版存的 "0" 對映到觀戰視角
      focusTarget: FOCUS_ME,
      bubbleFont: parseInt(localStorage.getItem("a2a-font") || "20", 10), // 基準字級 px:整個 UI 依此等比縮放
      nowTick: Date.now(),  // 每分鐘跳動,驅動在線狀態的重新計算
    };
  },
  computed: {
    myName() { return this.name.trim() || "user"; },
    /** 鏡頭目標解析成實際名字:ME → 我、具名 → 該人、OFF → null(全員靠左)。 */
    focusName() {
      return this.focusTarget === FOCUS_ME ? this.myName : this.focusTarget;
    },
    authOn() { return rt.authEnabled; },  // 模板需要 reactive 依賴,包一層 computed
    /** 頭像列 = 在場者(含安靜待命的);離線者不佔位。 */
    /** 可交辦對象:只有註冊 agent(協定層擋 unknown agent),附在場標記 —
        發給沒人在的 agent 只會白等到逾時,選之前就該看得見。 */
    taskTargets() {
      return this.agents.map((a) => ({ name: a.name, present: this.present.includes(a.name) }));
    },
    onlineMembers() {
      if (!this.presenceSupported) {
        return this.members.filter((m) => this.presenceOf(m) === "active").slice(0, 6);
      }
      return this.present.map((name) => this.members.find((m) => m.name === name) || { name })
        .slice(0, 6);
    },
    /** timeline 的顯示列:日期分隔線 + ── NEW ── 未讀線 + 訊息(含 grouping 判定)。 */
    rows() {
      const out = [];
      let prevDay = "", prevMsg = null, unreadPlaced = false; // 旗標取代 out.some(免迴圈內線性掃描)
      for (const m of this.messages) {
        const d = dayOf(m.ts);
        if (d !== prevDay) { out.push({ type: "sep", key: "sep-" + d, date: d }); prevDay = d; prevMsg = null; }
        if (!unreadPlaced && this.unread.afterId && m.id > this.unread.afterId) {
          out.push({ type: "unread", key: "unread" });
          unreadPlaced = true;
          prevMsg = null;
        }
        // grouping:同人、視窗內、且非 task/引用訊息(那些需要完整表頭)
        const grouped = !!prevMsg && prevMsg.from === m.from && !m.task_id && !m.reply_to
          && (new Date(m.ts) - new Date(prevMsg.ts)) < GROUP_WINDOW_MS;
        out.push({ type: "msg", key: m.id, m, grouped });
        prevMsg = m;
      }
      return out;
    },
  },
  async mounted() {
    toastBus.show = this.showToast; // api 的延遲通知在此接上
    this.applyFont(); // 開機套用記憶的字級

    // 順序:config(mention 規則/色相)→ 首頁訊息 → 平行載入輔助資料
    try {
      const cfg = await this.api.config();
      rt.mentionPattern = cfg.mentionPattern;
      rt.authEnabled = !!cfg.authEnabled;
      rt.palette = { user: "#c9d1d9",
                     ...Object.fromEntries(Object.entries(cfg.agents).map(([n, a]) => [n, a.color])) };
    } catch (e) { /* fallback 規則已內建 */ }

    try {
      const data = await this.api.messages(this.room, `tail=${PAGE}`);
      this.messages = data.messages;
      this.lastId = data.last_id;
      this.hasMore = data.messages.length === PAGE;
    } catch (e) { this.showToast(">> 初始載入失敗,請重整", false); }

    if (this.unread.afterId >= this.lastId) this.unread.afterId = 0; // 沒有未讀就不畫線
    await Promise.all([this.tasks.load(), this.loadMembers(), this.loadRooms(),
                       this.loadPresence(), this.loadAgents()]);
    this.scrollToBottom();

    this.stream = useStream({
      url: `/api/rooms/${this.room}/stream?since_id=${this.lastId}&watcher=${encodeURIComponent(this.myName)}`,
      onMessage: (m) => this.handleIncoming(m),
      onOpen: () => (this.status = "online"),
      onError: () => (this.status = "reconnecting"),
    });
    this.stream.connect();

    this.unread.markRead(this.lastId);
    setInterval(() => (this.nowTick = Date.now()), 60000);
    setInterval(() => this.loadPresence(), PRESENCE_POLL_MS);  // 在場名單:輕量輪詢即可
    addEventListener("keydown", (e) => { // Esc:彈窗優先,其次取消引用
      if (e.key !== "Escape") return;
      if (this.modal) this.modal = null;
      else if (this.replyTo) this.replyTo = null;
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && this.isNearBottom()) this.unread.markRead(this.lastId);
    });
    if (location.hash.startsWith("#msg-")) { // #msg-N 錨點深連結
      const id = parseInt(location.hash.slice(5), 10);
      if (id) this.$nextTick(() => this.jumpTo(id));
    }
  },
  methods: {
    avatarOf, fmtFull,
    colorOf: colorHexOf,
    stateCls(state) { return stateMeta(state).cls; },
    stateShort(state) { return stateMeta(state).short; },
    isRegistered(name) { return name in rt.palette; },
    /** 三態 presence:● 活躍(在場且近期發言)/ ◐ 待命(在場但安靜)/ ○ 離線(無連線)。
        在場 = SSE 連線開著(bell 或瀏覽器),不再拿「最近有沒有發言」猜在不在。 */
    presenceOf(member) {
      const name = typeof member === "string" ? member : member.name;
      if (!this.presenceSupported) {  // 舊 hub 的退路:回到「近期發言 = 在線」的舊語意
        const seen = (typeof member === "object" && member.lastSeen)
          ? new Date(member.lastSeen).getTime() : 0;
        return this.nowTick - seen < ACTIVE_WINDOW_MS ? "active" : "offline";
      }
      if (!this.present.includes(name)) return "offline";
      const seen = (typeof member === "object" && member.lastSeen)
        ? new Date(member.lastSeen).getTime() : 0;
      return this.nowTick - seen < ACTIVE_WINDOW_MS ? "active" : "standby";
    },
    presenceLabel(member) {
      return { active: "● ACTIVE", standby: "◐ STANDBY", offline: "○ OFFLINE" }[this.presenceOf(member)];
    },
    async loadPresence() {
      if (!this.presenceSupported) return;   // 舊 hub:別再打了,免得白吵
      try { this.present = (await this.api.presence(this.room)).present; }
      catch (e) {
        if (e && e.status === 404) {  // 端點不存在 = hub 比前端舊,優雅降級
          this.presenceSupported = false;
          this.present = [];
          console.warn("[presence] hub 尚未支援 /presence,退回以發言時間推測在線");
        }
      }
    },

    /* ── SSE 進訊息:具名 handler 鏈(一步一責)── */
    handleIncoming(m) {
      if (m.id <= this.lastId) return; // 回放/即時交界去重
      const nearBottom = this.isNearBottom();
      this.appendMessage(m);
      this.bumpMemberActivity(m);
      this.refreshTasksIfRelevant(m);
      this.settleViewport(m, nearBottom);
    },
    appendMessage(m) {
      this.lastId = m.id;
      this.messages.push(m);
    },
    bumpMemberActivity(m) {
      const member = this.members.find((x) => x.name === m.from);
      if (member) { member.lastSeen = m.ts; member.messageCount++; }
      else this.loadMembers(); // 新面孔:重撈完整統計
    },
    refreshTasksIfRelevant(m) {
      if (!m.task_id && !m.reply_to) return;
      this.tasks.load().then(() => this.syncTaskModal());
    },
    settleViewport(m, nearBottom) {
      if (nearBottom && !document.hidden) {
        this.scrollToBottom();
        this.unread.markRead(this.lastId);
      } else {
        this.newBelow += nearBottom ? 0 : 1;
        this.unread.unseen++;
        this.unread.syncTitle();
      }
    },

    /* ── 資料載入 ── */
    async loadRooms() {
      try { this.rooms = (await this.api.rooms()).rooms; } catch (e) { /* request 已記錄 */ }
    },
    async loadAgents() {
      try { this.agents = (await this.api.agents()).agents; } catch (e) { /* request 已記錄 */ }
    },
    async loadMembers() {
      try { this.members = (await this.api.members(this.room)).members; } catch (e) { /* 同上 */ }
    },

    /* ── 發言 ── */
    async send(text) {
      const from = this.myName;
      localStorage.setItem("a2a-name", from);
      localStorage.setItem("a2a-token", this.token);
      const res = await this.api.send(this.room, { from, text, reply_to: this.replyTo || undefined },
                                      rt.authEnabled ? this.token : "");
      if (!res.ok) {
        const detail = res.data.detail || res.data.error || `HTTP ${res.status}`;
        this.showToast(`>> SEND FAILED: ${detail}`, false);
        return; // 失敗保留草稿與 replyTo(訊息不能無聲消失)
      }
      this.$refs.composer.clear();
      this.replyTo = null;
    },
    /** 發任務:走協定門。這是使用者主動操作,失敗一定要吵(與背景輪詢的靜默策略相反)。 */
    async sendTask({ text, target, deadlineSeconds }) {
      const sender = this.myName;
      localStorage.setItem("a2a-name", sender);
      const res = await this.api.sendTask(target, {
        room: this.room, text, sender, deadlineSeconds,
        token: this.authOn ? this.token : null });
      if (!res.ok) {
        const err = res.data.error || res.data;
        this.showToast(`>> 任務發送失敗:${err.message || err.detail || "unknown"}`, false);
        return;  // 保留草稿
      }
      this.$refs.composer.clear();
      this.tasks.load();
      this.showToast(`>> 任務已交辦給 ${target}`, true);
    },
    setReply(id) { this.replyTo = id; this.$refs.composer.focusBox(); },

    /* ── 捲動與導覽 ── */
    isNearBottom() {
      const el = this.$refs.list;
      return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    },
    onScroll() {
      const el = this.$refs.list;
      if (el.scrollHeight - el.scrollTop - el.clientHeight < 40) {
        this.newBelow = 0;
        this.unread.markRead(this.lastId);
      }
      if (el.scrollTop < 60 && this.hasMore && !this.loadingOlder) this.loadOlder();
    },
    async loadOlder() {
      this.loadingOlder = true;
      const el = this.$refs.list;
      const prevH = el.scrollHeight;
      const oldest = this.messages[0] ? this.messages[0].id : 0;
      try {
        const data = await this.api.messages(this.room, `before_id=${oldest}&tail=${PAGE}`);
        if (data.messages.length) {
          this.messages = [...data.messages, ...this.messages];
          await this.$nextTick();
          el.scrollTop += el.scrollHeight - prevH; // 補在上方,維持視覺位置
        }
        this.hasMore = data.messages.length === PAGE;
      } catch (e) { /* request 已記錄 */ }
      this.loadingOlder = false;
    },
    jumpTo(id) {
      const el = document.getElementById("msg-" + id);
      if (!el) return;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("flash");
      void el.offsetWidth; // 重置動畫
      el.classList.add("flash");
      setTimeout(() => el.classList.remove("flash"), FLASH_MS);
    },
    /** 彈窗內跳轉:先取值再關彈窗(原寫法先清 modal 再讀 modal,必炸)。 */
    jumpFromModal(mid) {
      const id = mid;
      this.modal = null;
      this.$nextTick(() => this.jumpTo(id));
    },
    jumpBottom() {
      this.scrollToBottom();
      this.newBelow = 0;
      this.unread.markRead(this.lastId);
    },
    scrollToBottom() {
      this.$nextTick(() => { const el = this.$refs.list; if (el) el.scrollTop = el.scrollHeight; });
    },

    /* ── 剪貼簿與錨點 ── */
    async copyText(text) {
      try { await navigator.clipboard.writeText(text); this.showToast(">> COPIED", true); }
      catch (e) { this.showToast(">> COPY FAILED", false); }
    },
    async anchorLink(id) {
      history.replaceState(null, "", "#msg-" + id);
      try {
        await navigator.clipboard.writeText(location.href);
        this.showToast(">> LINK COPIED", true);
      } catch (e) { this.showToast(">> ANCHOR SET", true); }
    },

    /* ── 偏好 ── */
    switchRoom(room) { location.href = `?room=${encodeURIComponent(room)}`; },
    /** 切鏡頭:點誰 = 以誰為視角、點當前主角 = 交回自己。
        具名視角是臨時檢視,不持久化 —— 重開一律回到「以自己為主角」。 */
    setFocus(target) {
      this.focusTarget = target;
      if (this.modal && this.modal.type === "member") this.modal = null;
    },
    /** 字級調整:delta ±2 步進、0 = 回預設 20;夾在 14~28 之間。
        改的是基準值,header/徽章/控制項全跟著等比縮放(styles.css 的 --ui-* 級距)。 */
    adjustFont(delta) {
      this.bubbleFont = delta === 0 ? 20 : Math.min(28, Math.max(14, this.bubbleFont + delta));
      localStorage.setItem("a2a-font", String(this.bubbleFont));
      this.applyFont();
    },
    applyFont() {
      document.documentElement.style.setProperty("--bubble-font", this.bubbleFont + "px");
    },

    /* ── 彈窗 ── */
    async openMember(name) {
      await this.loadMembers();
      const member = this.members.find((m) => m.name === name)
        || { name, messageCount: 0, mentionedCount: 0, firstSeen: null, lastSeen: null };
      this.modal = { type: "member", member };
    },
    openAgentCard(name) { // window 不進 data,免被 reactive 整包代理
      window.open(`/agents/${name}/.well-known/agent-card.json`);
    },
    async openTask(taskId) {
      await this.tasks.load();
      const summary = this.tasks.map[taskId];
      if (!summary) { this.showToast(">> TASK NOT FOUND(重啟後 in-flight task 會蒸發)", false); return; }
      this.modal = { type: "task", summary, full: null };
      this.fetchTaskFull(summary);
    },
    async fetchTaskFull(summary) {
      try {
        const resp = await this.api.rpc(summary.target, "GetTask", { id: summary.id });
        if (this.modal && this.modal.type === "task" && this.modal.summary.id === summary.id) {
          this.modal.full = resp.result || null;
        }
      } catch (e) { /* request 已記錄;彈窗維持 LOADING 字樣 */ }
    },
    /** task 彈窗開著時跟 SSE 即時同步。 */
    syncTaskModal() {
      if (!this.modal || this.modal.type !== "task") return;
      const cur = this.tasks.map[this.modal.summary.id];
      if (cur && cur.state !== this.modal.summary.state) {
        this.modal.summary = cur;
        this.fetchTaskFull(cur);
      }
    },

    /* ── toast ── */
    showToast(text, ok) {
      this.toast = text;
      this.toastOk = !!ok;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => (this.toast = null), TOAST_MS);
    },
  },
}).mount("#app");
