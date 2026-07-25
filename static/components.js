/* ═══════════════════════════════════════════════════════════════════════════
   components.js — 畫面上的五個零件

   每一個零件負責畫面的一塊,彼此不互相呼叫,只透過「上面傳下來的資料」
   跟「往上送的事件」溝通:

     HoloModal     — 彈出視窗的外框
     ChatHeader    — 最上面那一條(房間、在線的人、字級鈕)
     MdInline      — 一串文字元素(粗體、連結、@某人…)
     MessageItem   — 一則訊息(頭像、名字、泡泡)
     ChatComposer  — 最下面的輸入區

   這些零件會用到 util.js 的工具與 md.js 的解析結果,所以那兩個檔案要先載入。
   ═══════════════════════════════════════════════════════════════════════ */

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

/** 畫出一串「行內元素」(md.js 產出的那種)。
    因為 md.js 吐的是平的陣列 —— 粗體/斜體只是元素上的開關,不是巢狀結構 ——
    所以這裡一個迴圈就畫得完,不需要遞迴呼叫自己。
    每一種都用 {{ }} 插值,Vue 會自動把特殊符號轉成純文字(這就是不會被注入的原因)。 */
const MdInline = {
  props: ["tokens"],
  template: `<template v-for="(token, index) in tokens" :key="index"><span v-if="token.type === 'mention'" class="mention" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</span><a v-else-if="token.type === 'link'" :href="token.href" target="_blank" rel="noopener noreferrer" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</a><code v-else-if="token.type === 'code'" class="md-code-inline" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</code><span v-else :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</span></template>`,
};

const MessageItem = {
  components: { "md-inline": MdInline },
  props: ["m", "grouped", "me", "focusName", "taskInfo"],
  emits: ["reply", "jump", "open-member", "open-task", "copy", "anchor"],
  data: function () {
    // 每則訊息各自記住自己的顯示模式,互不影響。
    // 預設 false = 先看原文(老闆的要求:發現某則是 markdown 時再切過去)。
    return { showMarkdown: false };
  },
  computed: {
    /** 靠右的是「鏡頭主角」而非固定的自己 — focusName 由 root 解析(ME/具名/null)。 */
    isFocused() { return !!this.focusName && this.m.from === this.focusName; },
    /** Markdown 模式:把訊息切成一塊一塊(段落、標題、清單、表格…)。
        這裡明確讀一次 rt.mentionPattern,是為了讓 Vue 知道「這個計算結果跟
        mention 規則有關」—— 伺服器換設定時才會重新算。 */
    blocks() {
      return parseMarkdownBlocks(this.m.text, rt.mentionPattern);
    },

    /** 原文模式:不解析任何 markdown 語法,只認 @某人 與網址
        —— 也就是加 markdown 之前泡泡本來的樣子。換行交給 CSS 的 pre-wrap。 */
    rawTokens() {
      return parseTextOnly(this.m.text, rt.mentionPattern);
    },
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
        <button class="hover-btn mono" @click="showMarkdown = !showMarkdown"
                :title="showMarkdown ? '切回原文' : '以 Markdown 排版顯示'">⇄ {{ showMarkdown ? 'RAW' : 'MD' }}</button>
        <button class="hover-btn mono" @click="$emit('copy', m.text)">⧉ COPY</button>
        <button class="hover-btn mono" @click="$emit('reply', m.id)">⟲ REPLY</button>
      </div>
      <!-- 原文模式:整則訊息就是一段文字,換行交給 CSS 的 pre-wrap -->
      <div v-if="!showMarkdown" class="bubble chamfer-sm bubble-raw">
        <md-inline :tokens="rawTokens"></md-inline>
      </div>

      <!-- Markdown 模式:md.js 切好的塊,一塊一塊畫出來 -->
      <div v-else class="bubble chamfer-sm">
        <template v-for="(block, blockIndex) in blocks" :key="blockIndex">

          <!-- 程式碼區塊。這一行刻意不換行縮排,否則多出來的空白會被顯示出來 -->
          <pre v-if="block.type === 'code'" class="md-code"><code>{{ block.text }}</code></pre>

          <!-- 標題。level 是 1 到 6,對應 md-h1 ~ md-h6 -->
          <div v-else-if="block.type === 'heading'" class="md-h" :class="'md-h' + block.level">
            <md-inline :tokens="block.content"></md-inline>
          </div>

          <!-- 表格。外面包一層,是為了讓太寬的表格自己橫向捲動而不撐爆泡泡 -->
          <div v-else-if="block.type === 'table'" class="md-table-wrap">
            <table class="md-table">
              <thead>
                <tr>
                  <th v-for="(cell, cellIndex) in block.header" :key="cellIndex">
                    <md-inline :tokens="cell"></md-inline>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
                  <td v-for="(cell, cellIndex) in row" :key="cellIndex">
                    <md-inline :tokens="cell"></md-inline>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 有序清單(1. 2. 3.) -->
          <ol v-else-if="block.type === 'list' && block.ordered" class="md-list">
            <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
              <md-inline :tokens="item"></md-inline>
            </li>
          </ol>

          <!-- 無序清單(- 或 *)。跟上面幾乎一樣,但寫成兩段比用一行動態決定標籤名好讀 -->
          <ul v-else-if="block.type === 'list'" class="md-list">
            <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
              <md-inline :tokens="item"></md-inline>
            </li>
          </ul>

          <!-- 引用。每一行之間插一個換行 -->
          <blockquote v-else-if="block.type === 'quote'" class="md-quote">
            <template v-for="(line, lineIndex) in block.lines" :key="lineIndex">
              <br v-if="lineIndex > 0">
              <md-inline :tokens="line"></md-inline>
            </template>
          </blockquote>

          <!-- 普通段落。段落裡的單一換行也是換行 -->
          <p v-else class="md-p">
            <template v-for="(line, lineIndex) in block.lines" :key="lineIndex">
              <br v-if="lineIndex > 0">
              <md-inline :tokens="line"></md-inline>
            </template>
          </p>

        </template>
      </div>
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
