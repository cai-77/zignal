"""
Debug script — prints raw Finnhub earnings_calendar response for a list of symbols.
Run: python debug_earnings.py <FINNHUB_API_KEY> [SYMBOL ...]
Default symbols: GOOG NVDA AAPL MSFT AMZN
"""
import sys
from datetime import date, timedelta

def main():
    import getpass
    if len(sys.argv) >= 2:
        api_key = sys.argv[1]
        symbols = sys.argv[2:] if len(sys.argv) > 2 else ["GOOG", "NVDA", "AAPL", "MSFT", "AMZN"]
    else:
        api_key = getpass.getpass("Finnhub API key: ")
        raw = input("Symbols (comma-separated, or Enter for GOOG NVDA AAPL): ").strip()
        symbols = [s.strip().upper() for s in raw.split(",")] if raw else ["GOOG", "NVDA", "AAPL"]

    try:
        import finnhub
    except ImportError:
        print("finnhub package not installed — run: pip install finnhub-python")
        sys.exit(1)

    client = finnhub.Client(api_key=api_key)
    today = date.today()
    window_end = today + timedelta(days=365)   # wider window to catch everything

    print(f"Today          : {today}")
    print(f"Query window   : {today} → {window_end}  (365 days)")
    print("=" * 70)

    for symbol in symbols:
        print(f"\n{'─' * 70}")
        print(f"SYMBOL: {symbol}")
        print(f"{'─' * 70}")
        try:
            resp = client.earnings_calendar(
                _from=today.isoformat(),
                to=window_end.isoformat(),
                symbol=symbol,
                international=False,
            )
            events = resp.get("earningsCalendar", [])
            if not events:
                print("  ⚠  No events returned by API in the next 365 days")
            else:
                print(f"  {len(events)} event(s) returned:")
                for i, ev in enumerate(events):
                    raw_date = ev.get("date", "MISSING")
                    try:
                        parsed = date.fromisoformat(raw_date)
                        delta  = (parsed - today).days
                        future = "FUTURE" if parsed >= today else "PAST ← our filter skips this"
                    except ValueError:
                        parsed, delta, future = None, "?", "UNPARSEABLE"
                    print(f"  [{i}] date={raw_date}  ({delta}d from today)  {future}")
                    print(f"       hour={ev.get('hour','')!r}  epsEstimate={ev.get('epsEstimate')}  "
                          f"epsActual={ev.get('epsActual')}  year={ev.get('year')}  quarter={ev.get('quarter')}")

            # Also show what our code would return
            print()
            print("  → What our fetch_earnings_context() would return:")
            picked = None
            for ev in events:
                try:
                    edate = date.fromisoformat(ev["date"])
                except (KeyError, ValueError):
                    continue
                if edate < today:
                    continue
                picked = {
                    "date": edate.isoformat(),
                    "days_away": (edate - today).days,
                    "eps_estimate": ev.get("epsEstimate"),
                    "hour": ev.get("hour", ""),
                }
                break
            if picked:
                print(f"     date={picked['date']}  days_away={picked['days_away']}  "
                      f"eps={picked['eps_estimate']}  hour={picked['hour']!r}")
            else:
                print("     None  (no future date found)")

        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\n{'=' * 70}")
    print("Done.")

if __name__ == "__main__":
    main()
