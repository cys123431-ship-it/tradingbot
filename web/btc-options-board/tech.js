(function () {
  'use strict';

  var TFS = ['15m', '30m', '1h', '4h', '1d'];
  var TABS = [
    { v: 'all', name: '통합' },
    { v: '15m', name: '15분' },
    { v: '30m', name: '30분' },
    { v: '1h', name: '1시간' },
    { v: '4h', name: '4시간' },
    { v: '1d', name: '1일' }
  ];
  var TF_NAME = { '15m': '15분', '30m': '30분', '1h': '1시간', '4h': '4시간', '1d': '1일', '1w': '1주', '1M': '1개월' };
  var LS_KEY = 'btcopt.tf';
  var BASE = 'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT';

  var state = {
    tf: 'all',
    loading: true,
    failed: false,
    rates: null,
    combined: null,
    pivots: null
  };

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }

  function esc() { return false; } // unused placeholder

  function fetchKlines(iv, limit) {
    var url = BASE + '&interval=' + encodeURIComponent(iv) + '&limit=' + limit;
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (rows) {
      var out = { open: [], high: [], low: [], close: [], volume: [] };
      for (var i = 0; i < rows.length; i++) {
        var k = rows[i];
        out.open.push(parseFloat(k[1]));
        out.high.push(parseFloat(k[2]));
        out.low.push(parseFloat(k[3]));
        out.close.push(parseFloat(k[4]));
        out.volume.push(parseFloat(k[5]));
      }
      return out;
    });
  }

  function signed2(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var s = v.toFixed(2);
    if (v > 0) s = '+' + s;
    return s;
  }

  function num2(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return v.toFixed(2);
  }

  function num1(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return v.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function toneOf(score) {
    if (score < -0.1) return 'down';
    if (score > 0.1) return 'up';
    return 'neutral';
  }

  function actLabel(action) {
    if (action === 1) return '매수';
    if (action === -1) return '매도';
    return '중립';
  }

  function actTone(action) {
    if (action === 1) return 'up';
    if (action === -1) return 'down';
    return 'neutral';
  }

  function interpretSentence(oscScore, maScore) {
    if (maScore > 0.1 && oscScore < -0.1) return '추세는 위인데 단기 힘이 꺾이는 중';
    if (maScore < -0.1 && oscScore > 0.1) return '추세는 아래인데 단기 반등 힘이 붙는 중';
    if (maScore > 0.1 && oscScore >= -0.1) return '추세가 위 · 힘도 버티는 중';
    if (maScore < -0.1 && oscScore <= 0.1) return '추세가 아래 · 힘도 약함';
    return '추세·힘 모두 뚜렷하지 않음';
  }

  function timeStr() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  // ---------- gauge ----------
  function renderGauge(el, title, score, label, counts) {
    el.innerHTML = '<div class="g-title"></div>' +
      (window.BoardGauge ? window.BoardGauge(score, 'dir', false) : '') +
      '<div class="g-label"></div>' +
      '<div class="g-counts">' +
      '<span><em>매도</em><b></b></span>' +
      '<span><em>중립</em><b></b></span>' +
      '<span><em>매수</em><b></b></span>' +
      '</div>';
    var t = el.querySelector('.g-title');
    var l = el.querySelector('.g-label');
    var bs = el.querySelectorAll('.g-counts b');
    if (t) t.textContent = title;
    if (label !== undefined && label !== null && l) {
      l.textContent = label;
      l.setAttribute('data-tone', toneOf(score));
    }
    if (counts) {
      if (bs[0]) bs[0].textContent = String(counts.sell);
      if (bs[1]) bs[1].textContent = String(counts.neutral);
      if (bs[2]) bs[2].textContent = String(counts.buy);
    }
  }

  function gaugeLoading(el, title) {
    el.innerHTML = '<div class="g-title"></div>' +
      '<div class="g-label"></div>';
    var t = el.querySelector('.g-title');
    var l = el.querySelector('.g-label');
    if (t) t.textContent = title;
    if (l) l.textContent = '불러오는 중…';
  }

  // ---------- tabs ----------
  function buildTabs() {
    var box = $('tfTabs');
    if (!box) return;
    box.innerHTML = '';
    for (var i = 0; i < TABS.length; i++) {
      var b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('data-v', TABS[i].v);
      b.textContent = TABS[i].name;
      b.setAttribute('aria-pressed', TABS[i].v === state.tf ? 'true' : 'false');
      b.addEventListener('click', onTabClick);
      box.appendChild(b);
    }
  }

  function onTabClick(e) {
    var v = e.currentTarget.getAttribute('data-v');
    if (!v) return;
    state.tf = v;
    try { localStorage.setItem(LS_KEY, v); } catch (err) {}
    var box = $('tfTabs');
    if (box) {
      var btns = box.querySelectorAll('button');
      for (var i = 0; i < btns.length; i++) {
        btns[i].setAttribute('aria-pressed', btns[i].getAttribute('data-v') === v ? 'true' : 'false');
      }
    }
    draw();
  }

  function restoreTab() {
    var v = null;
    try { v = localStorage.getItem(LS_KEY); } catch (err) {}
    var ok = false;
    for (var i = 0; i < TABS.length; i++) { if (TABS[i].v === v) ok = true; }
    state.tf = ok ? v : 'all';
  }

  // ---------- hint ----------
  function setHint(text) {
    var h = $('techHint');
    if (h) h.textContent = text;
  }

  // ---------- data ----------
  function load() {
    var jobs = [];
    jobs.push(fetchKlines('15m', 1000));
    jobs.push(fetchKlines('30m', 1000));
    jobs.push(fetchKlines('1h', 1000));
    jobs.push(fetchKlines('4h', 1000));
    jobs.push(fetchKlines('1d', 1000));
    jobs.push(fetchKlines('1w', 3));
    jobs.push(fetchKlines('1M', 3));

    return Promise.all(jobs).then(function (res) {
      var c15 = res[0], c30 = res[1], c1h = res[2], c4h = res[3], c1d = res[4], c1w = res[5], c1M = res[6];
      var rates = {
        '15m': TA.rate(c15),
        '30m': TA.rate(c30),
        '1h': TA.rate(c1h),
        '4h': TA.rate(c4h),
        '1d': TA.rate(c1d)
      };
      var combined = TA.combine(rates);

      function lastTwo(o) {
        var n = o.close.length;
        if (n < 2) return null;
        return { H: o.high[n - 2], L: o.low[n - 2], C: o.close[n - 2], O: o.open[n - 2] };
      }

      var pivots = {};
      var dRef = lastTwo(c1d);
      var wRef = lastTwo(c1w);
      var mRef = lastTwo(c1M);
      if (dRef) {
        pivots['15m'] = { p: TA.pivots(dRef.H, dRef.L, dRef.C, dRef.O), hint: '전일 기준' };
        pivots['30m'] = { p: TA.pivots(dRef.H, dRef.L, dRef.C, dRef.O), hint: '전일 기준' };
        pivots['1h'] = { p: TA.pivots(dRef.H, dRef.L, dRef.C, dRef.O), hint: '전일 기준' };
      }
      if (wRef) {
        pivots['4h'] = { p: TA.pivots(wRef.H, wRef.L, wRef.C, wRef.O), hint: '전주 기준' };
      }
      if (mRef) {
        pivots['1d'] = { p: TA.pivots(mRef.H, mRef.L, mRef.C, mRef.O), hint: '전월 기준' };
      }

      state.rates = rates;
      state.combined = combined;
      state.pivots = pivots;
      state.loading = false;
      state.failed = false;
      setHint('바이낸스 BTCUSDT 무기한 · ' + timeStr() + ' 갱신');
      draw();
    }).catch(function (err) {
      state.loading = false;
      state.failed = true;
      setHint('바이낸스 BTCUSDT 무기한 · 불러오기 실패, 다시 시도 중');
      // 이전 화면 유지
    });
  }

  // ---------- draw ----------
  function showTile(el, show) {
    if (!el) return;
    if (show) el.removeAttribute('hidden');
    else el.setAttribute('hidden', '');
  }

  function draw() {
    if (!state.combined) {
      // 데이터 전 (로딩)
      gaugeLoading($('tgOsc'), '오실레이터');
      gaugeLoading($('tgSum'), '요약');
      gaugeLoading($('tgMa'), '이동평균');
      var line = $('techLine');
      if (line) line.textContent = '';
      return;
    }
    if (state.tf === 'all') drawAll();
    else drawSingle(state.tf);
  }

  function drawSingle(tf) {
    var r = state.rates[tf];
    var osc = r.oscTally;
    var ma = r.maTally;
    var sum = r.summary;

    renderGauge($('tgOsc'), '오실레이터', osc.score, osc.label, { sell: osc.sell, neutral: osc.neutral, buy: osc.buy });
    renderGauge($('tgSum'), '요약', sum.score, sum.label, { sell: sum.sell, neutral: sum.neutral, buy: sum.buy });
    renderGauge($('tgMa'), '이동평균', ma.score, ma.label, { sell: ma.sell, neutral: ma.neutral, buy: ma.buy });

    var line = $('techLine');
    if (line) {
      line.textContent = TF_NAME[tf] + ' 요약 ' + sum.label + ' · ' + interpretSentence(osc.score, ma.score);
    }

    // osc table
    fillRows($('techOsc'), r.osc, 2);
    // ma table
    fillRows($('techMa'), r.ma, 1);

    // pivots
    fillPivots($('techPiv'), tf);

    showTile($('techOscTile'), true);
    showTile($('techMaTile'), true);
    showTile($('techPivTile'), true);
    showTile($('techMatrixTile'), false);
    showTile($('techReadTile'), false);
  }

  function fillRows(table, rows, digits) {
    if (!table) return;
    var thead = table.querySelector('thead') || table.tHead;
    var tbody = table.querySelector('tbody') || table.tBodies[0];
    if (!thead || !tbody) return;
    thead.innerHTML = '<tr><th>이름</th><th>값</th><th>판정</th></tr>';
    tbody.innerHTML = '';
    if (!rows) return;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var tr = document.createElement('tr');
      var tdName = document.createElement('td');
      var tdVal = document.createElement('td');
      var tdAct = document.createElement('td');
      tdName.textContent = row.name;
      tdVal.textContent = (digits === 1) ? num1(row.value) : num2(row.value);
      tdAct.className = 'act';
      tdAct.setAttribute('data-tone', actTone(row.action));
      tdAct.textContent = actLabel(row.action);
      tr.appendChild(tdName);
      tr.appendChild(tdVal);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    }
  }

  var PIV_KEYS = ['r3', 'r2', 'r1', 'p', 's1', 's2', 's3'];
  var PIV_LABELS = ['R3', 'R2', 'R1', 'P', 'S1', 'S2', 'S3'];
  var PIV_METHODS = ['classic', 'fibonacci', 'camarilla', 'woodie', 'dm'];

  function fillPivots(table, tf) {
    if (!table) return;
    var thead = table.querySelector('thead') || table.tHead;
    var tbody = table.querySelector('tbody') || table.tBodies[0];
    if (!thead || !tbody) return;
    thead.innerHTML = '<tr><th>피벗</th><th>클래식</th><th>피보나치</th><th>카마릴라</th><th>우디</th><th>DM</th></tr>';
    tbody.innerHTML = '';

    var info = state.pivots ? state.pivots[tf] : null;
    var hint = $('pivHint');
    if (hint) hint.textContent = info ? info.hint : '';

    if (!info) return;
    var pv = info.p;
    for (var i = 0; i < PIV_KEYS.length; i++) {
      var k = PIV_KEYS[i];
      var tr = document.createElement('tr');
      if (k === 'p') tr.className = 'pivot-p';
      var tdName = document.createElement('td');
      tdName.textContent = PIV_LABELS[i];
      tr.appendChild(tdName);
      for (var m = 0; m < PIV_METHODS.length; m++) {
        var meth = pv[PIV_METHODS[m]];
        var td = document.createElement('td');
        var val = meth ? meth[k] : null;
        td.textContent = num1(val);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  function countScores(list) {
    var sell = 0, neutral = 0, buy = 0;
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      if (s < -0.1) sell++;
      else if (s > 0.1) buy++;
      else neutral++;
    }
    return { sell: sell, neutral: neutral, buy: buy };
  }

  function drawAll() {
    var c = state.combined;
    var rates = state.rates;

    var oscScores = [], sumScores = [], maScores = [];
    for (var i = 0; i < TFS.length; i++) {
      var r = rates[TFS[i]];
      oscScores.push(r.oscTally.score);
      sumScores.push(r.summary.score);
      maScores.push(r.maTally.score);
    }

    renderGauge($('tgOsc'), '오실레이터 (통합)', c.oscScore, c.oscLabel, countScores(oscScores));
    renderGauge($('tgSum'), '요약 (통합)', c.score, c.label, countScores(sumScores));
    renderGauge($('tgMa'), '이동평균 (통합)', c.maScore, c.maLabel, countScores(maScores));

    var line = $('techLine');
    if (line) line.textContent = '통합 ' + c.label + ' · ' + c.trend;

    // matrix
    var table = $('techMatrix');
    if (table) {
      var thead = table.querySelector('thead') || table.tHead;
      var tbody = table.querySelector('tbody') || table.tBodies[0];
      if (thead) thead.innerHTML = '<tr><th>시간</th><th>오실레이터</th><th>이동평균</th><th>요약</th><th>점수</th><th>비중</th></tr>';
      if (tbody) {
        tbody.innerHTML = '';
        var rows = c.rows || [];
        for (var j = 0; j < rows.length; j++) {
          var row = rows[j];
          var r2 = rates[row.tf] || {};
          var tr = document.createElement('tr');

          var tdTf = document.createElement('td');
          tdTf.textContent = TF_NAME[row.tf] || row.tf;
          tr.appendChild(tdTf);

          tr.appendChild(actCell(r2.oscTally ? r2.oscTally.score : 0, row.oscLabel));
          tr.appendChild(actCell(r2.maTally ? r2.maTally.score : 0, row.maLabel));
          tr.appendChild(actCell(row.score, row.label));

          var tdScore = document.createElement('td');
          tdScore.textContent = signed2(row.score);
          tr.appendChild(tdScore);

          var tdW = document.createElement('td');
          var w = TA.WEIGHTS ? TA.WEIGHTS[row.tf] : null;
          tdW.textContent = (w === null || w === undefined) ? '—' : ('x' + w);
          tr.appendChild(tdW);

          tbody.appendChild(tr);
        }

        var trT = document.createElement('tr');
        trT.className = 'total';
        var tds = [];
        tds.push(makeTd('통합'));
        tds.push(actCell(c.oscScore, c.oscLabel));
        tds.push(actCell(c.maScore, c.maLabel));
        tds.push(actCell(c.score, c.label));
        tds.push(makeTd(signed2(c.score)));
        tds.push(makeTd('—'));
        for (var t = 0; t < tds.length; t++) trT.appendChild(tds[t]);
        tbody.appendChild(trT);
      }
    }

    // read
    var read = $('techRead');
    if (read) {
      read.innerHTML = '';
      var lines = [];
      lines.push({ text: '종합: ' + c.label + ' (점수 ' + signed2(c.score) + ')', lead: true });
      lines.push({ text: c.trend });
      lines.push({ text: '단기 (15분·30분·1시간): ' + (c.short === null || c.short === undefined ? '—' : signed2(c.short) + ' ' + TA.ratingLabel(c.short)) });
      lines.push({ text: '장기 (4시간·1일): ' + (c.long === null || c.long === undefined ? '—' : signed2(c.long) + ' ' + TA.ratingLabel(c.long)) });
      lines.push({ text: '시간대 일치: ' + c.agree + ' / ' + c.total });
      lines.push({ text: interpretSentence(c.oscScore, c.maScore) });
      lines.push({ text: '비중: 15분 1 · 30분 1 · 1시간 1.5 · 4시간 2 · 1일 2.5 (긴 시간에 더 큰 비중)' });
      lines.push({ text: '규칙 기반 계산이며 매매 신호가 아닙니다.' });

      for (var k = 0; k < lines.length; k++) {
        var div = document.createElement('div');
        if (lines[k].lead) div.className = 'lead';
        div.textContent = lines[k].text;
        read.appendChild(div);
      }
    }

    showTile($('techOscTile'), false);
    showTile($('techMaTile'), false);
    showTile($('techPivTile'), false);
    showTile($('techMatrixTile'), true);
    showTile($('techReadTile'), true);
  }

  function makeTd(text) {
    var td = document.createElement('td');
    td.textContent = text;
    return td;
  }

  function actCell(score, label) {
    var td = document.createElement('td');
    td.className = 'act';
    td.setAttribute('data-tone', toneOf(score));
    td.textContent = label;
    return td;
  }

  // ---------- boot ----------
  function start() {
    restoreTab();
    buildTabs();
    setHint('바이낸스 BTCUSDT 무기한 · 불러오는 중…');
    draw(); // loading state
    load();
    setInterval(load, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
