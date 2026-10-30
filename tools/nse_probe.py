"""Check which NSE sources answer from this machine (no keys needed)."""
import sys, json, datetime as dt, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import radar
out = {}
d = radar.now_ist().date()
tests = {
    "holidays": lambda: len(radar.holidays()),
    "event_calendar": lambda: len(radar.nse_json(f"https://www.nseindia.com/api/event-calendar?index=equities&from_date={radar.nse_date(d)}&to_date={radar.nse_date(d)}")),
    "announcements": lambda: len(radar.nse_json(f"https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={radar.nse_date(d)}&to_date={radar.nse_date(d)}")),
    "bhavcopy": lambda: len(radar.bhavcopy(radar.prev_td(d)) or {}),
    "index_closes": lambda: len(radar.index_closes(radar.prev_td(d)) or {}),
}
for k, fn in tests.items():
    try: out[k] = {"ok": True, "n": fn()}
    except Exception as e: out[k] = {"ok": False, "error": str(e)[:300]}
out["when"] = radar.stamp()
print(json.dumps(out, indent=1))
open("probe.json", "w").write(json.dumps(out, indent=1))
