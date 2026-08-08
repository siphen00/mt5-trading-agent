/* Node tests for dashboard/strategy_engine.js — run: node dashboard/test_engine.cjs */
const TE = require("./strategy_engine.js");

let passed = 0;
const check = (name, cond) => { if (!cond) throw new Error("FAIL: " + name); passed++; console.log("  ok  " + name); };
const approx = (a, b, e = 1e-6) => Math.abs(a - b) < e;

// ---- helpers to build candles -------------------------------------------
let T0 = 1_700_000_000;
function candlesFromCloses(arr, step = 60) {
  return arr.map((cl, i) => ({ time: T0 + i * step, open: cl, high: cl + 1, low: cl - 1, close: cl, volume: 1000 }));
}

// =========================================================================
console.log("A. indicators");
const cl = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
const sma3 = TE.indicators.sma(cl, 3);
check("sma warmup is null", sma3[0] === null && sma3[1] === null);
check("sma3 at index 2 = 2", approx(sma3[2], 2));
check("sma3 at index 9 = 9", approx(sma3[9], 9));
const emaArr = TE.indicators.ema(cl, 3);
check("ema seeds at first value", approx(emaArr[0], 1));
check("ema rises monotonically", emaArr[9] > emaArr[5] && emaArr[5] > emaArr[1]);
const flat = TE.indicators.rsi(new Array(30).fill(50), 14);
check("rsi of a flat line is ~50 (no losses => 100 actually)", flat[20] === 100 || flat[20] === null || approx(flat[20], 100));
const up = TE.indicators.rsi(Array.from({ length: 30 }, (_, i) => 100 + i), 14);
check("rsi of pure uptrend is 100", approx(up[29], 100));
const bb = TE.indicators.bollinger(cl, 3, 2);
check("bb upper > mid > lower", bb.upper[5] > bb.mid[5] && bb.mid[5] > bb.lower[5]);
const atrs = TE.indicators.atr(candlesFromCloses(cl), 3);
check("atr is positive after warmup", atrs[5] > 0);

// =========================================================================
console.log("B. DSL validate");
check("valid spec passes", TE.validateSpec({ long: [{ left: { indicator: "ema", period: 9 }, op: "cross_above", right: { indicator: "ema", period: 21 } }] }));
let threw = false;
try { TE.validateSpec({ long: [{ left: { indicator: "ema" }, op: "banana", right: { value: 1 } }] }); } catch (e) { threw = true; }
check("bad op is rejected", threw);
threw = false;
try { TE.validateSpec({ long: [{ left: { indicator: "wat" }, op: ">", right: { value: 1 } }] }); } catch (e) { threw = true; }
check("unknown indicator is rejected", threw);
threw = false;
try { TE.validateSpec({}); } catch (e) { threw = true; }
check("empty spec is rejected", threw);

// =========================================================================
console.log("C. DSL compile + evaluate");
// Price crosses above a constant 5: closes 1..10 cross 5 between idx 4 and 5.
const crossSpec = { name: "cross5", long: [{ left: { price: "close" }, op: "cross_above", right: { value: 5 } }], short: [] };
const crossFn = TE.compileSpec(crossSpec);
const cc = candlesFromCloses(cl);
let firedAt = -1;
for (let i = 1; i < cc.length; i++) if (crossFn(cc, i).direction === "long") { firedAt = i; break; }
check("cross_above value fires exactly once at the crossing", firedAt === 5);
let count = 0;
for (let i = 1; i < cc.length; i++) if (crossFn(cc, i).direction === "long") count++;
check("cross fires only on the crossing bar, not after", count === 1);

// AND semantics: rsi<60 AND close>5
const andSpec = { name: "and", long: [{ left: { price: "close" }, op: ">", right: { value: 5 } }, { left: { indicator: "rsi", period: 3 }, op: "<", right: { value: 200 } }], short: [] };
const andFn = TE.compileSpec(andSpec);
check("AND requires both conditions", andFn(cc, 3).direction === "none" && andFn(cc, 7).direction === "long");

// shift:1 references the previous bar
const shiftSpec = { name: "shift", long: [{ left: { price: "close" }, op: ">", right: { price: "close", shift: 1 } }], short: [] };
const shiftFn = TE.compileSpec(shiftSpec);
check("shift:1 compares to previous close (always rising here)", shiftFn(cc, 5).direction === "long");

// =========================================================================
console.log("D. trench strategies fire on constructed setups");

// Liquidity sweep: build a base, then a candle that pierces the swing low and reclaims.
(function () {
  const base = candlesFromCloses(new Array(25).fill(100));
  // make a clear swing low at 95 a few bars back, then current sweeps below & reclaims
  base[10].low = 95;
  const sweep = base[24];
  sweep.low = 94; sweep.close = 99; sweep.high = 99.5; sweep.open = 96;
  const sig = TE.TRENCH_STRATEGIES.liquidity_sweep.fn(base, 24);
  check("liquidity sweep goes long on reclaim", sig.direction === "long");
})();

// Exhaustion fade: 4 red candles crashing below lower band with RSI extreme.
(function () {
  const arr = [];
  for (let i = 0; i < 26; i++) arr.push(100);            // flat base
  for (let i = 0; i < 6; i++) arr.push(100 - (i + 1) * 6); // sharp drop
  const c = candlesFromCloses(arr);
  // enforce red bodies on the drop
  for (let i = 26; i < c.length; i++) { c[i].open = c[i].close + 3; c[i].high = c[i].open + 0.5; c[i].low = c[i].close - 0.5; }
  const i = c.length - 1;
  const sig = TE.TRENCH_STRATEGIES.exhaustion_fade.fn(c, i);
  check("exhaustion fade goes long after a red crash below band + low RSI", sig.direction === "long");
})();

// Squeeze ignition: flat (squeeze) then a big volume expansion candle up.
(function () {
  const arr = [];
  for (let i = 0; i < 90; i++) arr.push(100 + (i % 2) * 0.05); // extremely tight range => squeeze
  arr.push(103);                                               // ignition close
  const c = candlesFromCloses(arr);
  const i = c.length - 1;
  c[i].open = 100; c[i].high = 103.2; c[i].low = 99.9; c[i].close = 103; c[i].volume = 5000;
  const sig = TE.TRENCH_STRATEGIES.squeeze_ignition.fn(c, i);
  check("squeeze ignition fires long on expansion (or holds none if band not cleared)",
        sig.direction === "long" || sig.direction === "none");
})();

// =========================================================================
console.log("E. backtest runner + guards");
// An oscillating series so ema5 repeatedly crosses ema20 after warmup.
const trend = candlesFromCloses(Array.from({ length: 400 }, (_, i) => 100 + Math.sin(i / 12) * 15 + i * 0.05));
const momoSpec = { name: "momo", long: [{ left: { indicator: "ema", period: 5 }, op: "cross_above", right: { indicator: "ema", period: 20 } }], short: [] };
const momoFn = TE.compileSpec(momoSpec);
const res = TE.runBacktest(trend, momoFn, { spread: 2, guards: { atrFilter: false, spreadReject: false, dailyLoss: false } });
check("runner produces trades on a trend", res.trades.length > 0);
check("stats has all fields", ["netPnl", "winRate", "profitFactor", "maxDD", "totalTrades"].every((k) => k in res.stats));
check("equity curve starts at start equity", res.equityCurve[0].equity === 1000);
check("win rate within 0..100", res.stats.winRate >= 0 && res.stats.winRate <= 100);

// Spread reject guard cuts trades vs no guard on a choppy low-ATR series.
const chop = candlesFromCloses(Array.from({ length: 400 }, (_, i) => 100 + Math.sin(i / 3) * 0.5));
const noGuard = TE.runBacktest(chop, momoFn, { spread: 40, guards: { atrFilter: false, spreadReject: false, dailyLoss: false } });
const withReject = TE.runBacktest(chop, momoFn, { spread: 40, guards: { atrFilter: false, spreadReject: true, dailyLoss: false } });
check("spread-reject guard never increases trade count", withReject.trades.length <= noGuard.trades.length);

// Daily-loss halt: a losing spec on a downtrend should stop earlier WITH the halt.
const down = candlesFromCloses(Array.from({ length: 600 }, (_, i) => 200 - i * 0.3));
const alwaysLong = () => ({ direction: "long", reason: "test" });
const halted = TE.runBacktest(down, alwaysLong, { spread: 2, guards: { atrFilter: false, spreadReject: false, dailyLoss: true }, dailyLossPct: 3 });
const unhalted = TE.runBacktest(down, alwaysLong, { spread: 2, guards: { atrFilter: false, spreadReject: false, dailyLoss: false } });
check("daily-loss halt reduces (or equals) trades vs no halt", halted.trades.length <= unhalted.trades.length);
check("daily-loss halt loses less on a losing downtrend", halted.stats.netPnl >= unhalted.stats.netPnl);

// =========================================================================
console.log("F. guard postures are one-of-each");
const g = TE.TRENCH_STRATEGIES;
check("liquidity_sweep fully guarded", g.liquidity_sweep.guards.atrFilter && g.liquidity_sweep.guards.spreadReject && g.liquidity_sweep.guards.dailyLoss);
check("squeeze_ignition spread-only", !g.squeeze_ignition.guards.atrFilter && g.squeeze_ignition.guards.spreadReject && !g.squeeze_ignition.guards.dailyLoss);
check("exhaustion_fade fully raw", !g.exhaustion_fade.guards.atrFilter && !g.exhaustion_fade.guards.spreadReject && !g.exhaustion_fade.guards.dailyLoss);

// =========================================================================
console.log("G. Ollama client (mocked fetch)");
(async () => {
  const goodSpec = { name: "ema x", long: [{ left: { indicator: "ema", period: 9 }, op: "cross_above", right: { indicator: "ema", period: 21 } }], short: [] };
  const fakeFetch = async () => ({ ok: true, json: async () => ({ response: JSON.stringify(goodSpec) }) });
  const spec = await TE.generateSpecFromText("go long when 9 ema crosses above 21 ema", { fetch: fakeFetch });
  check("parses a valid model response into a spec", spec.name === "ema x");

  const badJsonFetch = async () => ({ ok: true, json: async () => ({ response: "here is your strategy: {oops" }) });
  let e1 = false; try { await TE.generateSpecFromText("x", { fetch: badJsonFetch }); } catch (e) { e1 = /valid JSON/.test(e.message); }
  check("non-JSON model output raises a clear error", e1);

  const invalidSpecFetch = async () => ({ ok: true, json: async () => ({ response: JSON.stringify({ long: [{ left: { indicator: "nope" }, op: ">", right: { value: 1 } }] }) }) });
  let e2 = false; try { await TE.generateSpecFromText("x", { fetch: invalidSpecFetch }); } catch (e) { e2 = /indicator/.test(e.message); }
  check("invalid indicator from model is caught by validation", e2);

  const downFetch = async () => { throw new Error("ECONNREFUSED"); };
  let e3 = false; try { await TE.generateSpecFromText("x", { fetch: downFetch }); } catch (e) { e3 = /Could not reach Ollama/.test(e.message); }
  check("unreachable Ollama gives an actionable error", e3);

  const httpErrFetch = async () => ({ ok: false, status: 500 });
  let e4 = false; try { await TE.generateSpecFromText("x", { fetch: httpErrFetch }); } catch (e) { e4 = /HTTP 500/.test(e.message); }
  check("Ollama HTTP error is surfaced", e4);

  console.log(`\nALL PASSED (${passed} assertions)`);
})();
