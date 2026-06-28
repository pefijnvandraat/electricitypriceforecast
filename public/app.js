/* Stroomprijs voorspeller - reads pre-computed static JSON, slices client-side. */
const chart = echarts.init(document.getElementById('chart'), 'dark');
const $ = (id) => document.getElementById(id);
let current = null;

const DAY = 864e5;
const fmt = (v) => (v == null ? '-' : '\u20ac ' + Number(v).toFixed(3));

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

  render();
}

function render() {
  if (!current) return;
  const allin = $('mode').value === 'allin' && current.priced;
  const histDays = +$('hist').value;
  const fcDays = +$('fc').value;
  $('histLabel').textContent = histDays;
  $('fcLabel').textContent = fcDays;

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

  const series = [
    { name: 'Historie', type: 'line', showSymbol: false, data: histPts,
      lineStyle: { width: 1.4, color: '#4aa3ff' }, color: '#4aa3ff' },
  ];
  if (low.length) {
    series.push(
      { name: '_low', type: 'line', stack: 'band', showSymbol: false, data: low,
        lineStyle: { width: 0 }, areaStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
      { name: 'Onzekerheid (p10-p90)', type: 'line', stack: 'band', showSymbol: false, data: range,
        lineStyle: { width: 0 }, areaStyle: { color: 'rgba(255,159,64,.18)' }, silent: true },
    );
  }
  series.push({
    name: 'Voorspelling (p50)', type: 'line', showSymbol: false, data: med,
    lineStyle: { width: 2, color: '#ff9f40' }, color: '#ff9f40',
    markLine: {
      symbol: 'none', silent: true,
      lineStyle: { color: '#8aa0bd', type: 'dashed' },
      data: [{ xAxis: now.toISOString() }],
    },
  });

  chart.setOption({
    grid: { left: 56, right: 18, top: 30, bottom: 40 },
    legend: { top: 0, textStyle: { color: '#8aa0bd' },
      data: ['Historie', 'Voorspelling (p50)', 'Onzekerheid (p10-p90)'] },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => fmt(v),
    },
    xAxis: { type: 'time', axisLabel: { color: '#8aa0bd' } },
    yAxis: { type: 'value', name: '\u20ac/kWh', scale: true,
      axisLabel: { color: '#8aa0bd', formatter: (v) => v.toFixed(2) },
      splitLine: { lineStyle: { color: '#26324a' } } },
    series,
  }, true);

  const bits = [];
  bits.push('Gegenereerd: ' + new Date(now).toLocaleString('nl-NL'));
  if (current.mae_eur_mwh != null) bits.push('Backtest MAE: \u20ac ' + current.mae_eur_mwh + '/MWh');
  if (current.priced && current.taxes)
    bits.push('All-in = (kale + opslag + energiebelasting) \u00d7 1,' + current.taxes.btw_pct +
      '; heffingskorting \u20ac ' + current.taxes.belastingvermindering_eur_per_jaar + '/jaar');
  if (current.error) bits.push('\u26a0 ' + current.error);
  bits.push('Bronnen: ENTSO-E, Open-Meteo, Yahoo Finance (TTF/KRBN).');
  $('meta').textContent = bits.join('  \u2014  ');
}

['zone'].forEach(id => $(id).addEventListener('change', e => loadZone(e.target.value)));
['mode', 'hist', 'fc'].forEach(id => $(id).addEventListener('input', render));
window.addEventListener('resize', () => chart.resize());
loadMeta();
