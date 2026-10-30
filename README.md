# Earnings Radar

Tracks why Nifty 500 stocks rise or fall after quarterly results.

For each company that reports, it records when results were filed, picks the reaction day, and tracks the stock for 5 trading days: daily log returns, the sector index's returns, and the **abnormal return** (stock minus sector). Each evening it also collects the reasons news and analysts give for the move.

## How it runs

GitHub Actions runs everything (`.github/workflows/radar.yml`), weekdays, IST. GitHub may start a run a few minutes late.

| Run | Time | Does |
|---|---|---|
| Morning | 08:45 | Add today's reporters from the NSE event calendar |
| Afternoon | 15:20 | Check NSE announcements for filed results |
| Evening | 20:50 | Check again, price every tracked stock, then `agent.py` reads the news with Claude (API + web search) and writes reasons, 5-day summaries, fundamentals tables and outlooks; scores outlooks |
| Hypotheses | 23:07 on the 13th and 28th | One hypothesis per sector for the stocks that reported since the last window |

`radar.py` does all the number work; `agent.py` only does the reading, and `radar.py merge` / `hyposave` validate its answers before saving.

**Secrets** (repo Settings → Secrets and variables → Actions): `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (the `earnings-radar-bot` key, S3 only) and `ANTHROPIC_API_KEY`. Optional variables: `RADAR_MODEL` (default `claude-sonnet-5-5`), `RADAR_MAX_STOCKS` (stocks read per evening, default 15).

**Run by hand:** Actions tab → Earnings Radar → Run workflow (task, and `since` to backfill reporters), or push `.github/run-now.json` containing `{"task": "evening"}`.

No packages are needed: S3 is reached with the Python standard library.

## Fundamentals tables

For each stock the Evening run also writes two tables of reported figures (P&L, balance sheet, cash flow, ratios) as the news and broker sources state them: a **Day 1 view** (sources up to the reaction day) and a **Day 5 view** (everything up to day 5). Each row: statement, metric, basis (YoY/QoQ/vs estimate), value, change (% or bps), direction for the stock, sources. Only figures the sources state are recorded. They are stored under `fundamentals` in `events.json` and in `csv/fundamentals.csv`.

## Outlook to next results

After day 5 the Evening run writes the radar's own research view of each stock until its next quarterly results (about 91 days after the reaction day, `horizon_end`): a **stance** against its sector index (positive / neutral / negative), a confidence, a short thesis, **bull / base / bear scenarios** with odds, and what to watch, catalysts and risks. No price targets.

An outlook is written once and never changed. Every evening `radar.py` scores it: the stock's log return from the day-5 close minus its sector index's return over the same days (`excess`).

| Stance | On track / right when |
|---|---|
| positive | excess above +2% |
| negative | excess below −2% |
| neutral | excess within ±3% |

Between −2% and +2% a positive or negative call is "too close". At `horizon_end` the track is marked `final`. Earlier seasons' outlooks keep being scored after the season changes (the script finds every season folder in S3). Stored under `outlook` in `events.json` and in `csv/outlook.csv`. It is a model-generated research view, not investment advice.

## Sector hypotheses

Every 15 days (13th and 28th of each month) a separate task groups the stocks whose results were filed since the last window by sector (NSE industry) and writes, for each sector, a hypothesis of what drove the market's reaction: what the results showed, what the news emphasised, what analysts said, the evidence, and what would prove it wrong. The script adds the numbers (stocks, how many beat the sector, average abnormal moves). Stocks filed but not priced yet wait for the next window. Every window is kept in `s3://trl-earnings-radar/earnings-radar/hypotheses/hypotheses.json` and `hypotheses.csv`, outside the season folders.

## Rules

- Results filed before 15:30 IST on a trading day → that day is the reaction day (D1). Later, or on a non-trading day → next trading day.
- D0 = last close before D1 (the base). Track D1–D5.
- Returns are log returns as decimals. `abnormal = stock − sector index`. Nifty 50 is stored too.
- Stock closes: NSE bhavcopy. Index closes: NSE `ind_close_all` file. Calendar, filings, holidays: NSE APIs.

## Data (S3)

`s3://trl-earnings-radar/earnings-radar/<season>/`

| File | Holds |
|---|---|
| `events.json` | Every stock-quarter: filing time, reaction day, prices, reasons, summary |
| `runs.json` | Run log by date |
| `csv/events.csv`, `prices.csv`, `reasons.csv`, `summaries.csv`, `run_log.csv`, `fundamentals.csv`, `outlook.csv` | The same data as flat tables, ready for Postgres |
| `snapshots/<YYYY-MM-DD>/` | Copy of all of the above as it stood after that day's last run |

The first run of a season starts from `seed/<season>_events.json` if S3 has nothing yet.

## Config

- `config/settings.json` — season, sector-index map, sources, storage
- `config/nifty500.json` — universe (Nifty 500, includes Midcap 150), each stock mapped to a sector index

New season: change `season` in `config/settings.json`. Data goes to a new S3 folder.

## Local testing

```
python3 radar.py morning,check,log --task test --local ./store   # writes to ./store instead of S3
python3 radar.py evening --local ./store
```
