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
OLX periodically changes its page's HTML structure, which can break the
CSS selectors this script relies on (marked with `ADJUST_SELECTOR` in the
code). If that happens:
1. Open the OLX search page in your browser.
2. Right-click a listing → "Inspect".
3. Find the container element for a listing card and any nested elements
   for price/title/location.
4. Update the `soup.select(...)` calls in `parse_listing_cards()` to match.

## 5. Schedule it to run automatically
- **Linux/Mac (cron)**: `crontab -e`, then add a line like:
  ```
  */30 * * * * cd /path/to/olx_monitor && /usr/bin/python3 monitor_olx.py >> log.txt 2>&1
  ```
  (runs every 30 minutes)
- **Windows**: use Task Scheduler to run `python monitor_olx.py` on a
  recurring trigger, with "Start in" set to the script's folder.

## A note on scraping etiquette
- Don't set the check interval too aggressively (every 20-30+ minutes is
  reasonable) — frequent automated requests can get your IP rate-limited
  or blocked.
- This script is for personal use to track a market you're already
  watching manually, not for large-scale data collection or resale.
- Check OLX's terms of service / robots.txt periodically, since scraping
  policies can change.
