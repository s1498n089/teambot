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
 * 名字 → 顏色。同一個名字永遠得到同一個顏色,看久了能認人。
 *
 * 規則只有兩條:rt.palette 裡查得到就用查到的,查不到就用名字 hash 算一個。
 *
 * ★ rt.palette 現在【只剩一筆】:{ user: "#c9d1d9" }。
 *
 *   它曾經是伺服器開機時下發的「每個 agent 指定什麼顏色」對照表,
 *   但名冊動態化(2026-07-27)之後伺服器不再下發顏色 —— 開機時它並不知道
 *   會有誰連進來。現在所有成員的顏色都走下面那行 hash。
 *
 *   ⚠️ 那剩下的一筆【不要清掉】。"user" 是取名框上線之前的人類預設名,
 *      聊天室裡有 138 則歷史訊息掛在這個名字底下,靠這一筆維持它們原本的灰色。
 *      清掉它,那些舊訊息會突然換成 hash 算出來的隨機色 —— 不會壞,但會很怪。
 *      「只有一筆的對照表」看起來很想順手刪,所以這句話寫在這裡。
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
   所以想維持這條鐵律,要守的是【那兩個百分比】,不是去比對顏色值。

   身世:這條規則以前是後端協定層 a2a.py 的一個常數,用來擋「動態註冊時
   自己挑顏色」的 agent。註冊機制 2026-07-27 退役,那個檢查跟著沒了;
   規則本身仍然成立,只是守它的方式從「執行期檢查」變成「配色參數本身」。

   ⚠️ 這裡曾經放過一個 const SEMANTIC_GREEN = "#00ff88" —— 從搬過來到刪掉,
      沒有任何一行程式碼讀過它。有價值的是上面這段解釋,不是那個變數;
      留著一個沒人用的常數,只會讓下一個人以為某處有在比對它。 */


/* ───────────────────────────────────────────────────────────────────────
   第二部分:名字 → 頭像

   頭像是「用名字算出來的小圖」,不需要任何外部圖檔或服務:
   把名字變成數字,再用那個數字決定 5x5 方格裡哪些格子要塗色,
   左右鏡射一下就成了一張看起來像人臉的小圖(這種圖叫 identicon)。
   ─────────────────────────────────────────────────────────────────────── */

/* 算過的頭像存起來重複使用。key 是「名字|顏色」而不是只有名字 ——
   因為顏色一旦不同,圖也不同,只用名字當 key 會拿到舊顏色的圖。

   (這個設計原本是為了「伺服器的設定晚一步送到會改變成員顏色」而做的。
    伺服器 2026-07-27 起不再下發顏色,那個情境不會發生了;但 key 這樣設計
    本身無害,而且它守住的是「顏色變了圖就要重算」這條一般規則,所以留著。)

   ★ 這張快取【只增不減】,而且從今天起它真的會長:取名框上線之後,
     名字變成隨時可以改的東西,每改一次就多一筆。

     這是有意識的選擇,不是沒想到:每筆是一段幾百位元組的 SVG 字串,
     存幾千個名字也只有幾 MB,而重新整理頁面就全部清空。
     為它加 LRU 是拿複雜度換一個不存在的問題。

     什麼時候要回頭處理:如果將來聊天室變成長時間不重整的常駐頁面
     (例如做成桌面 App),或名字開始由程式大量產生,再說。 */
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

/* ★ 下面兩個函式都把語系寫死成 "en-GB",那【不是「用英國的格式」的意思】。

   它是一個慣用手法:en-GB 給的是 24 小時制與 DD/MM/YYYY,
   而 en-US 會給 AM/PM。真正的意圖是【不要跟著使用者的系統語系跑】。

   為什麼這很重要:聊天室的時間戳必須所有人看起來一樣。
   如果跟隨系統語系,allen 看到「下午 2:05」、bently 看到「14:05」,
   兩個人對著同一則訊息會念出不同的時間 —— 討論「#731 那則」的時候就會對不上。

   ⚠️ 所以請不要把它「在地化」成 navigator.language 或 undefined ——
      那正好破壞它存在的理由。要改格式的話,改的是後面的選項,不是語系。 */

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
 *
 * ★ 這裡手工組四行,而不是寫成一行 timestamp.toISOString().slice(0, 10)。
 *   看起來笨,但那一行是錯的:
 *
 *     getFullYear / getMonth / getDate  →  【本地時區】
 *     toISOString                       →  【UTC】
 *
 *   這條分隔線問的是「使用者感覺上的同一天」。在台灣(UTC+8),
 *   晚上 8 點之後的訊息在 UTC 已經算隔天了 —— 改用 toISOString 的話,
 *   每天晚上 8 點就會冒出一條寫著明天日期的分隔線。
 *
 *   那種 bug 會上線很久才有人發現,發現了也很難連回這一行。
 *   ⚠️ 醜的寫法沒有理由撐著就會被順手改乾淨,所以理由寫在這裡。
 *
 * @param {string} timestamp ISO 格式的時間字串
 * @returns {string} YYYY-MM-DD(本地時區)
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

/* 查不到狀態時用的預設值 —— 與其假裝它還在,不如誠實顯示成灰色的「已消失」。

   什麼時候會查不到?★ 這裡的因果換過一次,舊說法是「伺服器一重開任務就沒了」,
   那在任務持久化(tasks.json)上線之前是對的,現在不是 —— 重開會從檔案復原。

   今天還會 EVAPORATED 的是這兩種:
     ① 持久化上線之前留下的舊訊息,它們引用的任務從來沒被存進檔案
     ② 清理測試房間時連任務一起刪掉的那批

   兩種都是「訊息還在、任務真的不在了」,所以顯示成已消失是正確的。 */
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
