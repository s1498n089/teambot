/* ═══════════════════════════════════════════════════════════════════════════
   app.js — 共用狀態與整體編排

   這是最後載入、也是真正把畫面跑起來的檔案。它做三件事:

   1. 放「大家都要用到的東西」:設定值、伺服器下發的執行期設定
   2. 幾個把複雜狀態包起來的小工具(任務清單、未讀數、連線)
   3. 最外層的元件:把上面那些接起來,決定什麼時候要做什麼

   其他檔案(md / particles / util / api / components)都不認識這裡的東西;
   反過來這裡會用到它們 —— 所以它排在載入順序的最後。

   寫法約定同其他檔案:一行一件事、不用箭頭簡寫與解構、名字寫完整。
   ═══════════════════════════════════════════════════════════════════════ */

const createApp = Vue.createApp;
const reactive = Vue.reactive;
const computed = Vue.computed;


/* ───────────────────────────────────────────────────────────────────────
   第一部分:設定值

   所有「為什麼是這個數字」的答案都集中在這裡,不散落在程式碼中間。
   ─────────────────────────────────────────────────────────────────────── */

const PAGE = 100;                      // 一次載入幾則訊息(開頁與往上滑都用這個)
const GROUP_WINDOW_MS = 300000;        // 5 分鐘內同一個人連續發言,就不重複顯示名字
const ACTIVE_WINDOW_MS = 600000;       // 10 分鐘內講過話 → 算「活躍」,否則算「待命」
const PRESENCE_POLL_MS = 15000;        // 多久更新一次在線名單(很輕,只拿名字)
const TOAST_MS = 3500;                 // 提示訊息顯示多久
const FLASH_MS = 2000;                 // 跳到某則訊息時,那則閃爍多久
const API_FAIL_TOAST_THRESHOLD = 3;    // 連續失敗幾次才跳出來吵使用者
const DEFAULT_DEADLINE_SECONDS = 300;  // 派任務的預設逾時(跟伺服器同步)

/* 伺服器開機時下發的設定。用 reactive 包起來,是為了讓畫面上依賴它的地方
   在設定送到時自動重算 —— 例如 @某人 的規則換了,所有訊息要重新解析一次。
   這裡先放一份預設值,萬一拿不到設定也不會整個壞掉。 */
const rt = reactive({
  mentionPattern: "(?<![A-Za-z0-9_@.-])(@[\\w一-鿿-]+)",
  palette: { user: "#c9d1d9" },        // 人類的預設顏色;agent 的顏色由伺服器指定
  authEnabled: false,                  // 伺服器有沒有開認證(有的話畫面要多一個 token 欄)

  /* 成員名冊(伺服器給的),用來分辨「這個名字是 AI 還是人類」。

     ★ 為什麼不直接看 palette?因為 palette 裡塞了 user 的預設顏色 ——
       那是「畫面用的顏色表」,不是「誰是 AI 的名單」。混用會把人類算成 AI。

     判準本身很單純:【在名冊上 = AI,不在名冊 = 人類】。
     這不是我們自己發明的規則,而是伺服器早就在用的那一條 ——
     派任務給名冊外的名字會被協定層當場擋掉(unknown agent)。
     所以畫面上的標記與「能不能接任務」永遠一致,
     不可能出現「看起來是 AI 卻派不了任務」這種矛盾。 */
  agentNames: [],
});

/* api.js 需要「跟使用者說話」的能力,但它在畫面建好之前就先造好了。
   所以先給它這個空殼,等畫面準備好再把真正的函式塞進來。 */
const toastBus = { show: null };

/* 「鏡頭」決定這面牆以誰為第一人稱 —— 靠右的那位就是主角。
   預設是自己;點別人的頭像就把鏡頭交給他(用他的視角回顧對話),
   再點一次交回來。刻意沒有「關閉」狀態:鏡頭永遠有主角,
   少一個狀態就少一份要記的事。 */
const FOCUS_ME = "@me";

/* 取名時的兩條規則。放在這裡是因為它們是「規則」不是「狀態」——
   規則不會變,狀態會。

   NAME_PATTERN 與伺服器的名字白名單對齊(中英文、數字、- 和 _)。

   RESERVED_NAMES 對齊伺服器的 a2a.py:RESERVED_NAMES,但【故意少一個 user】:
   後端擋 user 是為了不讓 agent 註冊走人類的名字;而前端這裡問的對象就是人類,
   user 又是人類的預設名 —— 選它等於「不特別取名」,當然要允許。

   ★ poller 已經退役了,名字仍然保留(兩邊都是):
     退役的基礎設施名字被人拿去用,只會讓將來考古的人更困惑。 */
const NAME_PATTERN = /^[\w\u4e00-\u9fff-]+$/;
const RESERVED_NAMES = ["admin", "system", "hub", "server", "poller"];


/* ───────────────────────────────────────────────────────────────────────
   第二部分:把複雜狀態包起來的小工具

   這三個函式各自回傳一包「資料 + 操作那份資料的方法」。
   這樣做的好處是:主元件不必知道細節,只要說「載入任務」「標記已讀」。
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 任務清單。訊息上的徽章、標題列的計數、任務視窗都讀這一份。
 * @param {object} api 跟伺服器溝通的那包函式
 * @param {string} room 房間名
 * @returns {object} 任務狀態
 */
function useTasks(api, room) {
  const tasks = reactive({
    map: {},                           // 任務 id → 任務資料

    // 還沒結束的任務有幾個(標題列那個數字)
    active: computed(function () {
      const all = Object.values(tasks.map);
      let count = 0;

      for (const task of all) {
        if (task.state === "TASK_STATE_SUBMITTED" || task.state === "TASK_STATE_WORKING") {
          count = count + 1;
        }
      }
      return count;
    }),

    async load() {
      try {
        const data = await api.tasks(room);
        const nextMap = {};

        for (const task of data.tasks) {
          nextMap[task.id] = task;
        }
        tasks.map = nextMap;
      } catch (error) {
        // api.js 已經記錄過了。這裡不動 map,徽章就維持上一次的樣子
      }
    },
  });

  return tasks;
}

/**
 * 未讀狀態:那條「── NEW ──」分隔線畫在哪、瀏覽器分頁標題要不要加數字。
 * 已讀的位置存在瀏覽器裡,所以關掉再打開還記得上次看到哪。
 * @param {string} room 房間名(不同房間各記各的)
 * @returns {object} 未讀狀態
 */
function useUnread(room) {
  const storageKey = "a2a-read-" + room;
  const savedValue = localStorage.getItem(storageKey) || "0";

  const unread = reactive({
    afterId: parseInt(savedValue, 10),   // 上次離開時看到哪一則
    unseen: 0,                           // 這次還沒看到幾則

    markRead(lastId) {
      localStorage.setItem(storageKey, String(lastId));
      unread.unseen = 0;
      unread.syncTitle();
    },

    syncTitle() {
      if (unread.unseen > 0) {
        document.title = `(${unread.unseen}) A2A Chatroom`;
      } else {
        document.title = "A2A Chatroom";
      }
    },
  });

  return unread;
}

/**
 * 跟伺服器的即時連線(SSE)。伺服器有新訊息就會主動推過來,不必一直去問。
 *
 * 包成這樣是為了讓主元件只需要說「有訊息時做這個、斷線時做那個」,
 * 不必碰 EventSource 的細節。
 *
 * @param {object} options { url, onMessage, onOpen, onError }
 * @returns {object} { connect, close }
 */
function useStream(options) {
  const url = options.url;
  const onMessage = options.onMessage;
  const onOpen = options.onOpen;
  const onError = options.onError;

  let eventSource = null;

  return {
    connect() {
      eventSource = new EventSource(url);
      eventSource.onopen = onOpen;
      eventSource.onerror = onError;

      eventSource.onmessage = function (event) {
        onMessage(JSON.parse(event.data));
      };
    },

    close() {
      if (eventSource) {
        eventSource.close();
      }
    },
  };
}


/* ───────────────────────────────────────────────────────────────────────
   第三部分:最外層的元件

   它本身幾乎不做事,只負責「把零件接起來」與「決定什麼時候做什麼」。
   真正的畫面在 components.js,真正的溝通在 api.js。
   ─────────────────────────────────────────────────────────────────────── */

createApp({
  components: {
    "holo-modal": HoloModal,
    "chat-header": ChatHeader,
    "message-item": MessageItem,
    "chat-composer": ChatComposer,
  },

  setup() {
    // 這裡是「東西被造出來的地方」:先看網址要進哪個房間,再造出溝通窗口與那幾包狀態
    const params = new URLSearchParams(location.search);
    const room = params.get("room") || "main";

    function notifyUser(message, ok) {
      if (toastBus.show) {
        toastBus.show(message, ok);
      }
    }

    const api = createApi(notifyUser);

    return {
      room: room,
      api: api,
      tasks: useTasks(api, room),
      unread: useUnread(room),
    };
  },

  data() {
    /* 這個人以前取過名字嗎?
       ★ 要看 getItem 是不是 null,不能看「有沒有值」——
         因為沒取名時我們給的預設值就是 "user",兩者混在一起就分不出
         「他自己選了 user」和「他還沒被問過」。 */
    const hasChosenName = localStorage.getItem("a2a-name") !== null;
    const savedName = localStorage.getItem("a2a-name") || "user";
    const savedToken = localStorage.getItem("a2a-token") || "";
    const savedFont = localStorage.getItem("a2a-font") || "20";

    return {
      messages: [],
      members: [],          // 這個房間發言過的人
      agents: [],           // 有註冊的 agent —— 只有他們能被派任務
      present: [],          // 現在連著線的人(在場的事實來源)
      presenceSupported: true,  // 舊版伺服器沒有這個功能,遇到 404 就自動關掉
      rooms: [],
      lastId: 0,
      name: savedName,
      askingName: !hasChosenName,   // 第一次來的人,先問他叫什麼
      nameDraft: "",                // 取名框裡正在打的字
      nameError: "",                // 取名框的錯誤訊息(空字串 = 沒問題)
      nameWarning: "",              // 取名框的提醒(不擋人,再按一次就放行)
      nameChecking: false,          // 正在跟伺服器查撞名(避免連按)
        renameTimer: null,            // 改名後延遲重連的計時器(見 watch.myName)
      token: savedToken,    // 伺服器有開認證時,發言需要的個人憑證
      status: "connecting",
      replyTo: null,
      newBelow: 0,          // 下面有幾則新訊息還沒看到
      hasMore: false,       // 還有更舊的訊息可以載
      loadingOlder: false,
      toast: null,
      toastOk: false,
      modal: null,          // 目前開著的視窗:{ type: "member" | "task", ... }
      focusTarget: FOCUS_ME,
      bubbleFont: parseInt(savedFont, 10),  // 基準字級,整個介面依此等比縮放
      nowTick: Date.now(),  // 每分鐘更新一次,用來重算「誰還活躍」
    };
  },

  computed: {
    /** 我現在報什麼名字。空白就當作 user。 */
    myName() {
      const trimmed = this.name.trim();

      if (trimmed === "") {
        return "user";
      }
      return trimmed;
    },

    /** 鏡頭主角的實際名字。預設值代表「我自己」,其他就是那個人的名字。 */
    focusName() {
      if (this.focusTarget === FOCUS_ME) {
        return this.myName;
      }
      return this.focusTarget;
    },

    /** 伺服器有沒有開認證。包一層是為了讓畫面在設定送達時自動更新。 */
    authOn() {
      return rt.authEnabled;
    },

    /**
     * 可以派任務的對象:只有註冊過的 agent(協定層本來就擋沒註冊的),
     * 並標記他現在在不在 —— 派給沒人在的 agent 只會白等到逾時,
     * 這件事該在選之前就看得見。
     */
    taskTargets() {
      const targets = [];

      for (const agent of this.agents) {
        targets.push({
          name: agent.name,
          present: this.present.includes(agent.name),
        });
      }
      return targets;
    },

    /** 標題列上要顯示哪些人的頭像(最多 6 個)。 */
    onlineMembers() {
      // 伺服器不支援在線名單時的退路:回到「最近講過話 = 在線」的舊判斷
      if (!this.presenceSupported) {
        const recentlyActive = [];

        for (const member of this.members) {
          if (this.presenceOf(member) === "active") {
            recentlyActive.push(member);
          }
        }
        return recentlyActive.slice(0, 6);
      }

      const result = [];

      for (const name of this.present) {
        // 在場但這個房間還沒發過言的人,members 裡找不到 —— 至少把名字放進去
        const known = this.members.find(function (member) {
          return member.name === name;
        });

        if (known) {
          result.push(known);
        } else {
          result.push({ name: name });
        }
      }
      return result.slice(0, 6);
    },

    /**
     * 時間軸上要畫的每一列。除了訊息本身,還會插入兩種分隔線:
     * 日期換了插日期、第一則沒看過的訊息前面插「── NEW ──」。
     */
    rows() {
      const out = [];
      let previousDay = "";
      let previousMessage = null;
      let unreadLinePlaced = false;

      for (const message of this.messages) {
        const day = dayOf(message.ts);

        // 換日期了 → 插一條日期線,並且下一則一定要重新顯示名字
        if (day !== previousDay) {
          out.push({ type: "sep", key: "sep-" + day, date: day });
          previousDay = day;
          previousMessage = null;
        }

        // 第一則沒看過的訊息 → 插一條未讀線(只插一次)
        const isFirstUnread = !unreadLinePlaced
          && this.unread.afterId
          && message.id > this.unread.afterId;

        if (isFirstUnread) {
          out.push({ type: "unread", key: "unread" });
          unreadLinePlaced = true;
          previousMessage = null;
        }

        out.push({
          type: "msg",
          key: message.id,
          m: message,
          grouped: this.shouldGroupWithPrevious(message, previousMessage),
        });
        previousMessage = message;
      }
      return out;
    },
  },

  async mounted() {
    toastBus.show = this.showToast;   // api.js 的通知管道在這裡接上
    this.applyFont();                 // 套用記住的字級

    // 順序有意義:先拿設定(@某人 規則與顏色),再拿訊息,最後平行載入其他資料
    await this.loadConfig();
    await this.loadFirstPage();

    // 上次離開時已經看完了 → 不用畫未讀線
    if (this.unread.afterId >= this.lastId) {
      this.unread.afterId = 0;
    }

    await Promise.all([
      this.tasks.load(),
      this.loadMembers(),
      this.loadRooms(),
      this.loadPresence(),
      this.loadAgents(),
    ]);
    this.scrollToBottom();

    this.openStream();
    this.unread.markRead(this.lastId);
    this.startTimers();
    this.bindGlobalKeys();
    this.jumpToAnchorIfAny();
  },

  watch: {
    /**
     * 改了名字就重建即時連線 —— 否則伺服器的在場名單會一直記著舊名字。
     *
     * 為什麼要等一下才做:這個名字來自輸入框,使用者每按一個鍵都會觸發一次。
     * 不等的話「kevin」五個字會斷線重連五次,而每次重連伺服器都要重送一批訊息 ——
     * 打字打到一半畫面就開始卡。
     *
     * 800 毫秒是「打字停下來了」的常見門檻:比一般按鍵間隔長,
     * 又短到使用者不會覺得延遲。
     */
    myName(newName, oldName) {
      if (newName === oldName) {
        return;
      }
      clearTimeout(this.renameTimer);
      const self = this;
      this.renameTimer = setTimeout(function () {
        /* ★ 兩件事一起做,因為改名有【三個身分載體】,少一個就會出現怪現象:
             ① 發言身分 —— 發言時帶當下的名字,本來就會跟上
             ② 在線身分 —— 直播連線報上的名字,靠下面這行重建
             ③ 瀏覽器記憶 —— 下次打開時用哪個名字,靠這行存起來

           原本 ③ 只在「發言成功」與「取名框確認」時才寫入,所以
           單純改名字欄的人會遇到:改完當下正常,一重整又變回舊名字。 */
        localStorage.setItem("a2a-name", newName);
        self.openStream();
      }, 800);
    },
  },

  methods: {
    avatarOf: avatarOf,
    fmtFull: fmtFull,
    colorOf: colorHexOf,

    /* ── 開機流程(從 mounted 拆出來,讓 mounted 只剩一串看得懂的步驟)── */

    /** 拿伺服器設定。拿不到也沒關係,程式裡有一份預設值。 */
    async loadConfig() {
      try {
        const config = await this.api.config();
        rt.mentionPattern = config.mentionPattern;
        rt.authEnabled = !!config.authEnabled;

        const palette = { user: "#c9d1d9" };
        const agentNames = [];

        for (const name of Object.keys(config.agents)) {
          palette[name] = config.agents[name].color;
          agentNames.push(name);
        }
        rt.palette = palette;
        rt.agentNames = agentNames;
      } catch (error) {
        // 預設值已經在 rt 裡了,不做事就是正確的處理
      }
    },

    /** 開頁時載入最新的一批訊息。 */
    async loadFirstPage() {
      try {
        const data = await this.api.messages(this.room, `tail=${PAGE}`);
        this.messages = data.messages;
        this.lastId = data.last_id;
        this.hasMore = data.messages.length === PAGE;   // 剛好裝滿 → 上面應該還有
      } catch (error) {
        this.showToast(">> 初始載入失敗,請重整", false);
      }
    },

    /**
     * 開啟即時連線。斷線時 EventSource 會自己重連,我們只要改燈號。
     *
     * ★ 一開頭先關掉舊的那條:改名時會再呼叫這個函式一次,
     *   不關的話舊連線還掛在伺服器上,在場名單會同時看到新舊兩個名字。
     */
    openStream() {
      const self = this;

      if (this.stream) {
        this.stream.close();
      }

      /* watcher 帶的是【現在】的名字。這也是為什麼改名後必須重建連線 ——
         這個網址在連上的那一刻就固定了,之後改名它不會自己跟著變。 */
      const watcher = encodeURIComponent(this.myName);

      this.stream = useStream({
        url: `/api/rooms/${this.room}/stream?since_id=${this.lastId}&watcher=${watcher}`,

        onMessage: function (message) {
          self.handleIncoming(message);
        },
        onOpen: function () {
          self.status = "online";
        },
        onError: function () {
          self.status = "reconnecting";
        },
      });
      this.stream.connect();
    },

    /** 兩個定時器:一個讓「活躍/待命」會隨時間變化,一個更新在線名單。 */
    startTimers() {
      const self = this;

      setInterval(function () {
        self.nowTick = Date.now();
      }, 60000);

      setInterval(function () {
        self.loadPresence();
      }, PRESENCE_POLL_MS);
    },

    /** Esc 鍵:有視窗先關視窗,沒有的話取消「回覆某則」。 */
    bindGlobalKeys() {
      const self = this;

      addEventListener("keydown", function (event) {
        if (event.key !== "Escape") {
          return;
        }

        if (self.modal) {
          self.modal = null;
          return;
        }

        if (self.replyTo) {
          self.replyTo = null;
        }
      });

      // 切回這個分頁時,如果已經在最底下就順手標記為已讀
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden && self.isNearBottom()) {
          self.unread.markRead(self.lastId);
        }
      });
    },

    /** 網址帶著 #msg-123 進來 → 開頁後跳到那一則。 */
    jumpToAnchorIfAny() {
      if (!location.hash.startsWith("#msg-")) {
        return;
      }

      const id = parseInt(location.hash.slice(5), 10);

      if (!id) {
        return;
      }

      const self = this;
      this.$nextTick(function () {
        self.jumpTo(id);
      });
    },

    /* ── 給樣板用的小判斷 ── */

    stateCls(state) {
      return stateMeta(state).cls;
    },

    stateShort(state) {
      return stateMeta(state).short;
    },

    /** 這個名字是不是 AI?依據是伺服器給的成員名冊(見 rt.agentNames 的說明)。 */
    isAgent(name) {
      return rt.agentNames.indexOf(name) !== -1;
    },

    /**
     * 檢查取名框裡的名字能不能用。回傳錯誤訊息;空字串代表可以。
     *
     * ★ 這裡擋的都是「會造成混淆」的名字,不是權限控制 ——
     *   聊天室目前對所有人一視同仁。
     */
    validateName(raw) {
      const name = (raw || "").trim();

      if (!name) {
        return "要有個名字才能開始";
      }
      if (name.length > 32) {
        return "名字最多 32 個字";
      }
      /* 跟伺服器同一套規則:中英文、數字、- 和 _。
         為什麼一定要擋 @:訊息裡的 @某人 是點名語法,
         名字帶 @ 會讓點名解析認錯人。 */
      if (!NAME_PATTERN.test(name)) {
        return "只能用中英文、數字、- 和 _(不能有空白或 @)";
      }
      if (this.isAgent(name)) {
        return "「" + name + "」是 AI 成員的名字,換一個吧";
      }
      /* 系統保留字:被人拿去用會讓訊息看起來像系統發的。
         與伺服器的保留名單對齊,但【故意不含 user】——
         user 是人類的預設名,選它等於「不特別取名」,本來就該允許。 */
      if (RESERVED_NAMES.indexOf(name.toLowerCase()) !== -1) {
        return "「" + name + "」是系統保留的名字,換一個吧";
      }
      return "";
    },

    /**
     * 按下「就叫這個」:先檢查格式,再檢查有沒有跟別人撞名,都過了才存。
     *
     * ★ 撞名分成兩級,而且【故意】不做成同一種:
     *
     *     在線上有人用 → 硬擋。同時有兩個人叫同一個名字,訊息會分不清誰是誰。
     *     歷史上有人用 → 只提醒一次,再按一次就放行。
     *
     *   為什麼歷史不硬擋?因為人類沒有身分系統 —— 名字只存在各自的瀏覽器裡。
     *   硬擋的話,你清掉瀏覽器紀錄、或換一台電腦,就會【被自己用過的名字擋在門外】,
     *   而系統分不出「撞名的別人」與「回來的你自己」。
     *
     *   要真正保證名字唯一,得靠認證(AUTH=on):鑰匙綁名字,沒有你的鑰匙
     *   就不能用你的名字發言。這裡做的是防誤撞,不是防冒名。
     */
    async confirmName() {
      if (this.nameChecking) {
        return;                                  // 連按兩下時,第二下直接忽略
      }
      const name = this.nameDraft.trim();

      const error = this.validateName(name);
      if (error) {
        this.nameError = error;
        return;
      }

      this.nameChecking = true;
      try {
        const taken = await this.findNameConflict(name);
        if (taken === "online") {
          this.nameError = "「" + name + "」現在有人正在用,換一個吧";
          return;
        }
        if (taken === "history" && !this.nameWarning) {
          this.nameWarning = "之前有人用過「" + name + "」,訊息會混在一起。"
                           + "確定的話再按一次「就叫這個」。";
          return;                                // 第一次只提醒,不擋
        }
      } finally {
        this.nameChecking = false;
      }

      this.name = name;
      localStorage.setItem("a2a-name", this.name);
      this.askingName = false;
    },

    /**
     * 這個名字被佔用了嗎?回傳 "online" / "history" / ""(沒撞到)。
     *
     * 查不到就當作沒撞 —— 網路出問題不該把人卡在取名框前面進不來。
     */
    async findNameConflict(name) {
      try {
        const presence = await this.api.presence(this.room);
        const online = (presence && presence.present) || [];
        if (online.indexOf(name) !== -1) {
          return "online";
        }
      } catch (error) {
        return "";                               // 查不到就放行
      }
      try {
        const data = await this.api.members(this.room);
        const members = (data && data.members) || [];
        for (const member of members) {
          if (member.name === name) {
            return "history";
          }
        }
      } catch (error) {
        return "";
      }
      return "";
    },

    /** 取名框裡一改字,就把上一次的錯誤與提醒清掉(它們講的是舊名字)。 */
    onNameDraftInput() {
      this.nameError = "";
      this.nameWarning = "";
    },

    /** 按下「先跳過」:沿用預設的 user,但一樣記下來,不再問第二次。 */
    skipNaming() {
      localStorage.setItem("a2a-name", this.name);
      this.askingName = false;
    },

    /**
     * 這一則要不要跟上一則併在一起(不重複顯示名字與頭像)。
     * 條件:同一個人、時間夠近、而且不是任務或引用訊息 ——
     * 那兩種需要完整的表頭才看得懂。
     */
    shouldGroupWithPrevious(message, previousMessage) {
      if (!previousMessage) {
        return false;
      }
      if (previousMessage.from !== message.from) {
        return false;
      }
      if (message.task_id || message.reply_to) {
        return false;
      }

      const gap = new Date(message.ts) - new Date(previousMessage.ts);
      return gap < GROUP_WINDOW_MS;
    },

    /**
     * 某個人現在算什麼狀態,三種:
     *   active  在線而且最近講過話
     *   standby 在線但安靜(agent 待命中就是這樣)
     *   offline 沒有連線
     *
     * 「在線」看的是有沒有連線(瀏覽器開著,或 agent 的 bell 掛著),
     * **不是**用「最近有沒有發言」去猜 —— 安靜待命的 agent 也還在。
     *
     * @param {object|string} member 成員資料或單純一個名字
     * @returns {string} "active" | "standby" | "offline"
     */
    presenceOf(member) {
      let name = member;
      let lastSeenAt = 0;

      if (typeof member === "object") {
        name = member.name;

        if (member.lastSeen) {
          lastSeenAt = new Date(member.lastSeen).getTime();
        }
      }

      const spokeRecently = (this.nowTick - lastSeenAt) < ACTIVE_WINDOW_MS;

      // 伺服器不支援在線名單 → 退回舊語意:只分「最近講過話」與「沒有」
      if (!this.presenceSupported) {
        if (spokeRecently) {
          return "active";
        }
        return "offline";
      }

      if (!this.present.includes(name)) {
        return "offline";
      }

      if (spokeRecently) {
        return "active";
      }
      return "standby";
    },

    /** 狀態 → 顯示的字。 */
    presenceLabel(member) {
      const state = this.presenceOf(member);

      if (state === "active") {
        return "● ACTIVE";
      }
      if (state === "standby") {
        return "◐ STANDBY";
      }
      return "○ OFFLINE";
    },

    /* ── 資料載入 ── */

    /**
     * 更新在線名單。
     * 如果伺服器比前端舊、根本沒有這個功能(404),就永久關掉這個輪詢 ——
     * 每 15 秒去敲一個不存在的門只會洗版 console。
     */
    async loadPresence() {
      if (!this.presenceSupported) {
        return;
      }

      try {
        const data = await this.api.presence(this.room);
        this.present = data.present;
      } catch (error) {
        if (error && error.status === 404) {
          this.presenceSupported = false;
          this.present = [];
          console.warn("[presence] 伺服器尚未支援在線名單,改用發言時間推測");
        }
      }
    },

    async loadRooms() {
      try {
        const data = await this.api.rooms();
        this.rooms = data.rooms;
      } catch (error) {
        // api.js 已經記錄了
      }
    },

    async loadAgents() {
      try {
        const data = await this.api.agents();
        this.agents = data.agents;
      } catch (error) {
        // 同上
      }
    },

    async loadMembers() {
      try {
        const data = await this.api.members(this.room);
        this.members = data.members;
      } catch (error) {
        // 同上
      }
    },

    /* ── 收到新訊息時要做的四件事 ── */

    /**
     * 即時連線推來一則訊息。拆成四個具名步驟,每一步只做一件事。
     * @param {object} message 新訊息
     */
    handleIncoming(message) {
      // 剛連線時伺服器會補送一段,可能跟已經有的重疊 —— 舊的直接丟掉
      if (message.id <= this.lastId) {
        return;
      }

      // 要在畫面變動**之前**判斷,不然捲軸位置已經被推走了
      const wasNearBottom = this.isNearBottom();

      this.appendMessage(message);
      this.bumpMemberActivity(message);
      this.refreshTasksIfRelevant(message);
      this.settleViewport(wasNearBottom);
    },

    appendMessage(message) {
      this.lastId = message.id;
      this.messages.push(message);
    },

    /** 更新這個人的「最後發言時間」與發言數;沒見過的人就重撈一次名單。 */
    bumpMemberActivity(message) {
      const member = this.members.find(function (candidate) {
        return candidate.name === message.from;
      });

      if (!member) {
        this.loadMembers();
        return;
      }

      member.lastSeen = message.ts;
      member.messageCount = member.messageCount + 1;
    },

    /** 這則跟任務有關的話,順便更新任務狀態(徽章與開著的任務視窗)。 */
    refreshTasksIfRelevant(message) {
      if (!message.task_id && !message.reply_to) {
        return;
      }

      const self = this;
      this.tasks.load().then(function () {
        self.syncTaskModal();
      });
    },

    /**
     * 決定畫面要不要跟著捲下去。
     * 原本就在最底下 → 跟著捲並標記已讀;
     * 在上面看舊訊息 → 不要打斷他,改成在下面顯示「有新訊息」。
     * @param {boolean} wasNearBottom 訊息進來之前,使用者是不是在最底下
     */
    settleViewport(wasNearBottom) {
      if (wasNearBottom && !document.hidden) {
        this.scrollToBottom();
        this.unread.markRead(this.lastId);
        return;
      }

      if (!wasNearBottom) {
        this.newBelow = this.newBelow + 1;
      }
      this.unread.unseen = this.unread.unseen + 1;
      this.unread.syncTitle();
    },

    /* ── 送出 ── */

    /**
     * 送一則聊天訊息。
     * 失敗時**保留草稿與引用對象** —— 打好的字不能無聲消失。
     */
    async send(text) {
      const from = this.myName;
      localStorage.setItem("a2a-name", from);
      localStorage.setItem("a2a-token", this.token);

      const body = { from: from, text: text };

      if (this.replyTo) {
        body.reply_to = this.replyTo;
      }

      let token = "";
      if (rt.authEnabled) {
        token = this.token;
      }

      const result = await this.api.send(this.room, body, token);

      if (!result.ok) {
        const detail = result.data.detail || result.data.error || `HTTP ${result.status}`;
        this.showToast(`>> SEND FAILED: ${detail}`, false);
        return;
      }

      this.$refs.composer.clear();
      this.replyTo = null;
    },

    /**
     * 派一個任務。這是使用者主動按下去的,所以**失敗一定要說** ——
     * 跟背景輪詢那種「安靜失敗」的策略剛好相反。
     * @param {object} payload { text, target, deadlineSeconds }
     */
    async sendTask(payload) {
      const sender = this.myName;
      localStorage.setItem("a2a-name", sender);

      let token = null;
      if (this.authOn) {
        token = this.token;
      }

      const result = await this.api.sendTask(payload.target, {
        room: this.room,
        text: payload.text,
        sender: sender,
        deadlineSeconds: payload.deadlineSeconds,
        token: token,
      });

      if (!result.ok) {
        const error = result.data.error || result.data;
        const reason = error.message || error.detail || "unknown";
        this.showToast(`>> 任務發送失敗:${reason}`, false);
        return;
      }

      this.$refs.composer.clear();
      this.tasks.load();
      this.showToast(`>> 任務已交辦給 ${payload.target}`, true);
    },

    /** 點「回覆」:記住要回哪一則,並把游標移回輸入框。 */
    setReply(id) {
      this.replyTo = id;
      this.$refs.composer.focusBox();
    },

    /* ── 捲動與跳轉 ── */

    /** 使用者是不是已經在最底下附近(120 像素內都算)。 */
    isNearBottom() {
      const list = this.$refs.list;
      const distanceToBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
      return distanceToBottom < 120;
    },

    /** 捲動時:到底了就清掉未讀,快到頂了就去載更舊的訊息。 */
    onScroll() {
      const list = this.$refs.list;
      const distanceToBottom = list.scrollHeight - list.scrollTop - list.clientHeight;

      if (distanceToBottom < 40) {
        this.newBelow = 0;
        this.unread.markRead(this.lastId);
      }

      if (list.scrollTop < 60 && this.hasMore && !this.loadingOlder) {
        this.loadOlder();
      }
    },

    /**
     * 往上載入更舊的訊息。
     * 補在最上面之後,要把捲軸往下推回原本看的位置 —— 不然畫面會突然跳走。
     */
    async loadOlder() {
      this.loadingOlder = true;

      const list = this.$refs.list;
      const heightBefore = list.scrollHeight;

      let oldestId = 0;
      if (this.messages[0]) {
        oldestId = this.messages[0].id;
      }

      try {
        const data = await this.api.messages(this.room, `before_id=${oldestId}&tail=${PAGE}`);

        if (data.messages.length) {
          this.messages = data.messages.concat(this.messages);
          await this.$nextTick();
          list.scrollTop = list.scrollTop + (list.scrollHeight - heightBefore);
        }
        this.hasMore = data.messages.length === PAGE;
      } catch (error) {
        // api.js 已經記錄了
      }

      this.loadingOlder = false;
    },

    /** 跳到某一則訊息並讓它閃一下。 */
    jumpTo(id) {
      const element = document.getElementById("msg-" + id);

      if (!element) {
        return;
      }

      element.scrollIntoView({ behavior: "smooth", block: "center" });

      // 先移除再加上 class 才能重播動畫;中間讀一次 offsetWidth 是為了
      // 強迫瀏覽器立刻套用「已移除」的狀態,否則兩步會被合併成沒有變化
      element.classList.remove("flash");
      void element.offsetWidth;
      element.classList.add("flash");

      setTimeout(function () {
        element.classList.remove("flash");
      }, FLASH_MS);
    },

    /**
     * 從視窗裡點連結跳到某則訊息。
     * 先把 id 記下來再關視窗 —— 反過來的話,視窗關掉後就讀不到 id 了。
     */
    jumpFromModal(messageId) {
      const id = messageId;
      this.modal = null;

      const self = this;
      this.$nextTick(function () {
        self.jumpTo(id);
      });
    },

    jumpBottom() {
      this.scrollToBottom();
      this.newBelow = 0;
      this.unread.markRead(this.lastId);
    },

    /** 捲到最底。等畫面更新完才捲,否則高度還是舊的。 */
    scrollToBottom() {
      const self = this;

      this.$nextTick(function () {
        const list = self.$refs.list;

        if (list) {
          list.scrollTop = list.scrollHeight;
        }
      });
    },

    /* ── 複製與連結 ── */

    async copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
        this.showToast(">> COPIED", true);
      } catch (error) {
        this.showToast(">> COPY FAILED", false);
      }
    },

    /** 把網址改成指向這一則,並複製到剪貼簿 —— 可以貼給別人直接看到那一則。 */
    async anchorLink(id) {
      history.replaceState(null, "", "#msg-" + id);

      try {
        await navigator.clipboard.writeText(location.href);
        this.showToast(">> LINK COPIED", true);
      } catch (error) {
        // 複製失敗沒關係,網址已經改好了,使用者可以自己複製
        this.showToast(">> ANCHOR SET", true);
      }
    },

    /* ── 偏好設定 ── */

    switchRoom(room) {
      location.href = `?room=${encodeURIComponent(room)}`;
    },

    /**
     * 切換鏡頭。點誰就以誰的視角看這面牆,再點一次交回自己。
     * 具名視角只是臨時檢視,**不記住** —— 重開一律回到以自己為主角。
     */
    setFocus(target) {
      this.focusTarget = target;

      if (this.modal && this.modal.type === "member") {
        this.modal = null;
      }
    },

    /**
     * 調整字級。delta 為 0 代表「回到預設」,否則就是加減。
     * 改的是基準值,標題、徽章、控制項全都會跟著等比縮放
     * (在 styles.css 裡是用 calc 相對這個值算的)。
     * @param {number} delta -2 縮小、+2 放大、0 回預設
     */
    adjustFont(delta) {
      if (delta === 0) {
        this.bubbleFont = 20;
      } else {
        const next = this.bubbleFont + delta;
        this.bubbleFont = Math.min(28, Math.max(14, next));   // 夾在 14~28 之間
      }

      localStorage.setItem("a2a-font", String(this.bubbleFont));
      this.applyFont();
    },

    applyFont() {
      document.documentElement.style.setProperty("--bubble-font", this.bubbleFont + "px");
    },

    /* ── 彈出視窗 ── */

    /** 開某個人的資料卡。先重撈一次名單,數字才是最新的。 */
    async openMember(name) {
      await this.loadMembers();

      let member = this.members.find(function (candidate) {
        return candidate.name === name;
      });

      // 在場但這個房間還沒發過言的人,名單裡找不到 —— 給一張空白卡
      if (!member) {
        member = {
          name: name,
          messageCount: 0,
          mentionedCount: 0,
          firstSeen: null,
          lastSeen: null,
        };
      }

      this.modal = { type: "member", member: member };
    },

    /** 開 agent 的名片(A2A 協定規定的那份 JSON)。 */
    openAgentCard(name) {
      window.open(`/agents/${name}/.well-known/agent-card.json`);
    },

    /** 開任務視窗:先用手上的摘要立刻顯示,詳細內容再慢慢抓。 */
    async openTask(taskId) {
      await this.tasks.load();

      const summary = this.tasks.map[taskId];

      if (!summary) {
        this.showToast(">> TASK NOT FOUND(伺服器重開之前的任務會消失)", false);
        return;
      }

      this.modal = { type: "task", summary: summary, full: null };
      this.fetchTaskFull(summary);
    },

    /** 抓任務的完整歷程。抓回來時使用者可能已經關掉或換了一個任務,所以要再確認一次。 */
    async fetchTaskFull(summary) {
      try {
        const response = await this.api.rpc(summary.target, "GetTask", { id: summary.id });

        const stillShowingSameTask = this.modal
          && this.modal.type === "task"
          && this.modal.summary.id === summary.id;

        if (stillShowingSameTask) {
          this.modal.full = response.result || null;
        }
      } catch (error) {
        // 視窗會維持在「載入中」的字樣
      }
    },

    /** 任務視窗開著的時候,讓它跟著即時訊息一起更新。 */
    syncTaskModal() {
      if (!this.modal || this.modal.type !== "task") {
        return;
      }

      const current = this.tasks.map[this.modal.summary.id];

      if (current && current.state !== this.modal.summary.state) {
        this.modal.summary = current;
        this.fetchTaskFull(current);
      }
    },

    /* ── 提示訊息 ── */

    /**
     * 在畫面上方顯示一則提示,幾秒後自動消失。
     * @param {string} text 要顯示的字
     * @param {boolean} ok 是好消息還是壞消息(影響顏色)
     */
    showToast(text, ok) {
      const self = this;

      this.toast = text;
      this.toastOk = !!ok;

      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(function () {
        self.toast = null;
      }, TOAST_MS);
    },
  },
}).mount("#app");
