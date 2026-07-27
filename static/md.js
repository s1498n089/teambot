/* ═══════════════════════════════════════════════════════════════════════════
   md.js — 把聊天訊息的純文字,翻譯成「可以拿去畫的資料」

   ── 這個檔案在做什麼 ────────────────────────────────────────────────────
   使用者打的是純文字,例如:

       ## 標題
       這是 **粗體**,這是 `程式碼`
       - 清單第一項

   這個檔案負責把上面那串字,翻譯成一堆描述性的資料,例如:

       [ { type: "heading", level: 2, content: [...] },
         { type: "paragraph", lines: [...] },
         { type: "list", ordered: false, items: [...] } ]

   然後畫面那邊(app.js 的樣板)照著這些資料去畫。

   ── 為什麼不用現成的 markdown 套件 ─────────────────────────────────────
   現成套件(例如 marked)吐出來的是「HTML 字串」,要顯示就得把那串 HTML
   塞進網頁裡。可是聊天室的文字是**別人打的**,一旦別人打的東西能變成網頁
   的一部分,他就能讓程式碼在每個看聊天室的人的瀏覽器裡執行(例如偷走登入
   用的 token)。要防這件事,就得再裝一個「保全套件」一直守著。

   這個檔案的做法不一樣:它**從頭到尾不產生任何 HTML**,只產生上面那種資料。
   畫面那邊用 Vue 的插值語法({{ }})去顯示,Vue 會自動把特殊符號轉成純文字。
   所以「別人的文字變成可執行的東西」這件事,不是被擋下來,而是**根本沒有
   發生的管道**。

   代價是:功能只做常用的一部分,不是完整的 markdown 規格。

   ── 這個檔案的寫法約定 ──────────────────────────────────────────────────
   刻意寫得直白、少用簡寫,方便不熟 JavaScript 的人也能讀:
   - 不用展開運算子(...)、不用解構賦值
   - 不寫巢狀的三元運算子,一律用 if / else
   - 一行只做一件事
   - 變數名稱寫完整,不用 t、v、m 這種單字母

   ── 對外提供的兩個函式 ──────────────────────────────────────────────────
   parseMarkdownBlocks(text, mentionPattern) — 完整解析(Markdown 模式用)
   parseTextOnly(text, mentionPattern)       — 只認 @某人 與網址(原文模式用)

   兩個都是「純函式」:同樣的輸入永遠得到同樣的輸出,不依賴外部狀態,
   所以很好測試(見 mdtest.html)。
   ═══════════════════════════════════════════════════════════════════════ */


/* ───────────────────────────────────────────────────────────────────────
   第一部分:找出文字裡的特殊語法用的規則(正規表示式)
   ─────────────────────────────────────────────────────────────────────── */

/* ★ 先講這一區三顆規則的 g 旗標,因為它決定了它們【能怎麼用】。

   帶 g 的正規表示式【有記憶】:它會記住上次比對到哪裡(lastIndex),
   下次從那裡繼續。這對「一次找出全部」很方便,對「問一個是非題」是災難 ——
   同一顆規則連續 .test() 同一段文字,答案會 true、false、true、false 交替出現。

   這裡的分工是:

     URL_PATTERN      帶 g,只餵給 .split()   —— split 不看 lastIndex,安全
     getMentionRegex  帶 g,只餵給 .split()   —— 同上,而且它還被快取重複使用
     INLINE_SYNTAX    不帶 g,餵給 .exec()    —— 靠外面的 slice 自己往前走

   ⚠️ 帶 g 的那兩顆【不准拿去 .test() 或 .exec()】。
      最容易犯的是拿 getMentionRegex 去問「這則訊息有沒有點名我」——
      那會是隔一次才對的鬼故障(true、false、true……),
      而且看起來完全不像跟這裡有關。
      真要做那種判斷,現場 new RegExp(pattern) 建一顆乾淨的。 */

// 網址。只吃 ASCII 字元,所以中文和全形標點會自然成為網址的結尾。
const URL_PATTERN = /(https?:\/\/[A-Za-z0-9\-._~:/?#@!$&*+;=%()\[\]]+)/g;

// 把網址結尾的標點剝回一般文字。例如「請看 https://a.com。」的句號不屬於網址。
const URL_TRAILING_PUNCTUATION = /^(.*?)([.,;:!?)\]]*)$/;

// 行內語法。放在同一條規則裡用「或」串起來,誰先出現就先處理誰。
// 順序有意義:
//   1. `程式碼` 放最前面 —— 程式碼裡面的東西不該再被當成語法解析
//   2. **粗體** 要排在 *斜體* 前面 —— 否則 ** 會被當成兩個單獨的 *
//   3. 粗體與斜體的前後都要求「非空白字元」,這樣「5 * 3 * 2」這種算式
//      才不會被誤認成斜體
//
// ⚠️ 這裡刻意比標準 Markdown(CommonMark)**寬鬆**,而且不要「修正」它:
// 標準規定 ** 左邊若緊貼著文字就不算粗體的開始,所以「改放**粗體**」在
// GitHub 或編輯器預覽裡不會變粗,要寫成「改放 **粗體**」才行。
// 那條規則是為英文設計的 —— 英文本來就用空格分詞,中文不是。
// 逼中文使用者為了排版在句子中間插空格,是把標準的缺陷轉嫁給使用者,
// 所以這裡兩種寫法都認。代價是同一份文字在這裡與在 GitHub 上可能長得不同;
// 寫「要放到 GitHub 的檔案」時還是得照標準加空格。
// mdtest.html 有一條測試鎖住這個行為。
const INLINE_SYNTAX = new RegExp(
  "(`[^`\\n]+`)" +                      // 群組 1:`程式碼`
  "|(\\*\\*\\S(?:[^*]*\\S)?\\*\\*)" +   // 群組 2:**粗體**
  "|(\\*\\S(?:[^*\\n]*\\S)?\\*)" +      // 群組 3:*斜體*
  "|(!?\\[[^\\]\\n]*\\]\\([^)\\s]+\\))" // 群組 4:[文字](網址),開頭的 ! 也吃(見下)
);

/* ★ 圖片語法 ![說明](網址) 會被認出來,但【故意只做成連結,不做成圖片】。
   上面群組 4 開頭那個 !? 就是在吃那個驚嘆號;吃完之後走的是連結那條路,
   所以 ![貓](https://x/cat.png) 顯示成一個文字是「貓」的可點連結。

   這【不是還沒做】,是刻意不做,理由有兩層:

     ① 隱私:自動載圖等於【每個看聊天室的人都對那個網址發出一次請求】。
        貼圖的人因此拿得到所有讀者的 IP 與讀訊息的時間 —— 一則訊息變成追蹤器。
     ② 版面:外部圖片的尺寸不受控,一張大圖就能把整個聊天室的排版壓垮。

   降級成連結的意思是「你想看就自己點,自己承擔」。
   這跟檔案開頭那條「不產生任何 HTML」是同一條防線的延伸:
   別人的文字不該讓我的瀏覽器替他做事。

   ⚠️ 這條有測試鎖著:mdtest.html 的「安全:圖片語法只做成連結」。
      想加圖片支援的人會在這裡與那裡各被擋一次 —— 理由在源頭,鎖在測試。 */

// 把 [文字](網址) 拆成「文字」和「網址」兩部分。開頭的驚嘆號(圖片語法)一起吃掉。
const LINK_PARTS = /^!?\[([^\]\n]*)\]\(([^)\s]+)\)$/;

// 允許變成連結的網址開頭。只允許 http 與 https:
// 像 javascript: 開頭的網址點下去會執行程式碼,絕對不能放行。
const SAFE_URL_PREFIX = /^https?:\/\//i;

// 整行才成立的語法(必須從行首開始,前面最多只能有空白)
const CODE_FENCE = /^\s*```(.*)$/;                    // ``` 開始或結束一段程式碼
const HEADING = /^\s*(#{1,6})\s+(.*)$/;               // # 標題,井字號數量就是層級
const LIST_ITEM = /^\s*(?:[-*+]|\d+\.)\s+(.*)$/;      // - 項目 或 1. 項目
const QUOTE_LINE = /^\s*>\s?(.*)$/;                   // > 引用(括號裡是去掉 > 之後的內容)
const TABLE_ROW = /^\s*\|(.*)\|\s*$/;                 // | 欄 | 欄 |
const TABLE_SEPARATOR = /^\s*\|[\s:|-]+\|\s*$/;       // |---|---| 這種分隔線

// 建立 @某人 的比對規則會花一點時間,所以把上一次的結果存起來重複使用。
// 只有規則字串真的變了(伺服器換設定)才重新建立。
let cachedMentionSource = null;
let cachedMentionRegex = null;

/**
 * 取得「@某人」的比對規則。
 * @param {string} mentionPattern 規則字串,由伺服器的設定提供
 * @returns {RegExp} 可以用來切開文字的正規表示式
 */
function getMentionRegex(mentionPattern) {
  if (cachedMentionSource !== mentionPattern) {
    cachedMentionSource = mentionPattern;
    cachedMentionRegex = new RegExp(mentionPattern, "g");
  }
  return cachedMentionRegex;
}


/* ───────────────────────────────────────────────────────────────────────
   第二部分:製作一顆「行內元素」

   行內元素就是一段文字,加上「它是什麼」與「它長什麼樣」的說明。
   例如粗體的程式碼,會是這樣一顆:

       { type: "code", text: "abc", bold: true, italic: false }

   注意粗體與斜體是**兩個開關**,不是巢狀結構。這樣做的好處是所有元素
   都在同一層,畫面那邊用一個迴圈就能畫完,不需要遞迴。
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 製作一顆行內元素。
 * @param {string} type  種類:"text"(一般文字)、"mention"(@某人)、
 *                       "link"(連結)、"code"(行內程式碼)
 * @param {string} text  要顯示出來的文字
 * @param {object} styles 目前繼承下來的樣式開關,形如 { bold: true, italic: false }
 * @param {string} href  只有連結才需要:點下去要去的網址
 * @returns {object} 一顆行內元素
 */
function makeInlineToken(type, text, styles, href) {
  const token = {};
  token.type = type;
  token.text = text;
  token.bold = styles.bold === true;
  token.italic = styles.italic === true;
  if (href !== undefined) {
    token.href = href;
  }
  return token;
}

/**
 * 複製一份樣式開關,並打開其中一個。
 * 之所以要複製而不是直接改,是因為原本那份還要給同一層的其他文字用。
 * @param {object} styles 原本的樣式開關
 * @param {string} name   要打開哪一個:"bold" 或 "italic"
 * @returns {object} 新的一份樣式開關
 */
function withStyleTurnedOn(styles, name) {
  const copy = {};
  copy.bold = styles.bold === true;
  copy.italic = styles.italic === true;
  copy[name] = true;
  return copy;
}


/* ───────────────────────────────────────────────────────────────────────
   第三部分:把「沒有 markdown 語法的一般文字」切成元素

   這是最後一關 —— markdown 語法都處理完之後,剩下的普通文字會走到這裡,
   在這裡認出兩種東西:網址、以及 @某人。
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 把一段普通文字切成元素,結果會加到 output 陣列的尾巴。
 * @param {string} text           要處理的文字
 * @param {object} styles         繼承下來的樣式開關
 * @param {string} mentionPattern @某人 的比對規則
 * @param {Array}  output         結果要放進去的陣列(會被修改)
 */
function appendPlainText(text, styles, mentionPattern, output) {
  // 用網址規則把文字切開。因為規則有括號,切開後網址本身也會留在結果裡。
  const pieces = text.split(URL_PATTERN);

  for (let i = 0; i < pieces.length; i++) {
    const piece = pieces[i];

    if (piece === "" || piece === undefined) {
      continue;
    }

    // 這一段是不是網址?
    if (/^https?:\/\//.test(piece)) {
      // 把結尾的標點剝掉,例如「https://a.com。」的句號不屬於網址
      const parts = piece.match(URL_TRAILING_PUNCTUATION);
      const urlText = parts[1];
      const trailingPunctuation = parts[2];

      output.push(makeInlineToken("link", urlText, styles, urlText));

      if (trailingPunctuation !== "") {
        output.push(makeInlineToken("text", trailingPunctuation, styles));
      }
      continue;
    }

    // 不是網址,再看看裡面有沒有 @某人
    const subPieces = piece.split(getMentionRegex(mentionPattern));

    for (let j = 0; j < subPieces.length; j++) {
      const subPiece = subPieces[j];

      if (subPiece === "" || subPiece === undefined) {
        continue;
      }

      const isMention = subPiece.charAt(0) === "@" && subPiece.length > 1;

      if (isMention) {
        output.push(makeInlineToken("mention", subPiece, styles));
      } else {
        output.push(makeInlineToken("text", subPiece, styles));
      }
    }
  }
}


/* ───────────────────────────────────────────────────────────────────────
   第四部分:解析一行文字裡的 markdown 行內語法
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 解析一行文字,認出 `程式碼`、**粗體**、*斜體*、[文字](網址),
 * 剩下的部分交給 appendPlainText 處理。
 *
 * @param {string} text           要解析的一行文字
 * @param {object} styles         繼承下來的樣式開關,最外層傳 {}
 * @param {string} mentionPattern @某人 的比對規則
 * @returns {Array} 一串行內元素(平的,沒有巢狀)
 */
function parseInline(text, styles, mentionPattern) {
  if (styles === undefined) {
    styles = {};
  }

  const output = [];
  let remaining = text;

  while (true) {
    const found = INLINE_SYNTAX.exec(remaining);

    // 找不到任何語法就結束,剩下的全部當普通文字
    if (found === null) {
      break;
    }

    const matchedText = found[0];
    const matchedAt = found.index;

    // 語法前面那段普通文字先處理掉
    if (matchedAt > 0) {
      appendPlainText(remaining.slice(0, matchedAt), styles, mentionPattern, output);
    }

    if (found[1] !== undefined) {
      // 情況一:`程式碼`。裡面的內容原封不動,不再往下解析。
      const codeText = matchedText.slice(1, matchedText.length - 1);
      output.push(makeInlineToken("code", codeText, styles));

    } else if (found[2] !== undefined) {
      // 情況二:**粗體**。把裡面的內容再解析一次,並且把粗體開關打開。
      const boldInner = matchedText.slice(2, matchedText.length - 2);
      const boldStyles = withStyleTurnedOn(styles, "bold");
      const boldTokens = parseInline(boldInner, boldStyles, mentionPattern);
      for (let b = 0; b < boldTokens.length; b++) {
        output.push(boldTokens[b]);
      }

    } else if (found[3] !== undefined) {
      // 情況三:*斜體*。同上,打開斜體開關。
      const italicInner = matchedText.slice(1, matchedText.length - 1);
      const italicStyles = withStyleTurnedOn(styles, "italic");
      const italicTokens = parseInline(italicInner, italicStyles, mentionPattern);
      for (let k = 0; k < italicTokens.length; k++) {
        output.push(italicTokens[k]);
      }

    } else {
      // 情況四:[文字](網址)。
      const linkParts = matchedText.match(LINK_PARTS);
      let linkLabel = linkParts[1];
      const linkUrl = linkParts[2];

      if (SAFE_URL_PREFIX.test(linkUrl)) {
        // 沒有寫文字時就直接顯示網址本身
        if (linkLabel === "") {
          linkLabel = linkUrl;
        }
        output.push(makeInlineToken("link", linkLabel, styles, linkUrl));
      } else {
        // 網址開頭不在白名單內(例如 javascript:),不做成連結,
        // 整段原封不動當普通文字顯示。
        appendPlainText(matchedText, styles, mentionPattern, output);
      }
    }

    // 往後移動,繼續找下一個語法
    remaining = remaining.slice(matchedAt + matchedText.length);
  }

  // 最後剩下的文字
  if (remaining !== "") {
    appendPlainText(remaining, styles, mentionPattern, output);
  }

  return output;
}


/* ───────────────────────────────────────────────────────────────────────
   第五部分:表格
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 把「| 甲 | 乙 |」這樣的一行,拆成每一格的內容。
 * 注意:不支援用反斜線跳脫的直線(\|),那是刻意省略的功能。
 * @param {string} line           表格的一行
 * @param {string} mentionPattern @某人 的比對規則
 * @returns {Array} 每一格的行內元素陣列
 */
function parseTableRow(line, mentionPattern) {
  const inside = line.match(TABLE_ROW)[1];
  const rawCells = inside.split("|");
  const cells = [];

  for (let i = 0; i < rawCells.length; i++) {
    const cellText = rawCells[i].trim();
    cells.push(parseInline(cellText, {}, mentionPattern));
  }

  return cells;
}


/* ───────────────────────────────────────────────────────────────────────
   第六部分:把整則訊息切成一塊一塊

   一則訊息會被切成好幾塊,每一塊是段落、標題、清單、引用、程式碼或表格。
   ─────────────────────────────────────────────────────────────────────── */

/**
 * 判斷第 index 行是不是一張表格的開頭。
 *
 * 為什麼要三個條件而不是「這行是表格列」就好:句子裡出現的直線很常見
 * (例如「A | B 二選一」),只看這一行會把普通句子誤判成表格。
 * 必須下一行是 |---|---| 那種分隔線才算數。
 *
 * ★ 抽成函式是因為這個條件【原本一字不差地寫了兩遍】——
 *   startsNewBlock 一份、parseMarkdownBlocks 一份。三段式條件抄兩份很危險:
 *   哪天放寬表格規則卻只改了一處,結果會是「表格被段落吃掉」
 *   或「段落被當成表格」,而且兩種都不會報錯,只會默默畫錯。
 *
 * @param {Array}  lines 全部的行
 * @param {number} index 要判斷第幾行
 * @returns {boolean} 這一行是不是表格的開頭
 */
function looksLikeTableAt(lines, index) {
  if (!TABLE_ROW.test(lines[index])) {
    return false;
  }
  if (index + 1 >= lines.length) {
    return false;
  }
  return TABLE_SEPARATOR.test(lines[index + 1]);
}

/**
 * 判斷某一行是不是「新的一塊」的開頭。
 * 段落會一直往下吃,直到遇到空行或另一塊的開頭為止 —— 所以每次新增一種
 * 塊,這裡就要跟著讓一次路,否則新的塊會被段落吃掉。
 *
 * @param {Array}  lines 全部的行
 * @param {number} index 要判斷第幾行
 * @returns {boolean} 這一行是不是某一塊的開頭
 */
function startsNewBlock(lines, index) {
  const line = lines[index];

  if (CODE_FENCE.test(line)) {
    return true;
  }
  if (HEADING.test(line)) {
    return true;
  }
  if (LIST_ITEM.test(line)) {
    return true;
  }
  if (QUOTE_LINE.test(line)) {
    return true;
  }
  if (looksLikeTableAt(lines, index)) {
    return true;
  }
  return false;
}

/**
 * 把一則訊息的文字,解析成一串「塊」。這是這個檔案最主要的對外函式。
 *
 * 產出的塊有這幾種:
 *   { type: "code",      language, text }
 *   { type: "heading",   level, content }
 *   { type: "table",     header, rows }
 *   { type: "list",      ordered, items }
 *   { type: "quote",     lines }
 *   { type: "paragraph", lines }
 *
 * @param {string} text           整則訊息的文字
 * @param {string} mentionPattern @某人 的比對規則
 * @returns {Array} 一串塊
 */
function parseMarkdownBlocks(text, mentionPattern) {
  const lines = String(text).split("\n");
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    // ── 情況一:``` 程式碼區塊 ──
    // 裡面的內容完全不解析,原封不動保留(所以在裡面寫 # 不會變成標題)
    if (CODE_FENCE.test(line)) {
      const language = line.match(CODE_FENCE)[1].trim();
      const codeLines = [];
      index = index + 1;

      while (index < lines.length && !CODE_FENCE.test(lines[index])) {
        codeLines.push(lines[index]);
        index = index + 1;
      }
      // 跳過收尾的 ```。如果一直到最後都沒有收尾,這裡剛好也是結束。
      index = index + 1;

      blocks.push({ type: "code", language: language, text: codeLines.join("\n") });
      continue;
    }

    // ── 情況二:# 標題 ──
    if (HEADING.test(line)) {
      const headingParts = line.match(HEADING);
      const level = headingParts[1].length;   // 幾個井字號就是第幾層
      const headingText = headingParts[2];

      blocks.push({
        type: "heading",
        level: level,
        content: parseInline(headingText, {}, mentionPattern)
      });
      index = index + 1;
      continue;
    }

    // ── 情況三:表格 ──
    // 判斷條件抽在 looksLikeTableAt,startsNewBlock 用的是同一個 ——
    // 兩處必須永遠一致,否則段落與表格會互相吃掉對方。
    if (looksLikeTableAt(lines, index)) {
      const header = parseTableRow(line, mentionPattern);
      index = index + 2;                    // 跳過表頭與分隔線

      const tableRows = [];
      while (index < lines.length && TABLE_ROW.test(lines[index])) {
        tableRows.push(parseTableRow(lines[index], mentionPattern));
        index = index + 1;
      }

      blocks.push({ type: "table", header: header, rows: tableRows });
      continue;
    }

    // ── 情況四:清單 ──
    if (LIST_ITEM.test(line)) {
      const isOrdered = /^\s*\d+\./.test(line);
      const items = [];

      while (index < lines.length && LIST_ITEM.test(lines[index])) {
        const itemText = lines[index].match(LIST_ITEM)[1];
        items.push(parseInline(itemText, {}, mentionPattern));
        index = index + 1;
      }

      blocks.push({ type: "list", ordered: isOrdered, items: items });
      continue;
    }

    // ── 情況五:> 引用 ──
    // 用 QUOTE_LINE.test 而不是手寫 charAt 檢查:六種塊、六條規則、
    // 六個長得一樣的判斷句 —— 下一個要加第七種塊的人才有明確的樣板可抄。
    if (QUOTE_LINE.test(line)) {
      const quoteLines = [];

      while (index < lines.length && QUOTE_LINE.test(lines[index])) {
        const quoteText = lines[index].match(QUOTE_LINE)[1];
        quoteLines.push(parseInline(quoteText, {}, mentionPattern));
        index = index + 1;
      }

      blocks.push({ type: "quote", lines: quoteLines });
      continue;
    }

    // ── 情況六:空行 ──
    // 空行只負責把段落分開,本身不留下任何東西
    if (line.trim() === "") {
      index = index + 1;
      continue;
    }

    // ── 情況七:普通段落 ──
    // 一路吃到空行或另一塊的開頭為止。
    // 段落裡的單一換行**就是換行** —— 標準 markdown 會把它吃掉,但在聊天室
    // 裡按 Enter 就是想換行,照標準做反而會讓訊息全部黏在一起。
    const paragraphLines = [];

    while (index < lines.length
           && lines[index].trim() !== ""
           && !startsNewBlock(lines, index)) {
      paragraphLines.push(parseInline(lines[index], {}, mentionPattern));
      index = index + 1;
    }

    blocks.push({ type: "paragraph", lines: paragraphLines });
  }

  return blocks;
}

/**
 * 原文模式用:完全不解析 markdown 語法,只認出 @某人 與網址。
 * 這就是還沒有加 markdown 功能之前,泡泡本來的樣子。
 *
 * @param {string} text           整則訊息的文字
 * @param {string} mentionPattern @某人 的比對規則
 * @returns {Array} 一串行內元素
 */
function parseTextOnly(text, mentionPattern) {
  const output = [];
  appendPlainText(String(text), {}, mentionPattern, output);
  return output;
}
