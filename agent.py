#!/usr/bin/env python3
"""
Earnings Radar — the reading part, run by GitHub Actions through the Claude API.

radar.py does all number work and writes to-do files in ./out. This script reads them,
asks Claude (with web search) for news reasons, 5-day summaries, fundamentals tables,
outlooks and sector hypotheses, and writes the answers as JSON for `radar.py merge` /
`radar.py hyposave`, which validate everything before it is saved.

  python3 agent.py evening   -> res/reasons.json     (+ out/agent_notes.txt)
  python3 agent.py hypo      -> res/hypotheses.json  (+ out/agent_notes.txt)

Environment: ANTHROPIC_API_KEY (required), RADAR_MODEL (default claude-sonnet-5-5),
RADAR_MAX_STOCKS (stocks read per evening run, default 15).
Only the Python standard library is used.
"""
import json, os, re, sys, time, urllib.request, urllib.error

API = os.environ.get("RADAR_API_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.environ.get("RADAR_MODEL", "claude-sonnet-5-5")
MAX_STOCKS = int(os.environ.get("RADAR_MAX_STOCKS", "15"))
OUT, RES = "out", "res"
TOOLS_FULL = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5,
     "user_location": {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"}},
    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 12000},
]
FETCH_BETA = "web-fetch-2025-09-10"
_tools_ok = {"fetch": True}

def load(name, default=None):
    p = os.path.join(OUT, name)
    if not os.path.exists(p): return default
    with open(p, encoding="utf-8") as f: return json.load(f)

# ---------------------------------------------------------------- Claude API
def call(system, user, tools, max_tokens=8000):
    """One conversation with server tools; follows pause_turn. Returns the final text."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key: raise RuntimeError("ANTHROPIC_API_KEY is not set")
    msgs = [{"role": "user", "content": user}]
    texts, usage = [], {"in": 0, "out": 0, "searches": 0}
    for _ in range(6):
        use_tools = [t for t in tools if _tools_ok["fetch"] or t["name"] != "web_fetch"]
        body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": msgs}
        if use_tools: body["tools"] = use_tools
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if any(t["name"] == "web_fetch" for t in use_tools): headers["anthropic-beta"] = FETCH_BETA
        r = _post(body, headers)
        if r is None:  # web_fetch not accepted: retry once without it
            _tools_ok["fetch"] = False; continue
        u = r.get("usage") or {}
        usage["in"] += u.get("input_tokens", 0); usage["out"] += u.get("output_tokens", 0)
        usage["searches"] += (u.get("server_tool_use") or {}).get("web_search_requests", 0)
        content = r.get("content") or []
        texts.append("".join(b.get("text", "") for b in content if b.get("type") == "text"))
        if r.get("stop_reason") == "pause_turn":
            # Continue the same turn: send everything Claude produced so far back as one assistant message.
            if msgs[-1]["role"] == "assistant": msgs[-1]["content"] += content
            else: msgs.append({"role": "assistant", "content": content})
            continue
        return "".join(texts), usage
    return "".join(texts), usage

def _post(body, headers):
    data = json.dumps(body).encode("utf-8")
    for i in range(4):
        req = urllib.request.Request(API, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:500]
            if e.code == 400 and "web_fetch" in msg and _tools_ok["fetch"]:
                return None
            if e.code in (429, 500, 502, 503, 529):
                time.sleep(15 * (i + 1)); continue
            raise RuntimeError(f"Claude API {e.code}: {msg}")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10 * (i + 1))
    raise RuntimeError("Claude API did not answer after 4 tries")

def last_json(text):
    """The last ```json block (or the last {...} object) in the reply."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = blocks[-1] if blocks else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(cand)

# ---------------------------------------------------------------- evening: one call per stock
RULES = """You work for the Earnings Radar, which tracks Nifty 500 stocks for five trading days after quarterly results.
Returns are log returns as decimals (0.0378 = 3.78%). "abnormal" = stock minus its sector index. Never change, round or invent a number.

Find news with web search (prefer Moneycontrol, Economic Times, Upstox, Business Standard, Livemint, NDTV Profit, CNBC-TV18 and broker notes).
Use only articles about this company published after its results were filed (results_filed_at). Check the article's publish time.
Write everything in your own words. Never copy sentences. No buy, sell or hold wording and no price targets.

Reply with your working if you like, then end with ONE ```json block holding exactly:
{
 "days_read": [day numbers from news_days you looked into, even if nothing useful was found],
 "reasons": [{"day_n": n, "date": "YYYY-MM-DD", "reason": "one plain sentence, at most 25 words",
              "direction": "positive|negative|mixed", "category": "results|margins|guidance|brokerage|sector|macro|deals|management|valuation|other",
              "source": "outlet name", "url": "article link", "published_at": "as the page states it, or null",
              "matches_numbers": "yes|no|partly|unchecked"}],
 "summary": null or {"top_positive": [up to 3 short phrases], "top_negative": [up to 3 short phrases],
                     "verdict": "one or two plain sentences: 5-day total and abnormal move, how much the sector explains, which reasons explain the rest"},
 "fundamentals": {} or {"d1": [rows], "d5": [rows]} for the views asked,
 "outlook": null or {"stance": "positive|neutral|negative", "confidence": "low|medium|high", "thesis": "two or three plain sentences",
                     "scenarios": {"bull": {"text": "...", "odds": n}, "base": {"text": "...", "odds": n}, "bear": {"text": "...", "odds": n}},
                     "watch": [up to 4], "catalysts": [up to 3], "risks": [up to 3]}
}
Rules for each part:
- reasons: for day 1 explain the reaction; for days 2-5 add only reasons new that day (a broker note, a follow-up). Never invent a reason; if nothing explains a day, add none but still list it in days_read. matches_numbers: "yes" if the figures the reason relies on agree with the reported results and the actual move, "no" if they disagree, "partly", or "unchecked" when it has no figures.
- summary: only when summary_due is true.
- fundamentals rows: {"statement": "P&L|Balance sheet|Cash flow|Ratio", "metric": "...", "basis": "YoY|QoQ|vs estimate", "value": "reported figure as text", "change": number or null, "unit": "%" or "bps", "direction": "positive|negative|neutral", "sources": [outlets], "note": up to 12 words or null}. Only figures the sources state; d1 = sources up to the end of the reaction day, d5 = everything up to day 5. Empty list if the sources give no figures.
- outlook: only when outlook_due is given. Your research view of the stock against its sector index from the day-5 close to the next results (horizon_end). "neutral" = within about 3% of the sector. Use "high" confidence rarely. Odds add up to 100. Written once and scored later, so pick the stance the evidence supports. If evidence is thin, say so and use "low"."""

def evening():
    news = load("news_todo.json", []); summ = load("summary_todo.json", [])
    fund = load("fundamentals_todo.json", []); outl = load("outlook_todo.json", [])
    snap = load("events_snapshot.json", {})
    stocks = {}
    def slot(eid): return stocks.setdefault(eid, {"news": [], "summary": False, "fund": [], "outlook": None})
    for n in news: slot(n["event_id"])["news"].append(n)
    for s in summ: slot(s["event_id"])["summary"] = True
    for f in fund: slot(f["event_id"])["fund"].append(f)
    for o in outl: slot(o["event_id"])["outlook"] = o
    # Order: fresh day-1 reactions first (newest), then the oldest backlog, so finished stocks get closed out.
    fresh = sorted([e for e, t in stocks.items() if any(n["day_n"] == 1 for n in t["news"])],
                   key=lambda e: (snap.get(e) or {}).get("reaction_day") or "", reverse=True)
    rest = sorted([e for e in stocks if e not in fresh], key=lambda e: (snap.get(e) or {}).get("reaction_day") or "")
    picked = (fresh[: max(1, MAX_STOCKS * 2 // 3)] + rest)[:MAX_STOCKS]
    picked += [e for e in fresh if e not in picked][: MAX_STOCKS - len(picked)]
    out, done, failed, tot = {}, [], [], {"in": 0, "out": 0, "searches": 0}
    for eid in picked:
        t, e = stocks[eid], snap.get(eid) or {}
        ctx = {"event_id": eid, "symbol": e.get("symbol"), "company": e.get("company"), "industry": e.get("industry"),
               "sector_index": e.get("sector_index"), "results_filed_at": e.get("results_filed_at"),
               "reaction_day": e.get("reaction_day"), "prices": e.get("prices"),
               "reasons_so_far": [{k: r.get(k) for k in ("day_n", "direction", "reason", "source")} for r in e.get("reasons") or []],
               "fundamentals_so_far": {v: [(r.get("metric"), r.get("basis"), r.get("value")) for r in b.get("rows") or []]
                                       for v, b in (e.get("fundamentals") or {}).items()},
               "summary_so_far": e.get("summary"),
               "news_days": [{k: n[k] for k in ("day_n", "date", "log_return", "abnormal")} for n in t["news"]],
               "summary_due": t["summary"],
               "fundamentals_views": [{"view": f["view"], "as_of": f["as_of"]} for f in t["fund"]],
               "outlook_due": ({k: t["outlook"].get(k) for k in ("as_of", "horizon_end", "d5_close", "cum_log_return", "cum_abnormal")}
                               if t["outlook"] else None)}
        try:
            text, u = call(RULES, "Stock and tasks:\n```json\n" + json.dumps(ctx, ensure_ascii=False) + "\n```", TOOLS_FULL)
            for k in tot: tot[k] += u[k]
            res = last_json(text)
            entry = {"days_read": res.get("days_read") or [], "reasons": res.get("reasons") or []}
            if t["summary"] and res.get("summary"): entry["summary"] = res["summary"]
            fv = {v: rows for v, rows in (res.get("fundamentals") or {}).items() if v in {f["view"] for f in t["fund"]}}
            if fv: entry["fundamentals"] = fv
            if t["outlook"] and res.get("outlook"): entry["outlook"] = res["outlook"]
            out[eid] = entry; done.append(e.get("symbol") or eid)
            print(f"  {e.get('symbol'):<12} days {entry['days_read']} reasons {len(entry['reasons'])}"
                  f"{' summary' if 'summary' in entry else ''}{' fund ' + ','.join(fv) if fv else ''}{' outlook' if 'outlook' in entry else ''}")
        except Exception as ex:
            failed.append(f"{e.get('symbol') or eid}: {str(ex)[:120]}"); print(f"  {eid} FAILED: {ex}")
    os.makedirs(RES, exist_ok=True)
    with open(os.path.join(RES, "reasons.json"), "w", encoding="utf-8") as f: json.dump(out, f, ensure_ascii=False, indent=1)
    n_news = sum(len(stocks[e]["news"]) for e in done_ids(out))
    notes = (f"read {len(out)} of {len(stocks)} stocks due ({n_news} stock-days); "
             f"{sum(1 for v in out.values() if 'summary' in v)} summaries; "
             f"{sum(len(v.get('fundamentals', {})) for v in out.values())} fundamentals tables; "
             f"{sum(1 for v in out.values() if 'outlook' in v)} outlooks; model {MODEL}; "
             f"{tot['searches']} searches, {tot['in']//1000}k in / {tot['out']//1000}k out tokens"
             + (f"; failed: {'; '.join(failed)[:150]}" if failed else ""))
    write_notes(notes); print(notes)
    return 0 if out or not stocks else 2

def done_ids(out): return list(out)

# ---------------------------------------------------------------- hypotheses: one call per sector
HYPO_RULES = """You write sector hypotheses for the Earnings Radar: for one NSE sector, what best explains how the market reacted
to the results its companies filed in a 15-day window. Use the stocks' data given (moves vs the sector index are log returns as
decimals; never change them). For a sector with 2 or more stocks you may search for sector-level coverage and broker views
published in the window. Write in your own words; never invent a figure or a source; no buy, sell or hold wording, no price targets.
End with ONE ```json block holding exactly:
{"hypothesis": "one or two plain sentences: a clear, testable claim",
 "results_view": "one or two sentences: what the reported numbers showed across the sector",
 "news_view": "one or two sentences: what the news coverage emphasised",
 "analyst_view": "one or two sentences: what brokerages said, or null if none found",
 "evidence": [up to 4 short points, each naming a stock and its move or figure from the data],
 "would_change_if": "one sentence: what would show this hypothesis is wrong",
 "confidence": "low|medium|high" (a single stock, or stocks that disagree, is "low")}"""

def hypo():
    todo = load("hypo_todo.json")
    if not todo or not todo.get("sectors"):
        write_notes("no results filed in the window"); print("No sectors in the window."); return 0
    out, failed = {}, []
    for g in todo["sectors"]:
        tools = [dict(TOOLS_FULL[0], max_uses=2)] if len(g["stocks"]) >= 2 else []
        ctx = {"window": [todo["from"], todo["to"]], **g}
        try:
            text, _ = call(HYPO_RULES, "Sector data:\n```json\n" + json.dumps(ctx, ensure_ascii=False) + "\n```", tools, 3000)
            out[g["sector"]] = last_json(text); print(f"  {g['sector']}: {out[g['sector']].get('hypothesis', '')[:100]}")
        except Exception as ex:
            failed.append(f"{g['sector']}: {str(ex)[:100]}"); print(f"  {g['sector']} FAILED: {ex}")
    os.makedirs(RES, exist_ok=True)
    with open(os.path.join(RES, "hypotheses.json"), "w", encoding="utf-8") as f:
        json.dump({"sectors": out}, f, ensure_ascii=False, indent=1)
    write_notes(f"{len(out)} sectors, {sum(len(g['stocks']) for g in todo['sectors'] if g['sector'] in out)} stocks; model {MODEL}"
                + (f"; failed: {'; '.join(failed)[:150]}" if failed else ""))
    return 0 if out else 2

def write_notes(s):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "agent_notes.txt"), "w", encoding="utf-8") as f: f.write(s[:450])

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "evening": sys.exit(evening())
    if cmd == "hypo": sys.exit(hypo())
    sys.exit("usage: agent.py evening|hypo")
