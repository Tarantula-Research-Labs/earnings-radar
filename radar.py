#!/usr/bin/env python3
"""
Earnings Radar runner — the number work for each scheduled run.

Claude does the parts that need judgement (reading news). This script does
everything that must be exact: which stocks report, when results were filed,
reaction day, prices and returns.

Storage: S3, s3://<bucket>/<prefix>/<season>/ (see config/settings.json)
  events.json   every stock-quarter on the radar, with its prices and reasons
  runs.json     run log, keyed by date
  csv/*.csv     events, prices, reasons, summaries, fundamentals, outlook, run_log (flat tables)
  snapshots/<YYYY-MM-DD>/   copy of all of the above as it stood after that day's last run
On the first run of a season, events start from seed/<season>_events.json.
Credentials come from the usual AWS environment variables.

Commands (chain with commas, e.g. morning,check,log)
  morning  [--date D] [--since D]   add today's (and weekend/holiday) reporters
  check    [--now T]                look for filed results, set reaction day
  evening  [--now T]                check + prices/returns for every tracked stock
  merge    <reasons.json>           add news reasons, 5-day summaries, fundamentals, outlooks
  hypo     [--date D]               every 15 days: group the window's reporters by sector -> out/hypo_todo.json
  hyposave <hypotheses.json>        save the sector hypotheses to <prefix>/hypotheses/
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

    def _read_json(self, name, prefix=None):
        prefix = prefix or self.prefix
        if self.local:
            p = os.path.join(self.local, prefix, name)
            if not os.path.exists(p): return None
            with open(p, encoding="utf-8") as f: return json.load(f)
        try:
            obj = self.s3().get_object(Bucket=self.bucket, Key=f"{prefix}/{name}")
        except self.s3().exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def _write(self, name, text, ctype, prefix=None):
        prefix = prefix or self.prefix
        if self.local:
            p = os.path.join(self.local, prefix, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f: f.write(text)
            return
        self.s3().put_object(Bucket=self.bucket, Key=f"{prefix}/{name}",
                             Body=text.encode("utf-8"), ContentType=ctype)

    def season_prefix(self, sc):
        return f"{self.cfg.get('storage', {}).get('prefix', 'earnings-radar')}/{sc}"

    def other_seasons(self):
        """Season folders in storage other than the current one (plus any listed in track_seasons)."""
        root = self.cfg.get("storage", {}).get("prefix", "earnings-radar")
        found = {re.sub(r"\s+", "", s) for s in self.cfg.get("track_seasons") or []}
        try:
            if self.local:
                d = os.path.join(self.local, root)
                found |= {x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))} if os.path.isdir(d) else set()
            else:
                r = self.s3().list_objects_v2(Bucket=self.bucket, Prefix=f"{root}/", Delimiter="/")
                found |= {c["Prefix"][len(root) + 1:].strip("/") for c in r.get("CommonPrefixes") or []}
        except Exception as ex:
            print(f"Could not list season folders ({ex}); using track_seasons only.")
        found.discard(self.sc)
        return sorted(x for x in found if re.fullmatch(r"Q[1-4]FY\d{2}", x))

    def update_old_season(self, sc, fn):
        """Load an earlier season's events.json, let fn change it, save it back (with its CSVs) if fn returns True."""
        pre = self.season_prefix(sc)
        ev = self._read_json("events.json", pre)
        if not ev: return False
        if not fn(ev): return False
        self._write("events.json", json.dumps(ev, ensure_ascii=False), "application/json", pre)
        runs = self._read_json("runs.json", pre) or {}
        for name, text in export_csvs(ev, runs).items():
            self._write(f"csv/{name}.csv", text, "text/csv", pre)
        return True

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
        day = now_ist().date().isoformat()
        snap = f"snapshots/{day}"
        if not any(k.startswith("events/") for k in self.changed):
            # Only the run log changed: leave events.json alone so this run cannot overwrite
            # stock data another run saved in the meantime.
            self._write("runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
            self._write(f"{snap}/runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
            log = export_csvs({}, self.runs)["run_log"]
            self._write("csv/run_log.csv", log, "text/csv"); self._write(f"{snap}/csv/run_log.csv", log, "text/csv")
            print(f"\nSaved run log to {self.where()} (runs.json, csv/run_log.csv).")
            return
        self._write("events.json", json.dumps(self.events, ensure_ascii=False), "application/json")
        self._write("runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
        csvs = export_csvs(self.events, self.runs)
        for name, text in csvs.items():
            self._write(f"csv/{name}.csv", text, "text/csv")
        # Daily snapshot: the state at the end of today's latest run (overwritten by later runs the same day).
        self._write(f"{snap}/events.json", json.dumps(self.events, ensure_ascii=False), "application/json")
        self._write(f"{snap}/runs.json", json.dumps(self.runs, ensure_ascii=False), "application/json")
        for name, text in csvs.items():
            self._write(f"{snap}/csv/{name}.csv", text, "text/csv")
        n_ev = sum(1 for k in self.changed if k.startswith("events/"))
        print(f"\nSaved to {self.where()}: events.json ({len(self.events)} events, {n_ev} changed), "
              f"runs.json, csv/ ({len(csvs)} files), and a copy in {snap}/.")

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
                         "matches_numbers", "published_at", "captured_at"]),
        "summaries": _csv([dict(e["summary"], event_id=e["event_id"], symbol=e["symbol"]) for e in ev if e.get("summary")],
                          ["event_id", "symbol", "cum_log_return", "cum_abnormal", "cum_sector_return",
                           "top_positive", "top_negative", "verdict", "created_at"]),
        "run_log": _csv([dict(r, date=d) for d, doc in sorted(runs.items()) for r in doc.get("runs", [])],
                        ["date", "task", "started_at", "finished_at", "status", "notes"]),
        "fundamentals": _csv([dict(row, event_id=e["event_id"], symbol=e["symbol"], company=e.get("company"),
                                   sector_index=e.get("sector_index"), results_filed_at=e.get("results_filed_at"),
                                   view=view.upper(), as_of=blk.get("as_of"), recorded_at=blk.get("recorded_at"))
                              for e in ev for view, blk in sorted((e.get("fundamentals") or {}).items())
                              for row in blk.get("rows") or []],
                             ["event_id", "symbol", "company", "sector_index", "results_filed_at", "view", "as_of",
                              "statement", "metric", "basis", "value", "change", "unit", "direction", "sources", "note",
                              "recorded_at"]),
        "outlook": _csv([_outlook_row(e) for e in ev if e.get("outlook")],
                        ["event_id", "symbol", "company", "sector_index", "reaction_day", "written_at", "as_of",
                         "horizon_end", "stance", "confidence", "thesis", "bull", "bull_odds", "base", "base_odds",
                         "bear", "bear_odds", "watch", "catalysts", "risks", "from_date", "from_close", "from_sector",
                         "last_date", "last_close", "last_sector", "stock_return", "sector_return", "excess",
                         "track_status", "verdict"]),
    }

def _outlook_row(e):
    o = e["outlook"]; t = o.get("track") or {}; sc = o.get("scenarios") or {}
    row = {"event_id": e["event_id"], "symbol": e.get("symbol"), "company": e.get("company"),
           "sector_index": e.get("sector_index"), "reaction_day": e.get("reaction_day"),
           **{k: o.get(k) for k in ("written_at", "as_of", "horizon_end", "stance", "confidence", "thesis",
                                    "watch", "catalysts", "risks")},
           **{k: t.get(k) for k in ("from_date", "from_close", "from_sector", "last_date", "last_close",
                                    "last_sector", "stock_return", "sector_return", "excess", "verdict")},
           "track_status": t.get("status")}
    for k in ("bull", "base", "bear"):
        row[k] = (sc.get(k) or {}).get("text"); row[f"{k}_odds"] = (sc.get(k) or {}).get("odds")
    return row

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
    # Fundamentals tables still to write: Day 1 view once D1 is priced, Day 5 view once D5 is priced.
    # Built from every event (including done ones), so a missed table is picked up on a later run.
    fund = []
    for eid, e in sorted(db.all("events").items()):
        fv = e.get("fundamentals") or {}
        p = {x.get("day_n"): x for x in e.get("prices") or []}
        base = {"event_id": eid, "symbol": e.get("symbol"), "company": e.get("company"),
                "results_filed_at": e.get("results_filed_at"), "reaction_day": e.get("reaction_day")}
        if (e.get("day_count") or 0) >= 1 and "d1" not in fv and 1 in p:
            fund.append(dict(base, view="d1", as_of=p[1]["date"]))
        if (e.get("day_count") or 0) >= NEWS_DAYS and "d5" not in fv and NEWS_DAYS in p:
            fund.append(dict(base, view="d5", as_of=p[NEWS_DAYS]["date"]))
    # Outlooks still to write: every stock with day 5 priced and no outlook yet (oldest first).
    outl = []
    for eid, e in sorted(db.all("events").items(), key=lambda kv: (kv[1].get("reaction_day") or "", kv[0])):
        if e.get("outlook") or (e.get("day_count") or 0) < NEWS_DAYS or not e.get("reaction_day"): continue
        p5 = next((p for p in e.get("prices") or [] if p.get("day_n") == NEWS_DAYS), {})
        hz = d_parse(e["reaction_day"]) + dt.timedelta(days=HORIZON_DAYS)
        outl.append({"event_id": eid, "symbol": e.get("symbol"), "company": e.get("company"),
                     "industry": e.get("industry"), "sector_index": e.get("sector_index"),
                     "results_filed_at": e.get("results_filed_at"), "reaction_day": e.get("reaction_day"),
                     "as_of": p5.get("date"), "d5_close": p5.get("close"),
                     "cum_log_return": e.get("cum_log_return"), "cum_abnormal": e.get("cum_abnormal"),
                     "horizon_end": d_iso(hz if is_trading_day(hz) else next_td(hz))})
    # Track every outlook (this season, done stocks too) to its horizon.
    tracked = update_tracks(db.all("events"), today)
    for eid in tracked: db.put("events", eid, db.events[eid])
    old = []
    for sc in db.other_seasons():
        box = {}
        def fn(ev, box=box):
            box["n"] = len(update_tracks(ev, today)); return box["n"] > 0
        try:
            db.update_old_season(sc, fn); old.append(f"{sc} {box.get('n', 0)}")
        except Exception as ex:
            old.append(f"{sc} failed: {ex}")
    os.makedirs(db.out, exist_ok=True)
    with open(os.path.join(db.out, "outlook_todo.json"), "w") as f: json.dump(outl, f, indent=1)
    print(f"Outlooks due: {len(outl)} -> {db.out}/outlook_todo.json"
          + (" (" + ", ".join(x["symbol"] for x in outl) + ")" if outl else ""))
    print(f"Outlook tracks updated: {len(tracked)} this season" + (f"; earlier seasons: {', '.join(old)}" if old else ""))
    with open(os.path.join(db.out, "news_todo.json"), "w") as f: json.dump(news, f, indent=1)
    with open(os.path.join(db.out, "summary_todo.json"), "w") as f: json.dump(summaries, f, indent=1)
    with open(os.path.join(db.out, "fundamentals_todo.json"), "w") as f: json.dump(fund, f, indent=1)
    print(f"Fundamentals tables due: {len(fund)} -> {db.out}/fundamentals_todo.json"
          + (" (" + ", ".join(f"{x['symbol']} {x['view'].upper()}" for x in fund) + ")" if fund else ""))
    print(f"Prices updated for {sum(1 for k in db.changed if k.startswith('events/'))} stock(s).")
    if gaps: print("Missing NSE close (not published yet or symbol absent): " + "; ".join(gaps))
    print(f"News to read: {len(news)} stock-day(s) -> {db.out}/news_todo.json")
    for n in news:
        print(f"  {n['symbol']:<12} D{n['day_n']} {n['date']}  stock {fmt(n['log_return'])}  abnormal {fmt(n['abnormal'])}")
    print(f"5-day summaries due: {len(summaries)} -> {db.out}/summary_todo.json")
    res.update({"news": len(news), "summaries": len(summaries), "fundamentals": len(fund),
                "outlooks_due": len(outl), "tracks": len(tracked), "gaps": gaps})
    return res

def fmt(v): return "—" if v is None else f"{v*100:+.2f}%"

# ---------------------------------------------------------------- merge reasons / summary
CATS = {"results", "margins", "guidance", "brokerage", "sector", "macro", "deals", "management", "valuation", "other"}
DIRS = {"positive", "negative", "mixed"}
CHECKS = {"yes", "no", "partly", "unchecked"}

def norm_time(v):
    """Article publish time -> ISO 8601 in IST, or None if missing/unreadable.
    Accepts ISO 8601 (with or without Z/offset) and RFC 2822 (RSS pubDate).
    A time without a zone is taken as IST."""
    if not v or not isinstance(v, str): return None
    v = v.strip()
    t = None
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            t = parsedate_to_datetime(v)
        except (TypeError, ValueError, IndexError):
            return None
    if t is None: return None
    if t.tzinfo is None: t = t.replace(tzinfo=IST)
    t = t.astimezone(IST)
    if t > now_ist() + dt.timedelta(hours=1): return None  # a future time is a parsing mistake
    return t.isoformat(timespec="seconds")

STATEMENTS = {"p&l": "P&L", "pnl": "P&L", "profit and loss": "P&L", "income statement": "P&L",
              "balance sheet": "Balance sheet", "cash flow": "Cash flow", "ratio": "Ratio", "kpi": "Ratio"}
FDIRS = {"positive", "negative", "neutral"}

def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v): return round(float(v), 2)
    if isinstance(v, str):
        try: return round(float(v.replace("%", "").replace("bps", "").replace(",", "").replace("+", "").strip()), 2)
        except ValueError: return None
    return None

def clean_fund_rows(rows):
    """Validate one fundamentals table (rows the Evening task wrote from news/broker sources)."""
    out, seen = [], set()
    for r in rows or []:
        if not isinstance(r, dict): continue
        metric = str(r.get("metric") or "").strip()[:60]
        if not metric: continue
        st = STATEMENTS.get(str(r.get("statement") or "").strip().lower(), "Ratio")
        unit = ("bps" if str(r.get("unit") or "").strip().lower() == "bps" else "%") if _num(r.get("change")) is not None else None
        row = {"statement": st, "metric": metric,
               "basis": str(r.get("basis") or "").strip()[:20] or None,
               "value": str(r.get("value") or "").strip()[:40] or None,
               "change": _num(r.get("change")), "unit": unit,
               "direction": r.get("direction") if r.get("direction") in FDIRS else "neutral",
               "sources": [str(s)[:40] for s in (r.get("sources") or []) if s][:5],
               "note": str(r.get("note") or "").strip()[:120] or None}
        key = (st, metric.lower(), row["basis"])
        if key in seen: continue
        seen.add(key); out.append(row)
    return out[:25]

# ---------------------------------------------------------------- outlook (view to next quarter's results)
# Written once per stock, after day 5, by the Evening task. Never rewritten, so the track record is honest.
# Scored on the stock's move vs its sector index from the D5 close to the horizon (about the next results).
STANCES = {"positive", "neutral", "negative"}
CONFS = {"low", "medium", "high"}
HORIZON_DAYS = 91           # calendar days after the reaction day, about one quarter
CALL_BAND = 0.02            # positive/negative: right if excess beats the sector by more than 2%
NEUTRAL_BAND = 0.03         # neutral: right if excess stays within ±3%

def _txt(v, n): return str(v or "").strip()[:n] or None

def _list(v, n=5, w=160): return [str(x).strip()[:w] for x in (v or []) if str(x or "").strip()][:n]

def clean_outlook(o, e):
    """Validate the Evening task's outlook for one stock; None if unusable."""
    if not isinstance(o, dict): return None
    stance = str(o.get("stance") or "").lower()
    thesis = _txt(o.get("thesis"), 600)
    if stance not in STANCES or not thesis: return None
    sc, odds = {}, {}
    for k in ("bull", "base", "bear"):
        v = (o.get("scenarios") or {}).get(k)
        text = v.get("text") if isinstance(v, dict) else v
        sc[k] = {"text": _txt(text, 400), "odds": None}
        if isinstance(v, dict) and isinstance(v.get("odds"), (int, float)) and not isinstance(v.get("odds"), bool):
            odds[k] = v["odds"]
    if len(odds) == 3 and all(0 <= x <= 100 for x in odds.values()) and 90 <= sum(odds.values()) <= 110:
        for k in odds: sc[k]["odds"] = int(round(odds[k]))
    p = {x.get("day_n"): x for x in e.get("prices") or []}
    rd = d_parse(e["reaction_day"])
    hz = rd + dt.timedelta(days=HORIZON_DAYS)
    if not is_trading_day(hz): hz = next_td(hz)
    return {"stance": stance, "confidence": o.get("confidence") if o.get("confidence") in CONFS else "low",
            "thesis": thesis, "scenarios": sc,
            "watch": _list(o.get("watch")), "catalysts": _list(o.get("catalysts")), "risks": _list(o.get("risks")),
            "as_of": (p.get(NEWS_DAYS) or {}).get("date"), "horizon_end": d_iso(hz), "written_at": stamp()}

def verdict_for(stance, excess):
    if excess is None: return None
    if stance == "neutral": return "on_track" if abs(excess) <= NEUTRAL_BAND else "off_track"
    s = excess if stance == "positive" else -excess
    return "on_track" if s > CALL_BAND else "off_track" if s < -CALL_BAND else "too_close"

def latest_close_day(upto, floor):
    """Latest trading day <= upto whose NSE stock and index closes are published (not before floor)."""
    d = upto if is_trading_day(upto) else prev_td(upto)
    for _ in range(6):
        if d < floor: return None
        if bhavcopy(d) and index_closes(d): return d
        d = prev_td(d)
    return None

def update_tracks(events, today):
    """Bring every open outlook's track up to the latest close. Returns the ids that changed."""
    changed = []
    for eid, e in events.items():
        o = e.get("outlook")
        if not o: continue
        t = o.setdefault("track", {})
        sym, sx = e.get("symbol"), e.get("sector_index") or "Nifty 500"
        if not t.get("from_close"):
            p5 = next((p for p in e.get("prices") or [] if p.get("day_n") == NEWS_DAYS), None)
            ix = index_closes(d_parse(p5["date"])) if p5 else None
            if not p5 or not ix or sx not in ix: continue
            t.update({"from_date": p5["date"], "from_close": p5["close"], "from_sector": ix[sx]["close"],
                      "status": "open"})
            changed.append(eid)
        if t.get("status") == "final": continue
        hz = d_parse(o["horizon_end"])
        d = latest_close_day(min(today, hz), d_parse(t["from_date"]))
        if not d or (t.get("last_date") and d_iso(d) <= t["last_date"]): continue
        bc, ix = bhavcopy(d), index_closes(d)
        if sym not in bc or sx not in ix: continue
        sr = lr(bc[sym]["close"], t["from_close"]); xr = lr(ix[sx]["close"], t["from_sector"])
        ex = sr - xr if sr is not None and xr is not None else None
        t.update({"last_date": d_iso(d), "last_close": bc[sym]["close"], "last_sector": ix[sx]["close"],
                  "stock_return": sr, "sector_return": xr, "excess": ex,
                  "verdict": verdict_for(o["stance"], ex), "status": "final" if d >= hz else "open",
                  "note": "stock moved over 40% - check for split/bonus" if sr is not None and abs(sr) > 0.4 else None,
                  "updated_at": stamp()})
        if eid not in changed: changed.append(eid)
    return changed

def cmd_merge(db, args):
    with open(args.file, encoding="utf-8") as f: inp = json.load(f)
    events = db.all("events")
    captured = stamp()  # when this run recorded the reasons
    done = []
    for eid, payload in inp.items():
        e = events.get(eid)
        if not e: print(f"skip {eid}: not in database"); continue
        have = {(r.get("url"), r.get("reason")) for r in e.get("reasons") or []}
        add = 0
        for r in payload.get("reasons") or []:
            r = {k: r.get(k) for k in ("day_n", "date", "direction", "category", "reason", "source", "url", "matches_numbers",
                                       "published_at")}
            r["published_at"] = norm_time(r.get("published_at"))
            r["captured_at"] = captured
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
        # Fundamentals tables: {"d1": [rows], "d5": [rows]}. An empty list is kept on purpose
        # (sources gave no figures) so the table is not asked for again.
        fsaved = []
        for view, rows in (payload.get("fundamentals") or {}).items():
            view = str(view).lower()
            if view not in ("d1", "d5") or not isinstance(rows, list): continue
            day = 1 if view == "d1" else NEWS_DAYS
            as_of = next((p.get("date") for p in e.get("prices") or [] if p.get("day_n") == day), None)
            e.setdefault("fundamentals", {})[view] = {"rows": clean_fund_rows(rows), "as_of": as_of, "recorded_at": captured}
            fsaved.append(f"{view.upper()} {len(e['fundamentals'][view]['rows'])} rows")
        oadd = ""
        if payload.get("outlook"):
            if e.get("outlook"):
                print(f"skip outlook for {eid}: already written {e['outlook'].get('written_at')} (outlooks are never rewritten)")
            elif (e.get("day_count") or 0) < NEWS_DAYS or not e.get("reaction_day"):
                print(f"skip outlook for {eid}: day 5 not priced yet")
            else:
                o = clean_outlook(payload["outlook"], e)
                if o:
                    e["outlook"] = o; oadd = f" + outlook ({o['stance']})"
                    update_tracks({eid: e}, now_ist().date())
                else:
                    print(f"skip outlook for {eid}: needs stance positive/neutral/negative and a thesis")
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
        db.put("events", eid, e)
        done.append(f"{e['symbol']} +{add} reason(s)" + (f", fundamentals {', '.join(fsaved)}" if fsaved else "")
                    + (" + summary" if payload.get("summary") else "") + oadd)
    print("Merged: " + ("; ".join(done) or "nothing"))
    return {"merged": len(done)}

# ---------------------------------------------------------------- sector hypotheses (every 15 days)
# For each sector (NSE industry) with results filed in the window, the Hypotheses task writes what
# seems to have driven the stocks' reactions: results, news and analyst views. Kept for every window in
# <prefix>/hypotheses/hypotheses.json (+ hypotheses.csv), outside the season folders.
HYPO_DIR = "hypotheses"
HYPO_DAYS = 15

def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None

def _hypo_prefix(db):
    return db.season_prefix(HYPO_DIR)

def _load_hypos(db):
    return db._read_json("hypotheses.json", _hypo_prefix(db)) or {"windows": {}}

def cmd_hypo(db, args):
    """Write out/hypo_todo.json: the window's stocks grouped by sector, with their moves and evidence."""
    today = d_parse(args.date) if args.date else now_ist().date()
    wid = d_iso(today)
    prev = [w for k, w in (_load_hypos(db).get("windows") or {}).items() if k < wid]
    last_to = max((w.get("to") for w in prev), default=None)
    frm = d_parse(last_to) + dt.timedelta(days=1) if last_to and (today - d_parse(last_to)).days <= 2 * HYPO_DAYS \
        else today - dt.timedelta(days=HYPO_DAYS - 1)
    pool = dict(db.events)
    for sc in db.other_seasons():
        pool.update(db._read_json("events.json", db.season_prefix(sc)) or {})
    groups, too_early = {}, []
    for eid, e in sorted(pool.items()):
        if not e.get("results_filed_at"): continue
        fd = d_parse(e["results_filed_at"])
        if not (frm <= fd <= today): continue
        if (e.get("day_count") or 0) < 1:
            too_early.append(e.get("symbol")); continue
        p = {x.get("day_n"): x for x in e.get("prices") or []}
        fv = e.get("fundamentals") or {}
        fblk = fv.get("d5") or fv.get("d1") or {}
        s = e.get("summary") or {}
        stock = {"event_id": eid, "symbol": e.get("symbol"), "company": e.get("company"),
                 "results_filed_at": e.get("results_filed_at"), "reaction_day": e.get("reaction_day"),
                 "days_tracked": e.get("day_count"),
                 "d1_return": (p.get(1) or {}).get("log_return"), "d1_abnormal": (p.get(1) or {}).get("abnormal"),
                 "total_return": e.get("cum_log_return"), "total_abnormal": e.get("cum_abnormal"),
                 "verdict": s.get("verdict"), "top_positive": s.get("top_positive"), "top_negative": s.get("top_negative"),
                 "fundamentals": [{k: r.get(k) for k in ("metric", "basis", "value", "change", "unit", "direction")}
                                  for r in fblk.get("rows") or []],
                 "reasons": [{k: r.get(k) for k in ("day_n", "direction", "category", "reason", "source")}
                             for r in e.get("reasons") or []],
                 "outlook_stance": (e.get("outlook") or {}).get("stance")}
        key = e.get("industry") or e.get("sector_index") or "Other"
        g = groups.setdefault(key, {"sector": key, "sector_index": e.get("sector_index"), "stocks": []})
        g["stocks"].append(stock)
    todo = []
    for key, g in sorted(groups.items()):
        st = g["stocks"]
        g["stats"] = {"stocks": len(st), "beat_sector": sum(1 for x in st if (x["total_abnormal"] or 0) > 0),
                      "avg_d1_abnormal": _mean([x["d1_abnormal"] for x in st]),
                      "avg_total_abnormal": _mean([x["total_abnormal"] for x in st]),
                      "finished_5_days": sum(1 for x in st if (x["days_tracked"] or 0) >= NEWS_DAYS)}
        todo.append(g)
    out = {"window_id": wid, "from": d_iso(frm), "to": wid, "sectors": todo, "too_early": too_early}
    os.makedirs(db.out, exist_ok=True)
    with open(os.path.join(db.out, "hypo_todo.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"Hypothesis window {frm} to {today}: {len(todo)} sector(s), "
          f"{sum(len(g['stocks']) for g in todo)} stock(s) -> {db.out}/hypo_todo.json")
    for g in todo:
        print(f"  {g['sector']:<40} {g['stats']['stocks']} stock(s), {g['stats']['beat_sector']} beat sector, "
              f"avg abnormal {fmt(g['stats']['avg_total_abnormal'])}")
    if too_early: print("Filed but not priced yet (left out): " + ", ".join(too_early))
    return {"sectors": len(todo), "stocks": sum(len(g["stocks"]) for g in todo)}

CONF3 = {"low", "medium", "high"}

def cmd_hyposave(db, args):
    """Save the Hypotheses task's sector hypotheses for the window in out/hypo_todo.json."""
    with open(os.path.join(db.out, "hypo_todo.json"), encoding="utf-8") as f: todo = json.load(f)
    with open(args.file, encoding="utf-8") as f: inp = json.load(f)
    by = {g["sector"]: g for g in todo["sectors"]}
    secs = []
    for name, h in (inp.get("sectors") or inp).items():
        g = by.get(name)
        if not g or not isinstance(h, dict): print(f"skip {name}: not a sector in this window"); continue
        hyp = _txt(h.get("hypothesis"), 500)
        if not hyp: print(f"skip {name}: no hypothesis"); continue
        secs.append({"sector": name, "sector_index": g.get("sector_index"), "stats": g["stats"],
                     "stocks": [{k: x.get(k) for k in ("event_id", "symbol", "company", "reaction_day", "days_tracked",
                                                       "d1_abnormal", "total_abnormal")} for x in g["stocks"]],
                     "hypothesis": hyp, "results_view": _txt(h.get("results_view"), 500),
                     "news_view": _txt(h.get("news_view"), 500), "analyst_view": _txt(h.get("analyst_view"), 500),
                     "evidence": _list(h.get("evidence"), 5, 200),
                     "would_change_if": _txt(h.get("would_change_if"), 300),
                     "confidence": h.get("confidence") if h.get("confidence") in CONF3 else "low"})
    allh = _load_hypos(db)
    allh.setdefault("windows", {})[todo["window_id"]] = {
        "window_id": todo["window_id"], "from": todo["from"], "to": todo["to"], "written_at": stamp(),
        "too_early": todo.get("too_early") or [], "sectors": secs}
    pre = _hypo_prefix(db)
    db._write("hypotheses.json", json.dumps(allh, ensure_ascii=False), "application/json", pre)
    rows = [dict(sx, window_id=w["window_id"], window_from=w["from"], window_to=w["to"], written_at=w["written_at"],
                 symbols=[x["symbol"] for x in sx["stocks"]], **{f"stat_{k}": v for k, v in sx["stats"].items()})
            for _, w in sorted(allh["windows"].items()) for sx in w["sectors"]]
    db._write("hypotheses.csv", _csv(rows, ["window_id", "window_from", "window_to", "written_at", "sector", "sector_index",
                                             "symbols", "stat_stocks", "stat_beat_sector", "stat_avg_d1_abnormal",
                                             "stat_avg_total_abnormal", "stat_finished_5_days", "confidence", "hypothesis",
                                             "results_view", "news_view", "analyst_view", "evidence", "would_change_if"]),
              "text/csv", pre)
    print(f"Saved {len(secs)} sector hypothesis(es) for {todo['from']} to {todo['to']} -> {pre}/hypotheses.json")
    return {"hypotheses": len(secs)}

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
    fns = {"morning": cmd_morning, "check": cmd_check, "evening": cmd_evening, "merge": cmd_merge,
           "hypo": cmd_hypo, "hyposave": cmd_hyposave}
    bad = [s for s in steps if s not in fns and s != "log"]
    if bad: ap.error(f"unknown command(s): {bad}")
    if "merge" in steps or "hyposave" in steps:
        if not args.files: ap.error("merge / hyposave need a JSON file")
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
