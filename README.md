# Google Maps + Email Extraction Lead-Gen Bot

A beginner-friendly Python bot that:
1. Searches Google Maps for businesses matching a **keyword + city**
2. Extracts business details (name, phone, address, website, rating, city/state/pin)
3. Visits each business's website (and common `/contact`, `/about` pages) to find a public email address
4. Cleans and de-duplicates the data
5. Saves everything to `output/keyword_city.xlsx` and `output/keyword_city.csv`

## Project structure

```
gmaps_lead_bot/
├── main.py             # Entry point — run this file
├── config.py           # All settings (delays, paths, regex, etc.)
├── gmaps_scraper.py     # Selenium logic for scraping Google Maps
├── email_extractor.py  # requests + BeautifulSoup + regex email finder
├── utils.py             # Deduplication + saving to Excel/CSV
├── requirements.txt
└── output/              # Generated .xlsx/.csv files land here
```

## Setup

1. Install Google Chrome (the bot drives a real Chrome browser via Selenium).
2. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

   `webdriver-manager` will automatically download the correct ChromeDriver
   version for you — no manual driver setup needed.

## Usage

Interactive mode:

```bash
python main.py
```

You'll be prompted for a keyword and city.

Command-line mode:

```bash
python main.py --keyword "Civil" --city "Jaipur"
```

Output files will appear as `output/civil_jaipur.xlsx` and `output/civil_jaipur.csv`.

## How it works (data flow)

```
Google Maps search → business listing + website URL
        ↓
Visit website homepage → check /contact, /contact-us, /about, etc.
        ↓
Regex + mailto: link scan → candidate emails
        ↓
Filter out junk (image files, placeholders, tracking pixels)
        ↓
Deduplicate businesses (by name + phone)
        ↓
Save to Excel + CSV
```

## Tuning behavior

Everything adjustable lives in `config.py`:
- `MAX_SCROLLS` / `SCROLL_DELAY` — how many Google Maps results to load
- `HEADLESS` — set to `False` if you want to watch the browser work
- `CONTACT_PAGE_PATHS` — which sub-pages to check for emails
- `EMAIL_BLOCKLIST_SUBSTRINGS` — filter out more false-positive email patterns as you encounter them

## Important notes before you run this at scale

- **Google's Terms of Service** restrict automated scraping of Google Maps.
  Consider Google's official **Places API** for production/commercial use —
  it's more reliable and reduces the risk of your IP getting blocked or
  your account facing action.
- **Respect `robots.txt`** and rate limits on the company websites you visit
  — the delays in `config.py` are a starting point, not a guarantee of
  politeness. Scrape only what you need.
- **Email/anti-spam law**: if you plan to email the addresses you collect,
  look into your local requirements (e.g. **CAN-SPAM** in the US, **CASL**
  in Canada, **GDPR/PECR** in the EU/UK, **India's IT Act/DPDP Act**).
  These generally require things like a working unsubscribe link, accurate
  sender info, and honoring opt-outs — collecting an email doesn't by
  itself grant permission to send unlimited marketing mail to it.
- This bot only extracts emails that businesses have **already published
  publicly** on their own websites (not hidden or scraped from private
  sources).

## Possible upgrades

- Add proxy rotation if scraping large volumes
- Add retry logic with exponential backoff for flaky websites
- Swap the Google Maps scraper for the official Places API for more
  reliable, ToS-compliant data
- Add a simple GUI (e.g. with `tkinter` or `streamlit`) on top of `main.py`
