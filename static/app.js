/* A2A Chatroom — 前端邏輯層。
   分層(review #152 共識):
     ChatApi(Repository)   — HTTP 唯一出入口 + 統一錯誤策略
     composables            — useTasks / useUnread / useStream(SRP,root 瘦身)
     元件                    — HoloModal(彈窗皮)/ ChatHeader / MessageItem / ChatComposer
     root                    — 只做編排;SSE 進訊息走具名 handler 鏈
   零建置:純 CDN Vue 3,無打包器。 */

const { createApp, reactive, computed } = Vue;

/* ═══════════ 常數(魔數集中地)═══════════ */
const PAGE = 100;                    // 初載與懶載的每頁訊息數
const GROUP_WINDOW_MS = 300000;      // 同人連續發言的 grouping 視窗(5 分鐘)
const ONLINE_WINDOW_MS = 600000;     // lastSeen 在此窗內視為在線(10 分鐘)
const TOAST_MS = 3500;
const FLASH_MS = 2000;               // 跳轉脈衝動畫的 class 存留時間
const API_FAIL_TOAST_THRESHOLD = 3;  // 連續失敗達此數才吵使用者(#152 must-3)

/* 執行期設定:開機從 /api/config 灌入 — reactive 讓 tokens/顏色 computed 真正依賴它
   (#152 must-2:regex 熱替換必須觸發重算,不能靠呼叫順序保命)。 */
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
function tokenize(text) {
  const out = [];
  for (const chunk of text.split(URL_RE)) {
    if (!chunk) continue;
    if (/^https?:\/\//.test(chunk)) {
      const m = chunk.match(URL_TRAIL_RE);
      out.push({ t: "link", v: m[1] });
      if (m[2]) out.push({ t: "text", v: m[2] });
      continue;
    }
    for (const p of chunk.split(mentionRegex())) {
      if (!p) continue;
      out.push({ t: p.startsWith("@") && p.length > 1 ? "mention" : "text", v: p });
    }
  }
  return out;
}

function fmtTime(ts) { return new Date(ts).toLocaleTimeString("en-GB", { hour12: false }); }
function fmtFull(ts) { return new Date(ts).toLocaleString("en-GB", { hour12: false }); }
function dayOf(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* task 狀態的單一事實來源(#152 should-4):CSS class 與縮寫都查這張表,
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

/* ═══════════ ChatApi(Repository:HTTP 唯一出入口)═══════════ */

function createApi(notify) {
  let failStreak = 0;

  /** 通用 GET/RPC:失敗 console.warn,連續失敗才 toast(#152 must-3 誠實原則)。 */
  async function request(url, options) {
    try {
      const res = await fetch(url, options);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      failStreak = 0;
      return await res.json();
    } catch (err) {
      console.warn("[api]", url, err.message || err);
      if (++failStreak >= API_FAIL_TOAST_THRESHOLD) notify(`>> API 連續失敗:${url}`, false);
      throw err;
    }
  }

  return {
    config: () => request("/api/config"),
    rooms: () => request("/api/rooms"),
    members: (room) => request(`/api/rooms/${room}/members`),
    tasks: (room) => request(`/api/rooms/${room}/tasks`),
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
    rpc: (agent, method, params) => request(`/agents/${agent}/a2a`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    }),
  };
}

/* ═══════════ Composables(#152 should-2)═══════════ */

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
    if (W < 560) target = Math.floor(target / 2); // 手機減半;resize 時重判(#152 nice-5)
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

/** Holographic 彈窗的皮(#152 should-3):overlay/四角/close 只寫一次,內容走 slot。 */
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
  props: ["room", "rooms", "status", "activeTasks", "msgCount", "lastId", "onlineMembers", "focusMode"],
  emits: ["switch-room", "toggle-focus", "open-member", "adjust-font"],
  computed: {
    statusText() { return { connecting: "CONNECTING", online: "ONLINE", reconnecting: "RECONNECT" }[this.status]; },
  },
  methods: { avatarOf },
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
      <img v-for="m in onlineMembers" :key="m.name" :src="avatarOf(m.name)" :title="m.name"
           @click="$emit('open-member', m.name)">
    </span>
    <span class="spacer"></span>
    <span class="font-ctl" title="聊天字級(老闆 #262)">
      <button class="mono" @click="$emit('adjust-font', -2)">A-</button>
      <button class="mono" @click="$emit('adjust-font', 0)">A</button>
      <button class="mono" @click="$emit('adjust-font', 2)">A+</button>
    </span>
    <button class="focus-toggle mono" :class="{ on: focusMode }" @click="$emit('toggle-focus')">FOCUS</button>
    <span class="meta mono" :class="{ 'task-counter': activeTasks, zero: !activeTasks }">TASKS:{{ activeTasks }}</span>
    <span class="meta mono msg-count">MSG:{{ msgCount }} LAST:#{{ lastId }}</span>
  </header>`,
};

const MessageItem = {
  props: ["m", "grouped", "me", "focusMode", "taskInfo"],
  emits: ["reply", "jump", "open-member", "open-task", "copy", "anchor"],
  computed: {
    isOwn() { return this.m.from === this.me; },
    /** tokenize 內部讀 rt.mentionPattern(reactive)→ config 熱替換會觸發重算(#152 must-2) */
    tokens() { return tokenize(this.m.text); },
    senderColor() { return colorHexOf(this.m.from); },
    registered() { return this.m.from in rt.palette; },
    pingMe() { return (this.m.mentions || []).includes(this.me); },
    badge() { return stateMeta(this.taskInfo ? this.taskInfo.state : null); },
  },
  methods: { avatarOf, fmtTime, fmtFull },
  template: `
  <div class="msg" :id="'msg-' + m.id"
       :class="{ grouped, own: focusMode && isOwn, 'ping-me': pingMe }"
       :style="{ '--sender': senderColor }">
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
      <div class="bubble chamfer-sm"><template v-for="(p, i) in tokens" :key="i"><span v-if="p.t === 'mention'" class="mention">{{ p.v }}</span><a v-else-if="p.t === 'link'" :href="p.v" target="_blank" rel="noopener noreferrer">{{ p.v }}</a><span v-else>{{ p.v }}</span></template></div>
    </div>
  </div>`,
};

const ChatComposer = {
  props: ["name", "room", "replyTo", "authEnabled", "token"],
  emits: ["update:name", "update:token", "send", "cancel-reply"],
  data() { return { draft: "" }; },
  computed: {
    nameWidth() { return Math.max(3, (this.name || "").length + 1) + "ch"; }, // 名字欄自適應不截斷
  },
  methods: {
    fire() { const t = this.draft.trim(); if (t) this.$emit("send", t); },
    clear() { this.draft = ""; },       // 父層在「送出成功」後才呼叫 — 失敗保留草稿
    focusBox() { this.$refs.box.focus(); },
  },
  template: `
  <footer>
    <div v-if="replyTo" class="reply-bar mono">RE #{{ replyTo }}
      <span class="x" @click="$emit('cancel-reply')">✕</span><span class="hint">[ESC]</span>
    </div>
    <div class="input-row">
      <span class="prompt mono">
        <input class="name" :value="name" :style="{ width: nameWidth }"
               @input="$emit('update:name', $event.target.value)">@{{ room }} &gt;_
      </span>
      <input v-if="authEnabled" class="token mono" type="password" :value="token"
             placeholder="token" title="AUTH 已啟用:發言需要你的 bearer token"
             @input="$emit('update:token', $event.target.value)">
      <textarea ref="box" v-model="draft" placeholder="輸入訊息,Enter 送出(Shift+Enter 換行)"
                @keydown.enter.exact.prevent="fire"
                @keydown.esc="$emit('cancel-reply')"></textarea>
      <button class="send" @click="fire">SEND</button>
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
      focusMode: localStorage.getItem("a2a-focus") !== "0", // 老闆拍板:預設開(自己靠右)
      bubbleFont: parseInt(localStorage.getItem("a2a-font") || "18", 10), // 聊天字級 px(老闆 #262),A-/A/A+ 調整
      nowTick: Date.now(),  // 每分鐘跳動,驅動在線狀態的重新計算
    };
  },
  computed: {
    myName() { return this.name.trim() || "user"; },
    authOn() { return rt.authEnabled; },  // 模板需要 reactive 依賴,包一層 computed
    onlineMembers() { return this.members.filter((m) => this.isOnline(m)).slice(0, 6); },
    /** timeline 的顯示列:日期分隔線 + ── NEW ── 未讀線 + 訊息(含 grouping 判定)。 */
    rows() {
      const out = [];
      let prevDay = "", prevMsg = null, unreadPlaced = false; // 旗標取代 out.some(#152 nice-3)
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
    this.applyFont(); // 開機套用記憶的字級(老闆 #262)

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
    await Promise.all([this.tasks.load(), this.loadMembers(), this.loadRooms()]);
    this.scrollToBottom();

    this.stream = useStream({
      url: `/api/rooms/${this.room}/stream?since_id=${this.lastId}`,
      onMessage: (m) => this.handleIncoming(m),
      onOpen: () => (this.status = "online"),
      onError: () => (this.status = "reconnecting"),
    });
    this.stream.connect();

    this.unread.markRead(this.lastId);
    setInterval(() => (this.nowTick = Date.now()), 60000);
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
    isOnline(member) {
      return member.messageCount > 0 && member.lastSeen
        && this.nowTick - new Date(member.lastSeen).getTime() < ONLINE_WINDOW_MS;
    },

    /* ── SSE 進訊息:具名 handler 鏈(#152 should-2,一步一責)── */
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
        return; // 失敗保留草稿與 replyTo(#123 實彈的教訓)
      }
      this.$refs.composer.clear();
      this.replyTo = null;
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
    /** 彈窗內跳轉:先取值再關彈窗(#152 must-1 — 原寫法先清 modal 再讀 modal,必炸)。 */
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
    toggleFocus() {
      this.focusMode = !this.focusMode;
      localStorage.setItem("a2a-focus", this.focusMode ? "1" : "0");
    },
    /** 字級調整(老闆 #262):delta ±2 步進、0 = 回預設 18;夾在 14~26 之間。 */
    adjustFont(delta) {
      this.bubbleFont = delta === 0 ? 18 : Math.min(26, Math.max(14, this.bubbleFont + delta));
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
    openAgentCard(name) { // window 不進 data,免被 reactive 整包代理(#152 nice-2)
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
    /** task 彈窗開著時跟 SSE 即時同步(alice v5 驗收回饋)。 */
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
