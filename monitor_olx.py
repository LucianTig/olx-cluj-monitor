"""
OLX Property Opportunity Monitor
=================================

Scrapes an OLX real estate search (e.g. apartments for sale/rent in Cluj-Napoca),
extracts price and area, computes price/m2, and flags listings that look like
good deals compared to a threshold you set.

IMPORTANT NOTES BEFORE YOU RUN THIS:
1. OLX has no public API for this, so we scrape the HTML. Their page structure
   can change, which will break the CSS selectors below (search for
   "ADJUST_SELECTOR" comments if that happens — inspect the page with your
   browser's DevTools and update accordingly).
2. Be a polite scraper: don't run this too often, and don't hammer the
   site with parallel requests. Check OLX's robots.txt / terms of service
   for any restrictions on automated access before relying on this
   long-term. Requests go through curl_cffi with Chrome TLS impersonation
   (see IMPERSONATE below) because OLX's CloudFront WAF blocks plain HTTP
   clients by TLS/HTTP fingerprint, not just by User-Agent header.
3. Prices/areas are parsed from the listing title & card text with regex —
   real listings are messy, so some entries may be skipped if they don't
   match the expected pattern. That's expected and safe (better to skip than
   to misread a price).

SETUP:
    pip install curl_cffi beautifulsoup4

USAGE:
    python monitor_olx.py

    Run it manually first to confirm it finds listings, then schedule it
    (cron on Linux/Mac, Task Scheduler on Windows) to run periodically.
"""

import json
import re
import time
import smtplib
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit this section
# ─────────────────────────────────────────────────────────────────────────

# The OLX search URL for what you want to monitor. Build this by doing the
# search manually on olx.ro with your filters (city, price range, rooms,
# etc.) and copying the resulting URL here.
SEARCH_URLS = [
    "https://www.olx.ro/imobiliare/apartamente-garsoniere-de-vanzare/cluj-napoca/",
]

# Flag a listing as a "good opportunity" if its price per m2 is at or below
# this value (in EUR or RON — match whatever currency OLX shows for your
# search). This is the upper bound for what gets stored at all — the HTML
# report then lets you filter down further into the bands below.
MAX_PRICE_PER_SQM = 3000

# Price/m2 bands offered as a dropdown filter in the HTML report. Each band
# is [lower, upper) except the last, which includes MAX_PRICE_PER_SQM.
# Adjust based on the market research you already did for Cluj.
PRICE_BANDS = [2200, 2500, 2800, 3000]

# Safety ceiling on how many listing pages deep to check per search URL.
# In practice pagination stops well before this once it detects the last
# real page or catches up to listings already seen in a previous run (see
# scrape_search) — this just bounds worst case if that detection ever fails.
MAX_PAGES = 60

# Where to keep track of listings we've already alerted on, so we don't
# spam repeat notifications for the same ad.
SEEN_FILE = Path("seen_listings.json")

# Cumulative record of every opportunity ever found (used to render the
# HTML report below), and the report file itself.
FOUND_FILE = Path("found_opportunities.json")
HTML_REPORT_FILE = Path("opportunities.html")

# Delay between requests (seconds) — be polite to OLX's servers
REQUEST_DELAY = 3

# Optional email alerts — leave EMAIL_ENABLED = False to just log to console
EMAIL_ENABLED = False
EMAIL_FROM = "your_email@gmail.com"
EMAIL_TO = "your_email@gmail.com"
EMAIL_APP_PASSWORD = "your_app_password"   # use an app-specific password, not your real password
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# OLX's CloudFront WAF blocks plain HTTP clients by TLS/HTTP fingerprint,
# not just User-Agent — curl_cffi's `impersonate` reproduces a real Chrome
# fingerprint end-to-end, so no extra headers are needed here.
IMPERSONATE = "chrome124"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Listing:
    listing_id: str
    title: str
    price: float
    area_sqm: Optional[float]
    price_per_sqm: Optional[float]
    location: str
    url: str


# ─────────────────────────────────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────────────────────────────────

def fetch_page(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, impersonate=IMPERSONATE, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except RequestException as e:
        log.error(f"Failed to fetch {url}: {e}")
        return None


def parse_price(text: str) -> Optional[float]:
    """Extract a numeric price from strings like '65 000 €' or '1.200 lei'."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_area(text: str) -> Optional[float]:
    """Extract area in sqm from listing text, e.g. '70 mp' or '70 m²'."""
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:mp\b|m²)", text, re.IGNORECASE)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


def parse_listing_cards(soup: BeautifulSoup) -> list[Listing]:
    """
    Parse individual listing cards from an OLX search results page.

    ADJUST_SELECTOR: OLX listing cards currently use a data-cy attribute
    like 'l-card' on the container, with a numeric DOM `id` attribute that
    serves as a reliable listing id (many real-estate cards link out to
    storia.ro instead of olx.ro, so the old "ID...html"-in-URL pattern
    doesn't always match). Area (e.g. "66 m²") is rendered as a card
    parameter next to the location/date line, not inside the title. If
    this stops matching, inspect the page in your browser (right-click a
    listing -> Inspect) and update the selectors below.
    """
    listings = []
    cards = soup.select("[data-cy='l-card']")

    for card in cards:
        try:
            listing_id = card.get("id")

            link_tag = card.select_one("[data-testid='card-title-link']") or card.select_one("a")
            if not link_tag or not link_tag.get("href"):
                continue
            href = link_tag["href"]
            url = href if href.startswith("http") else f"https://www.olx.ro{href}"

            if not listing_id:
                listing_id_match = re.search(r"ID([A-Za-z0-9]+)", url)
                listing_id = listing_id_match.group(1) if listing_id_match else url

            title_tag = card.select_one("h4, h6")
            title = title_tag.get_text(strip=True) if title_tag else "N/A"

            price_tag = card.select_one("[data-testid='ad-price']")
            price_text = price_tag.get_text(strip=True) if price_tag else ""
            price = parse_price(price_text)

            location_tag = card.select_one("[data-testid='location-date']")
            location = location_tag.get_text(strip=True) if location_tag else "N/A"

            # Area lives in a sibling parameter next to location/date, e.g.
            # "Cluj-Napoca - Reactualizat azi la 16:12  66 m²" — pull it from
            # that container's full text rather than just the title.
            params_text = title
            if location_tag and location_tag.parent:
                params_text += " " + location_tag.parent.get_text(" ", strip=True)
            area = parse_area(params_text)

            price_per_sqm = None
            if price and area and area > 0:
                price_per_sqm = round(price / area, 2)

            if price is None:
                continue  # skip cards we couldn't parse a price for

            listings.append(
                Listing(
                    listing_id=listing_id,
                    title=title,
                    price=price,
                    area_sqm=area,
                    price_per_sqm=price_per_sqm,
                    location=location,
                    url=url,
                )
            )
        except Exception as e:
            log.warning(f"Skipped a card due to parse error: {e}")
            continue

    return listings


def scrape_search(base_url: str, max_pages: int, seen: set) -> list[Listing]:
    """Page through a search, stopping as soon as we've covered everything new.

    Two end-of-results signals are handled:
    - OLX doesn't return an empty page past the last real one — it silently
      redirects back to page 1's results. We detect this by comparing the
      first listing id on each page to page 1's first listing id.
    - Once a page contains zero listings we haven't seen in a previous run,
      we've caught up to already-processed territory (OLX sorts newest
      first), so there's no need to keep paging further.
    """
    all_listings = []
    first_page_first_id = None
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        log.info(f"Fetching page {page}: {url}")
        soup = fetch_page(url)
        if soup is None:
            break
        page_listings = parse_listing_cards(soup)
        if not page_listings:
            log.info("No more listings found, stopping pagination.")
            break

        if page == 1:
            first_page_first_id = page_listings[0].listing_id
        elif page_listings[0].listing_id == first_page_first_id:
            log.info(f"Page {page} matches page 1 (past the last real page); stopping pagination.")
            break

        all_listings.extend(page_listings)

        if all(l.listing_id in seen for l in page_listings):
            log.info(f"Page {page} had no unseen listings; assuming we've caught up, stopping pagination.")
            break

        time.sleep(REQUEST_DELAY)
    return all_listings


# ─────────────────────────────────────────────────────────────────────────
# SEEN-LISTINGS TRACKING (avoid duplicate alerts)
# ─────────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def load_found() -> dict:
    """Load the cumulative record of every opportunity found so far, keyed by listing_id."""
    if FOUND_FILE.exists():
        return json.loads(FOUND_FILE.read_text(encoding="utf-8"))
    return {}


def save_found(found: dict) -> None:
    FOUND_FILE.write_text(json.dumps(found, indent=2, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────────────────────────────────

def send_email_alert(listings: list[Listing]) -> None:
    if not listings:
        return
    body_lines = []
    for l in listings:
        body_lines.append(
            f"{l.title}\n"
            f"  Price: {l.price} | Area: {l.area_sqm} mp | "
            f"Price/mp: {l.price_per_sqm}\n"
            f"  Location: {l.location}\n"
            f"  {l.url}\n"
        )
    body = "\n".join(body_lines)

    msg = MIMEText(body)
    msg["Subject"] = f"OLX: {len(listings)} good opportunity(ies) found"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("Email alert sent.")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def report(listings: list[Listing]) -> None:
    for l in listings:
        log.info(
            f"OPPORTUNITY: {l.title} | {l.price} | {l.area_sqm} mp | "
            f"{l.price_per_sqm}/mp | {l.url}"
        )
    if EMAIL_ENABLED:
        send_email_alert(listings)


def _escape_html(value) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _price_bands() -> list[tuple[float, float]]:
    """Turn PRICE_BANDS into (lower, upper) pairs, e.g. [2200, 2500, 2800, 3000]
    -> [(0, 2200), (2200, 2500), (2500, 2800), (2800, 3000)]."""
    bounds = [0] + list(PRICE_BANDS)
    return [(bounds[i - 1], bounds[i]) for i in range(1, len(bounds))]


def generate_html_report(found: dict) -> None:
    """Render the cumulative list of opportunities as a browsable HTML table,
    best price/m2 first, with a dropdown to filter by price/m2 band."""
    rows = sorted(
        found.values(),
        key=lambda r: r["price_per_sqm"] if r["price_per_sqm"] is not None else float("inf"),
    )

    bands = _price_bands()
    band_options = "".join(
        f'<option value="{lo},{hi}">{"≤ " + f"{hi:,.0f}" if lo == 0 else f"{lo:,.0f} – {hi:,.0f}"} €/m²</option>'
        for lo, hi in bands
    )
    all_option = f'<option value="0,{PRICE_BANDS[-1]}" selected>All (≤ {PRICE_BANDS[-1]:,.0f} €/m²)</option>'

    table_rows = "".join(
        f"""
        <tr data-pps="{r['price_per_sqm']}" data-title="{_escape_html(r['title'].lower())}" data-found-date="{_escape_html(r.get('found_at', '')[:10])}">
          <td>{i}</td>
          <td><a href="{_escape_html(r['url'])}" target="_blank" rel="noopener">{_escape_html(r['title'])}</a></td>
          <td>{r['price']:,.0f}</td>
          <td>{r['area_sqm']:.0f} m²</td>
          <td class="price-per-sqm">{r['price_per_sqm']:,.0f}</td>
          <td>{_escape_html(r['location'])}</td>
          <td>{_escape_html(r.get('found_at', ''))}</td>
        </tr>"""
        for i, r in enumerate(rows, start=1)
    )

    body = (
        f"""<table>
          <thead>
            <tr><th>#</th><th>Listing</th><th>Price</th><th>Area</th><th>Price/m²</th><th>Location</th><th>Found at</th></tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>"""
        if rows
        else '<div class="empty">No opportunities found yet.</div>'
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>OLX Cluj-Napoca — Good Deals</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
    margin: 0; padding: 2rem;
    background: #f7f7f8; color: #1a1a1a;
  }}
  h1 {{ font-size: 1.4rem; margin: 0 0 0.2rem; }}
  .subtitle {{ color: #666; margin-bottom: 1rem; font-size: 0.9rem; }}
  .controls {{ margin-bottom: 1.2rem; display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 1.5rem; }}
  .controls label {{ font-size: 0.9rem; color: #444; }}
  select, input[type="search"] {{
    font: inherit; font-size: 0.9rem; padding: 0.4rem 0.6rem;
    border-radius: 6px; border: 1px solid #ccc; background: #fff; color: #1a1a1a;
  }}
  input[type="search"] {{ min-width: 220px; }}
  table {{
    width: 100%; border-collapse: collapse; background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12); border-radius: 8px; overflow: hidden;
  }}
  th, td {{ padding: 0.6rem 0.8rem; text-align: left; font-size: 0.92rem; }}
  th {{ background: #eef0f3; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  tr:hover {{ background: #eef6ff; }}
  tr.hidden {{ display: none; }}
  a {{ color: #0a66c2; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .price-per-sqm {{ font-weight: 600; }}
  .empty {{ padding: 2rem; text-align: center; color: #888; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #16181c; color: #e6e6e6; }}
    table {{ background: #1f2229; }}
    th {{ background: #2a2e37; }}
    tr:nth-child(even) {{ background: #23262d; }}
    tr:hover {{ background: #2b3446; }}
    a {{ color: #7db8ff; }}
    .subtitle {{ color: #999; }}
    .controls label {{ color: #ccc; }}
    select, input[type="search"] {{ background: #1f2229; color: #e6e6e6; border-color: #3a3f4b; }}
  }}
</style>
</head>
<body>
  <h1>OLX Cluj-Napoca — Good Deals</h1>
  <div class="subtitle">
    <span id="visible-count">{len(rows)}</span> of {len(rows)} opportunit{'y' if len(rows) == 1 else 'ies'} shown &nbsp;•&nbsp;
    Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
  <div class="controls">
    <label for="band-filter">Price/m² range:</label>
    <select id="band-filter">
      {all_option}
      {band_options}
    </select>
    <label for="search-box">Search listing:</label>
    <input type="search" id="search-box" placeholder="e.g. central">
    <label for="date-from">Found between:</label>
    <input type="date" id="date-from">
    <span>and</span>
    <input type="date" id="date-to">
  </div>
  {body}
  <script>
    (function() {{
      var select = document.getElementById('band-filter');
      var search = document.getElementById('search-box');
      var dateFrom = document.getElementById('date-from');
      var dateTo = document.getElementById('date-to');
      var countEl = document.getElementById('visible-count');
      var rows = document.querySelectorAll('tbody tr[data-pps]');
      if (!select || !search || !dateFrom || !dateTo) return;
      function applyFilters() {{
        var parts = select.value.split(',');
        var min = parseFloat(parts[0]);
        var max = parseFloat(parts[1]);
        var query = search.value.trim().toLowerCase();
        var from = dateFrom.value;
        var to = dateTo.value;
        var visible = 0;
        rows.forEach(function(row) {{
          var pps = parseFloat(row.getAttribute('data-pps'));
          var title = row.getAttribute('data-title') || '';
          var foundDate = row.getAttribute('data-found-date') || '';
          var show = pps >= min && pps <= max
            && (!query || title.indexOf(query) !== -1)
            && (!from || !foundDate || foundDate >= from)
            && (!to || !foundDate || foundDate <= to);
          row.classList.toggle('hidden', !show);
          if (show) visible++;
        }});
        if (countEl) countEl.textContent = visible;
      }}
      select.addEventListener('change', applyFilters);
      search.addEventListener('input', applyFilters);
      dateFrom.addEventListener('change', applyFilters);
      dateTo.addEventListener('change', applyFilters);
      applyFilters();
    }})();
  </script>
</body>
</html>
"""
    HTML_REPORT_FILE.write_text(html, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    seen = load_seen()
    found = load_found()
    new_opportunities = []

    for search_url in SEARCH_URLS:
        listings = scrape_search(search_url, MAX_PAGES, seen)
        log.info(f"Parsed {len(listings)} listings from {search_url}")

        for listing in listings:
            if listing.listing_id in seen:
                continue
            seen.add(listing.listing_id)

            if listing.price_per_sqm is not None and listing.price_per_sqm <= MAX_PRICE_PER_SQM:
                new_opportunities.append(listing)

    save_seen(seen)

    if new_opportunities:
        log.info(f"Found {len(new_opportunities)} new opportunities.")
        report(new_opportunities)
        for listing in new_opportunities:
            record = asdict(listing)
            record["found_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            found[listing.listing_id] = record
        save_found(found)
    else:
        log.info("No new opportunities this run.")

    generate_html_report(found)
    log.info(f"HTML report updated: {HTML_REPORT_FILE.resolve()}")


if __name__ == "__main__":
    main()
