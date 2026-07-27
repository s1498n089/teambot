/* ═══════════════════════════════════════════════════════════════════════════
   util.js — 到處都會用到的小工具

   這裡放的都是「給一個東西、算出另一個東西」的小函式:名字算出顏色、
   名字畫出頭像、時間戳排成好看的格式、任務狀態查出對應的樣式。

   為什麼獨立成一個檔案:這些工具跟畫面長怎樣、跟伺服器怎麼溝通都無關,
   分開放之後,想找「頭像是怎麼畫出來的」就只要看這一個檔案。

   注意:colorHexOf 會去讀共用狀態 rt.palette(定義在 app.js)。
   函式裡面的東西是「被呼叫的時候」才去找,而呼叫一定發生在畫面開始跑之後,
   那時 app.js 早就載入完了,所以載入順序不會出問題。

   寫法約定同 md.js:不用展開運算子與解構、不寫巢狀三元、一行只做一件事、
   名字寫完整。字串樣板(`${}`)保留,因為它比一段一段相加更好讀。
   ═══════════════════════════════════════════════════════════════════════ */


/* ───────────────────────────────────────────────────────────────────────
   第一部分:名字 → 顏色
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 把名字算成一個固定的數字(這種做法叫 hash)。
 *
 * 用途:同一個名字每次都要得到同一個顏色、同一張頭像,所以需要一個
 * 「看起來很隨機、但同樣輸入永遠同樣輸出」的數字。這裡用的是 FNV-1a 演算法。
 *
 * @param {string} name 名字
 * @returns {number} 0 到 42 億之間的整數,同一個名字永遠得到同一個值
 */
function hashName(name) {
  let hash = 2166136261;                 // FNV-1a 的起始值,照演算法規定

  for (const character of name) {
    // 逐字把「這個字的編號」揉進 hash 裡
    hash = hash ^ character.codePointAt(0);
    hash = Math.imul(hash, 16777619);    // imul 是「32 位元的乘法」,溢位行為才正確
  }

  // >>> 0 的作用是把結果轉成「非負整數」(位元運算會產生負數)
  return hash >>> 0;
}

/**
 * 名字 → 顏色。
 *
 * 已經註冊的成員,顏色由伺服器指定(存在 rt.palette 裡);
 * 沒註冊過的名字沒人指定顏色,就用 hash 生一個 —— 這樣同一個名字
 * 每次進來都是同一個顏色,看久了能認人。
 *
 * @param {string} name 名字
 * @returns {string} CSS 顏色字串
 */
function colorHexOf(name) {
  const assignedColor = rt.palette[name];

  if (assignedColor) {
    return assignedColor;
  }

  const hue = hashName(name) % 360;      // 色相環是 0~359 度
  return `hsl(${hue} 60% 62%)`;
}


/* ★ 語意色獨占:#00ff88 這個螢光綠是系統專用的(點名、連線正常、NEW 徽章),
     任何成員的顏色都不該撞到它,否則畫面上會分不出「這是系統在說話」還是「某個人」。

   這條規則【不需要任何檢查程式碼】就成立,原因在上面那一行:
   成員的顏色一律走 hsl(hue 60% 62%),而 60% 飽和度 / 62% 亮度
   產不出 #00ff88 那種螢光感 —— 參數本身就把那個色域擋在外面。

   (這個常數以前住在後端的協定層 a2a.py,用來擋動態註冊時自己挑顏色的 agent。
    註冊機制 2026-07-27 退役後那個檢查沒了,而它本來就是視覺規則不是協定規則,
    所以搬來這裡跟配色函式作鄰居。) */
const SEMANTIC_GREEN = "#00ff88";


/* ───────────────────────────────────────────────────────────────────────
   第二部分:名字 → 頭像

   頭像是「用名字算出來的小圖」,不需要任何外部圖檔或服務:
   把名字變成數字,再用那個數字決定 5x5 方格裡哪些格子要塗色,
   左右鏡射一下就成了一張看起來像人臉的小圖(這種圖叫 identicon)。
   ─────────────────────────────────────────────────────────────────────── */

// 算過的頭像存起來重複使用。key 帶著顏色,是因為伺服器的設定晚一步送到時
// 會改變成員顏色,那時舊的圖就不能再用了。
const avatarCache = {};

/**
 * 產生一個「每次呼叫都吐出 0~1 之間數字」的小工具。
 *
 * 為什麼不用 Math.random():那個每次結果都不一樣,頭像會一直變。
 * 這裡用的是 xorshift —— 給同一個起始值,吐出來的序列永遠相同。
 *
 * @param {number} seed 起始值
 * @returns {function} 呼叫一次得到一個 0~1 的數字
 */
function makeStableRandom(seed) {
  let state = seed;

  return function nextRandom() {
    state = state ^ (state << 13);
    state = state ^ (state >>> 17);
    state = state ^ (state << 5);
    return (state >>> 0) / 4294967296;   // 除以 2 的 32 次方,壓到 0~1
  };
}

/**
 * 名字 → 頭像圖(SVG 格式的 data 網址,可以直接放進 img 的 src)。
 * @param {string} name 名字
 * @returns {string} 可以當作圖片網址的字串
 */
function avatarOf(name) {
  const color = colorHexOf(name);
  const cacheKey = `${name}|${color}`;

  if (avatarCache[cacheKey]) {
    return avatarCache[cacheKey];
  }

  // hashName 有可能算出 0,而 0 會讓 xorshift 永遠吐 0(整張圖變空白),所以換成 1
  let seed = hashName(name);
  if (seed === 0) {
    seed = 1;
  }
  const nextRandom = makeStableRandom(seed);

  // 只決定左邊三欄,右邊兩欄用鏡射複製過去 —— 對稱的圖比較好看
  let squares = "";

  for (let y = 0; y < 5; y++) {
    for (let x = 0; x < 3; x++) {
      if (nextRandom() > 0.48) {
        squares = squares + `<rect x="${x}" y="${y}" width="1" height="1"/>`;

        if (x < 2) {
          const mirroredX = 4 - x;
          squares = squares + `<rect x="${mirroredX}" y="${y}" width="1" height="1"/>`;
        }
      }
    }
  }

  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-0.7 -0.7 6.4 6.4" shape-rendering="crispEdges">` +
    `<rect x="-0.7" y="-0.7" width="6.4" height="6.4" fill="#0d0d14"/>` +
    `<g fill="${color}">${squares}</g>` +
    `</svg>`;

  const dataUrl = "data:image/svg+xml," + encodeURIComponent(svg);
  avatarCache[cacheKey] = dataUrl;
  return dataUrl;
}


/* ───────────────────────────────────────────────────────────────────────
   第三部分:時間格式
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 時間戳 → 只有時分秒,例如 "14:05:32"。訊息旁邊顯示用。
 * @param {string} timestamp ISO 格式的時間字串
 * @returns {string}
 */
function fmtTime(timestamp) {
  const date = new Date(timestamp);
  return date.toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * 時間戳 → 完整日期加時間。滑鼠移上去才顯示的那種提示用。
 * @param {string} timestamp ISO 格式的時間字串
 * @returns {string}
 */
function fmtFull(timestamp) {
  const date = new Date(timestamp);
  return date.toLocaleString("en-GB", { hour12: false });
}

/**
 * 時間戳 → 只有日期,例如 "2026-07-25"。
 * 用途:判斷兩則訊息是不是同一天,不同天就插一條日期分隔線。
 * @param {string} timestamp ISO 格式的時間字串
 * @returns {string} YYYY-MM-DD
 */
function dayOf(timestamp) {
  const date = new Date(timestamp);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");  // 月份從 0 開始算,要加 1
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}


/* ───────────────────────────────────────────────────────────────────────
   第四部分:任務狀態 → 顯示樣式
   ─────────────────────────────────────────────────────────────────────── */

/* 任務狀態的單一對照表:CSS class 與顯示用的縮寫都查這裡。
   將來多出新狀態,只要在這張表加一列,畫面那邊完全不用改。 */
const STATE_META = {
  TASK_STATE_SUBMITTED: { cls: "open", short: "SUBMITTED" },
  TASK_STATE_WORKING: { cls: "open", short: "WORKING" },
  TASK_STATE_COMPLETED: { cls: "done", short: "COMPLETED" },
  TASK_STATE_FAILED: { cls: "failed", short: "FAILED" },
  TASK_STATE_REJECTED: { cls: "failed", short: "REJECTED" },
  TASK_STATE_CANCELED: { cls: "canceled", short: "CANCELED" },
};

// 查不到狀態時用的預設值。會查不到,是因為伺服器重開之前的任務已經沒了 ——
// 與其假裝它還在,不如誠實顯示成灰色的「已消失」。
const STATE_GONE = { cls: "gone", short: "EVAPORATED" };

/**
 * 任務狀態 → 該怎麼顯示。
 * @param {string} state 任務狀態字串,可能是 null
 * @returns {object} { cls: CSS class, short: 顯示用的縮寫 }
 */
function stateMeta(state) {
  const meta = STATE_META[state];

  if (meta) {
    return meta;
  }
  return STATE_GONE;
}
