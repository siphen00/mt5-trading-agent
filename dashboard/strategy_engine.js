/*
 * strategy_engine.js — shared engine for the Backtest Lab and the Trenches page.
 *
 * ONE codebase for: the indicator library, a small human-readable strategy DSL
 * + interpreter, the three "Trenches" strategies (aggressive 1m/3m scalps),
 * a backtest runner that mirrors the FIXED lab runner (spread cost, ATR chop
 * filter, optional daily-loss halt, entry at ask/bid), and the Ollama client
 * that turns plain-language strategies into runnable DSL specs.
 *
 * Works in the browser (attaches window.TE) and in node (module.exports = TE)
 * so it can be unit-tested headlessly.
 *
 * IMPORTANT REALITY CHECK (kept in code on purpose): the Trenches strategies
 * are legitimate but AGGRESSIVE. Risky does not mean profitable — on 1m/3m the
 * spread and slippage eat edge fast. This engine exists to let you find out a
 * strategy DOESN'T work cheaply, in backtest, before any capital is near it.
 */
(function (root) {
  "use strict";

  // =========================================================================
  // Indicator library — arrays aligned by index, null during warmup.
  // =========================================================================
  const closes = (c) => c.map((x) => x.close);
  const highs = (c) => c.map((x) => x.high);
  const lows = (c) => c.map((x) => x.low);
  const vols = (c) => c.map((x) => x.volume);

  function ema(values, span) {
    const k = 2 / (span + 1);
    const out = new Array(values.length).fill(null);
    let prev = values[0];
    out[0] = prev;
    for (let i = 1; i < values.length; i++) { prev = values[i] * k + prev * (1 - k); out[i] = prev; }
    return out;
  }
  function sma(values, period) {
    const out = new Array(values.length).fill(null);
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
      sum += values[i];
      if (i >= period) sum -= values[i - period];
      if (i >= period - 1) out[i] = sum / period;
    }
    return out;
  }
  function stddev(values, period) {
    const out = new Array(values.length).fill(null);
    for (let i = period - 1; i < values.length; i++) {
      const slice = values.slice(i - period + 1, i + 1);
      const m = slice.reduce((a, b) => a + b, 0) / period;
      out[i] = Math.sqrt(slice.reduce((a, b) => a + (b - m) * (b - m), 0) / period);
    }
    return out;
  }
  function atr(candles, period = 14) {
    const tr = new Array(candles.length).fill(null);
    for (let i = 0; i < candles.length; i++) {
      const h = candles[i].high, l = candles[i].low;
      const pc = i > 0 ? candles[i - 1].close : candles[i].close;
      tr[i] = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
    }
    return sma(tr, period);
  }
  function rsi(values, period = 14) {
    const out = new Array(values.length).fill(null);
    let avgGain = 0, avgLoss = 0;
    for (let i = 1; i < values.length; i++) {
      const d = values[i] - values[i - 1];
      const g = Math.max(d, 0), l = Math.max(-d, 0);
      if (i <= period) {
        avgGain += g / period; avgLoss += l / period;
        if (i === period) { const rs = avgLoss === 0 ? Infinity : avgGain / avgLoss; out[i] = 100 - 100 / (1 + rs); }
      } else {
        avgGain = (avgGain * (period - 1) + g) / period;
        avgLoss = (avgLoss * (period - 1) + l) / period;
        const rs = avgLoss === 0 ? Infinity : avgGain / avgLoss;
        out[i] = 100 - 100 / (1 + rs);
      }
    }
    return out;
  }
  function bollinger(values, period = 20, mult = 2) {
    const mid = sma(values, period), sd = stddev(values, period);
    const upper = new Array(values.length).fill(null);
    const lower = new Array(values.length).fill(null);
    for (let i = 0; i < values.length; i++) {
      if (mid[i] != null && sd[i] != null) { upper[i] = mid[i] + mult * sd[i]; lower[i] = mid[i] - mult * sd[i]; }
    }
    return { mid, upper, lower };
  }
  function rollingMax(values, period) {
    const out = new Array(values.length).fill(null);
    for (let i = period - 1; i < values.length; i++) out[i] = Math.max(...values.slice(i - period + 1, i + 1));
    return out;
  }
  function rollingMin(values, period) {
    const out = new Array(values.length).fill(null);
    for (let i = period - 1; i < values.length; i++) out[i] = Math.min(...values.slice(i - period + 1, i + 1));
    return out;
  }
  function vwapRolling(candles, period = 50) {
    const out = new Array(candles.length).fill(null);
    for (let i = period - 1; i < candles.length; i++) {
      let pv = 0, vv = 0;
      for (let j = i - period + 1; j <= i; j++) {
        const tp = (candles[j].high + candles[j].low + candles[j].close) / 3;
        pv += tp * candles[j].volume; vv += candles[j].volume;
      }
      out[i] = vv > 0 ? pv / vv : null;
    }
    return out;
  }

  const indicators = { ema, sma, stddev, atr, rsi, bollinger, rollingMax, rollingMin, vwapRolling,
                       closes, highs, lows, vols };

  // =========================================================================
  // DSL — human-language strategies compile to this, and so can hand-written specs.
  //
  // spec = {
  //   name: string,
  //   long:  Condition[],   // ALL must hold to go long
  //   short: Condition[],   // ALL must hold to go short
  //   stop_atr?: number, target_atr?: number
  // }
  // Condition = { left: Operand, op: Op, right: Operand }
  // Operand   = { value:number } | { price:"close"|"open"|"high"|"low", shift?:int }
  //           | { indicator:"ema"|"sma"|"rsi"|"atr"|"bb_upper"|"bb_lower"|"bb_mid"|"vwap",
  //               period?:int, mult?:number, shift?:int }
  // Op        = ">" | "<" | ">=" | "<=" | "cross_above" | "cross_below" | "rising" | "falling"
  // =========================================================================
  const ALLOWED_INDICATORS = ["ema", "sma", "rsi", "atr", "bb_upper", "bb_lower", "bb_mid", "vwap"];
  const ALLOWED_OPS = [">", "<", ">=", "<=", "cross_above", "cross_below", "rising", "falling"];
  const ALLOWED_PRICES = ["close", "open", "high", "low"];

  function validateSpec(spec) {
    if (!spec || typeof spec !== "object") throw new Error("spec must be an object");
    if (!Array.isArray(spec.long) && !Array.isArray(spec.short))
      throw new Error("spec needs at least a 'long' or 'short' array of conditions");
    const checkSide = (side, arr) => {
      if (arr == null) return;
      if (!Array.isArray(arr)) throw new Error(`'${side}' must be an array of conditions`);
      arr.forEach((cond, k) => {
        if (!cond.op || !ALLOWED_OPS.includes(cond.op))
          throw new Error(`${side}[${k}]: op must be one of ${ALLOWED_OPS.join(", ")}`);
        checkOperand(`${side}[${k}].left`, cond.left);
        if (!["rising", "falling"].includes(cond.op)) checkOperand(`${side}[${k}].right`, cond.right);
      });
    };
    checkSide("long", spec.long);
    checkSide("short", spec.short);
    return true;
  }
  function checkOperand(where, op) {
    if (!op || typeof op !== "object") throw new Error(`${where}: operand missing`);
    if ("value" in op) { if (typeof op.value !== "number") throw new Error(`${where}: value must be a number`); return; }
    if ("price" in op) { if (!ALLOWED_PRICES.includes(op.price)) throw new Error(`${where}: price must be one of ${ALLOWED_PRICES.join(", ")}`); return; }
    if ("indicator" in op) { if (!ALLOWED_INDICATORS.includes(op.indicator)) throw new Error(`${where}: indicator must be one of ${ALLOWED_INDICATORS.join(", ")}`); return; }
    throw new Error(`${where}: operand must have 'value', 'price', or 'indicator'`);
  }

  // Resolve an operand to a numeric series (aligned by index), cached per run.
  function operandSeries(op, candles, cache) {
    const key = JSON.stringify(op);
    if (cache.has(key)) return cache.get(key);
    let series;
    const shift = op.shift || 0;
    if ("value" in op) {
      series = new Array(candles.length).fill(op.value);
    } else if ("price" in op) {
      series = candles.map((c) => c[op.price]);
    } else {
      const p = op.period || defaultPeriod(op.indicator);
      switch (op.indicator) {
        case "ema": series = ema(closes(candles), p); break;
        case "sma": series = sma(closes(candles), p); break;
        case "rsi": series = rsi(closes(candles), p); break;
        case "atr": series = atr(candles, p); break;
        case "vwap": series = vwapRolling(candles, p); break;
        case "bb_upper": series = bollinger(closes(candles), p, op.mult || 2).upper; break;
        case "bb_lower": series = bollinger(closes(candles), p, op.mult || 2).lower; break;
        case "bb_mid": series = bollinger(closes(candles), p, op.mult || 2).mid; break;
        default: throw new Error(`unknown indicator ${op.indicator}`);
      }
    }
    if (shift > 0) series = series.map((_, i) => (i - shift >= 0 ? series[i - shift] : null));
    cache.set(key, series);
    return series;
  }
  function defaultPeriod(ind) {
    return ({ ema: 9, sma: 20, rsi: 14, atr: 14, bb_upper: 20, bb_lower: 20, bb_mid: 20, vwap: 50 })[ind] || 14;
  }

  function evalCondition(cond, L, R, i) {
    const a = L[i], ap = i > 0 ? L[i - 1] : null;
    switch (cond.op) {
      case ">": return a != null && R[i] != null && a > R[i];
      case "<": return a != null && R[i] != null && a < R[i];
      case ">=": return a != null && R[i] != null && a >= R[i];
      case "<=": return a != null && R[i] != null && a <= R[i];
      case "cross_above": return ap != null && R[i - 1] != null && R[i] != null && ap <= R[i - 1] && a > R[i];
      case "cross_below": return ap != null && R[i - 1] != null && R[i] != null && ap >= R[i - 1] && a < R[i];
      case "rising": return ap != null && a != null && a > ap;
      case "falling": return ap != null && a != null && a < ap;
      default: return false;
    }
  }

  // Compile a spec into a (candles, i, ctx) => {direction, reason} function.
  function compileSpec(spec) {
    validateSpec(spec);
    const cache = new WeakMap(); // per-candles-array series cache
    const build = (arr, candles, c) => (arr || []).map((cond) => ({
      cond, L: operandSeries(cond.left, candles, c),
      R: ["rising", "falling"].includes(cond.op) ? null : operandSeries(cond.right, candles, c),
    }));
    let compiledFor = null, longC = null, shortC = null;
    return function (candles, i) {
      if (compiledFor !== candles) {
        const c = new Map();
        longC = build(spec.long, candles, c);
        shortC = build(spec.short, candles, c);
        compiledFor = candles;
      }
      const longOk = longC.length > 0 && longC.every((x) => evalCondition(x.cond, x.L, x.R, i));
      const shortOk = shortC.length > 0 && shortC.every((x) => evalCondition(x.cond, x.L, x.R, i));
      if (longOk && !shortOk) return { direction: "long", reason: spec.name || "custom long" };
      if (shortOk && !longOk) return { direction: "short", reason: spec.name || "custom short" };
      return { direction: "none", reason: "" };
    };
  }

  // =========================================================================
  // The three Trenches strategies. Each carries its guard posture so the
  // Trenches page can run "one of each" for comparison.
  // =========================================================================
  function body(c) { return Math.abs(c.close - c.open); }
  function isUp(c) { return c.close > c.open; }
  function isDown(c) { return c.close < c.open; }

  // 1) Liquidity Sweep Reversal — 1m. Fully guarded. Fades stop-hunts: price
  //    wicks past a recent swing extreme then closes back inside → reversal.
  function liquiditySweep(candles, i) {
    const look = 20;
    if (i < look + 2) return { direction: "none", reason: "" };
    const win = candles.slice(i - look, i);           // prior window (excl. current)
    const swingLow = Math.min(...win.map((c) => c.low));
    const swingHigh = Math.max(...win.map((c) => c.high));
    const c = candles[i];
    const mid = (c.high + c.low) / 2;
    // Long: pierced below the swing low but closed back above it, closing strong.
    if (c.low < swingLow && c.close > swingLow && c.close > mid)
      return { direction: "long", reason: `swept ${swingLow.toFixed(1)} low, reclaimed` };
    if (c.high > swingHigh && c.close < swingHigh && c.close < mid)
      return { direction: "short", reason: `swept ${swingHigh.toFixed(1)} high, rejected` };
    return { direction: "none", reason: "" };
  }

  // 2) Volatility Squeeze Ignition — 3m. Spread-guard only. A Bollinger squeeze
  //    (width in the bottom quintile of its recent range) followed by an
  //    expansion candle (large range + strong body + volume spike) → trade the
  //    breakout direction.
  function squeezeIgnition(candles, i) {
    const period = 20, lookback = 60;
    if (i < lookback + period) return { direction: "none", reason: "" };
    const cl = closes(candles);
    const bb = bollinger(cl, period, 2);
    const width = (idx) => (bb.upper[idx] != null ? (bb.upper[idx] - bb.lower[idx]) / bb.mid[idx] : null);
    const w = width(i - 1);                            // squeeze measured on the PRIOR bar
    if (w == null) return { direction: "none", reason: "" };
    const recent = [];
    for (let j = i - lookback; j < i; j++) { const x = width(j); if (x != null) recent.push(x); }
    recent.sort((a, b) => a - b);
    const quintile = recent[Math.floor(recent.length * 0.2)] ?? Infinity;
    const squeezed = w <= quintile;

    const a = atr(candles, 14)[i];
    const c = candles[i];
    const bigRange = a != null && (c.high - c.low) > a * 1.3;
    const volAvg = sma(vols(candles), 20)[i];
    const volSpike = volAvg != null && c.volume > volAvg * 1.4;
    const strongBody = body(c) > (c.high - c.low) * 0.6;

    if (squeezed && bigRange && volSpike && strongBody) {
      if (c.close > bb.upper[i]) return { direction: "long", reason: "squeeze → upside ignition" };
      if (c.close < bb.lower[i]) return { direction: "short", reason: "squeeze → downside ignition" };
    }
    return { direction: "none", reason: "" };
  }

  // 3) Exhaustion Fade — 1m. Fully raw (no guards). Knife-catch: N consecutive
  //    same-colour candles into an overextension beyond the Bollinger band with
  //    an RSI extreme → fade it. Highest variance of the three by design.
  function exhaustionFade(candles, i) {
    const run = 4;
    if (i < 30) return { direction: "none", reason: "" };
    const cl = closes(candles);
    const bb = bollinger(cl, 20, 2.5);
    const r = rsi(cl, 14)[i];
    const c = candles[i];
    let downStreak = 0, upStreak = 0;
    for (let j = i; j > i - run; j--) { if (isDown(candles[j])) downStreak++; if (isUp(candles[j])) upStreak++; }
    if (downStreak >= run && bb.lower[i] != null && c.close < bb.lower[i] && r != null && r < 25)
      return { direction: "long", reason: `${run}+ red, below lower band, RSI ${r.toFixed(0)}` };
    if (upStreak >= run && bb.upper[i] != null && c.close > bb.upper[i] && r != null && r > 75)
      return { direction: "short", reason: `${run}+ green, above upper band, RSI ${r.toFixed(0)}` };
    return { direction: "none", reason: "" };
  }

  const TRENCH_STRATEGIES = {
    liquidity_sweep: {
      name: "Liquidity Sweep Reversal", timeframe: "M1", fn: liquiditySweep,
      guards: { atrFilter: true, spreadReject: true, dailyLoss: true },
      blurb: "Fades stop-hunts: a wick past a recent swing extreme that closes back inside. Counter-trend, fully guarded.",
    },
    squeeze_ignition: {
      name: "Volatility Squeeze Ignition", timeframe: "M3", fn: squeezeIgnition,
      guards: { atrFilter: false, spreadReject: true, dailyLoss: false },
      blurb: "Bollinger squeeze then an expansion candle with volume. Momentum breakout, spread-guard only.",
    },
    exhaustion_fade: {
      name: "Exhaustion Fade", timeframe: "M1", fn: exhaustionFade,
      guards: { atrFilter: false, spreadReject: false, dailyLoss: false },
      blurb: "Knife-catch: consecutive candles into a band overextension + RSI extreme. Fully raw — highest variance.",
    },
  };

  // =========================================================================
  // Backtest runner — mirrors the FIXED lab runner (spread cost, ATR chop
  // filter, entry at ask/bid) and adds an optional daily-loss halt so the
  // guard comparison on the Trenches page is meaningful.
  // =========================================================================
  const MAX_SPREAD_ATR_FRACTION = 0.15;
  const SPREAD_STOP_MULTIPLE = 3.0;

  function atrFilterOk(atr14, i, period = 20, minMultiplier = 1.0) {
    if (i < period) return true;
    const cur = atr14[i];
    if (!cur) return true;
    let sum = 0, n = 0;
    for (let j = i - period + 1; j <= i; j++) { if (atr14[j]) { sum += atr14[j]; n++; } }
    if (!n) return true;
    return cur >= (sum / n) * minMultiplier;
  }
  function utcDay(unixSeconds) { return Math.floor(unixSeconds / 86400); }

  function runBacktest(candles, signalFn, opts = {}) {
    const {
      spread = 12, startEquity = 1000, warmup = 70,
      guards = { atrFilter: true, spreadReject: true, dailyLoss: false },
      dailyLossPct = 3.0, sessionFilter = null, // sessionFilter: (unixSeconds)=>bool
    } = opts;

    const atr14 = atr(candles, 14);
    const trades = [];
    let equity = startEquity;
    const equityCurve = [{ time: candles[0].time, equity }];
    let openTrade = null;

    let curDay = null, dayStartEquity = equity, halted = false;

    for (let i = warmup; i < candles.length; i++) {
      const c = candles[i];

      // reset the daily-loss halt at each new UTC day
      if (guards.dailyLoss) {
        const d = utcDay(c.time);
        if (d !== curDay) { curDay = d; dayStartEquity = equity; halted = false; }
      }

      if (openTrade) {
        const hitStop = openTrade.direction === "long" ? c.low <= openTrade.stop : c.high >= openTrade.stop;
        const hitTarget = openTrade.direction === "long" ? c.high >= openTrade.target : c.low <= openTrade.target;
        if (hitStop || hitTarget) {
          const exitPrice = hitStop ? openTrade.stop : openTrade.target;
          const pnl = (openTrade.direction === "long" ? exitPrice - openTrade.entry : openTrade.entry - exitPrice) * openTrade.units;
          equity += pnl;
          trades.push({ ...openTrade, exit: exitPrice, exitTime: c.time, pnl, outcome: hitTarget ? "win" : "loss" });
          equityCurve.push({ time: c.time, equity });
          openTrade = null;
        }
      }
      if (openTrade) continue;

      if (guards.dailyLoss && !halted && dayStartEquity > 0) {
        if ((equity - dayStartEquity) / dayStartEquity * 100 <= -dailyLossPct) halted = true;
      }
      if (halted) continue;
      if (sessionFilter && !sessionFilter(c.time)) continue;
      if (guards.atrFilter && !atrFilterOk(atr14, i)) continue;

      const sig = signalFn(candles, i);
      if (!sig || sig.direction === "none") continue;

      const atrNow = atr14[i] || equity * 0.002;
      if (guards.spreadReject && atrNow > 0 && spread / atrNow > MAX_SPREAD_ATR_FRACTION) continue;

      const stopDist = Math.max(atrNow * 1.5, spread * SPREAD_STOP_MULTIPLE);
      const targetDist = Math.max(atrNow * 2.5, spread * SPREAD_STOP_MULTIPLE * 1.67);
      const entry = sig.direction === "long" ? c.close + spread / 2 : c.close - spread / 2;
      const stop = sig.direction === "long" ? entry - stopDist : entry + stopDist;
      const target = sig.direction === "long" ? entry + targetDist : entry - targetDist;
      const units = (equity * 0.005) / stopDist;
      openTrade = { direction: sig.direction, entry, stop, target, units, time: c.time, reason: sig.reason };
    }

    return { trades, equityCurve, stats: computeStats(trades, equityCurve, startEquity) };
  }

  function computeStats(trades, equityCurve, startEquity) {
    const wins = trades.filter((t) => t.pnl > 0);
    const losses = trades.filter((t) => t.pnl <= 0);
    const netPnl = trades.reduce((s, t) => s + t.pnl, 0);
    const grossWin = wins.reduce((s, t) => s + t.pnl, 0);
    const grossLoss = Math.abs(losses.reduce((s, t) => s + t.pnl, 0));
    let peak = -Infinity, maxDD = 0;
    equityCurve.forEach((e) => { peak = Math.max(peak, e.equity); maxDD = Math.min(maxDD, ((e.equity - peak) / peak) * 100 || 0); });
    return {
      netPnl, netPnlPct: (netPnl / startEquity) * 100,
      winRate: trades.length ? (wins.length / trades.length) * 100 : 0,
      wins: wins.length, losses: losses.length,
      profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
      maxDD, totalTrades: trades.length,
      avgTrade: trades.length ? netPnl / trades.length : 0,
    };
  }

  // =========================================================================
  // Ollama client — plain-language strategy -> validated DSL spec.
  // Uses format:"json" so the model must return parseable JSON, temperature 0
  // for determinism. Never eval's model output; the JSON is interpreted by the
  // sandboxed DSL above, so a bad/hostile response can only fail validation.
  // =========================================================================
  function buildPrompt(text) {
    return [
      "You convert a trader's plain-language strategy into a strict JSON rule spec.",
      "Output ONLY JSON. No prose, no markdown.",
      "",
      "Schema:",
      '{ "name": string, "long": Condition[], "short": Condition[], "stop_atr": number, "target_atr": number }',
      'Condition = { "left": Operand, "op": Op, "right": Operand }   (right omitted only for rising/falling)',
      'Operand = { "value": number } | { "price": "close|open|high|low", "shift": int }',
      '        | { "indicator": "ema|sma|rsi|atr|bb_upper|bb_lower|bb_mid|vwap", "period": int, "mult": number, "shift": int }',
      'Op = ">" | "<" | ">=" | "<=" | "cross_above" | "cross_below" | "rising" | "falling"',
      "Every condition in an array must hold for that side to fire. Use shift:1 for the previous bar.",
      "",
      "Example — 'go long when the 9 EMA crosses above the 21 EMA and RSI under 60':",
      '{ "name":"ema cross", "long":[ {"left":{"indicator":"ema","period":9},"op":"cross_above","right":{"indicator":"ema","period":21}}, {"left":{"indicator":"rsi","period":14},"op":"<","right":{"value":60}} ], "short":[], "stop_atr":1.5, "target_atr":2.5 }',
      "",
      "Trader's strategy:",
      text,
    ].join("\n");
  }

  // =========================================================================
  // In-browser natural-language parser — turns plain English into a DSL spec
  // with NO network call. Works on the https Pages site (no Ollama, no
  // localhost, no mixed-content block). Deterministic and limited by design;
  // the output is shown as editable JSON so anything it misses can be hand-fixed.
  // =========================================================================
  function parseTextToSpec(text) {
    if (!text || !text.trim()) throw new Error("Type a strategy first.");
    const raw = text.toLowerCase().replace(/\s+/g, " ");
    const spec = { name: "custom", long: [], short: [], stop_atr: 1.5, target_atr: 2.5 };

    // stop / target in ATR multiples
    let m;
    if ((m = raw.match(/stop[^.,;]*?(\d+(?:\.\d+)?)\s*(?:x|times|\*)?\s*atr/))) spec.stop_atr = parseFloat(m[1]);
    if ((m = raw.match(/(?:target|take[ -]?profit|tp|exit)[^.,;]*?(\d+(?:\.\d+)?)\s*(?:x|times|\*)?\s*atr/))) spec.target_atr = parseFloat(m[1]);

    // Split into direction-tagged segments. Everything after a "long/buy" word
    // belongs to the long side until a "short/sell" word flips it, and so on.
    const dirRe = /\b(long|buy|bullish|short|sell|bearish)\b/g;
    const marks = [];
    let mm;
    while ((mm = dirRe.exec(raw)) !== null) {
      const side = /long|buy|bullish/.test(mm[1]) ? "long" : "short";
      marks.push({ side, idx: mm.index });
    }
    const segments = [];
    if (marks.length === 0) {
      segments.push({ side: "long", text: raw }); // default: treat as a long entry
    } else {
      for (let i = 0; i < marks.length; i++) {
        const start = marks[i].idx;
        const end = i + 1 < marks.length ? marks[i + 1].idx : raw.length;
        segments.push({ side: marks[i].side, text: raw.slice(start, end) });
      }
    }

    const maToOperand = (period, kind) => ({ indicator: kind, period: parseInt(period, 10) });
    const cmpOp = (word) => (/above|over|greater|>|higher/.test(word) ? ">" : "<");

    for (const seg of segments) {
      const s = seg.text;
      const conds = [];

      // 1) MA cross MA — "9 ema crosses above the 21 ema"
      let re = /(\d+)\s*(ema|sma)\s*(?:crosses?|crossing|cross)\s*(above|below)\s*(?:the\s*)?(\d+)\s*(ema|sma)/g;
      while ((m = re.exec(s)) !== null)
        conds.push({ left: maToOperand(m[1], m[2]), op: /above/.test(m[3]) ? "cross_above" : "cross_below", right: maToOperand(m[4], m[5]) });

      // 2) price crosses a MA/VWAP — "price crosses above the 20 ema"
      re = /(?:price|close)\s*(?:crosses?|crossing|cross)\s*(above|below)\s*(?:the\s*)?(?:(\d+)\s*(ema|sma)|vwap)/g;
      while ((m = re.exec(s)) !== null) {
        const right = m[2] ? maToOperand(m[2], m[3]) : { indicator: "vwap", period: 50 };
        conds.push({ left: { price: "close" }, op: /above/.test(m[1]) ? "cross_above" : "cross_below", right });
      }

      // 3) price above/below a MA/VWAP (no cross) — "price above vwap", "close under 50 ema"
      re = /(?:price|close)\s*(?:is\s*)?(above|over|below|under)\s*(?:the\s*)?(?:(\d+)\s*(ema|sma)|vwap)/g;
      while ((m = re.exec(s)) !== null) {
        const right = m[2] ? maToOperand(m[2], m[3]) : { indicator: "vwap", period: 50 };
        conds.push({ left: { price: "close" }, op: cmpOp(m[1]), right });
      }

      // 4) RSI threshold — "rsi under 30", "rsi is above 70"
      re = /rsi\s*(?:is\s*)?(above|over|greater than|>|below|under|less than|<)\s*(\d+)/g;
      while ((m = re.exec(s)) !== null)
        conds.push({ left: { indicator: "rsi", period: 14 }, op: cmpOp(m[1]), right: { value: parseInt(m[2], 10) } });

      // 5) Bollinger band — "close below the lower band", "above upper band"
      re = /(above|over|below|under)\s*(?:the\s*)?(upper|lower)\s*(?:bollinger\s*)?band/g;
      while ((m = re.exec(s)) !== null)
        conds.push({ left: { price: "close" }, op: cmpOp(m[1]), right: { indicator: m[2] === "upper" ? "bb_upper" : "bb_lower", period: 20 } });

      for (const c of conds) spec[seg.side].push(c);
    }

    if (spec.long.length === 0 && spec.short.length === 0) {
      throw new Error(
        "Couldn't parse any rules from that. Try phrasing like: " +
        "\"go long when the 9 EMA crosses above the 21 EMA and RSI under 60\". " +
        "Supported: EMA/SMA crosses, price vs EMA/SMA/VWAP, RSI thresholds, Bollinger bands, ATR stop/target. " +
        "Or edit the JSON directly below."
      );
    }
    validateSpec(spec);
    return spec;
  }

  async function generateSpecFromText(text, cfg = {}) {
    const host = cfg.host || "http://localhost:11434";
    const model = cfg.model || "qwen2.5:1.5b";
    const fetchFn = cfg.fetch || (typeof fetch !== "undefined" ? fetch : null);
    if (!fetchFn) throw new Error("no fetch available");
    let res;
    try {
      res = await fetchFn(`${host}/api/generate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, prompt: buildPrompt(text), stream: false, format: "json", options: { temperature: 0 } }),
      });
    } catch (e) {
      throw new Error("Could not reach Ollama at " + host + ". Is `ollama serve` running, and is the dashboard opened locally (http://localhost, not the https Pages site)?");
    }
    if (!res.ok) throw new Error(`Ollama returned HTTP ${res.status}`);
    const data = await res.json();
    let spec;
    try { spec = JSON.parse(data.response); }
    catch (e) { throw new Error("The model did not return valid JSON. Try rephrasing, or edit the rules by hand."); }
    validateSpec(spec);
    return spec;
  }

  const TE = {
    indicators, compileSpec, validateSpec, TRENCH_STRATEGIES,
    runBacktest, computeStats, atrFilterOk, buildPrompt, generateSpecFromText, parseTextToSpec,
    ALLOWED_INDICATORS, ALLOWED_OPS, ALLOWED_PRICES,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = TE;
  root.TE = TE;
})(typeof window !== "undefined" ? window : globalThis);
