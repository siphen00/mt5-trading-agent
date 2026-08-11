"""
Tests for the news and daily-bias agents.
Run from repo root:  python3 -m agents.test_agents

No network: RSS/calendar payloads are synthetic, and price structures are
constructed so the expected bias is unambiguous.
"""
import json
from datetime import datetime, timezone, timedelta

from agents import news_agent as na
from agents import bias_agent as ba

passed = 0
def check(name, cond):
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ok  {name}")


# ===========================================================================
print("A. news: html stripping and scoring")

check("strips tags", na.strip_html("<p>Fed <b>raises</b> rates</p>") == "Fed raises rates")
check("decodes entities", na.strip_html("Oil &amp; gas") == "Oil & gas")
check("drops script blocks", "alert" not in na.strip_html("<script>alert(1)</script>Hi"))

s1, c1 = na.score_text("Fed holds interest rates steady as inflation cools")
check("central bank + macro both matched", "central_bank" in c1 and "macro_data" in c1)
check("multi-factor headline scores high", s1 >= na.HIGH_SCORE)
check("high impact label", na.impact_label(s1) == "high")

s2, c2 = na.score_text("Missile strike escalates conflict near key shipping lane")
check("war keywords matched", "war" in c2)
check("war headline is at least medium", s2 >= na.MEDIUM_SCORE)

s3, c3 = na.score_text("Local bakery wins county pie contest")
check("irrelevant headline scores zero", s3 == 0 and c3 == [])

# a category counts once, no keyword stuffing
s4, _ = na.score_text("war war war war war")
s5, _ = na.score_text("war")
check("repeated keyword doesn't inflate score", s4 == s5)


# ===========================================================================
print("B. news: RSS and Atom parsing")

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Fed signals rate cut</title><link>https://x.com/a</link>
<description>&lt;p&gt;Powell hints at &lt;b&gt;easing&lt;/b&gt;&lt;/p&gt;</description>
<pubDate>Wed, 06 Aug 2026 12:00:00 GMT</pubDate></item>
<item><title>Bakery news</title><link>https://x.com/b</link>
<description>nothing market moving</description>
<pubDate>Wed, 06 Aug 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""
items = na.parse_feed(RSS, "TestFeed", "macro")
check("parses both rss items", len(items) == 2)
check("title extracted", items[0].title == "Fed signals rate cut")
check("summary html stripped", "<b>" not in items[0].summary and "easing" in items[0].summary)
check("pubDate -> iso utc", items[0].published.startswith("2026-08-06T12:00"))
check("default category applied", "macro" in items[1].categories)
check("relevant item outscores filler", items[0].score > items[1].score)

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>ECB raises rates</title><link href="https://y.com/1"/>
<summary>Frankfurt moves on inflation</summary>
<published>2026-08-06T09:30:00Z</published></entry></feed>"""
aitems = na.parse_feed(ATOM, "AtomFeed")
check("parses atom entry", len(aitems) == 1 and aitems[0].title == "ECB raises rates")
check("atom link href extracted", aitems[0].url == "https://y.com/1")
check("atom iso date parsed", aitems[0].published.startswith("2026-08-06T09:30"))

check("malformed xml returns empty, not crash", na.parse_feed("<not xml", "Bad") == [])
check("empty feed returns empty", na.parse_feed("<rss><channel></channel></rss>", "E") == [])


# ===========================================================================
print("C. news: dedupe and ranking")

a = na.NewsItem("Fed cuts rates today", "u1", "A", None, 100, "", ["central_bank"], 5, "medium")
b = na.NewsItem("Fed cuts rates today!", "u2", "B", None, 200, "", ["central_bank"], 9, "high")
c = na.NewsItem("Totally different story", "u3", "C", None, 300, "", [], 3, "low")
out = na.dedupe([a, b, c])
check("near-duplicate titles collapse", len(out) == 2)
check("keeps the higher-scored copy", any(i.url == "u2" for i in out) and not any(i.url == "u1" for i in out)


)

# ===========================================================================
print("D. news: economic calendar filtering")

now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
def ev(title, impact, hours_from_now, country="USD"):
    return {"title": title, "impact": impact, "country": country,
            "date": (now + timedelta(hours=hours_from_now)).isoformat(),
            "forecast": "1.0%", "previous": "0.8%"}

events = [
    ev("Core CPI m/m", "High", 3),
    ev("FOMC Statement", "High", 20),
    ev("Flash PMI", "Medium", 5),
    ev("Bank Holiday", "Low", 2),            # filtered: low impact
    ev("Old NFP", "High", -30),              # filtered: too far in the past
    ev("Next week CPI", "High", 200),        # filtered: beyond window
]
cal = na.parse_calendar(events, now=now)
titles = [c["title"] for c in cal]
check("keeps high and medium only", "Bank Holiday" not in titles)
check("drops stale past events", "Old NFP" not in titles)
check("drops events beyond the window", "Next week CPI" not in titles)
check("keeps the three in-window events", len(cal) == 3)
check("sorted chronologically", cal[0]["title"] == "Core CPI m/m" and cal[-1]["title"] == "FOMC Statement")
check("upcoming flag set", all(c["upcoming"] for c in cal))
check("forecast carried through", cal[0]["forecast"] == "1.0%")

recent = na.parse_calendar([ev("Just released CPI", "High", -2)], now=now)
check("recently released event retained", len(recent) == 1 and not recent[0]["upcoming"])


# ===========================================================================
print("E. bias: indicators")

check("sma warmup null", ba.sma([1,2,3], 5)[0] is None)
check("sma correct", abs(ba.sma([1,2,3,4,5], 5)[-1] - 3.0) < 1e-9)
check("rsi of pure uptrend is 100", abs(ba.rsi(list(range(1,40)))[-1] - 100) < 1e-6)
check("rsi of pure downtrend is 0", abs(ba.rsi(list(range(40,1,-1)))[-1] - 0) < 1e-6)


# ===========================================================================
print("F. bias: swing structure")

def mk(prices):
    """Build candles whose highs/lows follow the given closes."""
    return [{"time": 1_700_000_000 + i*86400, "open": p, "high": p+50,
             "low": p-50, "close": p, "volume": 100} for i, p in enumerate(prices)]

def wave(n=120, base=20000, trend=120, amp=1500, period=10):
    """Realistic swing structure: a trend with oscillation wide enough that
    2-bar fractal pivots actually confirm (a 1-bar zig-zag never would)."""
    import math
    return [base + trend*i + amp*math.sin(2*math.pi*i/period) for i in range(n)]

up = mk(wave())
sw_up = ba.find_swings(up)
check("finds pivots in realistic swing data", len(sw_up) > 5)
bias, desc = ba.structure_from_swings(sw_up)
check("rising wave reads bullish", bias == "bullish")
check("bullish description names HH and HL", "higher high" in desc and "higher low" in desc)

down = mk(wave(trend=-120, base=60000))
bias, desc = ba.structure_from_swings(ba.find_swings(down))
check("falling wave reads bearish", bias == "bearish")
check("bearish description names LH and LL", "lower high" in desc and "lower low" in desc)

flat = mk(wave(trend=0))
check("flat oscillation is not directional",
      ba.structure_from_swings(ba.find_swings(flat))[0] == "ranging")

check("no swings -> ranging", ba.structure_from_swings([])[0] == "ranging")
check("last 2 bars never confirmed as swings (no repaint)",
      all(s.index <= len(up)-3 for s in sw_up))

print("G. bias: confluence")

rising = [100 + i for i in range(250)]
b, d, vals = ba.ma_confluence(rising)
check("price above rising stack is bullish", b == "bullish")
check("ma values populated", vals["sma20"] and vals["sma50"] and vals["sma200"])
falling = [400 - i for i in range(250)]
check("price below falling stack is bearish", ba.ma_confluence(falling)[0] == "bearish")

check("overbought rsi flags mean-reversion risk", ba.rsi_state(rising)[0] == "bearish")
check("oversold rsi flags bounce risk", ba.rsi_state(falling)[0] == "bullish")


# ===========================================================================
print("H. bias: end-to-end")

d1 = mk(wave())
res = ba.build_bias(d1, d1)
check("clean uptrend -> bullish", res.bias == "bullish")
check("confidence is meaningful", res.confidence >= 40)
check("score positive", res.score > 0)
check("rationale states the bias", "BULLISH" in res.rationale)
check("rationale quotes the real price", f"{d1[-1]['close']:,.0f}" in res.rationale)
check("four factors reported", len(res.factors) == 4)
check("levels include support/resistance", "support" in res.levels and "resistance" in res.levels)
check("serialises to json", json.loads(json.dumps(res.to_dict()))["bias"] == "bullish")

dd = mk(wave(trend=-120, base=60000))
resd = ba.build_bias(dd, dd)
check("clean downtrend -> bearish", resd.bias == "bearish")
check("bearish rationale", "BEARISH" in resd.rationale)
check("bearish score negative", resd.score < 0)

flatd = mk(wave(trend=0))
resf = ba.build_bias(flatd, flatd)
check("flat market is not given a strong directional call", resf.bias == "ranging")
check("ranging rationale advises caution", "RANGING" in resf.rationale)

check("insufficient history -> ranging, no crash",
      ba.build_bias(mk([1,2,3]), mk([1,2,3])).bias == "ranging")

# The rationale must be honest about disagreement when factors conflict.
conflicted = [f for f in res.factors if f["bias"] != res.bias and f["bias"] != "ranging"]
if conflicted:
    check("rationale surfaces opposing factors", "against" in res.rationale.lower())
else:
    check("no opposing factors to report", True)

# Ollama polish must never flip the bias.
bull = ba.build_bias(mk(wave()), mk(wave()))
original = bull.rationale
import agents.bias_agent as _ba
_orig_urlopen = None
class _FakeResp:
    def __init__(self, text): self._t = text
    def read(self): return json.dumps({"response": self._t}).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
import urllib.request as _u
_saved = _u.urlopen
_u.urlopen = lambda *a, **k: _FakeResp("The market is clearly BEARISH and falling apart.")
flipped = _ba.polish_with_ollama(bull)
check("ollama rewrite that flips direction is rejected", flipped.rationale == original)
_u.urlopen = lambda *a, **k: _FakeResp("BTC holds a bullish tone with buyers in control.")
ok = _ba.polish_with_ollama(ba.build_bias(mk(wave()), mk(wave())))
check("consistent ollama rewrite is accepted", "bullish tone" in ok.rationale)
_u.urlopen = _saved

print(f"\nALL PASSED ({passed} assertions)")
