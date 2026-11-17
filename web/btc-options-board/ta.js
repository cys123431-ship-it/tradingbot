(function (root) {
  'use strict';

  // ---------- helpers ----------
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function nullArr(n) { var a = new Array(n); for (var i = 0; i < n; i++) a[i] = null; return a; }
  function down(n) { return Math.floor(n); }

  // ---------- basic moving averages ----------
  function sma(src, n) {
    var len = src ? src.length : 0, out = nullArr(len);
    if (!len || n <= 0) return out;
    var sum = 0, cnt = 0;
    for (var i = 0; i < len; i++) {
      var v = src[i];
      if (isNum(v)) { sum += v; cnt++; } else { sum = 0; cnt = 0; }
      if (cnt >= n) {
        // subtract the value leaving the window
        var old = src[i - n];
        if (i >= n) { sum -= old; }
        out[i] = sum / n;
      }
    }
    return out;
  }

  // generic EMA-like with given alpha; seed = SMA of first n valid
  function emaLike(src, n, alpha) {
    var len = src ? src.length : 0, out = nullArr(len);
    if (!len || n <= 0) return out;
    var sum = 0, cnt = 0, prev = null, seedIdx = -1;
    for (var i = 0; i < len; i++) {
      var v = src[i];
      if (isNum(v)) {
        if (prev === null) {
          sum += v; cnt++;
          if (cnt === n) {
            prev = sum / n;
            seedIdx = i;
          }
        } else {
          prev = alpha * v + (1 - alpha) * prev;
        }
      }
      if (prev !== null && i >= seedIdx) out[i] = prev;
    }
    return out;
  }

  function ema(src, n) { return emaLike(src, n, 2 / (n + 1)); }
  function rma(src, n) { return emaLike(src, n, 1 / n); }

  function wma(src, n) {
    var len = src ? src.length : 0, out = nullArr(len);
    if (!len || n <= 0) return out;
    var denom = n * (n + 1) / 2;
    for (var i = n - 1; i < len; i++) {
      var s = 0, ok = true, w = 1;
      for (var j = i - n + 1; j <= i; j++) {
        var v = src[j];
        if (!isNum(v)) { ok = false; break; }
        s += v * w; w++;
      }
      if (ok) out[i] = s / denom;
    }
    return out;
  }

  function hma(src, n) {
    var len = src ? src.length : 0, out = nullArr(len);
    if (!len || n <= 0) return out;
    var half = down(n / 2); if (half < 1) half = 1;
    var sq = down(Math.sqrt(n)); if (sq < 1) sq = 1;
    var w1 = wma(src, half), w2 = wma(src, n);
    var diff = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(w1[i]) && isNum(w2[i])) diff[i] = 2 * w1[i] - w2[i];
    }
    return wma(diff, sq);
  }

  function vwma(close, volume, n) {
    var len = close ? close.length : 0, out = nullArr(len);
    if (!len || n <= 0) return out;
    var cv = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(close[i]) && isNum(volume[i])) cv[i] = close[i] * volume[i];
      else cv[i] = null;
    }
    var a = sma(cv, n), b = sma(volume, n);
    for (var k = 0; k < len; k++) {
      if (isNum(a[k]) && isNum(b[k]) && b[k] !== 0) out[k] = a[k] / b[k];
    }
    return out;
  }

  function highest(src, n) {
    var len = src ? src.length : 0, out = nullArr(len);
    for (var i = n - 1; i < len; i++) {
      var m = -Infinity, ok = true;
      for (var j = i - n + 1; j <= i; j++) {
        var v = src[j];
        if (!isNum(v)) { ok = false; break; }
        if (v > m) m = v;
      }
      if (ok) out[i] = m;
    }
    return out;
  }

  function lowest(src, n) {
    var len = src ? src.length : 0, out = nullArr(len);
    for (var i = n - 1; i < len; i++) {
      var m = Infinity, ok = true;
      for (var j = i - n + 1; j <= i; j++) {
        var v = src[j];
        if (!isNum(v)) { ok = false; break; }
        if (v < m) m = v;
      }
      if (ok) out[i] = m;
    }
    return out;
  }

  function change(src, k) {
    var len = src ? src.length : 0, out = nullArr(len);
    for (var i = k; i < len; i++) {
      if (isNum(src[i]) && isNum(src[i - k])) out[i] = src[i] - src[i - k];
    }
    return out;
  }

  // ---------- osc indicators ----------
  function rsi(close, n) {
    n = n || 14;
    var len = close ? close.length : 0, out = nullArr(len);
    if (!len) return out;
    var gain = nullArr(len), loss = nullArr(len);
    for (var i = 1; i < len; i++) {
      if (!isNum(close[i]) || !isNum(close[i - 1])) continue;
      var d = close[i] - close[i - 1];
      gain[i] = d > 0 ? d : 0;
      loss[i] = d < 0 ? -d : 0;
    }
    var ag = rma(gain, n), al = rma(loss, n);
    for (var k = 0; k < len; k++) {
      if (isNum(ag[k]) && isNum(al[k])) {
        if (al[k] === 0) out[k] = 100;
        else out[k] = 100 - 100 / (1 + ag[k] / al[k]);
      }
    }
    return out;
  }

  function stoch(high, low, close, kLen, kSmooth, dSmooth) {
    kLen = kLen || 14; kSmooth = kSmooth || 3; dSmooth = dSmooth || 3;
    var len = close ? close.length : 0;
    var hh = highest(high, kLen), ll = lowest(low, kLen);
    var raw = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(hh[i]) && isNum(ll[i]) && isNum(close[i])) {
        var den = hh[i] - ll[i];
        raw[i] = den === 0 ? 50 : 100 * (close[i] - ll[i]) / den;
      }
    }
    var k = sma(raw, kSmooth);
    return { k: k, d: sma(k, dSmooth) };
  }

  function cci(high, low, close, n) {
    n = n || 20;
    var len = close ? close.length : 0, out = nullArr(len);
    var tp = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(high[i]) && isNum(low[i]) && isNum(close[i])) tp[i] = (high[i] + low[i] + close[i]) / 3;
    }
    var tpSma = sma(tp, n);
    for (var j = n - 1; j < len; j++) {
      if (!isNum(tpSma[j]) || !isNum(tp[j])) continue;
      var sum = 0, ok = true;
      for (var m = j - n + 1; m <= j; m++) {
        if (!isNum(tp[m])) { ok = false; break; }
        sum += Math.abs(tp[m] - tpSma[j]);
      }
      if (!ok) continue;
      var md = sum / n;
      out[j] = md === 0 ? 0 : (tp[j] - tpSma[j]) / (0.015 * md);
    }
    return out;
  }

  function dmi(high, low, close, n) {
    n = n || 14;
    var len = close ? close.length : 0;
    var plus = nullArr(len), minus = nullArr(len), adx = nullArr(len);
    var pdm = nullArr(len), mdm = nullArr(len), tr = nullArr(len);
    for (var i = 1; i < len; i++) {
      if (!isNum(high[i]) || !isNum(high[i - 1]) || !isNum(low[i]) || !isNum(low[i - 1]) ||
          !isNum(close[i - 1])) continue;
      var up = high[i] - high[i - 1];
      var dn = low[i - 1] - low[i];
      pdm[i] = (up > dn && up > 0) ? up : 0;
      mdm[i] = (dn > up && dn > 0) ? dn : 0;
      var r1 = high[i] - low[i];
      var r2 = Math.abs(high[i] - close[i - 1]);
      var r3 = Math.abs(low[i] - close[i - 1]);
      tr[i] = Math.max(r1, Math.max(r2, r3));
    }
    var ap = rma(pdm, n), am = rma(mdm, n), atr = rma(tr, n);
    for (var k = 0; k < len; k++) {
      if (isNum(ap[k]) && isNum(am[k]) && isNum(atr[k]) && atr[k] !== 0) {
        plus[k] = 100 * ap[k] / atr[k];
        minus[k] = 100 * am[k] / atr[k];
      }
    }
    var dx = nullArr(len);
    for (var j = 0; j < len; j++) {
      if (isNum(plus[j]) && isNum(minus[j])) {
        var s = plus[j] + minus[j];
        dx[j] = s === 0 ? 0 : 100 * Math.abs(plus[j] - minus[j]) / s;
      }
    }
    adx = rma(dx, n);
    return { plus: plus, minus: minus, adx: adx };
  }

  function ao(high, low) {
    var len = high ? high.length : 0, hl2 = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(high[i]) && isNum(low[i])) hl2[i] = (high[i] + low[i]) / 2;
    }
    var a = sma(hl2, 5), b = sma(hl2, 34), out = nullArr(len);
    for (var j = 0; j < len; j++) {
      if (isNum(a[j]) && isNum(b[j])) out[j] = a[j] - b[j];
    }
    return out;
  }

  function mom(close, n) { return change(close, n || 10); }

  function macd(close, fast, slow, sig) {
    fast = fast || 12; slow = slow || 26; sig = sig || 9;
    var len = close ? close.length : 0;
    var ef = ema(close, fast), es = ema(close, slow);
    var m = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(ef[i]) && isNum(es[i])) m[i] = ef[i] - es[i];
    }
    var signal = ema(m, sig), hist = nullArr(len);
    for (var j = 0; j < len; j++) {
      if (isNum(m[j]) && isNum(signal[j])) hist[j] = m[j] - signal[j];
    }
    return { macd: m, signal: signal, hist: hist };
  }

  function stochRsi(close, kSmooth, dSmooth, rsiLen, stochLen) {
    kSmooth = kSmooth || 3; dSmooth = dSmooth || 3; rsiLen = rsiLen || 14; stochLen = stochLen || 14;
    var len = close ? close.length : 0;
    var r = rsi(close, rsiLen);
    var hh = highest(r, stochLen), ll = lowest(r, stochLen);
    var raw = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(hh[i]) && isNum(ll[i]) && isNum(r[i])) {
        var den = hh[i] - ll[i];
        raw[i] = den === 0 ? 50 : 100 * (r[i] - ll[i]) / den;
      }
    }
    var k = sma(raw, kSmooth);
    return { k: k, d: sma(k, dSmooth) };
  }

  function willr(high, low, close, n) {
    n = n || 14;
    var len = close ? close.length : 0, out = nullArr(len);
    var hh = highest(high, n), ll = lowest(low, n);
    for (var i = 0; i < len; i++) {
      if (isNum(hh[i]) && isNum(ll[i]) && isNum(close[i])) {
        var den = hh[i] - ll[i];
        out[i] = den === 0 ? -50 : -100 * (hh[i] - close[i]) / den;
      }
    }
    return out;
  }

  function bbp(high, low, close, n) {
    n = n || 13;
    var len = close ? close.length : 0, out = nullArr(len);
    var e = ema(close, n);
    for (var i = 0; i < len; i++) {
      if (isNum(e[i]) && isNum(high[i]) && isNum(low[i])) {
        out[i] = (high[i] - e[i]) + (low[i] - e[i]);
      }
    }
    return out;
  }

  function uo(high, low, close, a, b, c) {
    a = a || 7; b = b || 14; c = c || 28;
    var len = close ? close.length : 0, out = nullArr(len);
    var bp = nullArr(len), tr = nullArr(len);
    for (var i = 1; i < len; i++) {
      if (!isNum(high[i]) || !isNum(low[i]) || !isNum(close[i]) ||
          !isNum(close[i - 1])) continue;
      bp[i] = close[i] - Math.min(low[i], close[i - 1]);
      tr[i] = Math.max(high[i], close[i - 1]) - Math.min(low[i], close[i - 1]);
    }
    function sumN(arr, idx, n) {
      var s = 0;
      for (var j = idx - n + 1; j <= idx; j++) {
        if (!isNum(arr[j])) return null;
        s += arr[j];
      }
      return s;
    }
    for (var k = c; k < len; k++) {
      var b7 = sumN(bp, k, a), t7 = sumN(tr, k, a);
      var b14 = sumN(bp, k, b), t14 = sumN(tr, k, b);
      var b28 = sumN(bp, k, c), t28 = sumN(tr, k, c);
      if (b7 === null || b14 === null || b28 === null) continue;
      if (!t7 || !t14 || !t28) continue;
      var av7 = b7 / t7, av14 = b14 / t14, av28 = b28 / t28;
      out[k] = 100 * (4 * av7 + 2 * av14 + av28) / 7;
    }
    return out;
  }

  function ichimoku(high, low, conv, base) {
    conv = conv || 9; base = base || 26;
    var len = high ? high.length : 0;
    var hh1 = highest(high, conv), ll1 = lowest(low, conv);
    var hh2 = highest(high, base), ll2 = lowest(low, base);
    var conversion = nullArr(len), baseL = nullArr(len);
    for (var i = 0; i < len; i++) {
      if (isNum(hh1[i]) && isNum(ll1[i])) conversion[i] = (hh1[i] + ll1[i]) / 2;
      if (isNum(hh2[i]) && isNum(ll2[i])) baseL[i] = (hh2[i] + ll2[i]) / 2;
    }
    return { conversion: conversion, base: baseL };
  }

  // ---------- pivots ----------
  function pivots(H, L, C, O) {
    var R = H - L;
    var row = function (s3, s2, s1, p, r1, r2, r3) {
      return { s3: s3, s2: s2, s1: s1, p: p, r1: r1, r2: r2, r3: r3 };
    };
    var pc = (H + L + C) / 3;
    var classic = row(pc - 2 * R, pc - R, 2 * pc - H, pc, 2 * pc - L, pc + R, pc + 2 * R);
    var fib = row(pc - R, pc - 0.618 * R, pc - 0.382 * R, pc, pc + 0.382 * R, pc + 0.618 * R, pc + R);
    var cam = row(C - 1.1 * R / 4, C - 1.1 * R / 6, C - 1.1 * R / 12, pc,
                  C + 1.1 * R / 12, C + 1.1 * R / 6, C + 1.1 * R / 4);
    var pw = (H + L + 2 * C) / 4;
    var woodie = row(L - 2 * (H - pw), pw - R, 2 * pw - H, pw, 2 * pw - L, pw + R, H + 2 * (pw - L));
    var x = C < O ? (H + 2 * L + C) : (C > O ? (2 * H + L + C) : (H + L + 2 * C));
    var pdm = x / 4;
    var dm = row(null, null, x / 2 - H, pdm, x / 2 - L, null, null);
    return { classic: classic, fibonacci: fib, camarilla: cam, woodie: woodie, dm: dm };
  }

  // ---------- rating ----------
  function ratingLabel(s) {
    if (s > 0.5) return '강한 매수';
    if (s > 0.1) return '매수';
    if (s >= -0.1) return '중립';
    if (s >= -0.5) return '매도';
    return '강한 매도';
  }

  function last(arr) { return arr && arr.length ? arr[arr.length - 1] : null; }
  function prev(arr) { return arr && arr.length > 1 ? arr[arr.length - 2] : null; }

  function actionMA(v, price) {
    if (!isNum(v)) return 0;
    if (v < price) return 1;
    if (v > price) return -1;
    return 0;
  }

  function tally(rows) {
    var buy = 0, neutral = 0, sell = 0, n = 0;
    for (var i = 0; i < rows.length; i++) {
      if (!isNum(rows[i].value)) continue;
      n++;
      if (rows[i].action > 0) buy++;
      else if (rows[i].action < 0) sell++;
      else neutral++;
    }
    var score = n ? (buy - sell) / n : 0;
    return { n: n, buy: buy, neutral: neutral, sell: sell, score: score, label: ratingLabel(score) };
  }

  function rate(c) {
    var open = c.open || [], high = c.high || [], low = c.low || [], close = c.close || [], volume = c.volume || [];
    var price = last(close);

    // --- oscillator rows ---
    var rsiV = rsi(close, 14);
    var rsiRow = { key: 'rsi', name: 'RSI (14)', value: last(rsiV), action: 0 };
    var rv = last(rsiV), rp = prev(rsiV);
    if (isNum(rv) && isNum(rp)) {
      if (rv < 30 && rv > rp) rsiRow.action = 1;
      else if (rv > 70 && rv < rp) rsiRow.action = -1;
    }

    var st = stoch(high, low, close, 14, 3, 3);
    var stRow = { key: 'stoch', name: '스토캐스틱 %K (14, 3, 3)', value: last(st.k), action: 0 };
    var sk = last(st.k), sd = last(st.d);
    if (isNum(sk) && isNum(sd)) {
      if (sk < 20 && sk > sd) stRow.action = 1;
      else if (sk > 80 && sk < sd) stRow.action = -1;
    }

    var cciV = cci(high, low, close, 20);
    var cciRow = { key: 'cci', name: 'CCI (20)', value: last(cciV), action: 0 };
    var cv = last(cciV), cp = prev(cciV);
    if (isNum(cv) && isNum(cp)) {
      if (cv < -100 && cv > cp) cciRow.action = 1;
      else if (cv > 100 && cv < cp) cciRow.action = -1;
    }

    var dm = dmi(high, low, close, 14);
    var adxRow = { key: 'adx', name: 'ADX (14)', value: last(dm.adx), action: 0 };
    var adxv = last(dm.adx), adxp = prev(dm.adx);
    var plv = last(dm.plus), mnv = last(dm.minus);
    if (isNum(adxv) && isNum(adxp) && adxv > 20 && adxv > adxp && isNum(plv) && isNum(mnv)) {
      if (plv > mnv) adxRow.action = 1;
      else if (mnv > plv) adxRow.action = -1;
    }

    var aoV = ao(high, low);
    var aoRow = { key: 'ao', name: 'AO', value: last(aoV), action: 0 };
    var av = last(aoV), ap = prev(aoV);
    if (isNum(av) && isNum(ap)) {
      if (av > 0 && av > ap) aoRow.action = 1;
      else if (av < 0 && av < ap) aoRow.action = -1;
    }

    var momV = mom(close, 10);
    var momRow = { key: 'mom', name: '모멘텀 (10)', value: last(momV), action: 0 };
    var mv = last(momV), mp = prev(momV);
    if (isNum(mv) && isNum(mp)) {
      if (mv > mp) momRow.action = 1;
      else if (mv < mp) momRow.action = -1;
    }

    var mc = macd(close, 12, 26, 9);
    var macdRow = { key: 'macd', name: 'MACD (12, 26)', value: last(mc.macd), action: 0 };
    var mcv = last(mc.macd), mcs = last(mc.signal);
    if (isNum(mcv) && isNum(mcs)) {
      if (mcv > mcs) macdRow.action = 1;
      else if (mcv < mcs) macdRow.action = -1;
    }

    var sr = stochRsi(close, 3, 3, 14, 14);
    var srRow = { key: 'stochrsi', name: '스토캐스틱 RSI (3, 3, 14, 14)', value: last(sr.k), action: 0 };
    var srk = last(sr.k), srd = last(sr.d);
    if (isNum(srk) && isNum(srd)) {
      if (srk < 20 && srk > srd) srRow.action = 1;
      else if (srk > 80 && srk < srd) srRow.action = -1;
    }

    var wrV = willr(high, low, close, 14);
    var wrRow = { key: 'willr', name: '윌리엄스 %R (14)', value: last(wrV), action: 0 };
    var wv = last(wrV), wp = prev(wrV);
    if (isNum(wv) && isNum(wp)) {
      if (wv < -80 && wv > wp) wrRow.action = 1;
      else if (wv > -20 && wv < wp) wrRow.action = -1;
    }

    var bbpV = bbp(high, low, close, 13);
    var bbpRow = { key: 'bbp', name: '불 베어 파워', value: last(bbpV), action: 0 };
    var bv = last(bbpV), bp = prev(bbpV);
    if (isNum(bv) && isNum(bp)) {
      if (bv > 0 && bv > bp) bbpRow.action = 1;
      else if (bv < 0 && bv < bp) bbpRow.action = -1;
    }

    var uoV = uo(high, low, close, 7, 14, 28);
    var uoRow = { key: 'uo', name: '얼티미트 오실레이터 (7, 14, 28)', value: last(uoV), action: 0 };
    var uv = last(uoV);
    if (isNum(uv)) {
      if (uv > 70) uoRow.action = 1;
      else if (uv < 30) uoRow.action = -1;
    }

    var osc = [rsiRow, stRow, cciRow, adxRow, aoRow, momRow, macdRow, srRow, wrRow, bbpRow, uoRow];

    // --- moving average rows ---
    var ma = [];
    var periods = [10, 20, 30, 50, 100, 200];
    function maName(isEma, p) {
      return (isEma ? '지수 이동평균' : '단순 이동평균') + ' (' + p + ')';
    }
    for (var i = 0; i < periods.length; i++) {
      var p = periods[i];
      var eV = ema(close, p);
      ma.push({ key: 'ema' + p, name: maName(true, p), value: last(eV), action: actionMA(last(eV), price) });
    }
    for (var j = 0; j < periods.length; j++) {
      var p2 = periods[j];
      var sV = sma(close, p2);
      ma.push({ key: 'sma' + p2, name: maName(false, p2), value: last(sV), action: actionMA(last(sV), price) });
    }
    // ichimoku base
    var ich = ichimoku(high, low, 9, 26);
    ma.push({ key: 'ichimoku', name: '일목 기준선 (9, 26, 52, 26)', value: last(ich.base), action: actionMA(last(ich.base), price) });
    // vwma
    var vw = vwma(close, volume, 20);
    ma.push({ key: 'vwma', name: '거래량 가중 이동평균 (20)', value: last(vw), action: actionMA(last(vw), price) });
    // hma
    var hm = hma(close, 9);
    ma.push({ key: 'hma', name: '헐 이동평균 (9)', value: last(hm), action: actionMA(last(hm), price) });

    var oscTally = tally(osc);
    var maTally = tally(ma);
    var score = (oscTally.score + maTally.score) / 2;

    return {
      price: price,
      osc: osc,
      ma: ma,
      oscTally: oscTally,
      maTally: maTally,
      summary: {
        score: score,
        label: ratingLabel(score),
        buy: oscTally.buy + maTally.buy,
        neutral: oscTally.neutral + maTally.neutral,
        sell: oscTally.sell + maTally.sell
      }
    };
  }

  // ---------- combine ----------
  var WEIGHTS = { '15m': 1, '30m': 1, '1h': 1.5, '4h': 2, '1d': 2.5 };
  var TF_ORDER = ['15m', '30m', '1h', '4h', '1d'];

  function combine(byTf) {
    byTf = byTf || {};
    var wsum = 0, ssum = 0, oscSum = 0, oscW = 0, maSum = 0, maW = 0;
    var present = [];
    for (var i = 0; i < TF_ORDER.length; i++) {
      var tf = TF_ORDER[i];
      var r = byTf[tf];
      if (!r || !r.summary) continue;
      present.push({ tf: tf, r: r, w: WEIGHTS[tf] });
      wsum += WEIGHTS[tf];
      ssum += WEIGHTS[tf] * r.summary.score;
      if (r.oscTally) { oscSum += WEIGHTS[tf] * r.oscTally.score; oscW += WEIGHTS[tf]; }
      if (r.maTally) { maSum += WEIGHTS[tf] * r.maTally.score; maW += WEIGHTS[tf]; }
    }
    var score = wsum ? ssum / wsum : 0;

    // short / long
    var shortVals = [], longVals = [];
    ['15m', '30m', '1h'].forEach(function (tf) {
      if (byTf[tf] && byTf[tf].summary) shortVals.push(byTf[tf].summary.score);
    });
    ['4h', '1d'].forEach(function (tf) {
      if (byTf[tf] && byTf[tf].summary) longVals.push(byTf[tf].summary.score);
    });
    function avg(a) { if (!a.length) return null; var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s / a.length; }
    var short = avg(shortVals), long = avg(longVals);

    var trend;
    if (short !== null && long !== null && short > 0.1 && long > 0.1) trend = '단기·장기 모두 매수 쪽 · 상승 추세 정렬';
    else if (short !== null && long !== null && short < -0.1 && long < -0.1) trend = '단기·장기 모두 매도 쪽 · 하락 추세 정렬';
    else if (short !== null && long !== null && short < -0.1 && long > 0.1) trend = '장기 상승 속 단기 조정 (눌림 구간)';
    else if (short !== null && long !== null && short > 0.1 && long < -0.1) trend = '장기 하락 속 단기 반등';
    else trend = '시간대별 방향이 엇갈림 · 뚜렷한 추세 없음';

    // agreement
    var agree = 0, total = present.length;
    for (var k = 0; k < present.length; k++) {
      var sv = present[k].r.summary.score;
      if (Math.abs(score) <= 0.1) {
        if (Math.abs(sv) <= 0.1) agree++;
      } else {
        if ((sv > 0 && score > 0) || (sv < 0 && score < 0)) agree++;
      }
    }

    var oscScore = oscW ? oscSum / oscW : 0;
    var maScore = maW ? maSum / maW : 0;

    var rows = present.map(function (p) {
      return {
        tf: p.tf,
        score: p.r.summary.score,
        label: p.r.summary.label,
        oscLabel: p.r.oscTally ? p.r.oscTally.label : null,
        maLabel: p.r.maTally ? p.r.maTally.label : null
      };
    });

    return {
      score: score,
      label: ratingLabel(score),
      short: short,
      long: long,
      trend: trend,
      agree: agree,
      total: total,
      oscScore: oscScore,
      oscLabel: ratingLabel(oscScore),
      maScore: maScore,
      maLabel: ratingLabel(maScore),
      rows: rows
    };
  }

  // ---------- export ----------
  var TA = {
    sma: sma, ema: ema, rma: rma, wma: wma, hma: hma, vwma: vwma,
    highest: highest, lowest: lowest, change: change,
    rsi: rsi, stoch: stoch, cci: cci, dmi: dmi, ao: ao, mom: mom,
    macd: macd, stochRsi: stochRsi, willr: willr, bbp: bbp, uo: uo, ichimoku: ichimoku,
    pivots: pivots, rate: rate, tally: tally, ratingLabel: ratingLabel,
    combine: combine, WEIGHTS: WEIGHTS
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = TA;
  else root.TA = TA;
})(typeof self !== 'undefined' ? self : this);
