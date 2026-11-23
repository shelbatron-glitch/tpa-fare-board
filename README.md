# TPA Fare Board

Checks Google Flights twice a week (Monday and Thursday mornings) for cheap flights out of Tampa, posts the results to a phone-friendly page the family can open, and emails when a new deal appears. Runs on GitHub's free scheduled jobs, so it doesn't need your computer to be on and doesn't use Claude.

## What's here

| File | What it does |
|---|---|
| `flight_tracker.py` | The checker. Reads the routes sheet, searches Google Flights, writes the results. |
| `tracker_config.json` | Which Google Sheet holds the routes, and the page's address (for the email button). |
| `.github/workflows/check-flights.yml` | Runs the checker on schedule, and adds a "Run workflow" button for a check on demand. (`check-flights-workflow.yml.txt` is the same file, in case the hidden `.github` folder doesn't upload.) |
| `docs/` | The web page. `docs/data/` is filled in by each check. |
| `routes_sheet_template.csv` | Starting point for the routes sheet: every current route, including Tokyo. |
| `state.json` | Created by the first check. Remembers which deals were already emailed. |

## Routes sheet

1. In Google Sheets: **File → Import → Upload** `routes_sheet_template.csv` → "Replace spreadsheet".
2. **Share → General access: Anyone with the link → Viewer** (so the checker can read it). Give family **Editor** access by email if they should be able to request routes.
3. Copy the long ID out of the sheet's address (between `/d/` and `/edit`) into `tracker_config.json` as `sheet_id`.

Each row is one route:

| Column | What to put |
|---|---|
| On | `Yes` to check it. Leave blank to pause it. |
| Destination | Any name you like, shown on the page. |
| Airport code | 3 letters. City codes work for all airports: `TYO` Tokyo, `CHI` Chicago, `LON` London, `NYC`. |
| Trip type | `Weekend` (Thursday → Sunday), `One-way`, or `Round trip`. |
| Limit per person ($) | A fare at or under this is a deal. |
| Earliest / Latest date | Like `2027-09-01`. |
| Days | Which departure days: `Any`, or e.g. `Thu, Fri, Sat`. Blank = Thursday for weekends, any day otherwise. |
| Trip length (days) | Round trips only. |
| Max layover (hrs) | Blank = 6. |
| Requested by | Who asked. A row with a name here but `On` blank shows on the page as "waiting". |

The top of the sheet also has **Travelers** (default 2) and **Email alerts** (Yes/No). If the sheet can't be read or a row doesn't make sense, the checker uses the built-in routes and says so at the top of the page.

## One-time setup

1. Create a **public** repository on your personal GitHub account (shelbatron-glitch), e.g. `tpa-fare-board`, and upload everything in this folder (free GitHub Pages needs public; the sheet ID and page are the only things exposed, no passwords).
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `EMAIL_SENDER`: the Gmail address that sends alerts
   - `EMAIL_PASSWORD`: its Gmail App Password (same one as in `email_settings.txt`)
   - `EMAIL_RECIPIENT`: who gets alerts (comma-separate for more than one)
3. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Save. The page address appears at the top after a minute.
4. Put that address in `tracker_config.json` as `page_url`.
5. **Actions → Check flights → Run workflow** to do the first check now. A full check (27 routes, ~450 searches) takes about an hour.

## On a phone

Open the page address, then:
- iPhone (Safari): Share → **Add to Home Screen**
- Android (Chrome): ⋮ → **Add to Home screen**

## If something goes wrong

- **The page says the latest check didn't go through.** Google may be showing GitHub's servers a robot check. The page keeps the last good prices. If it keeps happening, run `python flight_tracker.py` on your computer instead (same weekly timer as now) and upload the `docs/data` folder to the repository.
- **A route says "No fares".** Airlines only sell about 11 months out, so far-off dates (like late-2027 Tokyo) fill in as they open. Otherwise check the airport code.
- **GitHub emails about a failed run.** Open the run under Actions to see the log.
- Scheduled runs pause after 60 days without any change to the repository. Each check commits its results, so this shouldn't happen while it's working.

## Testing locally

```
pip install -r requirements.txt
python -m playwright install chromium
python flight_tracker.py --no-email      # real check, no email
python flight_tracker.py --sample        # made-up prices, to look at the page
```
