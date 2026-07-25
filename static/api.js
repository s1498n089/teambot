/* ═══════════════════════════════════════════════════════════════════════════
   api.js — 跟伺服器說話的唯一窗口

   畫面上所有需要「問伺服器」或「告訴伺服器」的事,都從這裡出去:
   拿訊息、送訊息、拿成員名單、派任務……

   為什麼要有這個檔案:如果每個元件各自去發請求,錯誤處理、認證標頭、
   逾時規則就會散落在十幾個地方,改一次要改十幾處。集中在這裡之後,
   「跟伺服器溝通」這件事就只有一個入口。

   用法:createApi(notify) 回傳一包函式。notify 是「要跟使用者說話時」
   呼叫的函式,由 app.js 傳進來 —— 這個檔案自己不碰畫面。

   兩種回傳風格,刻意不同,原因見各自的說明:
     - 大部分函式:失敗就 throw,呼叫端用 try/catch 處理
     - send / sendTask:失敗不 throw,回一個含 ok 的物件
   ═══════════════════════════════════════════════════════════════════════ */

/**
 * 建立一包「跟伺服器溝通」的函式。
 * @param {function} notify 要通知使用者時呼叫,形式為 notify(訊息, 是否成功)
 * @returns {object} 一包可以呼叫的 API 函式
 */
function createApi(notify) {
  // 連續失敗幾次了。只有「使用者主動觸發」的請求會累加,見下方 quiet 的說明。
  let failStreak = 0;

  /**
   * 組出送給伺服器的標頭。
   * 抽成函式是因為 send 與 sendTask 本來各寫了一份一模一樣的,兩份逐字相同。
   * @param {string} token 登入用的憑證,沒有就傳空的
   * @returns {object} 可以直接給 fetch 用的 headers
   */
  function buildHeaders(token) {
    const headers = {};
    headers["Content-Type"] = "application/json";

    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    return headers;
  }

  /**
   * 所有「拿資料」請求的共同管道:發出去、檢查有沒有成功、把 JSON 解出來。
   *
   * quiet 這個參數是給「背景自動輪詢」用的(例如每 15 秒更新一次在線名單)。
   * 背景請求失敗時**不彈通知也不計入連續失敗**,理由是:次要功能壞掉不該蓋住
   * 使用者正在看的畫面;而「連線真的斷了」這件事有畫面上方的 SERVER 燈負責
   * 通報,分工清楚。
   *
   * 失敗時丟出的錯誤會帶著 status(HTTP 狀態碼),讓呼叫端可以自己判斷 ——
   * 例如遇到 404 就知道「這個伺服器版本還沒有這個功能」,可以安靜降級。
   *
   * @param {string} url 要打的網址
   * @param {object} options fetch 的設定,不需要就傳 undefined
   * @param {boolean} quiet 是否為背景請求(失敗時安靜處理)
   * @returns {Promise<object>} 伺服器回傳的 JSON
   */
  async function request(url, options, quiet) {
    try {
      const response = await fetch(url, options);

      if (!response.ok) {
        const error = new Error(`HTTP ${response.status}`);
        error.status = response.status;
        throw error;
      }

      if (!quiet) {
        failStreak = 0;                 // 成功一次就把連續失敗歸零
      }
      return await response.json();

    } catch (error) {
      console.warn("[api]", url, error.message || error);

      if (!quiet) {
        failStreak = failStreak + 1;

        // 偶爾失敗不吵使用者,連續失敗才說 —— 網路本來就會偶爾抖一下
        if (failStreak >= API_FAIL_TOAST_THRESHOLD) {
          notify(`>> API 連續失敗:${url}`, false);
        }
      }
      throw error;
    }
  }

  /**
   * 把回應解成 JSON;解不出來(例如伺服器回了空字串)就當成空物件。
   * @param {Response} response fetch 的回應
   * @returns {Promise<object>}
   */
  async function readJsonOrEmpty(response) {
    try {
      return await response.json();
    } catch (error) {
      return {};
    }
  }

  return {
    /** 開機時拿設定:成員顏色、@某人 規則、有沒有開認證。 */
    config: function () {
      return request("/api/config");
    },

    /** 房間清單。背景更新,所以用 quiet。 */
    rooms: function () {
      return request("/api/rooms", undefined, true);
    },

    /** 已註冊的 agent 清單(派任務的下拉選單用)。 */
    agents: function () {
      return request("/agents", undefined, true);
    },

    /** 某個房間發言過的人。 */
    members: function (room) {
      return request(`/api/rooms/${room}/members`, undefined, true);
    },

    /** 某個房間的任務清單。 */
    tasks: function (room) {
      return request(`/api/rooms/${room}/tasks`, undefined, true);
    },

    /** 目前正連著這個房間的人(在線名單)。 */
    presence: function (room) {
      return request(`/api/rooms/${room}/presence`, undefined, true);
    },

    /**
     * 拿訊息。qs 是查詢字串,例如 "since_id=100&limit=50"。
     * 這個**不是** quiet:使用者往上滑載入舊訊息失敗時,他需要知道。
     */
    messages: function (room, qs) {
      return request(`/api/rooms/${room}/messages?${qs}`);
    },

    /**
     * 送出一則訊息。
     *
     * 這裡刻意**不走上面的 request、失敗也不 throw**,而是回一個含 ok 的物件。
     * 原因:送訊息失敗時伺服器會回一段說明(例如「這個名字不能用」),
     * 那段話是要**原封不動顯示給使用者看**的,不能被通用的錯誤處理吃掉。
     *
     * @param {string} room 房間名
     * @param {object} body 要送出的內容
     * @param {string} token 認證憑證(伺服器有開認證時才需要)
     * @returns {Promise<object>} { ok, status, data }
     */
    async send(room, body, token) {
      const response = await fetch(`/api/rooms/${room}/messages`, {
        method: "POST",
        headers: buildHeaders(token),
        body: JSON.stringify(body),
      });

      const data = await readJsonOrEmpty(response);
      return { ok: response.ok, status: response.status, data };
    },

    /**
     * 派一個任務給某個 agent。
     *
     * 走的是 A2A 協定的正門(/agents/名字/a2a),不是聊天室的門 —— 任務跟聊天
     * 是兩件事,詳見教學第 6 章。
     *
     * returnImmediately 固定為 true:另一種模式會一直等到對方做完才回覆,
     * 那會讓畫面卡住不動,對使用者是災難。
     *
     * @param {string} target 要派給誰
     * @param {object} options 任務內容,見下方逐項取出
     * @returns {Promise<object>} { ok, data }
     */
    async sendTask(target, options) {
      const room = options.room;
      const text = options.text;
      const sender = options.sender;
      const deadlineSeconds = options.deadlineSeconds;
      const token = options.token;

      const message = {
        role: "ROLE_USER",
        parts: [{ text: text }],
        messageId: crypto.randomUUID(),
        contextId: room,                // 任務屬於哪個房間,就是使用者現在待的房間
      };

      const requestBody = {
        jsonrpc: "2.0",
        id: 1,
        method: "SendMessage",
        params: {
          message: message,
          configuration: { returnImmediately: true },
          metadata: { senderName: sender, deadlineSeconds: deadlineSeconds },
        },
      };

      const response = await fetch(`/agents/${target}/a2a`, {
        method: "POST",
        headers: buildHeaders(token),
        body: JSON.stringify(requestBody),
      });

      const data = await readJsonOrEmpty(response);

      // A2A 的錯誤是包在正常回應裡的(HTTP 200 但 data.error 有東西),
      // 所以兩個都要檢查才知道到底成功沒有
      const succeeded = response.ok && !data.error;
      return { ok: succeeded, data: data };
    },

    /**
     * 直接呼叫 A2A 的其他方法(查任務、取消任務等)。
     * @param {string} agent 對象
     * @param {string} method A2A 方法名,例如 "GetTask"
     * @param {object} params 該方法的參數
     * @returns {Promise<object>}
     */
    rpc: function (agent, method, params) {
      const requestBody = {
        jsonrpc: "2.0",
        id: 1,
        method: method,
        params: params,
      };

      return request(`/agents/${agent}/a2a`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
    },
  };
}
