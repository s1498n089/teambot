/* ═══════════════════════════════════════════════════════════════════════════
   particles.js — 背景那些會飄的小點

   畫面最底層有一張畫布,上面飄著一些小綠點,靠得近的兩點之間會連一條線,
   滑鼠靠近時點會被輕輕吸過去。純粹是視覺效果,跟聊天功能完全無關 ——
   所以獨立成一個檔案,想關掉或換掉都只要動這裡。

   三件為了「不吃掉使用者電腦資源」而做的事:
   1. 點的數量跟著視窗大小算,並且封頂;手機再減半
   2. 高解析螢幕最多只放大到 1.5 倍(再高肉眼看不出差別,但很耗效能)
   3. 分頁切到背景就完全停住,切回來才繼續

   另外,如果使用者在系統設定裡表明「不想看動畫」(prefers-reduced-motion),
   這裡會直接把畫布移除 —— 那不只是省效能,對前庭系統敏感的人來說,
   會動的背景可能引發不適。

   寫法約定同其他檔案:一行一件事、不用箭頭簡寫、名字寫完整。
   ═══════════════════════════════════════════════════════════════════════ */

function startParticles() {
  const canvas = document.getElementById("particles");

  // 使用者表明不想看動畫 —— 整張畫布拿掉,只留 CSS 畫的靜態格線
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    canvas.remove();
    return;
  }

  const context = canvas.getContext("2d");

  // 高解析螢幕要畫更多像素才不會糊,但無上限地放大很耗效能,所以封在 1.5 倍
  const pixelRatio = Math.min(devicePixelRatio || 1, 1.5);

  let width = 0;
  let height = 0;
  let points = [];
  let animationId = null;

  // 滑鼠位置。一開始放在畫面外很遠的地方,表示「還沒進來」
  const mouse = { x: -10000, y: -10000 };

  /**
   * 造一個新的點:隨機位置、隨機的緩慢移動方向。
   * @returns {object} { x, y, vx, vy } —— vx/vy 是每一格要移動多少
   */
  function createPoint() {
    return {
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * 0.35,
      vy: (Math.random() - 0.5) * 0.35,
    };
  }

  /**
   * 視窗大小改變時重新設定畫布,並依面積決定要幾個點。
   * 視窗變小時多的點會被丟掉,變大時補新的 —— 所以密度看起來一直差不多。
   */
  function resize() {
    width = innerWidth;
    height = innerHeight;

    canvas.width = width * pixelRatio;
    canvas.height = height * pixelRatio;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);

    // 面積越大點越多,但最多 80 個
    let target = Math.min(80, Math.floor((width * height) / 18000));

    // 手機螢幕小、效能也弱,減半
    if (width < 560) {
      target = Math.floor(target / 2);
    }

    while (points.length < target) {
      points.push(createPoint());
    }
    points.length = target;      // 多的直接砍掉
  }

  /**
   * 讓一個點被滑鼠輕輕吸引。距離太遠就不管它。
   * @param {object} point 要處理的點
   */
  function applyMouseAttraction(point) {
    const dx = mouse.x - point.x;
    const dy = mouse.y - point.y;
    const distanceSquared = (dx * dx) + (dy * dy);

    // 150 像素以內才有感覺(150 的平方是 22500)。
    // 這裡比較「距離的平方」而不是距離,是為了省下開根號 —— 每一格每個點都要算,
    // 省下來的量很可觀。距離為 0 時除法會爆掉,所以也排除。
    const withinRange = distanceSquared < 22500 && distanceSquared > 1;

    if (!withinRange) {
      return;
    }

    const distance = Math.sqrt(distanceSquared);
    point.vx = point.vx + (dx / distance) * 0.015;
    point.vy = point.vy + (dy / distance) * 0.015;
  }

  /**
   * 把速度限制在一個範圍內,免得被滑鼠吸久了越跑越快。
   * @param {number} speed 目前速度
   * @returns {number} 限制後的速度
   */
  function clampSpeed(speed) {
    return Math.max(-0.6, Math.min(0.6, speed));
  }

  /** 移動所有的點,碰到邊界就彈回來。 */
  function movePoints() {
    for (const point of points) {
      applyMouseAttraction(point);

      point.vx = clampSpeed(point.vx);
      point.vy = clampSpeed(point.vy);

      point.x = point.x + point.vx;
      point.y = point.y + point.vy;

      if (point.x < 0 || point.x > width) {
        point.vx = point.vx * -1;
      }
      if (point.y < 0 || point.y > height) {
        point.vy = point.vy * -1;
      }
    }
  }

  /** 把每個點畫成一個 2x2 的小方塊。 */
  function drawPoints() {
    context.fillStyle = "rgba(0, 255, 136, 0.5)";

    for (const point of points) {
      context.fillRect(point.x - 1, point.y - 1, 2, 2);
    }
  }

  /**
   * 把靠得夠近的兩點連起來,越近的線越明顯。
   * 內層迴圈從 i + 1 開始,是為了每一對只畫一次(畫兩次會讓線看起來比較濃)。
   */
  function drawConnections() {
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i];
        const b = points[j];

        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distanceSquared = (dx * dx) + (dy * dy);

        // 120 像素以內才連線(120 的平方是 14400)
        if (distanceSquared >= 14400) {
          continue;
        }

        const distance = Math.sqrt(distanceSquared);
        const opacity = (1 - distance / 120) * 0.22;   // 越遠越淡

        context.strokeStyle = `rgba(0, 212, 255, ${opacity})`;
        context.beginPath();
        context.moveTo(a.x, a.y);
        context.lineTo(b.x, b.y);
        context.stroke();
      }
    }
  }

  /** 畫一格,然後預約下一格。 */
  function step() {
    context.clearRect(0, 0, width, height);
    movePoints();
    drawPoints();
    drawConnections();
    animationId = requestAnimationFrame(step);
  }

  addEventListener("resize", resize);

  addEventListener("mousemove", function (event) {
    mouse.x = event.clientX;
    mouse.y = event.clientY;
  });

  // 分頁被切到背景時完全停下來 —— 沒人在看的動畫不該繼續吃 CPU
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      cancelAnimationFrame(animationId);
      animationId = null;
      return;
    }

    if (!animationId) {
      step();
    }
  });

  resize();
  step();
}

startParticles();
