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
    if (d.getHours() === 0 && d.getMinutes() === 0) return d.getDate() + ' ' + months[d.getMonth()];
    return pad(d.getHours()) + ':' + pad(d.getMinutes());
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

  // forecast
  const f = current.forecast;
  const sfx = allin ? '_allin_kwh' : '_wholesale_kwh';
  const med = [], low = [], range = [], p90 = [];
  if (f) {
    f.time.forEach((t, i) => {
      if (new Date(t) > fcCut) return;
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
      if (new Date(t) < histCut) return;
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
  series.push({
    name: T('lfc'), type: 'line', showSymbol: false, data: med,
    lineStyle: { width: 2, color: fcCol }, color: fcCol,
    markLine: {
      symbol: 'none', silent: true,
      lineStyle: { color: muted, type: 'dashed' },
      label: { show: true, color: muted, formatter: (p) => axisFmt(p.value) },
      data: [{ xAxis: now.toISOString() }],
    },
  });

  chart.setOption({
    grid: { left: 56, right: 18, top: 30, bottom: 40 },
    legend: { top: 0, textStyle: { color: muted },
      data: [T('lhist'), T('lval'), T('lfc'), T('lband')] },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => fmt(v),
      axisPointer: { label: { formatter: (p) => tipFmt(p.value) } },
      formatter: (params) => {
        if (!params.length) return '';
        let html = tipFmt(params[0].axisValue) + '<br/>';
        params.forEach((p) => {
          if (!p.seriesName || p.seriesName[0] === '_') return;
          html += p.marker + ' ' + p.seriesName + ': ' + fmt(p.value[1]) + '<br/>';
        });
        return html;
      },
    },
    xAxis: { type: 'time', axisLabel: { color: muted, hideOverlap: true, formatter: axisFmt },
      splitLine: { show: false }, minInterval: 3600 * 1000, maxInterval: 12 * 3600 * 1000 },
    yAxis: { type: 'value', name: '\u20ac/kWh', scale: true,
      axisLabel: { color: muted, formatter: (v) => v.toFixed(2) },
      splitLine: { lineStyle: { color: lineCol } } },
    series,
  }, true);

  const loc = lang;
  const bits = [];
  bits.push(T('gen') + ': ' + new Date(now).toLocaleString(loc));
  bits.push(T('psrc'));
  if (current.mae_eur_mwh != null) bits.push('MAE: \u20ac ' + current.mae_eur_mwh + '/MWh');
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
window.addEventListener('resize', () => chart && chart.resize());
initChart();
buildPickers();
loadMeta();
