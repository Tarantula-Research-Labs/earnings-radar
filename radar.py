#!/usr/bin/env python3
"""
Earnings Radar runner — the number work for each scheduled run.

Claude does the parts that need judgement (reading news). This script does
everything that must be exact: which stocks report, when results were filed,
reaction day, prices and returns.

Storage: S3, s3://<bucket>/<prefix>/<season>/ (see config/settings.json)
  events.json   every stock-quarter on the radar, with its prices and reasons
  runs.json     run log, keyed by date
  csv/*.csv     events, prices, reasons, summaries, run_log (flat tables)
  snapshots/<YYYY-MM-DD>/   copy of all of the above as it stood after that day's last run
On the first run of a season, events start from seed/<season>_events.json.
Credentials come from the usual AWS environment variables.

Commands (chain with commas, e.g. morning,check,log)
  morning  [--date D] [--since D]   add today's (and weekend/holiday) reporters
  check    [--now T]                look for filed results, set reaction day
  evening  [--now T]                check + prices/returns for every tracked stock
  merge    <reasons.json>           add news reasons / 5-day summaries
  log      --task T [--status S] [--notes N]   add a run-log line

Flags: --out DIR (todo files + local snapshot, default ./out),
       --local DIR (use a local folder instead of S3, for testing).

Returns are log returns as decimals. abnormal = stock - sector index.
D0 = base close (trading day before reaction day). D1 = reaction day.
"""
import argparse, csv, datetime as dt, io, json, math, os, re, sys, time, zipfile
import urllib.request, urllib.error

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
CACHE = os.path.join(os.getcwd(), ".cache")
MARKET_CLOSE = dt.time(15, 30)
NEWS_DAYS = 5

# ---------------------------------------------------------------- http + cache
def http_get(url, accept="*/*", tries=3):
    """Bytes, or None for 404/403-on-archive (file not published yet)."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                                       "Accept-Language": "en-IN,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or (e.code == 403 and "nsearchives" in url):
                return None
            last = e
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 + 3 * i)
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")

def nse_json(url):
    b = http_get(url, accept="application/json")
    if b is None:
        raise RuntimeError(f"NSE returned nothing: {url}")
    return json.loads(b)

def cached(name, fn):
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    v = fn()
    if v is not None:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(v, f)
    return v

# ---------------------------------------------------------------- dates
def d_iso(d): return d.isoformat()
def d_parse(s): return dt.date.fromisoformat(s[:10])
def nse_date(d): return d.strftime("%d-%m-%Y")
def now_ist(): return dt.datetime.now(IST)

_holidays = None
def holidays():
    global _holidays
    if _holidays is None:
        def load():
            d = nse_json("https://www.nseindia.com/api/holiday-master?type=trading")
            return [dt.datetime.strptime(x["tradingDate"], "%d-%b-%Y").date().isoformat() for x in d.get("CM", [])]
        _holidays = set(cached(f"holidays_{now_ist().date().isoformat()}.json", load))
    return _holidays

def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in holidays()

def next_td(d):
    d += dt.timedelta(days=1)
    while not is_trading_day(d): d += dt.timedelta(days=1)
    return d

def prev_td(d):
    d -= dt.timedelta(days=1)
    while not is_trading_day(d): d -= dt.timedelta(days=1)
    return d

# ---------------------------------------------------------------- market data
def bhavcopy(d):
    """{symbol: {close, prev_close}} for EQ series, or None if not published yet."""
    def load():
        url = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        b = http_get(url)
        if b is None: return None
        z = zipfile.ZipFile(io.BytesIO(b))
        out = {}
        for r in csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8")):
            if r["SctySrs"] in ("EQ", "BE"):
                out[r["TckrSymb"]] = {"close": float(r["ClsPric"]), "prev_close": float(r["PrvsClsgPric"])}
        return out
    # only cache published files (cached() skips None)
    return cached(f"bhav_{d:%Y%m%d}.json", load)

def index_closes(d):
    """{index name: {close, prev_close}} or None if not published yet."""
    def load():
        b = http_get(f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv")
        if b is None: return None
        out = {}
        for r in csv.DictReader(io.StringIO(b.decode("utf-8", "replace"))):
            try:
                c = float(r["Closing Index Value"]); ch = float(r["Points Change"])
            except (ValueError, KeyError):
                continue
            out[r["Index Name"].strip()] = {"close": c, "prev_close": round(c - ch, 4)}
        return out
    return cached(f"idx_{d:%Y%m%d}.json", load)

def lr(a, b):
    return math.log(a / b) if a and b and a > 0 and b > 0 else None

# ---------------------------------------------------------------- storage (S3 or local folder)
REPO = os.path.dirname(os.path.abspath(__file__))

def season_code(cfg): return re.sub(r"\s+", "", cfg.get("season", "season"))

def read_repo_json(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return json.load(f)

def _real_key_id(v):
    return bool(v) and len(v) == 20 and v[:4] in ("AKIA", "ASIA") and v.isalnum()

def aws_session(region="ap-south-1"):
    """boto3 session that ignores placeholder AWS_* variables.

    Some sandboxes set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY to a placeholder
    (e.g. "proxy-redacted"). boto3 prefers environment variables over
    ~/.aws/credentials, so the placeholder would be sent to AWS and rejected as
    InvalidAccessKeyId. Drop them when they are not a real key id.
    """
    import boto3
    if not _real_key_id(os.environ.get("AWS_ACCESS_KEY_ID", "")):
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            os.environ.pop(k, None)
    return boto3.session.Session(region_name=region)

class Store:
    """Loads the season's state once, lets commands change it, saves it once."""
    def __init__(self, out, local=None):
        self.out, self.local = out, local
        self.cfg = read_repo_json("config", "settings.json")
        self.uni = read_repo_json("config", "nifty500.json")
        self.sc = season_code(self.cfg)
        st = self.cfg.get("storage", {})
        self.bucket = st.get("bucket"); self.region = st.get("region", "ap-south-1")
        self.prefix = f"{st.get('prefix', 'earnings-radar')}/{self.sc}"
        self._s3 = None
        self.events = self._read_json("events.json")
        if self.events is None:
            seed = os.path.join(REPO, "seed", f"{self.sc}_events.json")
            self.events = read_repo_json("seed", f"{self.sc}_events.json") if os.path.exists(seed) else {}
            print(f"No events.json in storage yet; starting from {'seed file' if self.events else 'empty'} "
                  f"({len(self.events)} events).")
        self.runs = self._read_json("runs.json") or {}
        self.changed = set()
        self._snapshot()

    # --- low-level
    def where(self):
        return os.path.join(self.local, self.prefix) if self.local else f"s3://{self.bucket}/{self.prefix}"

    def s3(self):
        if self._s3 is None:
            self._s3 = aws_session(self.region).client("s3")
        return self._s3

    def _read_json(self, name):
        if self.local:
            p = os.path.join(self.local, self.prefix, name)
            if not os.path.exists(p): return None
            with open(p, encoding="utf-8") as f: return json.load(f)
        try:
            obj = self.s3().get_object(Bucket=self.bucket, Key=f"{self.prefix}/{name}")
        except self.s3().exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def _write(self, name, text, ctype):
        if self.local:
            p = os.path.join(self.local, self.prefix, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f: f.write(text)
            return
        self.s3().put_object(Bucket=self.bucket, Key=f"{self.prefix}/{name}",
                             Body=text.encode("utf-8"), ContentType=ctype)

    def _snapshot(self):
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "events_snapshot.json"), "w", encoding="utf-8") as f:
            json.dump(self.events, f, ensure_ascii=False, indent=1)

    # --- interface used by the commands
    def get(self, coll, doc_id):
        if coll == "config": return self.cfg
        if coll == "universe": return self.uni
        if coll == "events": return self.events.get(doc_id)
        if coll == "runs": return self.runs.get(doc_id)
        return None

    def all(self, coll):
        return {k: v for k, v in self.events.items()} if coll == "events" else {}

    def put(self, coll, doc_id, doc):
        if coll == "events": self.events[doc_id] = doc
        elif coll == "runs": self.runs[doc_id] = doc
        self.changed.add(f"{coll}/{doc_id}")

    def flush(self):
        self._snapshot()
        if not self.changed:
            print(f"\nNothing changed; nothing saved to {self.where()}."); return
        self._write("events.json", json.dumps(self.events, ensure_ascii=False), "application/json")
        self._write("runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
        csvs = export_csvs(self.events, self.runs)
        for name, text in csvs.items():
            self._write(f"csv/{name}.csv", text, "text/csv")
        # Daily snapshot: the state at the end of today's latest run (overwritten by later runs the same day).
        day = now_ist().date().isoformat()
        snap = f"snapshots/{day}"
        self._write(f"{snap}/events.json", json.dumps(self.events, ensure_ascii=False), "application/json")
        self._write(f"{snap}/runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
        for name, text in csvs.items():
            self._write(f"{snap}/csv/{name}.csv", text, "text/csv")
        n_ev = sum(1 for k in self.changed if k.startswith("events/"))
        print(f"\nSaved to {self.where()}: events.json ({len(self.events)} events, {n_ev} changed), "
              f"runs.json, csv/ (5 files), and a copy in {snap}/.")

def _csv(rows, cols):
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["; ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else v)
                    for v in (r.get(c) for c in cols)])
    return buf.getvalue()

def export_csvs(events, runs):
    ev = [dict(e, event_id=k) for k, e in sorted(events.items())]
    return {
        "events": _csv(ev, ["event_id", "symbol", "company", "industry", "sector_index", "season", "meeting_date",
                            "purpose", "prev_close", "prev_close_date", "results_filed_at", "reaction_day", "status",
                            "day_count", "cum_log_return", "cum_abnormal", "updated_at"]),
        "prices": _csv([dict(p, event_id=e["event_id"], symbol=e["symbol"]) for e in ev for p in e.get("prices") or []],
                       ["event_id", "symbol", "day_n", "date", "close", "prev_close", "log_return",
                        "sector_log_return", "nifty_log_return", "abnormal", "source", "note"]),
        "reasons": _csv([dict(r, event_id=e["event_id"], symbol=e["symbol"]) for e in ev for r in e.get("reasons") or []],
                        ["event_id", "symbol", "day_n", "date", "direction", "category", "reason", "source", "url",
                         "matches_numbers"]),
        "summaries": _csv([dict(e["summary"], event_id=e["event_id"], symbol=e["symbol"]) for e in ev if e.get("summary")],
                          ["event_id", "symbol", "cum_log_return", "cum_abnormal", "cum_sector_return",
                           "top_positive", "top_negative", "verdict", "created_at"]),
        "run_log": _csv([dict(r, date=d) for d, doc in sorted(runs.items()) for r in doc.get("runs", [])],
                        ["date", "task", "started_at", "finished_at", "status", "notes"]),
    }

def load_ctx(db):
    cfg = db.get("config", "settings") or {}
    uni = db.get("universe", "nifty500") or {}
    umap = {s["symbol"]: s for s in uni.get("symbols", [])}
    return cfg, umap

def stamp(): return now_ist().isoformat(timespec="seconds")

# ---------------------------------------------------------------- morning
def cmd_morning(db, args):
    cfg, umap = load_ctx(db)
    today = d_parse(args.date) if args.date else now_ist().date()
    if not is_trading_day(today) and not args.since:
        print(f"{today} is not a trading day. Nothing to do."); return {"added": 0, "skipped": "holiday"}
    start = d_parse(args.since) if args.since else prev_td(today) + dt.timedelta(days=1)
    url = (f"https://www.nseindia.com/api/event-calendar?index=equities"
           f"&from_date={nse_date(start)}&to_date={nse_date(today)}")
    rows = nse_json(url)
    events = db.all("events")
    sc = season_code(cfg)
    added = []
    for r in rows:
        if "financial results" not in (r.get("purpose") or "").lower(): continue
        sym = r.get("symbol")
        if sym not in umap: continue
        eid = f"{sym}_{sc}"
        if eid in events or f"events/{eid}" in db.changed: continue
        mdate = dt.datetime.strptime(r["date"], "%d-%b-%Y").date()
        u = umap[sym]
        # provisional base: last close before the meeting (fixed once filing time is known)
        base_d = prev_td(mdate) if is_trading_day(mdate) else prev_td(mdate + dt.timedelta(days=1))
        bc = bhavcopy(base_d) or {}
        doc = {"symbol": sym, "company": u.get("company") or r.get("company"), "industry": u.get("industry"),
               "sector_index": u.get("sector_index") or "Nifty 500", "season": cfg.get("season"),
               "meeting_date": d_iso(mdate), "purpose": r.get("purpose"),
               "bm_desc": (r.get("bm_desc") or "")[:300],
               "prev_close": (bc.get(sym) or {}).get("close"), "prev_close_date": d_iso(base_d) if bc.get(sym) else None,
               "results_filed_at": None, "reaction_day": None, "status": "waiting", "day_count": 0,
               "cum_log_return": None, "cum_abnormal": None, "prices": [], "reasons": [], "summary": None,
               "created_at": stamp(), "updated_at": stamp()}
        db.put("events", eid, doc); events[eid] = doc; added.append(sym)
    print(f"Meetings {start}..{today}: {len(rows)} on NSE calendar; added {len(added)} Nifty 500 reporter(s): {', '.join(added) or 'none'}")
    return {"added": len(added), "symbols": added}

# ---------------------------------------------------------------- check
FILING_RE = re.compile(r"financial result", re.I)

def find_filing(anns, sym, mdate):
    best = None
    for a in anns:
        if a.get("symbol") != sym: continue
        desc = (a.get("desc") or ""); text = (a.get("attchmntText") or "")
        if "intimation" in desc.lower(): continue
        if not (desc.lower().startswith("outcome of board meeting") or desc.lower().startswith("financial result")
                or (FILING_RE.search(text) and "submitted" in text.lower())):
            continue
        if "to consider" in text.lower(): continue  # notice of a future meeting
        t = dt.datetime.strptime(a["an_dt"], "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        if t.date() < mdate: continue
        if best is None or t < best: best = t
    return best

def reaction_day_for(t):
    d = t.date()
    if is_trading_day(d) and t.timetz().replace(tzinfo=None) < MARKET_CLOSE:
        return d
    return next_td(d)

def cmd_check(db, args):
    now = dt.datetime.fromisoformat(args.now).astimezone(IST) if args.now else now_ist()
    today = now.date()
    events = db.all("events")
    waiting = {k: e for k, e in events.items() if e.get("status") == "waiting" and e.get("meeting_date")
               and d_parse(e["meeting_date"]) <= today}
    if not waiting:
        print("No stocks waiting for results today."); return {"filed": 0}
    frm = min(d_parse(e["meeting_date"]) for e in waiting.values())
    anns = nse_json(f"https://www.nseindia.com/api/corporate-announcements?index=equities"
                    f"&from_date={nse_date(frm)}&to_date={nse_date(today)}")
    filed, stale = [], []
    for eid, e in waiting.items():
        md = d_parse(e["meeting_date"])
        t = find_filing(anns, e["symbol"], md)
        if t and t <= now:
            rd = reaction_day_for(t)
            base = prev_td(rd)
            e.update({"results_filed_at": t.isoformat(timespec="seconds"), "reaction_day": d_iso(rd),
                      "prev_close_date": d_iso(base), "status": "tracking", "updated_at": stamp()})
            bc = bhavcopy(base) if base <= today else None
            if bc and e["symbol"] in bc: e["prev_close"] = bc[e["symbol"]]["close"]
            db.put("events", eid, e); filed.append(f"{e['symbol']} {t:%d-%b %H:%M} -> D1 {rd:%d-%b}")
        else:
            # three trading days past the meeting and nothing filed
            d, n = md, 0
            while d < today and n < 3:
                d = next_td(d); n += 1
            if n >= 3 and d <= today:
                e.update({"status": "no_results", "updated_at": stamp()}); db.put("events", eid, e)
                stale.append(e["symbol"])
    print(f"Filed: {len(filed)} " + ("| " + "; ".join(filed) if filed else ""))
    if stale: print("Marked no_results (3 trading days, nothing filed): " + ", ".join(stale))
    still = [e["symbol"] for k, e in waiting.items() if e.get("status") == "waiting"]
    print(f"Still waiting: {', '.join(still) or 'none'}")
    return {"filed": len(filed), "stale": stale, "waiting": still}

# ---------------------------------------------------------------- evening
def cmd_evening(db, args):
    res = cmd_check(db, args)
    now = dt.datetime.fromisoformat(args.now).astimezone(IST) if args.now else now_ist()
    today = now.date()
    cfg, _ = load_ctx(db)
    mkt = cfg.get("market_index", "Nifty 50")
    events = db.all("events")
    news, summaries, gaps = [], [], []
    for eid, e in sorted(events.items()):
        if e.get("status") != "tracking" or not e.get("reaction_day"): continue
        rd = d_parse(e["reaction_day"])
        if rd > today: continue
        days = [prev_td(rd), rd]
        while len(days) < NEWS_DAYS + 1: days.append(next_td(days[-1]))  # D0..D5, never D6
        sym, sx = e["symbol"], e.get("sector_index") or "Nifty 500"
        old = {p.get("day_n"): p for p in e.get("prices") or []}
        rows = []
        for n, d in enumerate(days):
            if d > today: break
            bc, ix = bhavcopy(d), index_closes(d)
            if not bc or sym not in bc:
                if d < today or now.time() >= dt.time(19, 0): gaps.append(f"{sym} D{n} {d}")
                break
            s = bc[sym]
            row = {"day_n": n, "date": d_iso(d), "close": s["close"], "prev_close": s["prev_close"], "source": "NSE bhavcopy"}
            if n >= 1:
                sr = lr(s["close"], s["prev_close"])
                xr = lr(ix[sx]["close"], ix[sx]["prev_close"]) if ix and sx in ix else None
                mr = lr(ix[mkt]["close"], ix[mkt]["prev_close"]) if ix and mkt in ix else None
                row.update({"log_return": sr, "sector_log_return": xr, "nifty_log_return": mr,
                            "abnormal": (sr - xr) if sr is not None and xr is not None else None})
                if sr is not None and abs(sr) > 0.25:
                    row["note"] = "move over 25% - check for split/bonus"
            rows.append(row)
        if not rows: continue
        d1 = [r for r in rows if r["day_n"] >= 1]
        e["prices"] = rows
        e["prev_close"], e["prev_close_date"] = rows[0]["close"], rows[0]["date"]
        e["day_count"] = len(d1)
        e["cum_log_return"] = sum(r["log_return"] for r in d1 if r.get("log_return") is not None) if d1 else None
        e["cum_abnormal"] = sum(r["abnormal"] for r in d1 if r.get("abnormal") is not None) if d1 else None
        e["updated_at"] = stamp()
        db.put("events", eid, e)
        news_done = set(e.get("news_done") or [])
        for r in d1:
            if r["day_n"] not in news_done:
                news.append({"event_id": eid, "symbol": sym, "company": e.get("company"), "day_n": r["day_n"],
                             "date": r["date"], "log_return": r["log_return"], "abnormal": r["abnormal"],
                             "sector_index": sx, "results_filed_at": e.get("results_filed_at")})
        if e["day_count"] >= NEWS_DAYS and not e.get("summary"):
            summaries.append({"event_id": eid, "symbol": sym, "cum_log_return": e["cum_log_return"],
                              "cum_abnormal": e["cum_abnormal"], "reasons_so_far": len(e.get("reasons") or [])})
    os.makedirs(db.out, exist_ok=True)
    with open(os.path.join(db.out, "news_todo.json"), "w") as f: json.dump(news, f, indent=1)
    with open(os.path.join(db.out, "summary_todo.json"), "w") as f: json.dump(summaries, f, indent=1)
    print(f"Prices updated for {sum(1 for k in db.changed if k.startswith('events/'))} stock(s).")
    if gaps: print("Missing NSE close (not published yet or symbol absent): " + "; ".join(gaps))
    print(f"News to read: {len(news)} stock-day(s) -> {db.out}/news_todo.json")
    for n in news:
        print(f"  {n['symbol']:<12} D{n['day_n']} {n['date']}  stock {fmt(n['log_return'])}  abnormal {fmt(n['abnormal'])}")
    print(f"5-day summaries due: {len(summaries)} -> {db.out}/summary_todo.json")
    res.update({"news": len(news), "summaries": len(summaries), "gaps": gaps})
    return res

def fmt(v): return "—" if v is None else f"{v*100:+.2f}%"

# ---------------------------------------------------------------- merge reasons / summary
CATS = {"results", "margins", "guidance", "brokerage", "sector", "macro", "deals", "management", "valuation", "other"}
DIRS = {"positive", "negative", "mixed"}
CHECKS = {"yes", "no", "partly", "unchecked"}

def cmd_merge(db, args):
    with open(args.file, encoding="utf-8") as f: inp = json.load(f)
    events = db.all("events")
    done = []
    for eid, payload in inp.items():
        e = events.get(eid)
        if not e: print(f"skip {eid}: not in database"); continue
        have = {(r.get("url"), r.get("reason")) for r in e.get("reasons") or []}
        add = 0
        for r in payload.get("reasons") or []:
            r = {k: r.get(k) for k in ("day_n", "date", "direction", "category", "reason", "source", "url", "matches_numbers")}
            r["direction"] = r["direction"] if r["direction"] in DIRS else "mixed"
            r["category"] = r["category"] if r["category"] in CATS else "other"
            r["matches_numbers"] = r["matches_numbers"] if r["matches_numbers"] in CHECKS else "unchecked"
            r["reason"] = (r.get("reason") or "").strip()[:300]
            if not r["reason"] or (r.get("url"), r["reason"]) in have: continue
            e.setdefault("reasons", []).append(r); have.add((r.get("url"), r["reason"])); add += 1
        # days whose news has been read (even when nothing useful was found)
        days = {int(d) for d in payload.get("days_read") or []}
        days |= {int(r["day_n"]) for r in payload.get("reasons") or [] if r.get("day_n") is not None}
        if days:
            e["news_done"] = sorted(set(e.get("news_done") or []) | days)
        if payload.get("summary"):
            s = payload["summary"]
            d1 = [p for p in e.get("prices") or [] if p.get("day_n", 0) >= 1]
            e["summary"] = {"cum_log_return": e.get("cum_log_return"), "cum_abnormal": e.get("cum_abnormal"),
                            "cum_sector_return": sum(p["sector_log_return"] for p in d1 if p.get("sector_log_return") is not None) if d1 else None,
                            "top_positive": [str(x)[:200] for x in (s.get("top_positive") or [])][:5],
                            "top_negative": [str(x)[:200] for x in (s.get("top_negative") or [])][:5],
                            "verdict": str(s.get("verdict") or "")[:600], "created_at": stamp()}
            e["status"] = "done"
        e["updated_at"] = stamp()
        db.put("events", eid, e); done.append(f"{e['symbol']} +{add} reason(s){' + summary' if payload.get('summary') else ''}")
    print("Merged: " + ("; ".join(done) or "nothing"))
    return {"merged": len(done)}

# ---------------------------------------------------------------- run log
def cmd_log(db, args, auto_status="ok", auto_notes=""):
    today = now_ist().date().isoformat()
    doc = db.get("runs", today) or {"date": today, "runs": []}
    status = args.status or auto_status
    notes = " | ".join(x for x in [args.notes, auto_notes] if x)
    doc["runs"].append({"task": args.task or "run", "started_at": args.started or stamp(), "finished_at": stamp(),
                        "status": status, "notes": notes[:500]})
    db.put("runs", today, doc)
    print(f"Run log: {args.task} {status} — {notes[:200]}")

# ---------------------------------------------------------------- selftest
def selftest(args):
    """Show which AWS credentials boto3 actually uses (never the secret) and test them."""
    print("AWS_* environment variables set:", sorted(k for k in os.environ if k.startswith("AWS_")) or "none")
    print("placeholder key in environment (ignored):",
          bool(os.environ.get("AWS_ACCESS_KEY_ID")) and not _real_key_id(os.environ.get("AWS_ACCESS_KEY_ID")))
    for p in ("~/.aws/credentials", "~/.aws/config"):
        p = os.path.expanduser(p)
        print(f"{p}: {'exists' if os.path.exists(p) else 'missing'}")
    s = aws_session()
    c = s.get_credentials()
    if not c:
        print("boto3 found NO credentials"); return
    f = c.get_frozen_credentials()
    kid, sec = f.access_key or "", f.secret_key or ""
    print(f"credential source: {c.method}")
    print(f"key id: {kid[:4]}…{kid[-4:]}  length {len(kid)} (should be 20)  "
          f"clean: {kid == kid.strip() and kid.isalnum()}")
    print(f"secret: length {len(sec)} (should be 40)  surrounding spaces: {sec != sec.strip()}  "
          f"token present: {bool(f.token)}")
    print(f"region: {s.region_name}")
    try:
        print("STS identity:", s.client("sts").get_caller_identity()["Arn"].split(":")[-1])
    except Exception as e:
        print("STS error:", getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__), "-", e)
    try:
        st = Store(args.out, None)
        print(f"S3 read OK: {st.where()} ({len(st.events)} events)")
    except Exception as e:
        print("S3 error:", e)

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", help="one command, or several joined by commas, e.g. morning,check,log")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--out", default="out"); ap.add_argument("--local")
    ap.add_argument("--date"); ap.add_argument("--since"); ap.add_argument("--now")
    ap.add_argument("--task"); ap.add_argument("--status"); ap.add_argument("--notes"); ap.add_argument("--started")
    args = ap.parse_args()
    steps = [c.strip() for c in args.cmd.split(",") if c.strip()]
    if steps == ["selftest"]: return selftest(args)
    fns = {"morning": cmd_morning, "check": cmd_check, "evening": cmd_evening, "merge": cmd_merge}
    bad = [s for s in steps if s not in fns and s != "log"]
    if bad: ap.error(f"unknown command(s): {bad}")
    if "merge" in steps:
        if not args.files: ap.error("merge needs the reasons JSON file")
        args.file = args.files[0]
    args.started = args.started or stamp()
    db = Store(args.out, args.local)
    notes, status = [], "ok"
    for s in steps:
        if s == "log": continue
        try:
            r = fns[s](db, args) or {}
            notes.append(f"{s}: " + ", ".join(f"{k}={v if not isinstance(v, list) else len(v)}" for k, v in r.items()))
        except Exception as e:
            status = "failed" if not db.changed else "partial"
            notes.append(f"{s} ERROR: {e}")
            print(f"!!! {s} failed: {e}", file=sys.stderr)
            break
    if "log" in steps:
        cmd_log(db, args, auto_status=status, auto_notes="; ".join(notes))
    db.flush()
    if status != "ok": sys.exit(1)

if __name__ == "__main__":
    main()
