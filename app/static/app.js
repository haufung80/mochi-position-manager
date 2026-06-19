// Equity charts via ECharts (loaded from a CDN on chart pages). The server embeds a
// payload (see _equity_chart_payload) as JSON in each `<div class="echart" data-chart='…'>`;
// on DOM-ready we find every such div and render it — no per-chart inline <script>.
//
// payload = {series: [{name, color, is_total, last, data: [[epoch_ms, value], …]}], capital_base}.
// Multi-line time series, dark theme, axis-trigger tooltip (replaces the old hand-rolled
// scrub). Legend (with last values) is server-rendered below the chart, so ECharts' own
// legend stays off to avoid duplicating the series names.

function renderEChart(el, payload) {
  if (!el || !window.echarts || !payload || !payload.series || !payload.series.length) return null;
  var chart = echarts.init(el);
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 58, right: 16, top: 12, bottom: 26 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#0f1115', borderColor: '#2a2e38',
      textStyle: { color: '#e6e6e6', fontSize: 11 },
      valueFormatter: function (v) { return v == null ? '—' : '$' + Number(v).toFixed(2); }
    },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#2a2e38' } },
      axisLabel: { color: '#888', fontSize: 9, hideOverlap: true },
      splitLine: { show: false }
    },
    yAxis: {
      type: 'value', scale: true,
      splitLine: { lineStyle: { color: '#1b1f27' } },
      axisLabel: {
        color: '#888', fontSize: 9,
        formatter: function (v) { return '$' + Number(v).toFixed(0); }
      }
    },
    series: payload.series.map(function (s) {
      return {
        name: s.name, type: 'line', data: s.data, showSymbol: false,
        lineStyle: { width: s.is_total ? 2.4 : 1.3, color: s.color, opacity: s.is_total ? 1 : 0.85 },
        itemStyle: { color: s.color },
        z: s.is_total ? 3 : 2,
        emphasis: { focus: 'series' }
      };
    })
  });
  return chart;
}

function initCharts() {
  if (!window.echarts) return;
  var charts = [];
  document.querySelectorAll('.echart[data-chart]').forEach(function (el) {
    var payload;
    try { payload = JSON.parse(el.getAttribute('data-chart')); } catch (e) { return; }
    var chart = renderEChart(el, payload);
    if (chart) charts.push(chart);
  });
  if (charts.length) {
    window.addEventListener('resize', function () {
      charts.forEach(function (c) { c.resize(); });
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initCharts);
} else {
  initCharts();
}
