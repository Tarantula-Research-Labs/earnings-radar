# Earnings Radar

Tracks why Nifty 500 stocks rise or fall after quarterly results.

For each company that reports, it records when results were filed, picks the reaction day, and tracks the stock for 5 trading days: daily log returns, the sector index's returns, and the **abnormal return** (stock minus sector). Each evening it also collects the reasons news and analysts give for the move.

## How it runs

Three Claude scheduled tasks (weekdays, IST) — prompts in [TASKS.md](TASKS.md):

| Task | Time | Does |
|---|---|---|
| Morning | 08:45 | Add today's reporters from the NSE event calendar |
| Afternoon | 15:20 | Check NSE announcements for filed results |
| Evening | 21:00 | Check again, price every tracked stock, read news, write 5-day summaries |

`radar.py` does all the number work. Claude only reads the news.

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
| `csv/events.csv`, `prices.csv`, `reasons.csv`, `summaries.csv`, `run_log.csv` | The same data as flat tables, ready for Postgres |

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
