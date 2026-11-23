"""
TPA Fare Board (cloud version of the Tampa Flight Tracker)

Runs on a schedule in GitHub Actions. Each run:
  1. Reads the routes from the Google Sheet named in tracker_config.json.
     If the sheet can't be read or a row doesn't make sense, it uses the routes
     below, so a typo never stops the tracker.
  2. Checks Google Flights for every route that's switched on.
  3. Writes docs/data/flights.json (the web page reads this) and docs/data/history.json.
  4. Emails only when a deal shows up that hasn't been emailed before
     (or an already-emailed deal gets at least 5% cheaper).

Nothing here uses Claude, so runs cost no tokens.

Run it on your own computer too:  python flight_tracker.py
(add --no-email to skip email, --sample to write made-up data for testing the page)
"""

from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.request import urlopen, Request
import smtplib
import ssl
import time
import csv
import io
import json
import os
import re
import sys

# ============================================================
# DEFAULTS (used when the Google Sheet can't be read)
# ============================================================

ORIGIN = "TPA"
DEFAULT_SETTINGS = {"passengers": 2, "email_alerts": True}

def _weekend(name, code, limit, domestic):
    return {"name": name, "code": code, "type": "weekend", "limit": limit,
            "start": datetime(2027, 1, 1), "end": datetime(2027, 6, 30),
            "days": [3], "length": 3, "max_layover": 4 if domestic else 6, "requested_by": ""}

DEFAULT_ROUTES = [
    _weekend("Chicago, IL (all airports)", "CHI", 100, True),
    _weekend("Chattanooga, TN", "CHA", 100, True),
    _weekend("Raleigh, NC", "RDU", 100, True),
    _weekend("Richmond, VA", "RIC", 100, True),
    _weekend("Washington D.C.", "DCA", 150, True),
    _weekend("Washington Dulles, VA", "IAD", 100, True),
    _weekend("Seattle, WA", "SEA", 200, True),
    _weekend("Salt Lake City, UT", "SLC", 200, True),
    _weekend("Phoenix, AZ", "PHX", 200, True),
    _weekend("Las Vegas, NV", "LAS", 150, True),
    _weekend("Page, AZ", "PGA", 200, True),
    _weekend("Bozeman, MT", "BZN", 200, True),
    _weekend("Kalispell, MT", "FCA", 200, True),
    _weekend("Jackson Hole, WY", "JAC", 200, True),
    _weekend("Honolulu, HI", "HNL", 400, False),
    _weekend("St. Thomas, USVI", "STT", 250, False),
    _weekend("Cancun, Mexico", "CUN", 250, False),
    _weekend("Mexico City, Mexico", "MEX", 250, False),
    _weekend("Bogota, Colombia", "BOG", 400, False),
    _weekend("Lima, Peru", "LIM", 400, False),
    _weekend("Sao Paulo, Brazil", "GRU", 400, False),
    _weekend("Calgary, Canada", "YYC", 200, False),
    _weekend("London, UK", "LHR", 400, False),
    _weekend("Paris, France", "CDG", 400, False),
    _weekend("Amsterdam, Netherlands", "AMS", 400, False),
    _weekend("Madrid, Spain", "MAD", 400, False),
    {"name": "Tokyo, Japan (all airports)", "code": "TYO", "type": "oneway", "limit": 600,
     "start": datetime(2027, 9, 1), "end": datetime(2027, 11, 30),
     "days": [3, 4, 5], "length": 0, "max_layover": 6, "requested_by": ""},   # Thu, Fri, Sat
]

MAX_STOPS = 2
MAX_WEEKENDS_PER_ROUTE = 16     # same as the home version
MAX_DATES_PER_ROUTE = 100       # one-way / round trip
MAX_SEARCHES_TOTAL = 1100       # keeps a run under ~3 hours even if the sheet gets long
HISTORY_DAYS = 180
REEMAIL_DROP = 0.05             # re-email a known deal if it gets 5% cheaper

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
CONFIG_FILE = os.path.join(ROOT, "tracker_config.json")
STATE_FILE = os.path.join(ROOT, "state.json")      # not shown on the page; remembers emailed deals

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TYPE_LABEL = {"weekend": "Thu–Sun weekend", "oneway": "One-way", "roundtrip": "Round trip"}

# ============================================================
# SETTINGS FROM THE GOOGLE SHEET
# ============================================================

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    for key in ("sheet_id", "gid"):
        if os.environ.get(key.upper()):
            cfg[key] = os.environ[key.upper()]
    return cfg

def fetch_sheet_tab(sheet_id, gid):
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        url += f"&gid={gid}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise ValueError("the sheet isn't shared as 'Anyone with the link can view'")
    return list(csv.reader(io.StringIO(text)))

def yes(value):
    return str(value).strip().lower() in ("yes", "y", "true", "on", "1", "x", "✓", "✔")

def to_number(value):
    m = re.search(r"-?\d+(\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None

def to_date(value):
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None

def to_days(value, default):
    v = str(value).strip().lower()
    if not v:
        return default
    if v in ("any", "all", "any day", "every day", "daily"):
        return list(range(7))
    days = sorted({DAY_NAMES.index(w[:3]) for w in re.split(r"[\s,;/&]+", v) if w[:3] in DAY_NAMES})
    return days or default

def to_type(value):
    v = str(value).strip().lower()
    if "one" in v:
        return "oneway"
    if "week" in v:
        return "weekend"
    if "round" in v or "return" in v:
        return "roundtrip"
    return None

def parse_settings_rows(rows):
    s = dict(DEFAULT_SETTINGS)
    for row in rows:
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        label, value = row[0].strip().lower(), row[1].strip()
        num = to_number(value)
        if ("traveler" in label or "passenger" in label or "people" in label) and num:
            s["passengers"] = int(min(max(num, 1), 9))
        elif "email" in label:
            s["email_alerts"] = yes(value)
    return s

def parse_route_rows(rows, notes):
    """Columns: On | Destination | Airport code | Trip type | Limit per person |
    Earliest date | Latest date | Days | Trip length (days) | Max layover (hrs) | Requested by"""
    routes, requests = [], []
    header_seen = False
    for row in rows:
        row = [c.strip() for c in row] + [""] * 11
        if not header_seen:
            header_seen = any("airport" in c.lower() for c in row)
            continue
        on, name, code, typ, limit, start, end, days, length, layover, who = row[:11]
        if not name and not code:
            continue
        code = code.upper()
        if not yes(on):
            if who:   # someone asked for it and it isn't switched on yet
                requests.append({"name": name or code, "code": code, "requested_by": who})
            continue
        label = name or code
        if not re.fullmatch(r"[A-Z]{3}", code):
            notes.append(f"'{label}' needs a 3-letter airport code, so it was skipped.")
            continue
        t = to_type(typ)
        if not t:
            notes.append(f"'{label}' trip type '{typ}' wasn't Weekend, One-way or Round trip, so it was skipped.")
            continue
        lim = to_number(limit)
        if not lim or lim <= 0:
            notes.append(f"'{label}' has no price limit, so it was skipped.")
            continue
        d1, d2 = to_date(start), to_date(end)
        if not d1 or not d2:
            notes.append(f"'{label}' needs an earliest and latest date (like 2027-09-01), so it was skipped.")
            continue
        if d1 > d2:
            d1, d2 = d2, d1
        n = to_number(length)
        if t == "roundtrip" and not n:
            notes.append(f"'{label}' is a round trip but has no trip length, so it was skipped.")
            continue
        hrs = to_number(layover)
        routes.append({
            "name": label, "code": code, "type": t, "limit": int(lim), "start": d1, "end": d2,
            "days": to_days(days, [3] if t == "weekend" else list(range(7))),
            "length": 3 if t == "weekend" else int(n or 0),
            "max_layover": hrs if hrs and hrs > 0 else 6, "requested_by": who,
        })
    return routes, requests

def load_choices():
    """Returns (settings, routes, requests, notes, source)."""
    notes = []
    cfg = load_config()
    if not cfg.get("sheet_id"):
        return dict(DEFAULT_SETTINGS), DEFAULT_ROUTES, [], notes, "defaults"
    try:
        rows = fetch_sheet_tab(cfg["sheet_id"], cfg.get("gid", ""))
        split = next((i for i, r in enumerate(rows) if any("airport" in c.lower() for c in r)), len(rows))
        settings = parse_settings_rows(rows[:split])
        routes, requests = parse_route_rows(rows[split:], notes)
        if not routes:
            notes.append("No routes were switched on in the sheet, so the usual routes were used.")
            routes = DEFAULT_ROUTES
        return settings, routes, requests, notes, "sheet"
    except Exception as e:
        notes.append(f"Couldn't read the settings sheet ({e}); used the usual routes.")
        return dict(DEFAULT_SETTINGS), DEFAULT_ROUTES, [], notes, "defaults"

# ============================================================
# DATES
# ============================================================

def spread(items, n):
    """Up to n items, evenly spaced across the list (keeps the first and last)."""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]

def trip_dates(route):
    """[(depart, return or None)] for a route, skipping dates that have passed."""
    tomorrow = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    cur, out = max(route["start"], tomorrow), []
    while cur <= route["end"]:
        if cur.weekday() in route["days"]:
            if route["type"] == "oneway":
                out.append((cur, None))
            else:
                back = cur + timedelta(days=route["length"])
                if route["type"] == "weekend" or back <= route["end"]:
                    out.append((cur, back))
        cur += timedelta(days=1)
    cap = MAX_WEEKENDS_PER_ROUTE if route["type"] == "weekend" else MAX_DATES_PER_ROUTE
    return spread(out, cap)

def window_label(route):
    a, b = route["start"], route["end"]
    if route["type"] == "weekend":
        return f"Thursdays {a:%b} – {b:%b %Y}"
    days = "any day" if len(route["days"]) == 7 else ", ".join(DAY_NAMES[d].title() for d in route["days"])
    length = f"{route['length']}-day trip, " if route["type"] == "roundtrip" else ""
    return f"{length}{a:%b} {a.day} – {b:%b} {b.day}, {b.year}, {days}"   # no %-d: Windows lacks it

# ============================================================
# GOOGLE FLIGHTS SCRAPER (same reading rules as the home version)
# ============================================================

class Blocked(Exception):
    pass

def flights_url(origin, dest, depart, back, passengers):
    return (f"https://www.google.com/travel/flights?q=Flights+from+{origin}+to+{dest}"
            f"+on+{depart:%Y-%m-%d}"
            + (f"+returning+{back:%Y-%m-%d}" if back else "+one+way")
            + f"+for+{passengers}+passengers")

FLIGHT_LABEL_RE = re.compile(r'^From ([0-9,]+) US dollars')

def parse_flight_label(label):
    m = FLIGHT_LABEL_RE.match(label)
    if not m:
        return None
    price = int(m.group(1).replace(',', ''))
    if re.search(r'Nonstop flight', label, re.IGNORECASE):
        stops = 0
    else:
        sm = re.search(r'(\d+) stops? flight', label, re.IGNORECASE)
        stops = int(sm.group(1)) if sm else None
    layovers = []
    for hrs, mins in re.findall(r'is an? (?:(\d+) hr)?\s*(?:(\d+) min)?\s*(?:overnight )?layover', label, re.IGNORECASE):
        if hrs or mins:
            layovers.append(int(hrs or 0) + int(mins or 0) / 60)
    return price, stops, layovers

def looks_blocked(page):
    try:
        if "/sorry/" in page.url:
            return True
        text = page.inner_text("body", timeout=3000)[:4000].lower()
        return "unusual traffic" in text or "not a robot" in text
    except Exception:
        return False

def scrape_flight(page, url, max_layover):
    """Cheapest total price meeting the stop/layover rules, or None."""
    try:
        page.goto(url, timeout=30000)
        try:
            page.wait_for_selector('[aria-label^="From "]', timeout=15000)
        except Exception:
            if looks_blocked(page):
                raise Blocked("Google Flights asked for a robot check")
            return None
        page.wait_for_timeout(1500)
        labels = page.eval_on_selector_all('[aria-label]', 'els => els.map(e => e.getAttribute("aria-label"))')
        flights = [f for f in (parse_flight_label(l or "") for l in dict.fromkeys(labels)) if f]
        ok = [f for f in flights if (f[1] is None or f[1] <= MAX_STOPS) and all(h <= max_layover for h in f[2])]
        return min(f[0] for f in ok) if ok else None
    except Blocked:
        raise
    except Exception as e:
        print(f"    Error: {e}")
        return None

def scrape_all(routes, settings, notes):
    from playwright.sync_api import sync_playwright
    pax = settings["passengers"]
    results, searches, got_any = [], 0, False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-US", timezone_id="America/New_York",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        page = ctx.new_page()
        for route in routes:
            dates = trip_dates(route)
            if searches + len(dates) > MAX_SEARCHES_TOTAL:
                notes.append(f"Skipped {route['name']} this time: the run hit its {MAX_SEARCHES_TOTAL}-search limit.")
                results.append({"route": route, "best": None, "dates_checked": 0})
                continue
            print(f"{route['name']} ({route['code']}) — {TYPE_LABEL[route['type']]}, {len(dates)} date(s)")
            best = None
            for depart, back in dates:
                url = flights_url(ORIGIN, route["code"], depart, back, pax)
                price = scrape_flight(page, url, route["max_layover"])
                searches += 1
                if price is not None:
                    got_any = True
                    if best is None or price < best["total"]:
                        best = {"total": price, "depart": depart, "return": back, "link": url}
                elif searches == 15 and not got_any:
                    raise Blocked("Google Flights returned no prices for the first 15 searches")
                time.sleep(2)
            if best:
                print(f"  Best: ${best['total']} total (${best['total'] // pax}/person)")
            else:
                print("  No qualifying flights found.")
            results.append({"route": route, "best": best, "dates_checked": len(dates)})
            time.sleep(3)
        browser.close()
    return results

# ============================================================
# OUTPUT
# ============================================================

def route_key(route):
    return f"{route['type']}:{route['code']}:{route['start']:%Y%m%d}"

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)

def route_json(r, pax, hist):
    route, best = r["route"], r["best"]
    key = route_key(route)
    out = {
        "key": key, "name": route["name"], "code": route["code"], "type": route["type"],
        "type_label": TYPE_LABEL[route["type"]], "window": window_label(route),
        "limit": route["limit"], "max_layover": route["max_layover"],
        "requested_by": route["requested_by"], "dates_checked": r["dates_checked"],
        "total": None, "per_person": None, "is_deal": False, "depart": "", "return": "", "link": "",
        "history": [p["pp"] for p in hist.get(key, [])][-30:],
    }
    if best:
        pp = best["total"] // pax
        out.update(total=best["total"], per_person=pp, is_deal=pp <= route["limit"],
                   depart=f"{best['depart']:%Y-%m-%d}",
                   **{"return": f"{best['return']:%Y-%m-%d}" if best["return"] else ""},
                   link=best["link"])
    return out

def update_history(results, pax, now):
    path = os.path.join(DATA_DIR, "history.json")
    hist = load_json(path, {})
    day = f"{now:%Y-%m-%d}"
    keep_after = f"{now - timedelta(days=HISTORY_DAYS):%Y-%m-%d}"
    for r in results:
        if not r["best"]:
            continue
        key = route_key(r["route"])
        pts = [p for p in hist.get(key, []) if p["d"] != day and p["d"] >= keep_after]
        pts.append({"d": day, "pp": r["best"]["total"] // pax})
        hist[key] = sorted(pts, key=lambda p: p["d"])
    write_json(path, hist)
    return hist

# ============================================================
# EMAIL (only new deals, or known deals that got cheaper)
# ============================================================

def send_email(deals, pax, page_url):
    sender = os.environ.get("EMAIL_SENDER", "")
    password = os.environ.get("EMAIL_PASSWORD", "").replace(" ", "")
    recipient = os.environ.get("EMAIL_RECIPIENT", "") or sender
    if not sender or not password:
        print("  Email skipped: EMAIL_SENDER / EMAIL_PASSWORD aren't set.")
        return False
    n = len(deals)
    subject = f"TPA Fare Board: {n} new deal{'s' if n != 1 else ''} — {datetime.now():%B %d, %Y}"
    rows = ""
    for d in deals:
        dates = f"{d['depart']} → {d['return']}" if d["return"] else f"{d['depart']} (one-way)"
        rows += f"""
        <tr>
          <td style="padding:12px;border-bottom:1px solid #eee;">
            <strong>{d['name']}</strong><br>
            <span style="color:#777;font-size:12px;">{d['type_label']} · {dates}</span></td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;">
            <strong style="color:#1a7f4b;font-size:18px;">${d['per_person']}/person</strong><br>
            <span style="color:#777;font-size:12px;">${d['total']} for {pax} · limit ${d['limit']}</span></td>
          <td style="padding:12px;border-bottom:1px solid #eee;text-align:center;">
            <a href="{d['link']}" style="background:#1a7f4b;color:#fff;padding:8px 14px;border-radius:6px;
               text-decoration:none;font-size:13px;font-weight:bold;">View</a></td>
        </tr>"""
    button = (f'<p style="text-align:center;margin:22px 0 6px;"><a href="{page_url}" '
              'style="background:#13315c;color:#fff;padding:12px 22px;border-radius:999px;'
              'text-decoration:none;font-weight:bold;">Open the Fare Board</a></p>') if page_url else ""
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;color:#222;">
      <h1 style="font-size:22px;margin:16px 0 4px;">New deals from Tampa</h1>
      <p style="color:#666;margin:0 0 12px;">Fares at or under your per-person limit. Max {MAX_STOPS} stops.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">{rows}</table>
      {button}
      <p style="color:#999;font-size:12px;text-align:center;margin-top:14px;">
        Prices are from Google Flights and change often. Check stops and layovers before booking.</p>
    </body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, recipient
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as server:
            server.login(sender, password)
            server.sendmail(sender, [r.strip() for r in recipient.split(",")], msg.as_string())
        print(f"  Emailed {n} deal(s).")
        return True
    except Exception as e:
        print(f"  Email error: {e}")
        return False

# ============================================================
# SAMPLE DATA (for testing the page without scraping)
# ============================================================

def sample_results(routes, pax):
    import random
    random.seed(7)
    out = []
    for route in routes:
        dates = trip_dates(route)
        if not dates or random.random() < 0.06:
            out.append({"route": route, "best": None, "dates_checked": len(dates)})
            continue
        depart, back = random.choice(dates)
        pp = int(route["limit"] * random.uniform(0.7, 1.9))
        out.append({"route": route, "dates_checked": len(dates), "best": {
            "total": pp * pax, "depart": depart, "return": back,
            "link": flights_url(ORIGIN, route["code"], depart, back, pax)}})
    return out

def sample_history(results, pax, now):
    import random
    random.seed(3)
    hist = {}
    for r in results:
        if r["best"]:
            pp = r["best"]["total"] // pax
            hist[route_key(r["route"])] = [
                {"d": f"{now - timedelta(days=3.5 * i):%Y-%m-%d}", "pp": int(pp * random.uniform(0.9, 1.35))}
                for i in range(12, 0, -1)] + [{"d": f"{now:%Y-%m-%d}", "pp": pp}]
    return hist

# ============================================================
# MAIN
# ============================================================

def main():
    args = set(sys.argv[1:])
    now = datetime.now(timezone.utc)
    print(f"TPA Fare Board --- {now:%Y-%m-%d %H:%M} UTC")

    settings, routes, requests, notes, source = load_choices()
    pax = settings["passengers"]
    print(f"Routes from: {source}; {len(routes)} route(s), {pax} traveler(s)")

    cfg = load_config()
    page_url = cfg.get("page_url", "")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{cfg['sheet_id']}/edit" if cfg.get("sheet_id") else ""
    out_path = os.path.join(DATA_DIR, "flights.json")
    previous = load_json(out_path, {})

    status = "ok"
    try:
        results = sample_results(routes, pax) if "--sample" in args else scrape_all(routes, settings, notes)
    except Blocked as e:
        results, status = [], "blocked"
        notes.append(f"{e}. Prices below are from the last good check.")

    if status == "ok" and not any(r["best"] for r in results):
        status = "empty"
        notes.append("Google Flights returned nothing this time. Prices below are from the last good check.")

    if status != "ok":
        previous.update(last_checked=now.isoformat(), status=status, notes=notes,
                        sheet_url=sheet_url, requests=requests)
        write_json(out_path, previous)
        print(f"Check failed ({status}); kept previous results.")
        sys.exit(0 if previous.get("routes") else 1)

    if "--sample" in args:
        hist = sample_history(results, pax, now)
        write_json(os.path.join(DATA_DIR, "history.json"), hist)
    else:
        hist = update_history(results, pax, now)

    routes_out = [route_json(r, pax, hist) for r in results]
    data = {
        "last_checked": now.isoformat(), "last_success": now.isoformat(), "status": "ok",
        "origin": ORIGIN, "passengers": pax, "max_stops": MAX_STOPS, "source": source,
        "notes": notes, "sheet_url": sheet_url, "requests": requests, "routes": routes_out,
    }
    write_json(out_path, data)
    deals = [r for r in routes_out if r["is_deal"]]
    print(f"Wrote {out_path}: {len(routes_out)} routes, {len(deals)} deal(s)")

    state = load_json(STATE_FILE, {"emailed": {}})
    emailed = state.get("emailed", {})
    to_send = [d for d in deals
               if f"{d['key']}|{d['depart']}|{d['return']}" not in emailed
               or d["per_person"] <= emailed[f"{d['key']}|{d['depart']}|{d['return']}"] * (1 - REEMAIL_DROP)]
    if to_send and settings["email_alerts"] and not ({"--no-email", "--sample"} & args):
        if send_email(to_send, pax, page_url):
            for d in to_send:
                emailed[f"{d['key']}|{d['depart']}|{d['return']}"] = d["per_person"]
    elif not to_send:
        print("  No new deals to email.")

    if "--sample" not in args:
        live = {f"{d['key']}|{d['depart']}|{d['return']}" for d in deals}
        write_json(STATE_FILE, {"emailed": {k: v for k, v in emailed.items() if k in live}})
    print("Done.")

if __name__ == "__main__":
    main()
