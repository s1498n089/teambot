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

/* 一次載入幾則訊息 —— 開頁與往上滑共用這一個。

   ★ 共用不只是「剛好都是 50」的巧合,它還消滅了一整類 bug:
     判斷「上面還有更舊的嗎」靠的是「這次有沒有【剛好裝滿】」,
     也就是拿回傳則數去比這個數字。如果開頁與往上滑各有一個常數,
     那個比較就有比錯的可能 —— 而比錯的下場是使用者往上滑再也載不到東西,
     且完全不會報錯。共用一個,就沒有比錯的餘地。

   (這裡短暫拆成過 FIRST_PAGE=50 / PAGE=100 兩個。將來如果真的想讓
    「開頁少一點、往上滑多一點」,再拆開即可 —— 但拆的同時要記得
    hasMore 的比較對象必須跟著那一次實際要的則數走。) */
const PAGE = 50;
const GROUP_WINDOW_MS = 300000;        // 5 分鐘內同一個人連續發言,就不重複顯示名字
const PRESENCE_POLL_MS = 15000;        // 多久更新一次在線名單(很輕,只拿名字)
const TOAST_MS = 3500;                 // 提示訊息顯示多久
const FLASH_MS = 2000;                 // 跳到某則訊息時,那則閃爍多久
const API_FAIL_TOAST_THRESHOLD = 3;    // 連續失敗幾次才跳出來吵使用者
const DEFAULT_DEADLINE_SECONDS = 300;  // 派任務的預設逾時(跟伺服器同步)
const AVATAR_SLOTS = 6;                // 標題列最多擺幾張頭像
const NAME_MAX_LENGTH = 32;            // 名字最長幾個字 —— 對齊伺服器 SENDER_RE 的 {1,32}
const FONT_DEFAULT = 20;               // 聊天基準字級,整個介面依此等比縮放
const FONT_MIN = 14;
const FONT_MAX = 28;

/* ★ 為什麼有些數字【沒有】搬上來:這一區收的是【政策】,不是【參數】。

   政策 = 產品決定,而且通常在別的地方也有一份對應物:
     NAME_MAX_LENGTH 對齊伺服器的白名單、字級三數與 styles.css 的 calc 綁在一起、
     AVATAR_SLOTS 是使用者感覺得到的行為。

   參數 = 某一段程式的局部手感,離開使用它的那幾行就沒有意義:
     isNearBottom 的 120 像素、onScroll 的 40 與 60 —— 這三個是同一個
     「捲動手感」的三個旋鈕,要一起調。搬上來反而切斷它們與使用處的關係,
     讀的人得在兩個地方之間來回才知道自己在調什麼。

   集中是為了讓「為什麼是這個數字」有地方回答,不是為了讓上面這一區變長。 */

/* 伺服器開機時下發的設定。用 reactive 包起來,是為了讓畫面上依賴它的地方
   在設定送到時自動重算 —— 例如 @某人 的規則換了,所有訊息要重新解析一次。
   這裡先放一份預設值,萬一拿不到設定也不會整個壞掉。 */
const rt = reactive({
  mentionPattern: "(?<![A-Za-z0-9_@.-])(@[\\w一-鿿-]+)",
  /* 名字 → 指定顏色。★ 現在只剩這一筆,而且【不要清掉】——
     "user" 是取名框上線前的人類預設名,聊天室裡有 138 則歷史訊息靠它維持灰色。
     完整說明在 util.js 的 colorHexOf(那裡是這條規則的正本)。

     那個 #c9d1d9 就是 styles.css 以前的 --human。這次搬家把定義端刪掉了,
     值留在這裡 —— 所以這行註解就是它的新家。 */
  palette: { user: "#c9d1d9" },

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

   ★ poller 沒有對應的行程,名字仍然保留(兩邊都是):
     基礎設施的代稱被人拿去當自己的名字用,只會製造混淆。
   ★ all 也擋掉:`@all` 是「點名所有人」的廣播名字 ——
     有人取這個名字,他就獨佔了那個字。 */
const NAME_PATTERN = /^[\w\u4e00-\u9fff-]+$/;
const RESERVED_NAMES = ["admin", "system", "hub", "server", "poller", "all"];


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
    /* ★ 名字【不從 localStorage 讀】—— 每次進來都要重選。
       身分的生命週期從此跟「這一次進房」一致,而那修掉了一個舊怪象:
       名字本來可以隨時改,但【已經發出去的訊息不會跟著改】——
       同一個人在歷史裡有兩個名字,而 @點名 只認得其中一個。

       字級仍然記著:它是【設定】不是【身分】,
       每次重整都要重設會很煩,而記錯了也不會讓歷史分裂。 */
    const savedFont = localStorage.getItem("a2a-font") || String(FONT_DEFAULT);

    /* ★ 「換房」跟「進場」是兩件事,而這一行是它們的分界。

       換房會重新載入頁面(見 goToRoom 的理由),而原本的流程把重載後的那一次
       當成【全新進場】—— 於是又問一次名字、又查一次撞名,
       然後查到自己上一秒那條還沒斷乾淨的連線,把自己擋在門外。

       所以換房前留一張條子在 sessionStorage 上,重載後拿到條子的就直接進去。

       ★★ 為什麼是 sessionStorage 而不是網址或 localStorage:
         sessionStorage  只活在【這一個分頁】,關掉就沒了 —— 跟「換房」的範圍完全一致
         網址            會被分享、被加書籤,別人點開就繞過取名框了
         localStorage    跨分頁共用,開新分頁會被誤認成換房

       ★★★ 條子【看一次就撕掉】:重新整理不該再被當成換房,
         那是真的重新進場(使用者可能想換個名字)。 */
    const switchingAs = sessionStorage.getItem("a2a-switching-as") || "";
    sessionStorage.removeItem("a2a-switching-as");

    return {
      messages: [],
      members: [],          // 這個房間發言過的人
      agents: [],           // 現在連著線的 agent —— 只有他們能被派任務
      present: [],          // 現在連著線的人(在場的事實來源)
      /* 這個房間的【成員】名字:有 cursor 的人。跟 present 是兩份資料,不要混 ——
         成員是持久的(關掉視窗還在),在場是連線(關掉就沒了)。
         邀請按鈕靠這份判斷「他進來了沒」,理由見 api.js 的 cursors()。 */
      roomMembers: [],
      rooms: [],
      lastId: 0,
      name: switchingAs,            // 進場前沒有身分 —— modal 填完才有(換房例外,見上面那張條子)
      askingName: !switchingAs,     // ★ 每次進來都問,不看瀏覽器記得什麼;只有換房不問
      roomDraft: "",                // 建新房間時輸入的名字
      roomError: "",                // 房間那一區的錯誤訊息
      pendingRoom: "",              // 選好的房間(還沒進場)
      armingDelete: "",             // 刪除鍵按過一次的那個房間(見 askDeleteRoom)
      /* 取名框裡正在打的字。★ 從網址預填(見 goToRoom):
         換房間會重新載入頁面,而「換個房間就要重打一次名字」很煩。
         把名字放網址而不是 storage 有兩個好處:狀態看得見(可分享、可清掉),
         而且不新增任何儲存機制 —— 房間本來就是靠網址傳的,順路而已。
         ★ 預填【不等於自動進場】:框還是會跳出來,還是要按一下確認。 */
      nameDraft: new URLSearchParams(location.search).get("name") || "",
      nameError: "",                // 取名框的錯誤訊息(空字串 = 沒問題)
      nameWarning: "",              // 取名框的提醒(不擋人,再按一次就放行)
      nameChecking: false,          // 正在跟伺服器查撞名(避免連按)
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
    };

    /* ★ 約定:「把手」類的東西【不進 data】—— 直接掛在 this 上就好。

       這裡指的是計時器 id、EventSource 物件這種【不需要驅動畫面】的東西
       (this.stream、this.toastTimer)。
       放進 data 會讓 Vue 替它們建立一整套響應式追蹤,而那份追蹤永遠不會被用到 ——
       它們不會出現在任何樣板裡。

       也不要用底線開頭:Vue 自己用 _ 與 $ 當內部命名空間,自訂屬性帶底線有撞名風險。

       (這條規矩是被一個反例逼出來的:曾經有第三個把手 renameTimer 放在 data 裡,
        三個一樣的東西兩種寫法,下一個要加把手的人不知道該學誰。
        那個計時器隨著「行內改名」一起退役了,但規矩留著。) */
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

    /**
     * 可以派任務的對象 = 現在連著線的 agent。
     *
     * ★ 這份清單會隨著 agent 上下線變動 —— 伺服器只回「此刻在線的」,
     *   所以要跟著在場名單一起定時重載(見 startTimers)。
     *
     *   沒有任何 agent 在線時這裡是空的 —— 那不是壞掉,
     *   那就是「現在房間裡只有人類」。畫面上要講清楚,不能只給一個空選單。
     */
    taskTargets() {
      const targets = [];

      for (const agent of this.agents) {
        targets.push({
          /* ★ present 目前【恆為 true】,而那不是 bug,是名冊動態化的必然:
             this.agents 來自 GET /agents,而伺服器那邊回的就是「此刻連著線的 agent」——
             所以能出現在這份清單裡的,必然也在 present 裡。

             名冊還寫死在程式裡的時候這個判斷有意義(名單上的人可以是離線的),
             動態化之後就沒有離線的成員了 —— 因為離線就不在名單上。

             留著不刪的理由:它是「畫面顯示的在線狀態」與「伺服器認定的在線」
             之間的接縫。哪天名冊改成含歷史成員(例如做「最近合作過的 agent」),
             ○ 就會重新出現,而這一行不必改。 */
          name: agent.name,
          present: this.present.includes(agent.name),
        });
      }
      return targets;
    },

    /**
     * 進場框裡「現在選中的是哪一間」—— **下拉、刪除鍵、進場按鈕共用這一個答案**。
     *
     * ★ 以前三處各自寫 `pendingRoom || room`,而 `room` 預設是 main。
     *   在一個【沒有 main 的 hub】上,那個值對不上任何一個 option,於是:
     *
     *       下拉      瀏覽器只好顯示第一個(lab)
     *       按鈕      照著 room 寫 ——「進 main」
     *       按下去    進一個不存在的房
     *
     *   **畫面顯示 lab、按鈕寫 main**,而兩者都不是使用者選的。
     *   收成一個 computed 之後,三處不可能再各說各話。
     *
     * ★★ fallback 退到清單第一個,不是退回 main:
     *   「預設值」的意義是「一個合理的起點」,而一個不存在的房間不是起點,是死路。
     */
    roomChoice() {
      if (this.pendingRoom) {
        return this.pendingRoom;              // 使用者選過了 —— 他說了算
      }
      const here = this.room;
      const listed = this.rooms.some(function (r) { return r.name === here; });
      if (listed) {
        return here;
      }
      // 清單還沒載回來時也會走到這裡 —— 那時退回 room 是對的(下一輪就有清單了)
      return this.rooms.length ? this.rooms[0].name : here;
    },

    /**
     * 可以邀請進這個房的人 = **在線的 agent** 裡,還不是本房成員的那些。
     *
     * 兩份資料相減,而且兩份都各自來:
     *
     *     this.agents       GET /agents        誰現在連著線(不分房)
     *     this.roomMembers  GET .../cursors    誰是這個房的成員
     *
     * ★ 不在後端做一個「可邀請的人」端點,是刻意的:那會把兩個獨立的事實
     *   焊成一個答案,而它們的更新節奏完全不同(在線每幾秒變,成員很少變)。
     *   相減這件事很便宜,焊死的代價卻要一直付。
     *
     * ★★ 只列 agent、不列人類:邀請的意思是「**讓對方的敲鈴器開始盯這個房**」,
     *   而人類沒有敲鈴器 —— 他要進來,自己開網頁選房就好,不需要別人替他建書籤。
     */
    invitableAgents() {
      const members = this.roomMembers;
      return this.agents
        .filter(function (agent) { return members.indexOf(agent.name) === -1; })
        .map(function (agent) { return agent.name; });
    },

    /** 標題列上要顯示哪些人的頭像(最多 6 個)。 */
    onlineMembers() {
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
      return result.slice(0, AVATAR_SLOTS);
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

  /**
   * 開機只做「殼」的部分 —— **不載任何房間的內容**。
   *
   * ★ 以前這裡直接把 main 的訊息撈下來,於是進場框後面永遠躺著一整串對話。
   *   那讓 main 事實上還是預設房(只是藏在毛玻璃後面),而使用者還沒選房 ——
   *   **畫面上顯示的東西,不該是使用者還沒做的選擇。**
   *
   * ★★ 留在這裡的兩樣是【進場框自己要用的】:
   *
   *       loadConfig   顏色與 @ 規則(整個介面的設定,不屬於任何房)
   *       loadRooms    「去哪一間?」那個下拉的內容 —— 沒有它進場框是空的
   *
   *   其餘全部搬進 enterRoom(),見那邊的清單。
   */
  async mounted() {
    toastBus.show = this.showToast;   // api.js 的通知管道在這裡接上
    this.applyFont();                 // 套用記住的字級

    await this.loadConfig();
    await this.loadRooms();

    // 綁定不載資料,兩種狀態都要有(Esc 關視窗、離開時的告別)
    this.bindGlobalKeys();
    this.bindFarewell();

    // ★ 還在問名字 = 還沒選房。換房回來的人身上有條子(見 data()),
    //   askingName 已經是 false,那條路直接進去,不必再按一次。
    if (!this.askingName) {
      await this.enterRoom();
    }
  },

  watch: {
    /**
     * 名字定下來就(重)掛即時連線 —— 直播連線要報上身分,伺服器的在場名單才對。
     *
     * ★ 這裡曾經有一個 800 毫秒的 debounce,因為名字來自一個【隨時可打字的輸入框】,
     *   每按一鍵都會觸發重連。那個輸入框已經拿掉了(身分在進場時定死),
     *   所以 debounce 也跟著走 —— 為某個機制而生的東西,那個機制沒了就該一起走,
     *   留下來就是沒人用的空殼。
     */
    myName(newName, oldName) {
      if (newName === oldName || !newName) {
        return;
      }
      this.openStream();
    },
  },

  methods: {
    avatarOf: avatarOf,
    colorOf: colorHexOf,

    /* ★ fmtFull 在 components.js 的 MessageItem 也掛了一份 —— 那【不是重複】:
         Vue 的 methods 不會跨元件繼承,誰的樣板要用就得自己掛。
         這一份是給 index.html 的根樣板用的(成員視窗與任務視窗的時間戳)。 */
    fmtFull: fmtFull,

    /**
     * 把 A2A 訊息的 parts 接成一串文字。
     *
     * 存在的理由是【樣板裡不寫運算】:這段原本寫成
     *   {{ h.parts.map(p => p.text).join('') }}
     * ——那是全專案唯一的箭頭函式,而每一支 JS 的檔頭都寫著不用箭頭簡寫。
     * 樣板裡留一個例外,下一個人就會寫第二個(「有前例」是最強的繁殖力)。
     *
     * @param {Array} parts A2A 的 parts 陣列
     * @returns {string} 接起來的文字
     */
    partsText(parts) {
      const list = parts || [];
      let out = "";

      for (const part of list) {
        if (part && part.text) {
          out = out + part.text;
        }
      }
      return out;
    },

    /* ── 開機流程(從 mounted 拆出來,讓 mounted 只剩一串看得懂的步驟)── */

    /** 拿伺服器設定。拿不到也沒關係,程式裡有一份預設值。 */
    async loadConfig() {
      try {
        const config = await this.api.config();
        rt.mentionPattern = config.mentionPattern;

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
         這個網址在連上的那一刻就固定了,之後改名它不會自己跟著變。

         ★ 還沒選好名字時【不報身分】:`myName` 在空白時會回 "user"(那是發言用的
           預設值),但拿它去報到會讓一個還卡在取名框前面的人出現在在場名單上 ——
           一個誰都還不是的幽靈,而且下一個想叫 user 的人會被它提醒撞名。

           `stream` 端點本來就支援匿名(不帶 watcher 就不列名),這裡用的正是
           那個預留的位置:進場前匿名看,選好名字之後 myName 一變就會重建連線
           (見 watch 的 myName),那時才報到。**不必發明新機制。** */
      const watcher = this.name.trim() ? encodeURIComponent(this.name.trim()) : "";
      const watcherParam = watcher ? `&watcher=${watcher}` : "";

      this.stream = useStream({
        url: `/api/rooms/${this.room}/stream?since_id=${this.lastId}${watcherParam}`,

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

    /** 定時更新在線名單。
     *
     * ★ 這裡曾經還有一個每分鐘的定時器,專門重算「誰還算活躍」——
     *   隨著 STANDBY 那個狀態一起沒了(2026-08-08)。**狀態消失,伺候它的機制也要跟著走**,
     *   不然會留下一個每分鐘跑一次、算完沒有人看的迴圈。
     */
    /**
     * 真的進到一個房間:把它的內容載進來、掛上直播、開始輪詢。
     *
     * ★ 這一整段本來全在 mounted 裡 —— 也就是「開頁就跑」。搬出來的時候
     *   要一個一個確認**誰在搭這班便車**,因為它們沒有一個是自己被呼叫的:
     *
     *       loadFirstPage    訊息本體
     *       unread.afterId   未讀線的位置 —— 它讀 lastId,所以必須排在訊息之後
     *       tasks / members / presence / agents   側邊那些數字
     *       scrollToBottom   捲到底(要等訊息畫出來才有意義)
     *       openStream       直播連線
     *       markRead         把未讀紅點清掉
     *       startTimers      每 15 秒重問在場名單
     *       jumpToAnchorIfAny 網址帶 #<訊息id> 時跳過去
     *
     *   漏掉任何一個都不會報錯,只會有一樣東西安靜地不動了 ——
     *   **拆一個「順便做了很多事」的函式,危險的從來不是它做的那件事。**
     *
     * ★★ 兩條路會走到這裡:開頁時身上就有條子(換房回來),
     *   以及在進場框按下「就叫這個」而房間沒變(見 confirmName)。
     *   房間有變的話走的是 goToRoom —— 那條路重新載入頁面,由 mounted 接手。
     */
    async enterRoom() {
      await this.loadFirstPage();

      // 上次離開時已經看完了 → 不用畫未讀線
      if (this.unread.afterId >= this.lastId) {
        this.unread.afterId = 0;
      }

      await Promise.all([
        this.tasks.load(),
        this.loadMembers(),
        this.loadPresence(),
        this.loadAgents(),
      ]);
      this.scrollToBottom();

      this.openStream();
      this.unread.markRead(this.lastId);
      this.startTimers();
      this.jumpToAnchorIfAny();
    },

    startTimers() {
      const self = this;

      setInterval(function () {
        self.loadPresence();
        // ★ 名冊也要跟著重載:它是「現在誰連著線」,會隨 agent 開關視窗變動 ——
        //   不重載的話,一個剛上線的 agent 要等到你重整頁面才會出現在派任務選單裡。
        self.loadAgents();
      }, PRESENCE_POLL_MS);
    },

    /** 離開這一頁時跟伺服器說一聲,別讓自己變成在場名單上的鬼影。
     *
     * ★ 用 `pagehide` 而不是 `beforeunload`:後者在手機瀏覽器上常常不觸發
     *   (切到背景被系統回收時就沒了),而 `pagehide` 兩種情況都會發。
     *
     * ★★ 它涵蓋的不只是關分頁 —— 重新整理、上一頁、關瀏覽器全都會經過這裡。
     *   換房那條路另外有一次(見 goToRoom),兩邊都送是刻意的:
     *   **告別是盡力而為的動作,重複送沒有壞處,漏送才有。**
     */
    bindFarewell() {
      const self = this;
      window.addEventListener("pagehide", function () {
        if (self.name) {
          self.api.leaveRoom(self.room, self.name);
        }
      });
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

    /** 這個名字現在有沒有 AI 在線上用?(取名框用:擋人類取到 AI 的名字) */
    isAgent(name) {
      return this.agents.some(function (agent) { return agent.name === name; });
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
      if (name.length > NAME_MAX_LENGTH) {
        return "名字最多 " + NAME_MAX_LENGTH + " 個字";
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
        // ★ 兩種撞名都【只提醒一次】,再按一次就放行 —— 沒有任何一種是硬擋。
        //
        //   在線撞名本來是硬擋的,2026-08-07 改掉,理由是它擋錯人了:
        //   在場名單來自「直播連線還開著沒有」,而連線死掉最久要 15 秒才被發現。
        //   換房會重新載入頁面,於是新頁面查到的是【自己上一秒那條還沒斷乾淨的連線】,
        //   使用者被自己的鬼影擋在門外,而畫面上寫的是「有人正在用」。
        //
        //   ★★ 更根本的一句:**在場名單本來就可能過期,拿一個會過期的東西去硬擋人
        //   是設計錯誤。** 它適合用來提醒,不適合用來判定。
        //
        //   降級的代價誠實記著:真的兩個人同時用同一個名字會變得容易一點。
        //   但那是罕見事件,而「被自己擋住」是每次換房都會遇到的 ——
        //   拿罕見換高頻,划算。
        if (taken && !this.nameWarning) {
          this.nameWarning = taken === "online"
            ? "「" + name + "」現在好像有人在用(也可能是你自己上一個分頁)。"
              + "確定的話再按一次「就叫這個」。"
            : "之前有人用過「" + name + "」,訊息會混在一起。"
              + "確定的話再按一次「就叫這個」。";
          return;                                // 第一次只提醒,不擋
        }
      } finally {
        this.nameChecking = false;
      }

      this.name = name;
      this.askingName = false;
      /* ★ 名字【不寫進 localStorage】—— 見 data() 的說明。
         選好的房間跟現在的不一樣就換過去(換房要重新掛直播與撈訊息)。

         ★★ 這裡看的是 `roomChoice` 而不是 `pendingRoom`:使用者沒動下拉時
         pendingRoom 是空的,但畫面上顯示的可能是 fallback 選出來的那一間
         (見 roomChoice)。照 pendingRoom 判斷的話,他會進到【畫面上沒寫的那個房】。 */
      const target = this.roomChoice;
      if (target && target !== this.room) {
        this.goToRoom(target);
        return;
      }

      /* ★ 房間沒變的那條路,以前【什麼都不用做】—— 因為開頁時就把 main 撈好了,
         關掉進場框就看得到。現在開頁不撈任何房,所以這裡必須自己把它載進來。

         這一行就是「拆掉便車之後,原本搭車的人得自己走」的那一步:
         那個空的 else 分支不是沒事做,是它的事**被別人順便做完了**。 */
      await this.enterRoom();
    },

    /**
     * 換房間 = 換網址重新載入。
     *
     * ★ 為什麼不用前端狀態切換:換房要重掛 SSE、重撈訊息、重算未讀、
     *   重置捲動位置……那等於把「載入一個房間」這件事寫兩遍(第一次載入一遍、
     *   切換再一遍),而兩遍遲早會分岔。重新載入只有一條路。
     */
    goToRoom(room) {
      const params = new URLSearchParams();
      params.set("room", room);
      const draft = (this.nameDraft || "").trim();
      if (draft) {
        params.set("name", draft);      // 讓下一頁的取名框預填,見 data()
      }
      // ★ 已經有身分的話,留一張條子告訴下一頁「這是換房,不是重新進場」——
      //   下一頁看到條子就直接進去,不再問名字、也不再查撞名(見 data())。
      //   還沒進場就換房(在 modal 裡點房間)的人沒有身分,那條路照樣要問。
      if (this.name) {
        sessionStorage.setItem("a2a-switching-as", this.name);
      }
      // ★★ 走之前跟伺服器說一聲,讓它立刻把這條連線從在場名單上拿掉。
      //   不說的話,那條線最久要 15 秒才被發現已死,而那 15 秒裡它是個「鬼」——
      //   下一頁會查到它、以為有別人在用這個名字。
      this.api.leaveRoom(this.room, this.name);
      window.location.search = params.toString();
    },

    /**
     * 建一個新房間 —— 而「建立」的動作就是【在裡面說第一句話】。
     *
     * ★ 房間不是一種資料,它是從訊息推導出來的(有訊息的房間就存在)。
     *   所以建房不需要新的儲存、新的 API、新的清理邏輯 ——
     *   發一則開場訊息,房間就在清單上了。
     *   而那則訊息本身也有用:它記下誰在什麼時候開的房。
     */
    async createRoom() {
      const room = (this.roomDraft || "").trim();
      this.roomError = "";
      if (!room) {
        this.roomError = "房間名不能空白";
        return;
      }
      const name = (this.nameDraft || "").trim();
      if (!name) {
        this.roomError = "先填上面的名字 —— 開場訊息要記下是誰開的房";
        return;
      }
      const body = { from: name, text: name + " 建立了這個房間" };
      const result = await this.api.send(room, body);
      if (!result.ok) {
        this.roomError = "建不起來:"
          + (result.data.detail || result.data.error || ("HTTP " + result.status));
        return;
      }
      this.pendingRoom = room;
      this.roomDraft = "";
      await this.loadRooms();
    },

    /**
     * 刪掉一個房間 —— 連同它的訊息與任務,而且【找不回來】。
     *
     * ★ 這是全站唯一的破壞性操作,所以要二次確認。
     *   main 由伺服器用結構擋住(不是靠這裡的確認框)——
     *   確認框可以按錯,而按錯的代價是所有人的預設房消失。
     */
    /**
     * 刪除鍵按第一下 —— 只是「上膛」,不會真的刪。
     *
     * ★ 為什麼不用 window.confirm:它會跳出系統對話框、阻塞整個頁面,
     *   而且長得跟這個介面完全不搭。兩段式按鈕用的是【已經有的狀態】,
     *   不新增 UI、不阻塞,而且危險程度看得見(鍵會變成紅色的「確定刪?」)。
     */
    askDeleteRoom(room) {
      if (this.armingDelete !== room) {
        this.armingDelete = room;
        return;
      }
      this.armingDelete = "";
      this.deleteRoom(room);
    },

    /** 選一間房。順便解除刪除鍵的上膛 —— 手移到別處就不該還舉著槍。 */
    pickRoom(room) {
      this.pendingRoom = room;
      this.armingDelete = "";
    },

    async deleteRoom(room) {
      const name = (this.nameDraft || "").trim() || "anonymous";
      this.roomError = "";
      try {
        await this.api.deleteRoom(room, name);
      } catch (error) {
        this.roomError = "刪不掉:" + error.message;
        return;
      }
      if (this.pendingRoom === room) {
        this.pendingRoom = "";
      }
      await this.loadRooms();
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
     * 某個人現在在不在:`"online"` 或 `"offline"`。
     *
     * 「在線」看的是**有沒有連線**(瀏覽器開著,或 agent 的 bell 掛著),
     * 不是用「最近有沒有發言」去猜 —— 安靜待命的 agent 也還在。
     *
     * ★ 這裡曾經有第三種狀態 `standby`(在線、但十分鐘內沒講話),
     *   2026-08-08 拿掉。它分辨的是「最近有沒有講話」,而**那不影響任何決定**:
     *   要派任務給誰、要不要敲他,看的都是「連線在不在」。
     *   一個安靜十一分鐘的 agent 跟安靜九分鐘的,對使用者是同一件事。
     *
     * ★★ 而它有代價:畫面上多一種狀態,讀的人得先想「STANDBY 是壞了嗎」——
     *   **一個不影響決定的區分,只會讓人多問一個問題。**
     *
     * @param {object|string} member 成員資料或單純一個名字
     * @returns {string} "online" | "offline"
     */
    presenceOf(member) {
      let name = member;

      if (typeof member === "object") {
        name = member.name;
      }

      if (this.present.includes(name)) {
        return "online";
      }
      return "offline";
    },

    /** 狀態 → 顯示的字。 */
    presenceLabel(member) {
      if (this.presenceOf(member) === "online") {
        return "● ONLINE";
      }
      return "○ OFFLINE";
    },

    /* ── 資料載入 ── */

    /** 更新在線名單。
     *
     * ★ 這裡曾經有一段「伺服器太舊沒有這個端點(404)就永久關掉輪詢」的降級,
     *   它降到的地方是「改用發言時間推測誰在線」—— 也就是 STANDBY 那套。
     *   那套 2026-08-08 拿掉之後,這個降級**沒有地方可以降**,一併移除。
     *   查不到就維持上一次的名單,下一輪再試。
     */
    async loadPresence() {
      try {
        const data = await this.api.presence(this.room);
        this.present = data.present;
      } catch (error) {
        // api.js 已經記錄過了。名單維持上一次的樣子,下一輪會再問一次
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

    /** 更新這個房間的成員名單(有 cursor 的人)。
     *
     * ★ 它只在【開邀請視窗的那一刻】跟【邀請成功之後】載入,不進定時輪詢:
     *   成員變動遠比在場變動少(加入一次就一直是),而這份資料只有那個視窗在用。
     *   為一個平常關著的視窗每 15 秒問一次,是替看不見的東西付錢。
     */
    async loadRoomMembers() {
      try {
        const data = await this.api.cursors(this.room);
        this.roomMembers = Object.keys(data.cursors || {});
      } catch (error) {
        // api.js 已經記錄了。名單維持上一次的樣子
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
      /* ★★★ 直播這條線上有【兩種東西】,而它們的差別是「有沒有 id」:

             訊息   有 id  —— 要進畫面、要推進續傳游標
             訊號   沒有 id —— 例如強制敲鈴、在場名單變動,只是通知,不是內容

         這一行以前不存在,而少了它會出事:下面那個 `message.id <= this.lastId`
         在 id 是 undefined 時算出 **false**(JS 的 NaN 比較永遠是 false),
         於是訊號被當成訊息一路往下走 —— `this.lastId` 被寫成 undefined,
         畫面多一則空泡泡,而**下次斷線重連時 since_id 也是 undefined**,
         伺服器就把整個房間重播一遍。

         ★ 那個 bug 一直都在(強制敲鈴就會觸發),只是要「按了敲鈴、又剛好斷線重連」
           兩件事疊起來才看得出來,所以躲了很久。 */
      if (message.id === undefined) {
        this.handleSignal(message);
        return;
      }

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

    /** 處理直播上的【訊號】(沒有 id 的那種)。
     *
     * ★ 不認得的型別**安靜忽略**,而那是刻意的:伺服器將來會加新的訊號,
     *   而使用者的分頁可能開著舊版的這個檔案 —— **舊前端遇到新訊號時,
     *   正確的行為是什麼都不做**,不是壞掉、也不是在 console 洗版。
     */
    handleSignal(signal) {
      if (signal.type === "presence") {
        this.present = signal.present;
        return;
      }
      // ring 之類的訊號不歸畫面管(那是敲鈴器在聽的),其餘一律忽略
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

      const body = { from: from, text: text };

      if (this.replyTo) {
        body.reply_to = this.replyTo;
      }

      // ★ finally 不是保險,是【唯一】保證解鎖的寫法:api.send 內部是 fetch,
      //   斷網時它會拋例外而不是回 { ok: false } —— 那條路繞過下面每一個 return。
      //   漏掉的話,一次斷網就讓輸入框永遠鎖死,而畫面上只看得出「這個網頁壞了」。
      let result;
      try {
        result = await this.api.send(this.room, body);
      } finally {
        this.$refs.composer.unlock();
      }

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

      // 理由同 send():解鎖走 finally,因為 fetch 拋例外那條路繞過所有 return。
      let result;
      try {
        result = await this.api.sendTask(payload.target, {
          room: this.room,
          text: payload.text,
          sender: sender,
          deadlineSeconds: payload.deadlineSeconds,
        });
      } finally {
        this.$refs.composer.unlock();
      }

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
      // ★ 直接轉給 goToRoom —— 這裡曾經自己寫過一行 location.href,
      //   於是「換房」有了兩條路:進場視窗裡選房間走 goToRoom,
      //   頂部下拉選單走這裡。兩條做的是同一件事,卻只有一條會
      //   留下換房的條子、跟伺服器說再見。
      //
      //   2026-08-07 實測時撞到:修好的是 goToRoom,而使用者用的是這條,
      //   所以「切房不該被自己的鬼擋住」在下拉選單那條路上完全沒生效。
      //   **同一件事有兩個實作,修好一個不算修好。**
      this.goToRoom(room);
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
        this.bubbleFont = FONT_DEFAULT;
      } else {
        const next = this.bubbleFont + delta;
        this.bubbleFont = Math.min(FONT_MAX, Math.max(FONT_MIN, next));
      }

      localStorage.setItem("a2a-font", String(this.bubbleFont));
      this.applyFont();
    },

    applyFont() {
      document.documentElement.style.setProperty("--bubble-font", this.bubbleFont + "px");
    },

    /* ── 邀請 ── */

    /**
     * 打開邀請視窗。
     *
     * ★ 兩份名單都在【開視窗的這一刻】才問:成員名單平常沒人看(見 loadRoomMembers),
     *   而在線名單雖然有輪詢,但這裡要的是「按下去那一秒還在線」——
     *   列出一個剛離線的人,按下去會建一個沒有人在用的書籤(無害,但看起來像壞掉)。
     */
    async openInvite() {
      this.modal = { type: "invite", pending: "" };
      await Promise.all([this.loadRoomMembers(), this.loadAgents()]);
    },

    /**
     * 邀請一個 agent 進這個房 —— 兩個動作,而且順序有意義。
     *
     *     ① PUT cursor=0   機制:他從此是這個房的成員,他的敲鈴器半分鐘內會接上來
     *     ② 發一則訊息      紀錄:誰邀了誰,事後查得到
     *
     * ★ 順序不能反。反過來的話,訊息說「他被邀請了」而書籤沒建成(①失敗),
     *   房間裡就留下一句永遠不會成真的話 —— **而訊息只增不改,那句話會一直在那裡。**
     *
     * ★★ 成功的定義是【②送出去了】,不是「他真的連上來了」。後者最多要等 30 秒,
     *   而按鈕不能轉 30 秒。那 30 秒是敲鈴器的工作,畫面不替它擔保 ——
     *   畫面只負責說「我把書籤放好了」,那句話當下就是真的。
     *
     * ★★★ 訊息裡**不 @ 對方**,是刻意的:@ 會讓他一進門就非回話不可。
     *   邀請只是把人拉進來,要他做事的時候再點名 —— 這跟 AGENTS.md 那句
     *   「沒人叫 agent 加入時,加入本身就是打擾」是同一個分寸。
     */
    async inviteAgent(name) {
      if (!this.modal || this.modal.pending) {
        return;                       // 已經有一個在跑了 —— 連按不該送出兩次
      }
      this.modal.pending = name;

      try {
        await this.api.invite(this.room, name);
      } catch (error) {
        this.modal.pending = "";
        this.showToast(`>> 邀請失敗:${error.message || error}`, false);
        return;
      }

      const result = await this.api.send(this.room, {
        from: this.myName,
        text: `${this.myName} 邀請 ${name} 進來了`
              + `(${name} 的敲鈴器會在半分鐘內自己接上)`,
      });

      /* ★ 書籤已經建好了,所以這裡【不回滾】—— 訊息沒送出只是少一筆紀錄,
         而邀請本身是成立的。把 cursor 刪掉反而會讓一個已經生效的動作消失。 */
      if (!result.ok) {
        const detail = result.data.detail || result.data.error || `HTTP ${result.status}`;
        this.showToast(`>> ${name} 已經邀請進來了,但那則紀錄沒送出:${detail}`, false);
      } else {
        this.showToast(`>> 已邀請 ${name} 進 ${this.room}`, true);
      }

      this.modal.pending = "";
      await this.loadRoomMembers();   // 名單刷新 → 他從「可邀請」那份消失
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

    /** 強制敲醒一個 agent(理由見 api.js 的 ring)。 */
    async forceRing(name) {
      const result = await this.api.ring(this.room, name);

      if (!result) {
        return;                     // 連不上時 request 自己會提示,這裡不重複講
      }

      // ★ 分成兩種訊息,因為使用者要做的事不一樣:
      //     敲到了     → 等它反應
      //     沒連著線   → 去把那個視窗打開,按幾次都沒用
      if (result.online) {
        this.showToast(`>> 已敲醒 ${name}`, true);
      } else {
        this.showToast(`>> ${name} 的敲鈴器沒連著線,敲不到`, false);
      }
    },

    /** 開任務視窗:先用手上的摘要立刻顯示,詳細內容再慢慢抓。 */
    async openTask(taskId) {
      await this.tasks.load();

      const summary = this.tasks.map[taskId];

      if (!summary) {
        // ★ 這句話與 components.js 的 badgeTitle 是【同一件事的兩種說法】,
        //   兩處要一起改。它原本寫「伺服器重開之前的任務會消失」——
        //   那在任務持久化上線前是對的,現在重開會從 tasks.json 復原。
        this.showToast(">> TASK NOT FOUND(這則訊息比任務持久化功能還早)", false);
        return;
      }

      this.modal = { type: "task", summary: summary, full: null };
      this.fetchTaskFull(summary);
    },

    /** 抓任務的完整歷程。抓回來時使用者可能已經關掉或換了一個任務,所以要再確認一次。 */
    async fetchTaskFull(summary) {
      try {
        const response = await this.api.rpc(summary.target, "GetTask",
                                            { id: summary.id });

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

      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(function () {
        self.toast = null;
      }, TOAST_MS);
    },
  },
}).mount("#app");
