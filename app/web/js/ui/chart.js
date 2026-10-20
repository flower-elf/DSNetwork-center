// 趋势图：Chart.js（vendor/chart-*.umd.min.js 提供全局变量 Chart）的面积折线图封装。
// 颜色取自 CSS 变量，切换主题后自动重绘。

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// canvas 需要具体颜色值：把 oklch 等写法交给浏览器换算成 rgb。
function resolveColor(value) {
  const probe = document.createElement('span');
  probe.style.color = value;
  document.body.appendChild(probe);
  const rgb = getComputedStyle(probe).color;
  probe.remove();
  return rgb;
}

function withAlpha(rgb, alpha) {
  const m = rgb.match(/rgba?\(([^)]+)\)/);
  if (!m) return rgb;
  const [r, g, b] = m[1].split(/[ ,/]+/).filter(Boolean).slice(0, 3);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

let chartLoader = null;

/** 按需加载 Chart.js；仪表盘与用量页共用一次加载。 */
export function ensureChart() {
  if (globalThis.Chart) return Promise.resolve();
  if (chartLoader) return chartLoader;
  chartLoader = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/web/vendor/chart-4.5.1.umd.min.js';
    script.async = true;
    script.onload = () => {
      if (globalThis.Chart) resolve();
      else reject(new Error('图表库没有初始化'));
    };
    script.onerror = () => reject(new Error('图表库加载失败'));
    document.head.appendChild(script);
  }).catch((err) => {
    chartLoader = null;
    throw err;
  });
  return chartLoader;
}

/**
 * series: [{ label, data: number[], color: '--chart-1' 或颜色值, dashed, fill, format(v), axis, order }]
 * 返回 { update({ labels, series }), destroy() }。
 */
export function createTrendChart(canvas, initial) {
  if (!globalThis.Chart) throw new Error('图表库尚未加载');
  let config = initial;
  let chart = null;

  function build() {
    const muted = resolveColor(cssVar('--muted-foreground'));
    const border = resolveColor(cssVar('--border'));
    const popover = resolveColor(cssVar('--popover'));
    const foreground = resolveColor(cssVar('--foreground'));
    const ctx = canvas.getContext('2d');
    const datasets = config.series.map((s) => {
      const base = resolveColor(s.color.startsWith('--') ? cssVar(s.color) : s.color);
      let background = 'transparent';
      if (s.fill) {
        const gradient = ctx.createLinearGradient(0, 0, 0, canvas.clientHeight || 220);
        gradient.addColorStop(0, withAlpha(base, 0.35));
        gradient.addColorStop(1, withAlpha(base, 0));
        background = gradient;
      }
      return {
        label: s.label,
        data: s.data,
        borderColor: base,
        backgroundColor: background,
        fill: !!s.fill,
        borderWidth: s.dashed ? 1.5 : 2,
        borderDash: s.dashed ? [4, 3] : [],
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0.35,
        yAxisID: s.axis || 'y',
        order: s.order || 0,
      };
    });
    const formatters = Object.fromEntries(config.series.map((s) => [s.label, s.format || ((v) => String(v))]));
    const scales = {
      x: { grid: { display: false }, border: { display: false }, ticks: { color: muted, font: { size: 11 }, maxRotation: 0, autoSkipPadding: 12 } },
      y: { beginAtZero: true, grid: { color: border, drawTicks: false }, border: { display: false, dash: [3, 3] },
        ticks: { color: muted, font: { size: 11 }, padding: 6, precision: 0 } },
    };
    if (config.series.some((s) => s.axis === 'y2')) {
      scales.y2 = { position: 'right', beginAtZero: true, grid: { display: false }, border: { display: false },
        ticks: { color: muted, font: { size: 11 }, padding: 6 } };
    }
    chart = new Chart(canvas, {
      type: 'line',
      data: { labels: config.labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { top: 4, right: 8, left: 0, bottom: 0 } },
        plugins: {
          // 图例只作说明，点击不切换数据集的显示，避免误点后某条线消失。
          legend: { display: config.series.length > 1, position: 'top', align: 'end',
            onClick: () => {}, onHover: () => {},
            labels: { color: muted, boxWidth: 10, boxHeight: 2, font: { size: 11 } } },
          tooltip: {
            backgroundColor: popover, titleColor: foreground, bodyColor: foreground, borderColor: border, borderWidth: 1,
            cornerRadius: 12, padding: 10, titleFont: { size: 12 }, bodyFont: { size: 12 },
            callbacks: { label: (item) => `${item.dataset.label}：${formatters[item.dataset.label](item.parsed.y)}` },
          },
        },
        scales,
      },
    });
  }

  const onTheme = () => { chart.destroy(); build(); };
  build();
  document.addEventListener('wb-theme-change', onTheme);
  return {
    update(next) {
      const sameShape = next.series.length === config.series.length
        && next.series.every((s, i) => s.label === config.series[i].label);
      config = next;
      if (!sameShape) {
        chart.destroy();
        build();
        return;
      }
      // 序列不变时只换数据，轮询刷新不会让整张图重新动画。
      chart.data.labels = next.labels;
      next.series.forEach((s, i) => { chart.data.datasets[i].data = s.data; });
      chart.update('none');
    },
    destroy() {
      document.removeEventListener('wb-theme-change', onTheme);
      chart.destroy();
    },
  };
}
