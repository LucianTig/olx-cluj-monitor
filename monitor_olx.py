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
2. Be a polite scraper: don't run this more often than every 20-30 minutes,
   set a real User-Agent, and don't hammer the site with parallel requests.
   Check OLX's robots.txt / terms of service for any restrictions on
   automated access before relying on this long-term.
3. Prices/areas are parsed from the listing title & card text with regex —
   real listings are messy, so some entries may be skipped if they don't
   match the expected pattern. That's expected and safe (better to skip than
   to misread a price).

SETUP:
    pip install requests beautifulsoup4

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

import requests
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
# search). Adjust based on the market research you already did for Cluj.
MAX_PRICE_PER_SQM = 2200

# How many listing pages deep to check per search URL
MAX_PAGES = 3

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

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
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
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


def scrape_search(base_url: str, max_pages: int) -> list[Listing]:
    all_listings = []
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
        all_listings.extend(page_listings)
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


def generate_html_report(found: dict) -> None:
    """Render the cumulative list of opportunities as a browsable HTML table,
    best price/m2 first."""
    rows = sorted(
        found.values(),
        key=lambda r: r["price_per_sqm"] if r["price_per_sqm"] is not None else float("inf"),
    )

    table_rows = "".join(
        f"""
        <tr>
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
  .subtitle {{ color: #666; margin-bottom: 1.5rem; font-size: 0.9rem; }}
  table {{
    width: 100%; border-collapse: collapse; background: #fff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12); border-radius: 8px; overflow: hidden;
  }}
  th, td {{ padding: 0.6rem 0.8rem; text-align: left; font-size: 0.92rem; }}
  th {{ background: #eef0f3; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  tr:hover {{ background: #eef6ff; }}
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
  }}
</style>
</head>
<body>
  <h1>OLX Cluj-Napoca — Good Deals</h1>
  <div class="subtitle">
    Threshold: ≤ {MAX_PRICE_PER_SQM:,.0f} / m² &nbsp;•&nbsp;
    {len(rows)} opportunit{'y' if len(rows) == 1 else 'ies'} found so far &nbsp;•&nbsp;
    Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
  {body}
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
        listings = scrape_search(search_url, MAX_PAGES)
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
