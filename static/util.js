/* ═══════════════════════════════════════════════════════════════════════════
   util.js — 到處都會用到的小工具

   這裡放的都是「給一個東西、算出另一個東西」的小函式:名字算出顏色、
   名字畫出頭像、時間戳排成好看的格式、任務狀態查出對應的樣式。

   為什麼獨立成一個檔案:這些工具跟畫面長怎樣、跟伺服器怎麼溝通都無關,
   分開放之後,想找「頭像是怎麼畫出來的」就只要看這一個檔案。

   注意:colorHexOf 會去讀共用狀態 rt.palette(定義在 app.js)。
   函式裡面的東西是「被呼叫的時候」才去找,而呼叫一定發生在畫面開始跑之後,
   那時 app.js 早就載入完了,所以載入順序不會出問題。
   ═══════════════════════════════════════════════════════════════════════ */

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

/* Markdown 與斷詞的解析全部搬到 md.js(先載入,見 index.html)。
   這裡只留兩個進入點的用法備忘:
     parseMarkdownBlocks(文字, mention規則) — Markdown 模式,回傳一串「塊」
     parseTextOnly(文字, mention規則)      — 原文模式,只認 @某人 與網址
   兩者都是純函式,mention 規則用參數傳進去(不再讓 md.js 去讀全域狀態),
   所以可以被 static/mdtest.html 直接測試。 */

function fmtTime(ts) { return new Date(ts).toLocaleTimeString("en-GB", { hour12: false }); }
function fmtFull(ts) { return new Date(ts).toLocaleString("en-GB", { hour12: false }); }
function dayOf(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* task 狀態的單一事實來源:CSS class 與縮寫都查這張表,
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
