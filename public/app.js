/* Stroomprijs voorspeller - reads pre-computed static JSON, slices client-side. */
const $ = (id) => document.getElementById(id);
let chart = null;
let current = null;
let lang = 'en';

const DAY = 864e5;
const fmt = (v) => (v == null ? '-' : '\u20ac ' + Number(v).toFixed(3));
const T = (k) => (I18N[lang] && I18N[lang][k]) || I18N.en[k] || k;

function initChart() {
  const theme = document.documentElement.getAttribute('data-theme') === 'light' ? null : 'dark';
  if (chart) chart.dispose();
  chart = echarts.init(document.getElementById('chart'), theme);
}

/* ---- theme ---- */
function applyTheme(mode) {
  document.documentElement.setAttribute('data-theme', mode);
  $('themeIcon').textContent = mode === 'light' ? '\u2600\uFE0F' : '\uD83C\uDF19';
  try { localStorage.setItem('theme', mode); } catch (e) {}
  initChart();
  if (current) render();
}

/* ---- i18n ---- */
function applyLang(code) {
  lang = I18N[code] ? code : 'en';
  try { localStorage.setItem('lang', lang); } catch (e) {}
  document.documentElement.setAttribute('lang', lang);
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    el.textContent = T(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.setAttribute('title', T(el.getAttribute('data-i18n-title')));
  });
  if (current) render();
}

function buildPickers() {
  const sel = $('lang');
  Object.keys(LANG_NAMES).sort((a, b) => LANG_NAMES[a].localeCompare(LANG_NAMES[b]))
    .forEach((code) => {
      const o = document.createElement('option');
      o.value = code; o.textContent = LANG_NAMES[code];
      sel.appendChild(o);
    });
  let savedLang = 'en', savedTheme = 'dark';
  try {
    savedLang = localStorage.getItem('lang') ||
      (navigator.language || 'en').slice(0, 2);
    savedTheme = localStorage.getItem('theme') || 'dark';
  } catch (e) {}
  if (!I18N[savedLang]) savedLang = 'en';
  sel.value = savedLang;
  applyTheme(savedTheme === 'light' ? 'light' : 'dark');
  applyLang(savedLang);
  sel.addEventListener('change', (e) => applyLang(e.target.value));
  $('themeBtn').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    applyTheme(cur === 'light' ? 'dark' : 'light');
  });
}

async function loadMeta() {
  let meta;
  try { meta = await fetch('data/meta.json').then(r => r.json()); }
  catch { $('meta').textContent = 'Nog geen data gepubliceerd. De CI-run moet eerst draaien (en de ENTSOE_TOKEN secret moet gezet zijn).'; return; }
  const sel = $('zone');
  sel.innerHTML = '';
  (meta.zones || []).forEach(z => {
    const o = document.createElement('option');
    o.value = z.code; o.textContent = z.name || z.code;
    sel.appendChild(o);
  });
  if (sel.value) await loadZone(sel.value);
}

async function loadZone(code) {
  try { current = await fetch('data/' + code + '.json').then(r => r.json()); }
  catch { $('meta').textContent = 'Kon zone-data niet laden.'; return; }

  const mode = $('mode');
  mode.querySelector('option[value="allin"]').disabled = !current.priced;
  if (!current.priced) mode.value = 'wholesale';

  $('hist').max = current.history_days || 730;
  $('fc').max = current.horizon_days || 16;
  if (+$('hist').value > +$('hist').max) $('hist').value = $('hist').max;
  if (+$('fc').value > +$('fc').max) $('fc').value = $('fc').max;

  // ENTSO-E validation toggle: only show when validation data is present.
  const wrap = $('entsoeWrap');
  const hasEntsoe = !!(current.entsoe_available && current.history &&
                       (current.history.entsoe_wholesale_kwh || current.history.entsoe_allin_kwh));
  wrap.hidden = !hasEntsoe;
  if (!hasEntsoe) $('entsoe').checked = false;   // default off

  render();
}

// EV charging optimizer.
// - No target: per day, the cheapest contiguous block of the hours needed.
// - With target datetime: pick the cheapest hours (anywhere up to the target)
//   that together reach the requested charge for the lowest overall cost.
// Operates on the forecast p50 series (`med`). Returns chart markAreas.
function computeEvWindows(med, fcCut) {
  const res = document.getElementById('evResult');
  if (!document.getElementById('evOn').checked || !med || !med.length) {
    if (res) res.innerHTML = '';
    return [];
  }
  const batt = Math.max(0, +document.getElementById('evBatt').value || 0);
  const pct = Math.max(0, Math.min(100, +document.getElementById('evPct').value || 0));
  const pow = Math.max(0.1, +document.getElementById('evPow').value || 0.1);
  const energy = batt * pct / 100;                         // kWh to add
  const hoursNeeded = Math.max(1, Math.ceil(energy / pow));
  const energyPerHour = energy / hoursNeeded;

  const targetRaw = document.getElementById('evTarget').value;
  const target = targetRaw ? new Date(targetRaw) : null;

  const pad = (n) => String(n).padStart(2, '0');
  const hh = (d) => pad(d.getHours()) + ':' + pad(d.getMinutes());
  const dlabel = (d) => d.toLocaleDateString(lang, { weekday: 'short', day: 'numeric', month: 'short' });

  let pts = med.map(([iso, v]) => ({ t: new Date(iso), iso, v }))
    .filter((p) => isFinite(p.v) && (!fcCut || p.t <= fcCut));

  // ---- TARGET MODE: cheapest hours until the deadline ----
  if (target) {
    const avail = pts.filter((p) => p.t <= target).sort((a, b) => a.t - b.t);
    if (res) {
      if (!avail.length) {
        res.innerHTML = '<div class="ev-head ev-warn">' + T('ev_target_past') + '</div>';
        return [];
      }
    }
    const w = Math.min(hoursNeeded, avail.length);
    const enough = avail.length >= hoursNeeded;
    // pick the w cheapest hours, then restore chronological order
    const chosen = [...avail].sort((a, b) => a.v - b.v).slice(0, w)
      .sort((a, b) => a.t - b.t);
    // group consecutive picked hours into windows
    const windows = [], rows = [];
    let i = 0;
    while (i < chosen.length) {
      let j = i;
      while (j + 1 < chosen.length &&
             chosen[j + 1].t - chosen[j].t === 3600e3) j++;
      const sP = chosen[i], eP = chosen[j];
      const end = new Date(eP.t.getTime() + 3600e3);
      windows.push({ startIso: sP.iso, endIso: end.toISOString() });
      let sum = 0; for (let k = i; k <= j; k++) sum += chosen[k].v;
      rows.push({ start: sP.t, end, hours: j - i + 1, avg: sum / (j - i + 1) });
      i = j + 1;
    }
    const totalCost = chosen.reduce((s, p) => s + p.v * energyPerHour, 0);
    if (res) {
      let html = '<div class="ev-head">' + T('ev_by') + ' ' + dlabel(target) + ' ' + hh(target) +
        ' &middot; <strong>' + w + ' ' + T('ev_hours') + '</strong> &middot; ' + energy.toFixed(1) + ' kWh' +
        ' &middot; ' + T('ev_total') + ' <strong>' + fmt(totalCost) + '</strong></div>';
      if (!enough) html += '<div class="ev-warn">' + T('ev_short') + '</div>';
      html += '<ul class="ev-list">';
      rows.forEach((r) => {
        html += '<li><span class="ev-day">' + dlabel(r.start) + '</span>' +
          '<span class="ev-win">' + hh(r.start) + '\u2013' + hh(r.end) + '</span>' +
          '<span class="ev-cost">' + r.hours + ' ' + T('ev_hours') + ' &middot; \u2300 ' + fmt(r.avg) + '</span></li>';
      });
      html += '</ul>';
      res.innerHTML = html;
    }
    return windows;
  }

  // ---- DEFAULT MODE: cheapest contiguous block per day ----
  const byDay = new Map();
  pts.forEach((p) => {
    const k = p.t.getFullYear() + '-' + p.t.getMonth() + '-' + p.t.getDate();
    if (!byDay.has(k)) byDay.set(k, []);
    byDay.get(k).push(p);
  });

  const windows = [], rows = [];
  [...byDay.values()].forEach((dayPts) => {
    dayPts.sort((a, b) => a.t - b.t);
    const n = dayPts.length;
    const w = Math.min(hoursNeeded, n);
    if (!w) return;
    let best = Infinity, bestI = 0;
    for (let i = 0; i + w <= n; i++) {
      let sum = 0;
      for (let j = i; j < i + w; j++) sum += dayPts[j].v;
      if (sum < best) { best = sum; bestI = i; }
    }
    const sP = dayPts[bestI], eP = dayPts[bestI + w - 1];
    const end = new Date(eP.t.getTime() + 3600e3);
    windows.push({ startIso: sP.iso, endIso: end.toISOString() });
    rows.push({ day: sP.t, start: sP.t, end, avg: best / w, cost: (best / w) * energy });
  });

  if (res) {
    let html = '<div class="ev-head">' + T('ev_need') + ': <strong>' + hoursNeeded +
      ' ' + T('ev_hours') + '</strong> &middot; ' + energy.toFixed(1) + ' kWh</div><ul class="ev-list">';
    rows.forEach((r) => {
      html += '<li><span class="ev-day">' + dlabel(r.day) + '</span>' +
        '<span class="ev-win">' + hh(r.start) + '\u2013' + hh(r.end) + '</span>' +
        '<span class="ev-cost">\u2300 ' + fmt(r.avg) + ' &middot; ' + fmt(r.cost) + '</span></li>';
    });
    html += '</ul>';
    res.innerHTML = html;
  }
  return windows;
}

function render() {
  if (!current) return;
  const allin = $('mode').value === 'allin' && current.priced;
  const histDays = +$('hist').value;
  const fcDays = +$('fc').value;
  $('histLabel').textContent = histDays;
  $('fcLabel').textContent = fcDays;

  const pad = (n) => String(n).padStart(2, '0');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const axisFmt = (val) => {
    const d = new Date(val);
    if (d.getHours() === 0 && d.getMinutes() === 0)
      return '{d|' + d.getDate() + ' ' + months[d.getMonth()] + '}';   // bold date
    return pad(d.getHours()) + ':' + pad(d.getMinutes());              // normal hour
  };
  const tipFmt = (val) => {
    const d = new Date(val);
    return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear() +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  };

  const now = current.generated_at ? new Date(current.generated_at) : new Date();
  const histCut = new Date(now.getTime() - histDays * DAY);
  const fcCut = new Date(now.getTime() + fcDays * DAY);

  // history
  const h = current.history || { time: [] };
  const hKey = allin ? 'allin_kwh' : 'wholesale_kwh';
  const histPts = [];
  (h.time || []).forEach((t, i) => {
    const d = new Date(t);
    if (d >= histCut) { const v = (h[hKey] || [])[i]; if (v != null) histPts.push([t, v]); }
  });

  // forecast (plot ALL points; the visible horizon is controlled by xAxis max / zoom)
  const f = current.forecast;
  const sfx = allin ? '_allin_kwh' : '_wholesale_kwh';
  const med = [], low = [], range = [], p90 = [];
  if (f) {
    f.time.forEach((t, i) => {
      const v50 = (f['p50' + sfx] || [])[i];
      const v10 = (f['p10' + sfx] || [])[i];
      const v90 = (f['p90' + sfx] || [])[i];
      if (v50 != null) med.push([t, v50]);
      if (v10 != null && v90 != null) {
        low.push([t, v10]);
        range.push([t, +(v90 - v10).toFixed(4)]);
        p90.push([t, v90]);
      }
    });
  }

  const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim() || '#8aa0bd';
  const lineCol = getComputedStyle(document.documentElement).getPropertyValue('--line').trim() || '#26324a';
  const histCol = getComputedStyle(document.documentElement).getPropertyValue('--hist').trim() || '#4aa3ff';
  const fcCol = getComputedStyle(document.documentElement).getPropertyValue('--fc').trim() || '#ff9f40';
  const bandCol = getComputedStyle(document.documentElement).getPropertyValue('--band').trim() || 'rgba(255,159,64,.18)';
  const ink = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim() || '#e6ecf5';

  const series = [
    { name: T('lhist'), type: 'line', showSymbol: false, data: histPts,
      lineStyle: { width: 1.4, color: histCol }, color: histCol },
  ];

  // ENTSO-E validation overlay (only when toggle on and data present)
  const showEntsoe = $('entsoe').checked && !$('entsoeWrap').hidden;
  if (showEntsoe) {
    const eKey = allin ? 'entsoe_allin_kwh' : 'entsoe_wholesale_kwh';
    const ePts = [];
    (h.time || []).forEach((t, i) => {
      const d = new Date(t);
      if (d < histCut) return;
      const v = (h[eKey] || [])[i];
      if (v != null) ePts.push([t, v]);
    });
    if (ePts.length) {
      series.push({
        name: T('lval'), type: 'line', showSymbol: false, data: ePts,
        lineStyle: { width: 1.2, color: '#46d39a', type: 'dashed' }, color: '#46d39a',
      });
    }
  }
  if (low.length) {
    series.push(
      { name: '_low', type: 'line', stack: 'band', showSymbol: false, data: low,
        lineStyle: { width: 0 }, areaStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
      { name: T('lband'), type: 'line', stack: 'band', showSymbol: false, data: range,
        lineStyle: { width: 0 }, areaStyle: { color: bandCol }, silent: true },
    );
  }
  // ---- EV charging window optimizer (client-side, on the forecast p50) ----
  const evWindows = computeEvWindows(med, fcCut);

  series.push({
    name: T('lfc'), type: 'line', showSymbol: false, data: med,
    lineStyle: { width: 2, color: fcCol }, color: fcCol,
    markLine: {
      symbol: 'none', silent: true,
      lineStyle: { color: muted, type: 'dashed' },
      label: { show: true, color: muted, formatter: () => pad(now.getHours()) + ':' + pad(now.getMinutes()) },
      data: [{ xAxis: now.toISOString() }],
    },
    markArea: evWindows.length ? {
      silent: true,
      itemStyle: { color: 'rgba(70,211,154,0.18)' },
      label: { show: false },
      data: evWindows.map((w) => [{ xAxis: w.startIso }, { xAxis: w.endIso }]),
    } : undefined,
  });

  // Visible window: history left edge .. forecast horizon (controlled by the slider),
  // but the user can mouse/touch-zoom the FORECAST horizon between 1 day and the max.
  const fcEnd = f && f.time.length ? new Date(f.time[f.time.length - 1]) : fcCut;
  const winStart = histCut;
  const isMobile = window.innerWidth < 640;

  chart.setOption({
    grid: { left: 48, right: 14, top: isMobile ? 54 : 30, bottom: 78 },
    legend: { top: 0, textStyle: { color: muted }, type: 'scroll',
      data: [T('lhist'), T('lval'), T('lfc'), T('lband')] },
    tooltip: {
      trigger: 'axis', confine: true,
      valueFormatter: (v) => fmt(v),
      axisPointer: { label: { formatter: (p) => tipFmt(p.value) } },
      formatter: (params) => {
        if (!params.length) return '';
        const detail = $('detail').checked;
        const bandName = T('lband');
        let html = tipFmt(params[0].axisValue) + '<br/>';
        params.forEach((p) => {
          if (!p.seriesName || p.seriesName[0] === '_') return;
          // Default: price only (history / forecast p50 / ENTSO-E). Detail adds the band.
          if (!detail && p.seriesName === bandName) return;
          html += p.marker + ' ' + p.seriesName + ': ' + fmt(p.value[1]) + '<br/>';
        });
        return html;
      },
    },
    dataZoom: [
      // wheel / pinch zoom; throttled so it does not jump too fast
      { type: 'inside', startValue: winStart, endValue: fcCut,
        minValueSpan: 12 * 3600 * 1000, zoomLock: false, zoomOnMouseWheel: true,
        moveOnMouseMove: false, moveOnMouseWheel: false, throttle: 90 },
      // bottom slider: tall, with large easy-to-grab handles
      { type: 'slider', startValue: winStart, endValue: fcCut,
        height: 34, bottom: 16, minValueSpan: 12 * 3600 * 1000, throttle: 90,
        borderColor: lineCol, fillerColor: bandCol, backgroundColor: 'transparent',
        dataBackground: { lineStyle: { color: muted, opacity: 0.4 },
          areaStyle: { color: muted, opacity: 0.12 } },
        handleSize: '160%', moveHandleSize: 9,
        handleStyle: { color: fcCol, borderColor: fcCol },
        textStyle: { color: muted, fontSize: isMobile ? 10 : 12 },
        labelFormatter: (v) => { const d = new Date(v); return d.getDate() + ' ' + months[d.getMonth()]; } },
    ],
    xAxis: { type: 'time', min: winStart, max: fcEnd,
      axisLabel: { color: muted, hideOverlap: true, formatter: axisFmt, fontSize: isMobile ? 10 : 12,
        rich: { d: { fontWeight: 'bold', color: ink, fontSize: isMobile ? 11 : 13 } } },
      splitLine: { show: false }, minInterval: 3600 * 1000, maxInterval: 12 * 3600 * 1000 },
    yAxis: { type: 'value', name: '\u20ac/kWh', scale: true,
      axisLabel: { color: muted, formatter: (v) => v.toFixed(2), fontSize: isMobile ? 10 : 12 },
      splitLine: { lineStyle: { color: lineCol } } },
    series,
  }, true);

  const loc = lang;
  const ds = $('datastamp');
  if (ds) ds.textContent = T('data_of') + ': ' + new Date(now).toLocaleString(loc,
    { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });

  const bits = [];
  bits.push(T('gen') + ': ' + new Date(now).toLocaleString(loc, { hour12: false }));
  bits.push(T('psrc'));
  if (current.mae_eur_mwh != null) bits.push('MAE: \u20ac ' + current.mae_eur_mwh + '/MWh');
  const lrn = current.learning;
  if (lrn) {
    let p = T('learn');
    if (lrn.peak_correction) p += ': ' + T('learn_peak') + ' ' +
      (lrn.peak_correction > 0 ? '+' : '') + lrn.peak_correction + ' \u20ac/MWh';
    if (lrn.n_samples) p += ' \u00b7 ' + lrn.n_samples + ' ' + T('learn_eval') +
      (lrn.coverage != null ? ' \u00b7 ' + Math.round(lrn.coverage * 100) + '% ' + T('learn_cov') : '');
    bits.push(p);
  }
  if (current.resid_demand) bits.push(T('resid'));
  if (current.priced && current.taxes)
    bits.push('All-in = (kale + opslag + energiebelasting) \u00d7 1,' + current.taxes.btw_pct +
      '; heffingskorting \u20ac ' + current.taxes.belastingvermindering_eur_per_jaar + '/jaar');
  if (current.error) bits.push('\u26a0 ' + current.error);
  bits.push('Energy-Charts, ENTSO-E, Open-Meteo, Yahoo Finance (TTF/KRBN).');
  $('meta').textContent = bits.join('  \u2014  ');
}

['zone'].forEach(id => $(id).addEventListener('change', e => loadZone(e.target.value)));
['mode', 'hist', 'fc'].forEach(id => $(id).addEventListener('input', render));
$('entsoe').addEventListener('change', render);
$('detail').addEventListener('change', render);
$('evOn').addEventListener('change', () => { $('evFields').hidden = !$('evOn').checked; render(); });
['evBatt', 'evPct', 'evPow', 'evTarget'].forEach(id => $(id).addEventListener('input', render));
window.addEventListener('resize', () => { if (chart) chart.resize(); if (current) render(); });
initChart();
buildPickers();
loadMeta();
