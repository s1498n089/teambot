/* ═══════════════════════════════════════════════════════════════════════════
   tools/mdtest.js — 在終端機跑 static/mdtest.html 那一頁的檢查

   ── 為什麼需要這支 ─────────────────────────────────────────────────────
   mdtest.html 是「用瀏覽器打開就會跑」的測試頁,這個設計本身很好 ——
   使用者不必安裝任何東西就能驗證。但它有一個對【改 code 的人】致命的缺點:

       改完 md.js 之後,得有人記得去按重新整理。

   而「得有人記得」正是今天反覆出錯的那個模式(拿了號碼牌沒讀內容、
   讀完忘了推書籤、hash 用手打)。Python 那邊有 pytest 擋著,
   前端這邊到目前為止只有「我等一下會去看」。

   所以這支工具做的事很單純:**把那一頁的檢查搬到終端機跑**,
   讓改 md.js 之後可以立刻 `node tools/mdtest.js` 拿到綠或紅。

   ── 它怎麼做到的 ───────────────────────────────────────────────────────
   測試頁是一個 HTML,裡面有兩段 <script>:一段載入 md.js,一段是檢查本身。
   這支工具把兩段的內容讀出來,接在一起丟給 Node 執行,
   並且假造一個最小的 document —— 因為原本那段程式最後會把結果寫進表格。

   ★ 刻意【不複製一份檢查清單】。如果這裡自己抄一份,兩邊就會分岔,
     而分岔的測試比沒有測試更糟(它會給你一個過期的綠燈)。
     真正的來源永遠是 mdtest.html,這支只是換一個地方執行它。

   ── 用法 ───────────────────────────────────────────────────────────────
       node tools/mdtest.js

   全過回傳 0,有失敗回傳 1(可以接進 CI 或 pre-commit)。
   瀏覽器那一頁照舊可用,兩邊跑的是同一份檢查。
   ═══════════════════════════════════════════════════════════════════════ */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const STATIC_DIR = path.join(__dirname, "..", "static");

/**
 * 從 HTML 裡把所有 <script> 的「行內內容」抓出來(有 src 的略過)。
 * @param {string} html 整份 HTML
 * @returns {Array<string>} 每段行內 script 的內容
 */
function extractInlineScripts(html) {
  const scripts = [];
  const pattern = /<script([^>]*)>([\s\S]*?)<\/script>/g;

  let found = pattern.exec(html);
  while (found !== null) {
    const attributes = found[1];
    const body = found[2];
    // 有 src 的是外部檔案,行內是空的,略過
    if (attributes.indexOf("src=") === -1) {
      scripts.push(body);
    }
    found = pattern.exec(html);
  }
  return scripts;
}

/**
 * 假造一個最小的 document。
 * 測試頁最後會把結果塞進 #rows 與 #summary —— 我們不需要那些 HTML,
 * 但要讓它塞得進去而不炸掉,同時把摘要文字留下來給終端機用。
 */
function makeFakeDocument(captured) {
  const elements = {};

  function makeElement(id) {
    const element = {
      id: id,
      innerHTML: "",
      className: "",
    };
    // textContent 被設定時記下來 —— 摘要那行就是走這裡
    Object.defineProperty(element, "textContent", {
      set: function (value) {
        captured[id] = value;
      },
      get: function () {
        return captured[id] === undefined ? "" : captured[id];
      },
    });
    return element;
  }

  return {
    getElementById: function (id) {
      if (elements[id] === undefined) {
        elements[id] = makeElement(id);
      }
      return elements[id];
    },
  };
}

function main() {
  const mdSource = fs.readFileSync(path.join(STATIC_DIR, "md.js"), "utf8");
  const html = fs.readFileSync(path.join(STATIC_DIR, "mdtest.html"), "utf8");

  const inlineScripts = extractInlineScripts(html);
  if (inlineScripts.length === 0) {
    console.error("✗ mdtest.html 裡找不到行內的檢查程式 —— 那一頁的結構變了?");
    return 1;
  }

  const captured = {};
  const context = {
    document: makeFakeDocument(captured),
    console: console,
  };
  vm.createContext(context);

  try {
    vm.runInContext(mdSource, context, { filename: "static/md.js" });
    for (let i = 0; i < inlineScripts.length; i++) {
      vm.runInContext(inlineScripts[i], context, { filename: "static/mdtest.html" });
    }
  } catch (error) {
    console.error("✗ 執行時炸了:" + error.message);
    console.error(error.stack);
    return 1;
  }

  const summary = captured["summary"];
  if (summary === undefined) {
    console.error("✗ 檢查跑完了卻沒有摘要 —— mdtest.html 的收尾方式變了?");
    return 1;
  }

  console.log(summary);
  // 摘要文字全過是「全部通過:N 項」,有失敗會出現「失敗」兩個字
  return summary.indexOf("失敗") === -1 ? 0 : 1;
}

/* ── 將來如果要把這個架子擴到其他前端檔案,這裡先記下候選 ─────────────
   (不是待辦,是「等真的需要第二個架子時,清單已經在了」)

   util.js 有兩處是【純函式、行為正確、但看起來很想被改乾淨】,
   而且改錯了不會炸,只會默默畫錯 —— 這種地方目前的唯一防線是註解:

     fmtTime / fmtFull 的語系固定  改成跟隨系統語系,不同人看到不同時間格式
     dayOf 用本地時區而非 UTC      改成 toISOString,每天晚上 8 點畫錯一條分隔線

   為兩個函式立第二個測試架子成本不成比例(mdtest 這個架子是被 md.js 的
   522 行與 28 條規則逼出來的,不是預先建的)。等累積到閾值再說。 */

process.exit(main());
