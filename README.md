# OLX Property Opportunity Monitor

## 1. Install dependencies
```
pip install -r requirements.txt
```

## 2. Configure
Open `monitor_olx.py` and edit the CONFIGURATION section near the top:
- `SEARCH_URLS`: go to olx.ro, set up your search filters (city, price,
  rooms, etc.), copy the URL from your browser into this list.
- `MAX_PRICE_PER_SQM`: your threshold for what counts as a "good deal".
- Optionally enable `EMAIL_ENABLED = True` and fill in your email settings
  to get alerts sent to your inbox instead of just printed to the console.

## 3. Run it
```
python monitor_olx.py
```

The first run will report all currently-matching listings (since none are
"seen" yet). After that, it only alerts on *new* listings that appear.

## 4. If it stops finding listings
Two different failure modes look similar (0 listings parsed) but need
different fixes — check the log for which one it is:
- **`403 Client Error` / "Request blocked" in the log**: OLX's CloudFront
  WAF is blocking the request by TLS/HTTP fingerprint (not just
  User-Agent). This script already works around it with `curl_cffi`'s
  Chrome impersonation (`IMPERSONATE` near the top of the file) — if it
  starts happening again, try bumping `IMPERSONATE` to a newer Chrome
  version string (curl_cffi ships a fixed set of supported versions).
- **No error, just 0 cards parsed**: OLX changed its page's HTML
  structure, breaking the CSS selectors this script relies on (marked
  with `ADJUST_SELECTOR` in the code). If that happens:
  1. Open the OLX search page in your browser.
  2. Right-click a listing → "Inspect".
  3. Find the container element for a listing card and any nested
     elements for price/title/location.
  4. Update the `soup.select(...)` calls in `parse_listing_cards()` to
     match.

## 5. Results

Every run writes:
- `monitor_log.txt` — raw run history (local runs only, via `run_monitor.bat`).
- `opportunities.html` — a browsable table of every opportunity found so
  far, sorted by best price/m² first. Backed by `found_opportunities.json`.

## 6. Schedule it to run automatically

**Option A — GitHub Actions (works even when your machine is off).**
This repo includes `.github/workflows/monitor.yml`, which runs the script
once a day (07:00 UTC) on GitHub's servers and commits the updated
`opportunities.html`, `seen_listings.json`, and `found_opportunities.json`
back to the repo. To use it:
1. Push this repo to GitHub (`gh repo create ... --push`).
2. Enable GitHub Pages (Settings → Pages → Deploy from branch → `master` /
   `/`, or `gh api -X POST repos/<owner>/<repo>/pages -f "source[branch]=master" -f "source[path]=/"`).
3. View results any time at `https://<username>.github.io/<repo>/opportunities.html`.
4. Trigger a run manually with `gh workflow run monitor.yml`, or wait for
   the daily schedule.

Only run **one** scheduler at a time (GitHub Actions *or* local) — running
both hits OLX twice as often and leaves the local and remote state files
(`seen_listings.json`, etc.) out of sync with each other.

**Option B — locally (only runs while your machine is on).**
- **Windows**: `run_monitor.bat` is already wired into Task Scheduler as a
  task named "OLX Monitor" (currently disabled in favor of GitHub Actions
  — re-enable with `schtasks /change /tn "OLX Monitor" /enable` if you
  want to run locally instead).
- **Linux/Mac (cron)**: `crontab -e`, then add a line like:
  ```
  0 * * * * cd /path/to/olx_monitor && /usr/bin/python3 monitor_olx.py >> monitor_log.txt 2>&1
  ```
  (runs hourly)

## A note on scraping etiquette
- Don't set the check interval too aggressively (every 20-30+ minutes is
  reasonable) — frequent automated requests can get your IP rate-limited
  or blocked.
- This script is for personal use to track a market you're already
  watching manually, not for large-scale data collection or resale.
- Check OLX's terms of service / robots.txt periodically, since scraping
  policies can change.
