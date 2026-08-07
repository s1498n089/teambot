/* ═══════════════════════════════════════════════════════════════════════════
   api.js — 跟伺服器說話的唯一窗口

   畫面上所有需要「問伺服器」或「告訴伺服器」的事,都從這裡出去:
   拿訊息、送訊息、拿成員名單、派任務……

   為什麼要有這個檔案:如果每個元件各自去發請求,錯誤處理、共用標頭、
   逾時規則就會散落在十幾個地方,改一次要改十幾處。集中在這裡之後,
   「跟伺服器溝通」這件事就只有一個入口。

   用法:createApi(notify) 回傳一包函式。notify 是「要跟使用者說話時」
   呼叫的函式,由 app.js 傳進來 —— 這個檔案自己不碰畫面。

   三種回傳風格,刻意不同,原因見各自的說明:
     - 大部分函式        失敗就 throw,呼叫端用 try/catch 處理
     - send              失敗不 throw,回 { ok, status, data }
     - sendTask          失敗不 throw,回 { ok, data } —— 沒有 status,見那裡的說明

   ── 這個檔案依賴外面的東西(兩個,都不在這裡定義)─────────────────────
   API_FAIL_TOAST_THRESHOLD   定義在 app.js。api.js 比 app.js 早載入,
                              能跑是因為它只在【請求失敗的當下】才被讀到,
                              而那時畫面早就啟動了。(util.js 依賴 rt 是同一個模式。)
   notify                     由 app.js 在建立時傳進來 —— 這個檔案自己不碰畫面。
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
   *
   * 抽成函式是因為 send / sendTask / rpc 三邊都要用同一組標頭。
   *
   * ★ 它以前還會在有 token 時補上 `Authorization: Bearer <token>` ——
   *   認證 2026-08-07 整套拔掉了,所以只剩 Content-Type。
   *   **函式留著**:三邊共用一組標頭這件事沒有變,而下次要加任何共同標頭
   *   (追蹤 id、版本號、或哪天回來的認證)都還是加在這裡。
   *
   * @returns {object} 可以直接給 fetch 用的 headers
   */
  function buildHeaders() {
    return { "Content-Type": "application/json" };
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
    /** 開機時拿設定:@某人 的解析規則、A2A 協定版本。
        (顏色不在這裡拿 —— 前端從名字自己算,見 util.js 的 colorHexOf。) */
    config: function () {
      return request("/api/config");
    },

    /** 房間清單。背景更新,所以用 quiet。 */
    rooms: function () {
      return request("/api/rooms", undefined, true);
    },

    /**
     * 刪掉一個房間 —— **全站唯一的破壞性操作**。
     *
     * `by` 是刪除者的名字,伺服器會記進 log。它跟發言走同一道身分關卡
     * (server.py 的 check_writer)—— 那道關現在不檢查任何東西,但位置留著。
     *
     * ★ 這個【不是】quiet:刪不掉的時候使用者一定要知道
     *   —— 例如刪 main(伺服器用結構擋住,回 403)。
     */
    deleteRoom: function (room, by) {
      return request(`/api/rooms/${encodeURIComponent(room)}?by=${encodeURIComponent(by)}`,
                     { method: "DELETE" });
    },

    /** 目前連著線的 agent(派任務的下拉選單用)。
        不是一份註冊名單 —— 現在有誰算誰,
        agent 的視窗一關就從這份清單上消失。 */
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
     * 走之前跟伺服器說一聲:把我這條直播連線從在場名單上拿掉。
     *
     * ★ 為什麼需要它:名單的事實來源是「連線還開著沒有」,而伺服器要等
     *   keep-alive 超時(15 秒)才會發現對方已經走了。那 15 秒裡,離開的人
     *   還掛在名單上 —— 換房重新載入時,新頁面會查到自己上一秒的鬼影。
     *
     * ★★ **一定要用 sendBeacon,不能用 fetch。** 頁面正在卸載時,
     *   瀏覽器會直接【取消】還沒送完的 fetch,而 sendBeacon 的整個存在理由
     *   就是「頁面死了也把這個請求送完」。這裡用錯 API 的話,
     *   平常測起來都對(因為手動呼叫時頁面還活著),只有真的換頁時失效。
     *
     * ★★★ 就算用對了也**不保證送得到** —— 當機、拔網路線、手機切背景被系統殺掉,
     *   都不會有告別訊息。所以這只是「讓鬼變少」,不是「讓鬼消失」:
     *   取名那邊仍然不能拿在場名單去硬擋人(見 app.js 的 confirmName)。
     *
     * 回傳 boolean(瀏覽器有沒有接受這個請求),失敗不需要處理 —— 它是盡力而為的。
     */
    leaveRoom: function (room, watcher) {
      if (!room || !watcher || !navigator.sendBeacon) {
        return false;
      }
      const url = `/api/rooms/${encodeURIComponent(room)}/leave`
                + `?watcher=${encodeURIComponent(watcher)}`;
      return navigator.sendBeacon(url);
    },

    /**
     * 強制敲某個 agent 的鈴 —— 人類的「喂,醒醒」。
     *
     * 為什麼需要這個按鈕:敲鈴器有「連敲三次沒反應就安靜」的不騷擾設計,
     * 而觸發它的不一定是「卡住」,也可能只是「正在忙」。一旦安靜下來,
     * 要重新開始敲得等對方的進度追上 —— 而追上需要被敲醒。**那是死結**,
     * 從畫面上看就是「這個 agent 對整個聊天室完全沒反應」。
     *
     * 人看得見畫面,他比計數器清楚該不該吵 —— 所以把判斷權交給他。
     */
    ring: function (room, name) {
      return request(`/api/rooms/${room}/ring/${encodeURIComponent(name)}`,
                     { method: "POST" });
    },

    /**
     * 拿訊息。qs 是查詢字串,例如 "tail=50" 或 "since_id=100"。
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
     * ★ 前端【刻意不帶 expect_last_id】(伺服器支援的樂觀鎖),而 agent 帶。
     *   這個不對稱是設計,不是漏做:
     *
     *     人在畫面上打字   撞車了就是多一則訊息,對話照樣成立 —— 擋下來反而礙事
     *     agent 在跑迴圈   它是「讀完再回」,錯過一則就會答錯 —— 必須被擋下來重讀
     *
     *   所以同一個機制對人是噪音、對機器是必要。要「補上」之前先想清楚這件事。
     *
     * ★★ 2026-07-31 這個時刻真的來了,而答案是**不補**。
     *
     *   當天有人按一次 Enter,房間收到兩則一模一樣的訊息(時間戳同一秒)。
     *   看起來很像「少了樂觀鎖」,但真正的破口是**送出到回應之間那段空窗**
     *   沒有上鎖 —— 草稿還在、按鈕還能按,任何再觸發一次的東西都會複製一則。
     *
     *   補 expect_last_id 會「順便」擋掉這個症狀,代價卻是把上面那個不對稱毀掉:
     *   兩個人同時在打字就會有一個被 409 擋下來,而他只是在聊天。
     *   **用錯的機制擋對的症狀,會在別的地方長出新的問題。**
     *
     *   正解是在送出端加 in-flight 鎖(見 components.js 的 ChatComposer.sending)。
     *
     * @param {string} room 房間名
     * @param {object} body 要送出的內容
     * @returns {Promise<object>} { ok, status, data }
     */
    async send(room, body) {
      const response = await fetch(`/api/rooms/${room}/messages`, {
        method: "POST",
        headers: buildHeaders(),
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
          // senderKind:畫面上派任務的一定是人在按按鈕,所以固定 human。
          // (agent 用 curl 打這個端點時要自報 "agent" —— 見根目錄的 AGENTS.md。)
          metadata: { senderName: sender, deadlineSeconds: deadlineSeconds,
                      senderKind: "human" },
        },
      };

      const response = await fetch(`/agents/${target}/a2a`, {
        method: "POST",
        headers: buildHeaders(),
        body: JSON.stringify(requestBody),
      });

      const data = await readJsonOrEmpty(response);

      // A2A 的錯誤是包在正常回應裡的(HTTP 200 但 data.error 有東西),
      // 所以兩個都要檢查才知道到底成功沒有。
      //
      // ⚠️ 這個 ok 把【兩種完全不同的失敗壓成同一個 false】:
      //      response.ok 為假  → HTTP 層failed(伺服器掛了、網路斷了)
      //      data.error 有值   → 協定層拒絕(對方不在線、任務被拒、方法不支援)
      //    呼叫端想分辨的話,現在得自己去看 data.error 在不在。
      //    沒有跟著 send 一起回傳 status,是因為目前沒有呼叫端需要它 ——
      //    哪天需要了,補上 status 比拆開 ok 便宜。
      const succeeded = response.ok && !data.error;
      return { ok: succeeded, data: data };
    },

    /**
     * 直接呼叫 A2A 的其他方法(查任務、取消任務等)。
     *
     * ★ 標頭走 buildHeaders,跟 send / sendTask 同一組 —— 它以前自己手寫
     *   Content-Type,結果漏掉了共用標頭裡的另一半。**共用的東西各寫一份,
     *   遲早會分岔,而分岔的那一半不會有人發現。**
     *
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
        headers: buildHeaders(),
        body: JSON.stringify(requestBody),
      });
    },
  };
}
