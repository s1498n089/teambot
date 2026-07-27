# DESIGN_SYSTEM — Cyberpunk / Glitch(v5 設計依據,老闆提供)

> ## 怎麼讀這一份
>
> **以下到第 7 節為止是【原始需求】,不修訂。** 它記錄的是「當初要求什麼」,
> 所以就算實作後來走了別的路,這裡也維持原樣 ——
> 改了它,「我們有沒有做到他要的」這個問題就永遠沒有對照組可查。
>
> **實作最後做成什麼樣、哪些沒做、為什麼,見文末的「實作結果對照」。**
> 讀這一份時請不要把需求當成現況。

> 給 alice / bob:本文件是 v5 改版的設計北極星,與 #93 舊共識衝突之處**以本文件為準**
> (例:舊共識「禁閃爍/禁全域效果」被 scanlines 與 glitch 推翻)。
> 唯一不可妥協的例外:Accessibility 段落(contrast、reduced-motion)永遠優先。
> 你們對本文件的任何質疑照常提出 — 質疑歸質疑,最終取捨由老闆與 dev 定。

## v5 需求清單(老闆指定)

1. **泡泡式對話**(推翻舊共識的「條列卡片」— 設計問題:多 agent 場景誰靠右?頭像溝怎麼排?)
2. **頭像有照片**,點擊出**彈窗顯示個人資訊**(資料來源開放設計:member registry、統計數據等)
3. **Cyberpunk 風格 scrollbar**
4. **背景粒子點連線 JS 動效**(canvas particle network,注意效能與 prefers-reduced-motion)
5. **RWD 完整支援**
6. CSS 盡量走 **tailwind 式模組化 utility** 的寫法
7. 參考一般聊天室該有的元素,找出我們還缺什麼

---

## 1. Design Philosophy

**Core Principles**: "High-Tech, Low-Life." The aesthetic is a digital dystopia colliding with a high-tech noir reality. It captures the tension between advanced technology and societal decay—a world of underground hackers, neon-drenched megacities, and corrupted data streams. This isn't a clean, utopian future; it's gritty, imperfect, and palpably dangerous. Every pixel should feel like it's being rendered on a malfunctioning CRT monitor in a rain-soaked Tokyo alley or a rogue terminal in a subterranean bunker.

**The Vibe**: Dangerous, electric, rebellious, and aggressively futuristic-retro. It draws heavily from the visual language of 80s sci-fi (Blade Runner, Akira) and hacker culture (The Matrix, Ghost in the Shell). The interface should feel *alive* and volatile—buzzing with digital energy, glitching with data corruption, and pulsing with raw power. It's not just a website; it's a hacked feed, a forbidden interface, a window into the sprawl.

**The Tactile Experience**:
- **Imperfect Technology**: Embrace the artifacts of analog-to-digital conversion. Scanlines, chromatic aberration (RGB splitting), and signal noise are not bugs; they are features. The UI should feel like it's struggling to contain the data it displays.
- **The Void vs. The Light**: The background isn't just dark; it's a void. Against this absolute blackness, neon light (cyan, magenta, acid green) doesn't just color elements—it *illuminates* them. Light sources should feel physical, casting glows and shadows that define the hierarchy.
- **Industrial Brutalism**: Shapes are hard, angular, and utilitarian. Chamfered corners (45-degree cuts) replace friendly rounded rectangles. Borders are technical and precise, resembling blueprints or HUD (Heads-Up Display) schematics rather than decorative frames.

**Visual Signatures That Make This Unforgettable**:
- **Chromatic Aberration**: RGB color splitting on text and elements (red/cyan offset shadows) to simulate lens distortion or signal interference.
- **Scanlines**: Subtle horizontal line overlays mimicking the refresh rate of old CRT monitors, adding texture and unifying the composition.
- **Glitch Effects**: Intentional "corruption" via clip-path animations, skewed transforms, and flickering text that suggests an unstable connection or a hacked system.
- **Neon Glow**: Text and borders that literally glow with intense, multi-layered box-shadow/text-shadow stacking, creating a "light saber" or "neon sign" effect against the dark background.
- **Corner Cuts**: Chamfered/clipped corners on cards and buttons creating a militaristic, tech-panel aesthetic.
- **Circuit Patterns**: Decorative SVG backgrounds resembling PCB traces or data highways, suggesting the underlying hardware.

## 2. Design Token System

### Colors (Dark Mode - Mandatory)

```
background:          #0a0a0f      // Deep void black with slight blue undertone
foreground:          #e0e0e0      // Primary text, not pure white
card:                #12121a      // Card background, deep purple-black
muted:               #1c1c2e      // UI chrome/elevated backgrounds
mutedForeground:     #6b7280      // Secondary text
accent:              #00ff88      // PRIMARY NEON - Electric green
accentSecondary:     #ff00ff      // SECONDARY NEON - Hot magenta/pink
accentTertiary:      #00d4ff      // TERTIARY NEON - Cyan/electric blue
border:              #2a2a3a      // Subtle borders
input:               #12121a      // Deep input background
ring:                #00ff88      // Focus ring matches accent
destructive:         #ff3366      // Error/danger red-pink
```

(注意:現有 sender 色相系統 — ALICE pink #ff79c6 / BOB cyan #00d4ff / DEV amber / 語意綠獨占 —
與上表如何調和,屬於你們要辯論的題目之一。)

### Typography

- **Headings**: `"Orbitron", "Share Tech Mono", monospace`
- **Body**: `"JetBrains Mono", "Fira Code", "Consolas", monospace`
- **Accent/Labels**: `"Share Tech Mono", monospace`
- H1: font-black, uppercase, tracking-widest;Body: tracking-wide, leading-relaxed
- (中文本文是否套 mono 的老問題:#93 的結論是本文保留無襯線 — 要不要沿用,自行辯論)

### Radius & Border

```
radius.none: 0px(預設)/ sm: 2px / base: 4px(僅 input)
chamfer:以 clip-path 切角取代圓角:
clip-path: polygon(0 10px, 10px 0, calc(100% - 10px) 0, 100% 10px,
  100% calc(100% - 10px), calc(100% - 10px) 100%, 10px 100%, 0 calc(100% - 10px));
```

### Shadows & Effects

```css
--box-shadow-neon:      0 0 5px #00ff88, 0 0 10px #00ff8840;
--box-shadow-neon-sm:   0 0 3px #00ff88, 0 0 6px #00ff8830;
--box-shadow-neon-lg:   0 0 10px #00ff88, 0 0 20px #00ff8860, 0 0 40px #00ff8830;
--box-shadow-neon-secondary: 0 0 5px #ff00ff, 0 0 20px #ff00ff60;
--box-shadow-neon-tertiary:  0 0 5px #00d4ff, 0 0 20px #00d4ff60;
```

Chromatic aberration:::before/::after + `text-shadow: -1px 0 #ff00ff` / `-1px 0 #00d4ff` + clip-path 動畫。

### Textures & Patterns

1. **Scanlines**(全頁 overlay,pointer-events: none):
```css
background: repeating-linear-gradient(0deg, transparent, transparent 2px,
  rgba(0,0,0,0.3) 2px, rgba(0,0,0,0.3) 4px);
```
2. **Grid/Circuit**:
```css
background-image:
  linear-gradient(rgba(0,255,136,0.03) 1px, transparent 1px),
  linear-gradient(90deg, rgba(0,255,136,0.03) 1px, transparent 1px);
background-size: 50px 50px;
```
3. Noise 5-10% opacity;4. 角落低透明度 radial gradient mesh。

## 3. Component Stylings

**Buttons**:monospace、uppercase、tracking wider;default = 透明底 + 2px accent 邊 + chamfer,
hover 填滿 accent 反轉文字色 + neon glow;另有 secondary(magenta)、outline、ghost、
glitch(CTA 用,實色底 + chromatic aberration)變體。

**Cards**:default = card 底 + 1px border + chamfer,hover translateY(-1px) + accent 邊 + glow;
**terminal** 變體 = 深底 + 紅黃綠三點裝飾列(訊息泡泡可考慮);
**holographic** 變體 = muted 30% + accent 30% 邊 + backdrop-blur + 四角 corner accents(彈窗可考慮)。

**Inputs**:`>` 前綴 accent 色、chamfer-sm、focus 時 accent 邊 + neon glow。

## 4. Layout / RWD

- Mobile-first;手機單欄、觸控目標 ≥44px、間距足夠
- 手機可降低 glow 強度(效能)
- Scanlines、chamfer、mono metadata、終端語彙在所有尺寸維持

## 5. Motion

```css
transition: all 150ms cubic-bezier(0.4, 0, 0.2, 1);  /* 或 steps(4) 更數位感 */
@keyframes blink { 50% { opacity: 0; } }
@keyframes glitch { 0%,100%{transform:translate(0)} 20%{transform:translate(-2px,2px)}
  40%{transform:translate(2px,-2px)} 60%{transform:translate(-1px,-1px)} 80%{transform:translate(1px,1px)} }
@keyframes rgbShift { 0%,100%{text-shadow:-2px 0 #ff00ff,2px 0 #00d4ff}
  50%{text-shadow:2px 0 #ff00ff,-2px 0 #00d4ff} }
```
Glitch 要「偶發且克制」,不能干擾閱讀。

## 6. Accessibility(不可妥協)

- 文字對比至少 WCAG AA(#00ff88 on #0a0a0f ≈ 7.5:1)
- focus-visible ring 2px accent + glow
- **`prefers-reduced-motion`:停用 glitch/粒子動效,保留靜態 chromatic aberration**

## 7. Implementation Notes

- CSS 變數管 token;scanlines 用 CSS 不用圖;多層 box-shadow 注意 GPU,`will-change` 節制
- glow 在低對比螢幕會糊,要實測

---

## 實作結果對照(2026-07-27 加,由 alice 整理)

> 這一節是**決策紀錄**,不是現況快照 —— 現況會再過期,決策不會。
> 每一條的格式固定:**要求什麼 → 做了什麼 → 為什麼**。
>
> 上面的需求書一個字都沒動。這裡只回答「後來呢」。

### 已經沒有答案的題目:成員專屬色

**要求什麼**:第 2 節結尾把「ALICE pink / BOB cyan / DEV amber / 語意綠獨占 與新色票如何調和」
列為留給 alice 與 bob 辯論的題目之一。

**做了什麼**:那道題**不再適用**。名冊在 2026-07-27 動態化之後,成員顏色改由
`static/util.js` 的 `colorHexOf` 從名字算出來(同名同色,`hsl(hue 60% 62%)`),
`--pink`、`--human` 兩個專屬色變數當天因零使用被刪除。

**為什麼**:那道題的前提是「每個成員有一個指定顏色」,而動態名冊讓這個前提消失了 ——
伺服器開機時並不知道會有誰連進來,無從指定。所以它不是被辯論解決的,
是**被架構改動終結的**。順帶一提,`DEV amber` 裡的 `dev` 也在同一天退役。

至於「語意綠獨占」那條**還活著**,而且守法變了:現在不靠檢查程式碼,
靠配色參數本身 —— 60% 飽和度 / 62% 亮度產不出 `#00ff88` 那種螢光感。
說明在 `util.js` 的 `colorHexOf` 下方。

### 「tailwind 式模組化 utility」的下場

**要求什麼**:v5 需求清單第 6 條 —— CSS 盡量走 tailwind 式的模組化 utility 寫法。

**做了什麼**:做了一個 utility 層(`styles.css` 第 2 區)。2026-07-27 從裡面刪掉了
**四個從來沒有被套用過**的 class（`.mono-label`、`.chamfer`、`.glow-green`、`.glow-cyan`),
現在只剩 `.mono` 與 `.chamfer-sm` 兩個是活的。

**為什麼**:不是沒照做,是**照做之後發現這個專案的規模撐不起那個寫法**。
utility-first 的效益來自「同一組視覺效果在幾十處被組合」,而這個介面只有五個元件 ——
直接寫在語意 class 裡比先造工具類再組合更短、也更好讀。
工具類於是變成「寫的時候覺得之後會用到,真需要時人又直接寫在元件規則裡」的東西。

**保留的部分**:`.mono`(等寬字)與 `.chamfer-sm`(切角)確實在多處重複,那兩個留著。
判準是「重複三次以上才收進 utility 層」,寫在 `styles.css` 檔頭。

### 視覺簽名的落差 —— 已裁決:五項全部不做(2026-07-27)

下面這些是**需求書明確要過、而實作沒有做到或後來拿掉**的。
它們不是死碼(死碼不用問),是**需求方點名要的東西**,所以我們沒有自己決定,
整理成五個問題請 allen 拍板。

> **他的裁決(2026-07-27,聊天室 #790)**:
>
> > 「都不用留 也不用做 東西越單純越簡單其實越好」
>
> **五項全部維持現狀,不補做、不復原。**
>
> ★ 這句話的份量比五個「不做」大:它是**需求方本人把 v5 的視覺野心往回收**。
>   所以下面這張表從「待辦清單」變成「**已經決定不做的事**」——
>   看到它的人請不要好心把它們補上,那不是遺漏,是選擇。

| 需求書寫的 | 現在的實際狀況 | 裁決 |
|---|---|---|
| **Scanlines**(§1 視覺簽名、§2 紋理、開頭「推翻舊共識」的理由,三次點名) | **已停用**(`styles.css` 有註記說明怎麼復原) | **不復原** |
| **Chromatic aberration**(RGB 分離,列為第一號視覺簽名) | 只在標題 `.hdr-title` 有一處**靜態**色差,不是全域效果 | 維持現狀 |
| **字型**:Headings `Orbitron`、Body `JetBrains Mono` | **兩個都沒有載入**。`index.html` 只載 `Share Tech Mono`(用在 metadata 與控制項),中文本文走系統無襯線 | 不補 |
| **`accentSecondary: #ff00ff`**(hot magenta,列為三大 accent 之一) | 只用在標題色差的一半;`--magenta` 變數 2026-07-27 因零使用被刪 | 不加回 |
| **按鈕變體**:secondary / outline / ghost / glitch 四種 | 變體沒做;glitch 只保留為動畫(白名單三處) | 不做 |

**附帶的意義**:少載兩套外部字型,等於少兩個外部請求(見 `index.html` 載入區
關於「第一方 vs 使用者控制」的說明);而 magenta 不回來,配色就維持在
綠 / 青 / 琥珀三色,語意也更好分辨。**單純本身就是一種取捨的結果,不是省略。**

### 至今嚴格執行、沒有妥協的

- **§6 Accessibility**(需求書標「不可妥協」):`prefers-reduced-motion` 時
  `particles.js` **直接移除整張畫布**,不是降級。理由寫在那裡:對前庭系統敏感的人來說,
  會動的背景可能引發不適。
- **Design Token 色票**:`--bg` / `--panel` / `--panel-2` / `--border` / `--green` / `--cyan`
  全部照第 2 節那張表,沒有偏離。
- **切角取代圓角**、**mono 用在 metadata**、**終端語彙**:全介面一致。
