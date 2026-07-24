# DESIGN_SYSTEM — Cyberpunk / Glitch(v5 設計依據,老闆提供)

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
