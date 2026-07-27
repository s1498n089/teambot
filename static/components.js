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

   讀法提示:每個零件都有固定的幾個欄位 ——
     props    別人傳給我的資料(我只能讀,不能改)
     emits    我會往上送的事件名稱
     data     我自己的狀態
     computed 從上面幾樣算出來的值(資料變了會自動重算)
     methods  我會做的動作
     template 我長什麼樣子
   ═══════════════════════════════════════════════════════════════════════ */


/* ───────────────────────────────────────────────────────────────────────
   HoloModal — 彈出視窗的外框
   ─────────────────────────────────────────────────────────────────────── */

/* 只負責「外框」:半透明背景、四個角、關閉鈕。裡面要放什麼由使用它的人決定
   (那就是 slot 的用途)—— 這樣成員視窗與任務視窗可以共用同一個外框。 */
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


/* ───────────────────────────────────────────────────────────────────────
   ChatHeader — 最上面那一條
   ─────────────────────────────────────────────────────────────────────── */

// 連線狀態 → 顯示的字。獨立成一張表,是因為將來多一種狀態只要加一列。
const STATUS_TEXT = {
  connecting: "SERVER…",
  online: "SERVER",
  reconnecting: "SERVER ✕",
};

const ChatHeader = {
  props: ["room", "rooms", "status", "activeTasks", "onlineMembers",
          "focusTarget", "focusName"],
  emits: ["switch-room", "open-member", "set-focus", "adjust-font"],

  data: function () {
    // 樣板裡要用到這個常數,而樣板只看得到元件自己的資料,所以帶進來
    return { FOCUS_ME: FOCUS_ME };
  },

  computed: {
    /**
     * 那顆燈旁邊的字。
     * 主詞刻意寫成 SERVER:它講的是「這個頁面跟伺服器的連線」,
     * 不是「某個成員在不在線上」—— 兩件事混淆過一次,所以寫明。
     */
    statusText: function () {
      return STATUS_TEXT[this.status];
    },
  },

  methods: {
    avatarOf: avatarOf,
    colorHexOf: colorHexOf,

    /**
     * 點某人的頭像 → 把「鏡頭」交給他(畫面改以他為第一人稱);
     * 再點同一個人 → 交還給自己。
     * @param {string} memberName 被點到的人
     */
    toggleFocus: function (memberName) {
      if (this.focusTarget === memberName) {
        this.$emit("set-focus", FOCUS_ME);
      } else {
        this.$emit("set-focus", memberName);
      }
    },

    /** 這個人是不是目前的鏡頭主角。 */
    isFocusedMember: function (memberName) {
      return memberName === this.focusName;
    },

    /** 主角的頭像要用他自己的顏色描邊,其他人不要。 */
    borderStyleFor: function (memberName) {
      if (this.isFocusedMember(memberName)) {
        return { borderColor: colorHexOf(memberName) };
      }
      return {};
    },
  },

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
      <img v-for="member in onlineMembers" :key="member.name" :src="avatarOf(member.name)"
           :title="'以 ' + member.name + ' 的視角檢視(再點一次交回自己)'"
           :class="{ focused: isFocusedMember(member.name) }"
           :style="borderStyleFor(member.name)"
           @click="toggleFocus(member.name)">
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


/* ───────────────────────────────────────────────────────────────────────
   MdInline — 畫出一串行內元素
   ─────────────────────────────────────────────────────────────────────── */

/* 把 md.js 產出的行內元素畫出來。因為 md.js 吐的是「平的」陣列 ——
   粗體與斜體只是元素上的開關,不是巢狀結構 —— 所以這裡一個迴圈就畫得完,
   不需要遞迴呼叫自己。

   每一種都用 {{ }} 插值,Vue 會自動把特殊符號轉成純文字,
   這就是「別人發的訊息不可能變成可執行的東西」的原因。

   ⚠️ 下面那行樣板**故意擠成一行,不要好心把它換行縮排**:
   這些都是行內元素,標籤之間只要有換行或空白,瀏覽器就會把它算成一個空格,
   於是「粗體」跟後面的標點之間會冒出多餘的空隙。 */
const MdInline = {
  props: ["tokens"],
  template: `<template v-for="(token, index) in tokens" :key="index"><span v-if="token.type === 'mention'" class="mention" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</span><a v-else-if="token.type === 'link'" :href="token.href" target="_blank" rel="noopener noreferrer" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</a><code v-else-if="token.type === 'code'" class="md-code-inline" :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</code><span v-else :class="{ 'md-b': token.bold, 'md-i': token.italic }">{{ token.text }}</span></template>`,
};


/* ───────────────────────────────────────────────────────────────────────
   MessageItem — 一則訊息
   ─────────────────────────────────────────────────────────────────────── */

const MessageItem = {
  components: { "md-inline": MdInline },
  props: ["m", "grouped", "me", "focusName", "taskInfo"],
  emits: ["reply", "jump", "open-member", "open-task", "copy", "anchor"],

  data: function () {
    // 每則訊息各自記住自己的顯示模式,互不影響。
    // 預設 false = 先看原文(發現某則是 Markdown 時再切過去)。
    return { showMarkdown: false };
  },

  computed: {
    /**
     * 這則要不要靠右顯示。
     * 靠右的是「鏡頭主角」而不是固定的自己 —— 把鏡頭交給別人時,
     * 整面牆會改以他的視角呈現。
     */
    isFocused: function () {
      if (!this.focusName) {
        return false;
      }
      return this.m.from === this.focusName;
    },

    /**
     * Markdown 模式:把訊息切成一塊一塊(段落、標題、清單、表格…)。
     * 這裡明確讀一次 rt.mentionPattern,是為了讓 Vue 知道「這個結果跟
     * @某人 的規則有關」—— 伺服器換設定時才會重新算。
     */
    blocks: function () {
      return parseMarkdownBlocks(this.m.text, rt.mentionPattern);
    },

    /**
     * 原文模式:不解析任何 Markdown 語法,只認 @某人 與網址
     * —— 也就是還沒有 Markdown 功能之前,泡泡本來的樣子。
     */
    rawTokens: function () {
      return parseTextOnly(this.m.text, rt.mentionPattern);
    },

    /** 發話者的顏色,用來畫泡泡左邊那條線。 */
    senderColor: function () {
      return colorHexOf(this.m.from);
    },

    /** 這個名字有沒有正式註冊過(沒有的話名牌會加註記)。 */
    registered: function () {
      return this.m.from in rt.palette;
    },

    /**
     * 這則訊息是 AI 說的還是人類說的?
     *
     * 判準是「在不在伺服器的成員名冊上」,不是另外標記的欄位 ——
     * 因為那份名冊同時決定了「能不能被派任務」,兩者用同一份資料才不會打架。
     * 人類接不了任務,所以名冊外的一律顯示成 HUMAN。
     */
    isAgent: function () {
      return rt.agentNames.indexOf(this.m.from) !== -1;
    },

    /**
     * 這則有沒有點到「我」。
     * 綠色永遠只代表「跟我有關」,不會因為把鏡頭交給別人而失效 ——
     * 這條規則是刻意的:警示色一旦有兩種意思就不可靠了。
     */
    pingMe: function () {
      const mentions = this.m.mentions || [];
      return mentions.includes(this.me);
    },

    /**
     * 這則有沒有點到「鏡頭主角」(而且主角不是我)。
     * 用主角自己的顏色標記,不搶綠色 —— 兩個資訊同時出現才不會打架。
     */
    pingFocus: function () {
      if (!this.focusName) {
        return false;
      }
      if (this.focusName === this.me) {
        return false;
      }
      const mentions = this.m.mentions || [];
      return mentions.includes(this.focusName);
    },

    /** 主角的顏色;沒有主角時給透明(等於不畫)。 */
    focusColor: function () {
      if (this.focusName) {
        return colorHexOf(this.focusName);
      }
      return "transparent";
    },

    /** 任務徽章要長什麼樣。查不到任務資訊時會拿到「已消失」的樣式。 */
    badge: function () {
      if (this.taskInfo) {
        return stateMeta(this.taskInfo.state);
      }
      return stateMeta(null);
    },

    /** 任務徽章上的字。任務資訊還在就寫 TASK,查不到就加一個記號。 */
    badgeText: function () {
      if (this.taskInfo) {
        return "TASK";
      }
      return "TASK⌀";
    },

    /** 滑鼠移到任務徽章上的說明。 */
    badgeTitle: function () {
      if (this.taskInfo) {
        return this.taskInfo.state;
      }
      return "EVAPORATED(伺服器重開之前的任務,狀態已經沒了)";
    },

    /** 切換鈕上的字:顯示的是「按下去會變成什麼」。 */
    toggleLabel: function () {
      if (this.showMarkdown) {
        return "RAW";
      }
      return "MD";
    },

    /** 切換鈕的說明文字。 */
    toggleTitle: function () {
      if (this.showMarkdown) {
        return "切回原文";
      }
      return "以 Markdown 排版顯示";
    },
  },

  methods: {
    avatarOf: avatarOf,
    fmtTime: fmtTime,
    fmtFull: fmtFull,

    /** 切換這一則要看原文還是看排版。 */
    toggleMarkdown: function () {
      this.showMarkdown = !this.showMarkdown;
    },
  },

  template: `
  <div class="msg" :id="'msg-' + m.id"
       :class="{ grouped: grouped, own: isFocused, 'ping-me': pingMe, 'ping-focus': pingFocus }"
       :style="{ '--sender': senderColor, '--focus-color': focusColor }">

    <div class="gutter">
      <img v-if="!grouped" class="avatar" :src="avatarOf(m.from)" :style="{ borderColor: senderColor }"
           :title="m.from" @click="$emit('open-member', m.from)">
    </div>

    <div class="msg-body">
      <!-- 名字那一列。連續發言的第二則以後(grouped)不重複顯示 -->
      <div v-if="!grouped" class="msg-head mono">
        <span class="pill" :class="{ unreg: !registered }"
              :style="{ color: senderColor, borderColor: senderColor }">{{ m.from.toUpperCase() }}</span>

        <!-- 種類徽章:一眼看出這句話是 AI 說的還是人類說的。
             人類接不了 A2A 任務,所以這個區分在派任務時很實際。 -->
        <span class="kind-tag" :class="isAgent ? 'kind-ai' : 'kind-human'"
              :title="isAgent ? '在成員名冊上,可以被派任務' : '不在成員名冊上,不能被派任務'"
        >{{ isAgent ? 'AI' : 'HUMAN' }}</span>

        <!-- 名字既不在名冊、也不是已知的人類預設名時,額外提醒一句 -->
        <span v-if="!registered && !isAgent" class="unreg-tag">UNREGISTERED</span>

        <span v-if="m.task_id" class="task-badge" :class="badge.cls"
              :title="badgeTitle" @click="$emit('open-task', m.task_id)">{{ badgeText }}</span>

        <span v-if="m.reply_to" class="reply-chip" @click="$emit('jump', m.reply_to)">&gt;&gt; #{{ m.reply_to }}</span>

        <span class="time" :title="fmtFull(m.ts)" @click="$emit('anchor', m.id)">#{{ m.id }} · {{ fmtTime(m.ts) }}</span>

        <span class="head-spacer"></span>

        <button class="hover-btn mono" @click="toggleMarkdown" :title="toggleTitle">⇄ {{ toggleLabel }}</button>
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


/* ───────────────────────────────────────────────────────────────────────
   ChatComposer — 最下面的輸入區
   ─────────────────────────────────────────────────────────────────────── */

const ChatComposer = {
  props: ["name", "room", "replyTo", "authEnabled", "token", "targets"],
  emits: ["update:name", "update:token", "send", "send-task", "cancel-reply"],

  data: function () {
    return {
      draft: "",
      mode: "msg",        // "msg" 走聊天、"task" 走 A2A 協定,是兩道不同的門
      target: "",         // 任務要交辦給誰。刻意**不**從訊息文字裡的 @ 自動帶入 ——
                          // 交辦對象是協定欄位、@ 是社交語法,兩件事不該黏在一起
      deadline: DEFAULT_DEADLINE_SECONDS,
    };
  },

  computed: {
    /** 現在是不是「派任務」模式。 */
    isTask: function () {
      return this.mode === "task";
    },

    /** 名字輸入框的寬度,跟著字數走 —— 名字長也不會被截掉。 */
    nameWidth: function () {
      const currentName = this.name || "";
      const charCount = Math.max(3, currentName.length + 1);
      return charCount + "ch";
    },

    /** 現在能不能送出。派任務時還必須先選好對象。 */
    canFire: function () {
      const hasText = this.draft.trim() !== "";

      if (!hasText) {
        return false;
      }
      if (this.isTask && !this.target) {
        return false;
      }
      return true;
    },

    /** 送出鈕上的字。 */
    sendLabel: function () {
      if (this.isTask) {
        return "SEND TASK";
      }
      return "SEND";
    },

    /** 輸入框裡的提示字。 */
    placeholder: function () {
      if (this.isTask) {
        return "任務內容,Enter 送出(Shift+Enter 換行)";
      }
      return "輸入訊息,Enter 送出(Shift+Enter 換行)";
    },
  },

  methods: {
    /** 按下送出(或按 Enter)。依照目前模式決定要走哪一道門。 */
    fire: function () {
      const text = this.draft.trim();

      if (text === "") {
        return;
      }

      if (!this.isTask) {
        this.$emit("send", text);
        return;
      }

      // 沒選對象就不送。送出鈕本來就會變灰,這裡是第二道保險。
      if (!this.target) {
        return;
      }

      let deadlineSeconds = Number(this.deadline);
      if (!deadlineSeconds) {
        deadlineSeconds = DEFAULT_DEADLINE_SECONDS;
      }

      this.$emit("send-task", {
        text: text,
        target: this.target,
        deadlineSeconds: deadlineSeconds,
      });
    },

    /** 清空草稿。由上層在「確定送出成功」之後才呼叫 —— 送失敗要留著讓人重試。 */
    clear: function () {
      this.draft = "";
    },

    /** 讓游標回到輸入框。 */
    focusBox: function () {
      this.$refs.box.focus();
    },
  },

  template: `
  <footer>
    <div v-if="replyTo" class="reply-bar mono">RE #{{ replyTo }}
      <span class="x" @click="$emit('cancel-reply')">✕</span><span class="hint">[ESC]</span>
    </div>

    <!-- 只有派任務模式才出現的那一列:交給誰、多久算逾時 -->
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
      <!-- 身分區:上面一列是「我是誰、在哪個房間」,下面一列是「這則要走哪道門」 -->
      <span class="identity">
        <span class="prompt mono">
          <input class="name" :value="name" :style="{ width: nameWidth }"
                 @input="$emit('update:name', $event.target.value)">@{{ room }} &gt;_
        </span>

        <span class="mode-ctl">
          <button class="mono" :class="{ on: !isTask }" @click="mode = 'msg'">MSG</button>
          <button class="mono" :class="{ on: isTask }" @click="mode = 'task'">TASK</button>
        </span>
      </span>

      <input v-if="authEnabled" class="token mono" type="password" :value="token"
             placeholder="token" title="伺服器已啟用認證:發言需要你的 token"
             @input="$emit('update:token', $event.target.value)">

      <textarea ref="box" v-model="draft" :placeholder="placeholder"
                @keydown.enter.exact.prevent="fire"
                @keydown.esc="$emit('cancel-reply')"></textarea>

      <button class="send" :class="{ task: isTask }" :disabled="!canFire" @click="fire">{{ sendLabel }}</button>
    </div>
  </footer>`,
};
