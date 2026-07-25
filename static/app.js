/* ═══════════════════════════════════════════════════════════════════════════
   app.js — 共用狀態與整體編排

   這是最後載入、也是真正把畫面跑起來的檔案。它做三件事:

   1. 放「大家都要用到的東西」:設定值、伺服器下發的執行期設定
   2. 幾個把複雜狀態包起來的小工具(任務清單、未讀數、連線)
   3. 最外層的元件:把上面那些接起來,決定什麼時候要做什麼

   其他檔案(md / util / api / components / particles)都不認識這裡的東西;
   反過來這裡會用到它們 —— 所以它排在載入順序的最後。

   寫法約定同其他檔案:一行一件事、不用箭頭簡寫與解構、名字寫完整。
   ═══════════════════════════════════════════════════════════════════════ */


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

/* 鏡頭(FOCUS):決定「這面牆以誰為第一人稱」——靠右的那位。
   預設 FOCUS_ME:跟著名字欄動(你報什麼身分,就以誰為主角);點頭像把鏡頭
   交給別人(以他人視角回顧),再點一次交回自己。沒有「關閉」狀態 ——
   鏡頭永遠有主角,少一個狀態少一份心智負擔。 */
const FOCUS_ME = "@me";

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
