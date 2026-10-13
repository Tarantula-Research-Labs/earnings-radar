# Earnings Radar

Tracks why Nifty 500 stocks rise or fall after quarterly results.

For each company that reports, it records when results were filed, picks the reaction day, and tracks the stock for 5 trading days: daily log returns, the sector index's returns, and the **abnormal return** (stock minus sector). Each evening it also collects the reasons news and analysts give for the move.

## How it runs

Three Claude scheduled tasks (weekdays, IST) — prompts in [TASKS.md](TASKS.md):

| Task | Time | Does |
|---|---|---|
| Morning | 08:45 | Add today's reporters from the NSE event calendar |
| Afternoon | 15:20 | Check NSE announcements for filed results |
| Evening | 21:00 | Check again, price every tracked stock, read news, write 5-day summaries, fundamentals and outlooks, score outlooks |

`radar.py` does all the number work. Claude only reads the news.

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
pip install boto3
python3 radar.py morning,check,log --task test --local ./store   # writes to ./store instead of S3
python3 radar.py evening --local ./store
```
