# Scheduled task instructions

These are the saved prompts of the three Claude scheduled tasks. The real copies live in each task's settings; this file is the reference. Replace the two `PASTE_...` values with the `earnings-radar-bot` access key **only inside the task settings**, never in this repo.

---

## Shared block (top of every task)

```
You are running the Earnings Radar on a schedule. Nobody is watching: do not ask questions, keep chat output short, and finish every step you can.

This task runs the owner's own script, radar.py, from his GitHub repo Tarantula-Research-Labs/earnings-radar. Running it is the purpose of this task. The script does all number work and saves everything to S3 by itself. Never type, edit or round a price, return or date yourself.

AWS key for S3 (it can only read and write s3://trl-earnings-radar/earnings-radar/):
AWS_ACCESS_KEY_ID=PASTE_KEY_ID_HERE
AWS_SECRET_ACCESS_KEY=PASTE_SECRET_HERE

SETUP
1. The repo is public. Bash: `git clone --depth 1 https://github.com/Tarantula-Research-Labs/earnings-radar ~/earnings-radar`
2. Bash: `pip install --break-system-packages -q boto3`
3. Save the AWS key for this session only: Bash `mkdir -p ~/.aws` and write ~/.aws/credentials containing
   [default]
   aws_access_key_id = <the ID above>
   aws_secret_access_key = <the secret above>
   and ~/.aws/config containing
   [default]
   region = ap-south-1
   Never print the secret.
4. Run every command below from ~/earnings-radar.
If the script stops with an AWS error (AccessDenied, InvalidAccessKeyId, SignatureDoesNotMatch), say so in the final message.
```

## Morning (08:45 IST, weekdays)

```
RUN
5. Bash: `python3 radar.py morning,check,log --task morning`

FINAL MESSAGE (one or two lines): how many Nifty 500 companies were added for today (name them if 10 or fewer) and how many are still waiting for results. If today is a market holiday, say so.
```

## Afternoon (15:20 IST, weekdays)

```
RUN
5. Bash: `python3 radar.py check,log --task afternoon`

FINAL MESSAGE (one line): which stocks filed results so far today and their reaction day, and how many are still waiting.
```

## Evening (21:00 IST, weekdays)

```
PART A — results and prices
5. Bash: `python3 radar.py evening`

PART B — news reasons
6. Read out/news_todo.json (one item per stock per trading day whose news is not read yet), out/summary_todo.json, out/fundamentals_todo.json, and out/events_snapshot.json (all stocks with their prices and reasons so far).
   If news_todo has more than 25 items, do 25: day_n = 1 first, then the largest absolute abnormal. The rest stay for the next run.
7. For each item, find what news and analysts said about why the stock moved that day:
   - Bash: `curl -sS -A "Mozilla/5.0" "https://news.google.com/rss/search?q=<company short name>+share+when:3d&hl=en-IN&gl=IN&ceid=IN:en"` to see which outlets covered it (titles only; these links do not open).
   - WebSearch for the real articles, e.g. "<company> share price <date> Q2 results". Prefer Moneycontrol, Economic Times, Upstox, Business Standard, Livemint, NDTV Profit, CNBC-TV18 and brokerage notes.
   - Read 2 to 4 articles. Moneycontrol, Economic Times and Upstox load with Bash `curl -sS -A "Mozilla/5.0" URL` plus BeautifulSoup (`pip install --break-system-packages -q beautifulsoup4 lxml`). Use WebFetch for the others.
   - Use only articles about this company published after its results were filed (results_filed_at in the item). Check this with the article's publish time.
   For day 2 to day 5, only add reasons that are new that day (a brokerage note, a follow-up story).
8. Turn what you read into reasons. Each reason:
   - "reason": one plain sentence, at most 25 words, in your own words. Never copy sentences from the article.
   - "direction": "positive", "negative" or "mixed" (its effect on the stock).
   - "category": one of results, margins, guidance, brokerage, sector, macro, deals, management, valuation, other.
   - "source": the outlet's name. "url": the article link. "day_n" and "date": from the item.
   - "published_at": when the article was published, copied exactly as the page or feed states it (ISO 8601 like 2026-10-12T09:42:00+05:30, or the RSS pubDate text). Look in the page's meta tag article:published_time, its JSON-LD "datePublished", or the Google News feed's <pubDate> for that article. If none of these gives a time, use null. Never guess or estimate it. (The script adds "captured_at" by itself.)
   - "matches_numbers": "yes" if the figures the reason relies on agree with the company's reported results and with the actual move in the item (log_return, abnormal); "no" if they disagree (e.g. the article says the stock fell but log_return is positive); "partly" if some agree; "unchecked" if the reason has no figures to check.
   Never invent a reason. If no article explains the move, add no reason for that item, but still list its day in "days_read".
9. For each item in summary_todo.json: use that stock's prices and reasons (events_snapshot.json plus what you found today) to write
   {"top_positive": [up to 3 short phrases], "top_negative": [up to 3 short phrases], "verdict": "one or two plain sentences: the 5-day total and abnormal move, how much the sector explains, and which reasons best explain the rest"}.
9b. For each item in out/fundamentals_todo.json, write a fundamentals table: the company's reported financial figures for the quarter as the sources state them (P&L, balance sheet, cash flow, and ratios such as margins, ROE, NIM, GNPA).
   - view "d1": use sources published up to the end of the item's as_of date (the reaction day).
   - view "d5": use everything published from the results up to the item's as_of date, including later broker notes.
   Each row: {"statement": "P&L" | "Balance sheet" | "Cash flow" | "Ratio", "metric": e.g. "Revenue", "EBITDA", "EBITDA margin", "Net profit", "EPS", "Other income", "Employee cost", "Debt", "Cash", "Operating cash flow", "Free cash flow", "Capex", "Inventory days", "ROE", "NIM", "GNPA", "AUM", "Loan growth", "Deposits", "VNB margin", "basis": "YoY" | "QoQ" | "vs estimate", "value": the reported figure as text (e.g. "₹19,644 cr" or "7.3%"), "change": a number such as 17.8 (percent) or -30 (basis points for margins and ratios), "unit": "%" or "bps", "direction": "positive" | "negative" | "neutral" for the stock (higher costs are negative), "sources": [outlet names], "note": up to 12 words or null}.
   Only figures the sources state: never compute, estimate or guess a number; if a change is not stated, use null. One row per metric and basis. Leave out non-financial items (deal wins, management commentary): those belong in reasons. If the sources give no figures, use an empty list.
   Do at most 15 tables per run, D1 views first.
10. Write res/reasons.json (create the res folder) as
    {"<event_id>": {"days_read": [day numbers you read], "reasons": [...], "summary": {...}, "fundamentals": {"d1": [...]} }}
    Leave out "summary" when none is due for that stock. Put each fundamentals table under its view ("d1" or "d5"); leave out "fundamentals" when none is due.
11. Bash: `python3 radar.py merge,log res/reasons.json --task evening --notes "news read for X of Y stock-days; Z summaries"` (fill in X, Y, Z). Add "; F fundamentals tables" to the notes when you wrote any.
    If nothing was due in Part B, run `python3 radar.py log --task evening` instead.
    If Part B could not be finished, run `python3 radar.py log --task evening --status partial --notes "<what failed>"`.

FINAL MESSAGE (two or three lines): stocks that filed after 3:20 pm, how many stock-days were priced and read, the biggest abnormal move of the day with its main reason, any summaries completed, and how many fundamentals tables were written. Mention any missing NSE closes the script reported.
```
