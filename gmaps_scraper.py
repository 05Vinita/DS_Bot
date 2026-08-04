"""
gmaps_scraper.py
-----------------
Handles everything related to scraping business listings from Google Maps
using Selenium (since Google Maps is a JavaScript-heavy site that requires
a real browser to render results).

Main function to use from outside this file:
    scrape_google_maps(keyword, city) -> list[dict]
"""

import csv
import os
import re
import time
from urllib.parse import quote_plus, urlparse, unquote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    InvalidSessionIdException,
)
from webdriver_manager.chrome import ChromeDriverManager

import config


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
# Google Maps ships obfuscated CSS class names ("m6QErb", "aIFcqe", ...) that
# change without warning, and element IDs are generated fresh on every page
# load ("ucc-0", "ucc-1", ...). Anything selected by class or ID WILL break --
# that is exactly why the old By.ID("searchboxinput") lookup stopped working.
#
# The selectors below deliberately use semantic attributes only: role, name
# and data-item-id. Those have stayed stable for years because Google's own
# accessibility layer depends on them, so they are much safer to build on.

RESULTS_FEED = 'div[role="feed"]'
PLACE_LINK = 'div[role="feed"] a[href*="/maps/place/"]'
END_OF_LIST_TEXT = "you've reached the end of the list"

ADDRESS_BTN = 'button[data-item-id="address"]'
PHONE_BTN = 'button[data-item-id^="phone"]'
WEBSITE_LINK = 'a[data-item-id="authority"]'
RATING_SPAN = 'span[role="img"][aria-label*="star"]'
# The category badge ("Hospital", "Dental clinic", ...) sits right under the
# heading. It has no data-item-id, but its jsaction has stayed stable for
# the same reason data-item-id has: Google's own click-tracking depends on it.
CATEGORY_BTN = 'button[jsaction*="category"]'

# Hosts that are never a company's own website. Businesses without a real
# site often list their JustDial page, Facebook page or a WhatsApp link
# instead, and those are useless in a Website column -- the social profiles
# are already collected separately, and a directory listing tells you nothing.
NOT_A_COMPANY_SITE = {
    "justdial.com", "indiamart.com", "sulekha.com", "tradeindia.com",
    "exportersindia.com", "yellowpages.in", "indiayellowpages.com",
    "zaubacorp.com", "tofler.in", "yelp.com", "urbanpro.com", "sitejabber.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "youtu.be", "pinterest.com", "threads.net", "t.me",
    "wa.me", "whatsapp.com", "api.whatsapp.com", "chat.whatsapp.com",
    "g.page", "goo.gl", "maps.app.goo.gl", "business.site", "sites.google.com",
    "linktr.ee", "bit.ly", "tinyurl.com", "amazon.in", "flipkart.com",
}


def _is_company_website(url):
    """
    True when a URL looks like the company's own site.

    Google's "Website" field is whatever the business entered, so it is
    regularly a JustDial listing or a Facebook page. Those belong in neither
    the Website column nor the email scraper, which would otherwise go
    hunting for contact details on facebook.com.
    """
    if not url:
        return False
    host = urlparse(url if url.startswith("http") else "https://" + url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False

    for blocked in NOT_A_COMPANY_SITE:
        if host == blocked or host.endswith("." + blocked):
            return False
    return True

# Indian PIN codes are always six digits.
PIN_RE = re.compile(r"\b(\d{6})\b")

# Address segments that are building/plot identifiers rather than localities.
JUNK_PREFIX_RE = re.compile(
    r"^(shop|plot|gala|unit|office|flat|room|floor|no\.?|building|bldg"
    r"|survey|khasra|near|opp\.?|opposite|behind|part)\b",
    re.I,
)

# Building and complex names that sit where a locality would, e.g.
# "Astral Tower, Sector 45, Gurugram" -- the locality is Sector 45.
JUNK_WORD_RE = re.compile(
    r"\b(tower|towers|retreat|plaza|complex|apartment|apartments|society"
    r"|residency|enclave|villa|villas|heights|arcade|chamber|chambers"
    r"|estate|campus|wing|block)\b",
    re.I,
)

# Address vocabulary. These are real words in business names ("XYZ School
# Sector 45") but they belong to the address, so they must never become
# trade keywords -- "school sector" is not a thing anyone searches for.
LOCATION_WORDS = {
    "sector", "phase", "block", "road", "marg", "nagar", "colony",
    "extension", "vihar", "puram", "pura", "chowk", "gali", "street",
    "lane", "park", "city", "town", "district", "near", "opp", "cross",
}

# Indian cities routinely appear under two names. Someone typing "Gurgaon"
# still needs to match addresses that say "Gurugram", or locality discovery
# silently finds almost nothing.
CITY_ALIASES = {
    "gurgaon": "gurugram", "bangalore": "bengaluru", "bombay": "mumbai",
    "calcutta": "kolkata", "madras": "chennai", "poona": "pune",
    "baroda": "vadodara", "mysore": "mysuru", "simla": "shimla",
    "cochin": "kochi", "trivandrum": "thiruvananthapuram",
    "pondicherry": "puducherry", "benares": "varanasi", "banaras": "varanasi",
    "allahabad": "prayagraj", "orissa": "odisha",
}
# make the mapping work in both directions
CITY_ALIASES.update({v: k for k, v in list(CITY_ALIASES.items())})


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _norm(text):
    """Lowercase + strip punctuation, for comparing names/queries/localities."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _digits(text):
    return re.sub(r"\D", "", text or "")


def _session_is_dead(err):
    """Chrome disconnects show up under several exception types."""
    msg = str(err).lower()
    return any(s in msg for s in (
        "invalid session id", "disconnected", "not connected to devtools",
        "chrome not reachable", "target window already closed",
    ))


# Region names people type that are not a city at all. No address ever says
# "Delhi NCR" -- it says New Delhi, Noida or Gurugram -- so without this the
# city segment is never found, locality discovery returns nothing, and the
# whole locality stage silently does no work.
REGION_MEMBERS = {
    "delhi ncr": ["delhi", "new delhi", "noida", "gurugram", "gurgaon",
                  "ghaziabad", "faridabad", "greater noida"],
    "ncr": ["delhi", "new delhi", "noida", "gurugram", "gurgaon",
            "ghaziabad", "faridabad"],
    "national capital region": ["delhi", "new delhi", "noida", "gurugram",
                                "gurgaon", "ghaziabad", "faridabad"],
    "mumbai metropolitan region": ["mumbai", "navi mumbai", "thane",
                                   "kalyan", "mira road"],
    "mmr": ["mumbai", "navi mumbai", "thane"],
    "tricity": ["chandigarh", "mohali", "panchkula"],
    "hyderabad secunderabad": ["hyderabad", "secunderabad"],
}


def _city_forms(city):
    """
    Every spelling of the city we should accept in an address.

    Also expands a region into its member cities, so "Delhi NCR" matches the
    New Delhi, Noida and Gurugram addresses that a search for it returns.
    """
    base = _norm(city)
    forms = {base}

    if base in REGION_MEMBERS:
        forms.update(_norm(c) for c in REGION_MEMBERS[base])

    for form in list(forms):
        if form in CITY_ALIASES:
            forms.add(_norm(CITY_ALIASES[form]))
    return forms


# Google renders little map-pin and phone icons inside the detail panel using
# Material Icons, which live in Unicode's Private Use Area. Selenium reads
# them as real characters, so they land in the spreadsheet as U+E0C8 and show
# up as an empty box in Excel -- one at the start of every address.
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]")


def _strip_icons(text):
    """Removes icon glyphs and tidies the whitespace they leave behind."""
    if not text:
        return ""
    cleaned = PRIVATE_USE_RE.sub(" ", str(text))
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s*\n\s*", "\n", cleaned)
    return cleaned.strip()


def _clean_address(address):
    """
    Maps returns the address button's text with embedded newlines, e.g.
    "Astral Tower, Sector 45\nGurugram, Haryana 122003". Splitting on commas
    alone leaves "Sector 45\nGurugram" glued together as one segment, which
    breaks both locality discovery and the City/State/Pin columns.
    """
    text = _strip_icons(address)
    return re.sub(r"\s*[\n\r]+\s*", ", ", text).strip()


class ScrapingHalted(Exception):
    """
    Raised when it is no longer worth continuing -- either Chrome died, or
    Google has started serving stub pages instead of real ones.

    Detecting this matters more than it sounds. Once Google throttles a
    session every search still "succeeds", it just returns a handful of
    listings whose detail pages never render. Without a check the bot happily
    burns through every remaining query collecting nothing.
    """


def _seen_path(folder=None):
    folder = folder or getattr(config, "OUTPUT_DIR", "output")
    return os.path.join(folder, "_seen.csv")


def _scope_key(keyword, city):
    """
    Identifies one industry-in-one-city, e.g. "healthcare industries|mumbai".

    Everything the bot remembers is filed under this. Without it, a Mumbai
    healthcare run skips businesses collected by a Noida coaching run --
    completely different companies that were never scraped for this
    industry at all.
    """
    # Singularise the keyword and canonicalise the city, so "Coaching
    # Institutes" in "Gurugram" resumes the same memory as "Coaching
    # Institute" in "Gurgaon" -- the user means the same thing, and treating
    # them as separate would re-scrape the whole city.
    words = " ".join(_singular(w) for w in _tokens(keyword))

    city_n = _norm(city)
    # Pick one spelling of a two-name city, alphabetically, so both map here
    alias = CITY_ALIASES.get(city_n)
    if alias:
        city_n = min(city_n, _norm(alias))

    return f"{words}|{city_n}"


def _load_seen(folder=None, scope=None):
    """
    Reads back what earlier runs already collected FOR THIS INDUSTRY AND CITY.

    Without this, MAX_RESULTS is not a page size, it is a wall: every run
    starts from the same city-wide search and re-collects the same first
    two hundred businesses. Three runs would cost three times the scraping
    and still leave you with two hundred leads.

    Returns (urls, business_keys); empty sets if there is nothing to read.
    """
    urls, keys = set(), set()
    if not getattr(config, "RESUME_ACROSS_RUNS", True):
        return urls, keys

    path = _seen_path(folder)
    if not os.path.exists(path):
        return urls, keys
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                # Rows written before scopes existed have no "scope" column.
                # Treating those as belonging to every scope would repeat the
                # old bug, so they are ignored and simply re-scraped once.
                if scope and row.get("scope") != scope:
                    continue
                if row.get("url"):
                    urls.add(row["url"])
                if row.get("key"):
                    keys.add(row["key"])
    except Exception as err:
        print(f"(could not read {path}: {err} -- starting fresh)")
    return urls, keys


def _append_seen(records, folder=None, scope=""):
    """Appends this run's finds, tagged with the scope they belong to."""
    if not records or not getattr(config, "RESUME_ACROSS_RUNS", True):
        return
    folder = folder or getattr(config, "OUTPUT_DIR", "output")
    path = _seen_path(folder)
    try:
        os.makedirs(folder, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f, fieldnames=["scope", "url", "key", "name"])
            if new_file:
                writer.writeheader()
            for rec in records:
                writer.writerow({
                    "scope": scope,
                    "url": rec.get("_url", ""),
                    "key": _business_key(rec),
                    "name": rec.get("Company Name", ""),
                })
    except Exception as err:
        print(f"  (could not update {path}: {err})")


def _append_master(records, folder=None, keyword="", city=""):
    """
    Keeps one growing file with every lead from every run, deduplicated.

    Each run writes its own dated output, but those only hold that run's
    new finds. This is the file to actually work from.
    """
    if not records:
        return
    folder = folder or getattr(config, "OUTPUT_DIR", "output")
    path = os.path.join(folder, "_all_leads.csv")
    fields = ["Searched Industry", "Searched City"] + [
        k for k in records[0].keys() if not k.startswith("_")
    ]
    try:
        os.makedirs(folder, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            for rec in records:
                row = dict(rec)
                row["Searched Industry"] = keyword
                row["Searched City"] = city
                writer.writerow(row)
    except Exception as err:
        print(f"  (could not update {path}: {err})")


def _load_prior_records(folder=None, city=None, keyword=None):
    """
    Reads back the names and addresses collected by earlier runs IN THIS CITY.

    The city filter is essential. _all_leads.csv accumulates every run, so a
    Gurgaon run that reads the whole file learns its trade vocabulary from
    Noida businesses and starts searching "coaching noida in Gurgaon" -- three
    of six discovered terms in one real run were Noida terms, which wasted
    those searches entirely.

    Discovery works off what the seed search returns -- but on a second run
    that search returns nothing new, so there would be no names to read a
    trade vocabulary from and no addresses to read localities from, and the
    run would stop after one query having found nothing. Feeding the earlier
    data back in means each run starts knowing everything the last one
    learned, and goes straight to the searches it has not tried yet.
    """
    if not getattr(config, "RESUME_ACROSS_RUNS", True):
        return []
    folder = folder or getattr(config, "OUTPUT_DIR", "output")
    path = os.path.join(folder, "_all_leads.csv")
    if not os.path.exists(path):
        return []
    try:
        forms = _city_forms(city) if city else set()
        want = _norm(keyword) if keyword else ""
        with open(path, newline="", encoding="utf-8-sig") as f:
            records = []
            for row in csv.DictReader(f):
                # Only learn from rows collected for this same industry.
                # Learning trade vocabulary from a different industry is how a
                # healthcare run ended up searching "iit jee".
                if want:
                    row_industry = _norm(row.get("Searched Industry", ""))
                    if not row_industry or row_industry != want:
                        continue
                if forms:
                    row_city = _norm(row.get("City", ""))
                    address = _norm(row.get("Address", ""))
                    if not any(f in row_city or f in address for f in forms):
                        continue
                records.append({
                    "Company Name": row.get("Company Name", ""),
                    "Address": row.get("Address", ""),
                })
            return records
    except Exception as err:
        print(f"(could not read {path}: {err})")
        return []


def _save_recovery(records, folder=None):
    """
    Dumps whatever has been collected so far to a recovery CSV.

    A long scrape that dies at the end used to lose everything, because the
    exception escaped before main.py ever reached the save step. This runs
    often enough that a crash costs minutes, not the whole run.
    """
    if not records:
        return
    folder = folder or getattr(config, "OUTPUT_DIR", "output")
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "_recovery.csv")
        fields = [k for k in records[0].keys() if not k.startswith("_")]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
    except Exception as err:
        print(f"  (could not write recovery file: {err})")


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _create_driver():
    """
    Creates and returns a configured Selenium Chrome WebDriver.
    Using webdriver-manager means the user doesn't need to manually
    download/manage the correct ChromeDriver version.
    """
    options = Options()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1000")
    options.add_argument(f"user-agent={config.USER_AGENT}")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def _build_search_url(query):
    """
    Builds a direct Google Maps search URL instead of typing into the search
    box. This removes the most fragile step in the whole scraper -- there is
    no input element to find, no submit button, no autocomplete to fight.

    hl=en matters more than it looks: the rating selector keys off an
    aria-label containing "star", and consent buttons are matched by English
    text. Without hl=en, Google may serve a Hindi interface and both would
    silently stop matching.
    """
    return f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en&gl=in"


def _handle_consent_popup(driver):
    """
    Google often shows a cookie-consent screen before the real map loads.
    Sometimes it is a full page redirect that takes a moment to render, so
    we retry a few times. If no consent screen appears, this does nothing.
    """
    possible_texts = ["Accept all", "I agree", "Accept", "Reject all", "Agree"]

    for _attempt in range(3):
        try:
            for button in driver.find_elements(By.TAG_NAME, "button"):
                try:
                    btn_text = button.text.strip()
                except Exception:
                    continue
                if btn_text in possible_texts:
                    button.click()
                    time.sleep(2)
                    print(f"Dismissed consent popup ('{btn_text}').")
                    return
        except Exception:
            pass
        time.sleep(1)


def _save_debug_snapshot(driver, folder="debug"):
    """Screenshot + raw HTML, so you can see what Chrome saw when it failed."""
    try:
        os.makedirs(folder, exist_ok=True)
        shot_path = os.path.join(folder, "debug_screenshot.png")
        html_path = os.path.join(folder, "debug_page.html")

        driver.save_screenshot(shot_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)

        print(f"  Saved a screenshot to: {shot_path}")
        print(f"  Saved the page HTML to: {html_path}")
    except Exception as err:
        print(f"  (Could not save debug files: {err})")


# Words that are structure, not trade: company suffixes, filler, marketing.
STOPWORDS = {
    "and", "the", "of", "for", "at", "on", "to", "in", "a", "an", "by",
    "with", "from", "pvt", "ltd", "limited", "private", "llp", "inc", "corp",
    "co", "company", "india", "indian", "best", "top", "new", "no", "our",
    "we", "us", "your", "leading", "based", "complete", "all", "more", "get",
    "online", "since",
}

# Fine inside a phrase ("garment manufacturer") but too vague on their own.
GENERIC_ALONE = {
    "manufacturer", "exporter", "supplier", "solution", "industry",
    "work", "factory", "world", "brand", "association", "label",
    "sourcing", "custom", "product", "group", "centre", "center", "house",
    "point", "hub", "mart",
}

# A discovered term must still describe the same line of business as what the
# user typed. Without this check a Mumbai healthcare run inherited "iit jee"
# from an earlier coaching run and filled 64 of 180 rows with coaching
# institutes -- a whole different industry in the healthcare file.
INDUSTRY_GROUPS = [
    {"health", "healthcare", "medical", "medicare", "pharma", "pharmaceutical",
     "clinic", "hospital", "surgical", "diagnostic", "medtech", "dental",
     "ayurvedic", "nursing", "wellness", "lab", "pathology", "care"},
    {"coaching", "tuition", "tutorial", "institute", "academy", "school",
     "classes", "class", "education", "learning", "preschool", "daycare",
     "jee", "neet", "iit", "ias", "upsc", "cat", "clat", "training"},
    {"garment", "clothing", "apparel", "textile", "fashion", "fabric",
     "boutique", "tailor", "readymade", "hosiery"},
    {"civil", "construction", "builder", "contractor", "architect",
     "interior", "engineering", "infrastructure", "developer"},
    {"restaurant", "cafe", "bakery", "catering", "food", "kitchen", "hotel"},
    {"salon", "spa", "beauty", "parlour", "parlor", "barber"},
    {"gym", "fitness", "yoga", "sports", "wellness"},
]


def _is_trade_word(words):
    """
    True when at least one word describes a line of business.

    Brand names slip past every other filter -- "aakash", "physics wallah",
    "allen career" contain nothing that identifies an industry, so they were
    inherited straight into a healthcare run. They make poor searches anyway:
    a brand returns that one chain's branches, not the trade. Requiring a
    recognisable trade word keeps discovery on generic terms like
    "medical equipment", which is what actually widens coverage.
    """
    return bool(words & ALL_TRADE_WORDS)


def _industry_group(words):
    """Which industry bucket a set of words belongs to, if any."""
    for index, group in enumerate(INDUSTRY_GROUPS):
        if words & group:
            return index
    return None


# Trade words that are generic enough to pair with anything, on top of the
# industry buckets above.
GENERIC_TRADE_WORDS = {
    "equipment", "device", "supply", "supplies", "trading", "trader",
    "wholesale", "retail", "dealer", "distributor", "consultant",
    "consultancy", "agency", "studio", "workshop", "repair", "rental",
    "company", "enterprise", "corporation", "manufacturer", "exporter",
    "importer", "service", "services", "solution", "solutions", "care",
    "technology", "technologies", "systems", "labs", "research",
}

ALL_TRADE_WORDS = set(GENERIC_TRADE_WORDS)
for _group in INDUSTRY_GROUPS:
    ALL_TRADE_WORDS |= _group


def _singular(word):
    """
    Crude de-pluralisation so 'garments' and 'garment' count as one.

    Only used for COUNTING. The word that finally gets searched keeps its
    original spelling, because stripping the "s" from "classes" leaves
    "classe", which is not a word and returns almost nothing on Maps.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"                # industries -> industry
    if len(word) > 4 and word.endswith("es") and word[-3] in "sxzh":
        return word[:-2]                      # classes -> class, boxes -> box
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _chunks(text):
    """
    Splits a business name on punctuation before building phrases, so a
    two-word term can never straddle a separator. Without this,
    "Manufacturer | Ready-to-Wear" yields the nonsense term
    "manufacturer ready".
    """
    return re.split(r"[|,/\-\u2013\u2014\[\]()&:;.]+", (text or "").lower())


def _tokens(text):
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _rank_trade_terms(names, keyword, city, limit):
    """
    Reads the real trade vocabulary out of the business names found so far.

    Whatever the user types is rarely how Maps indexes a trade -- "Fashion
    Industries" returns little, while the businesses it does return call
    themselves garment and clothing manufacturers. Those are the words worth
    searching next, and they come straight from the data rather than from a
    hardcoded list, so this works for any industry.

    Two-word phrases are preferred over single words: "coaching institute"
    is a far better search than "coaching" or "institute" alone.
    """
    # Every word of the city and its aliases, plus common neighbours that
    # show up in business names. "greater noida" must not become a search
    # term when the user asked for Gurgaon.
    city_t = _city_forms(city) | {_singular(t) for t in _tokens(city)}
    for form in list(city_t):
        city_t |= {_singular(t) for t in _tokens(form)}
    keyword_t = {_singular(t) for t in _tokens(keyword)}

    # A thin seed search is exactly when discovery matters most -- a query
    # that returned 20 businesses needs the extra searches far more than one
    # that returned 120. So the bar for "this word appeared often enough"
    # scales down rather than rejecting everything.
    if len(names) >= 40:
        min_count = 3
    elif len(names) >= 15:
        min_count = 2
    else:
        min_count = 1

    counts = {}
    surface = {}     # normalised key -> the most common real spelling

    def note(key, text):
        counts[key] = counts.get(key, 0) + 1
        seen = surface.setdefault(key, {})
        seen[text] = seen.get(text, 0) + 1

    for name in names:
        for chunk in _chunks(name):
            raw = [
                t for t in _tokens(chunk)
                if t not in STOPWORDS
                and len(t) > 2 and not t.isdigit()
                and not any(form in t for form in city_t)
                and _singular(t) not in LOCATION_WORDS
            ]
            toks = [_singular(t) for t in raw]

            for i in range(len(toks) - 1):
                note(f"{toks[i]} {toks[i + 1]}", f"{raw[i]} {raw[i + 1]}")
            for norm_tok, raw_tok in zip(toks, raw):
                if norm_tok in GENERIC_ALONE:
                    continue
                note(norm_tok, raw_tok)

    # Any city name at all, not just this one: a Gurgaon search should not
    # inherit "noida" from a previous run's data either.
    all_city_words = set()
    for name in list(CITY_ALIASES) + list(CITY_ALIASES.values()):
        all_city_words |= {_singular(t) for t in _tokens(name)}

    # Which industry did the user actually ask about?
    keyword_group = _industry_group(keyword_t)

    phrases, words = [], []
    for term, n in counts.items():
        if n < min_count:
            continue
        term_tokens = {_singular(t) for t in _tokens(term)}
        if term_tokens <= keyword_t:             # just restates what they typed
            continue
        if term_tokens & city_t or term_tokens & all_city_words:
            continue                             # a place name, not a trade

        # Reject a term that clearly belongs to a different trade. Terms with
        # no recognisable industry are kept -- most real ones look like that,
        # and only an outright conflict is worth blocking.
        term_group = _industry_group(term_tokens)
        if keyword_group is not None and term_group is not None:
            if term_group != keyword_group:
                continue

        # Must name a trade, not just a brand
        if not _is_trade_word(term_tokens):
            continue

        (phrases if " " in term else words).append((n, term))

    phrases.sort(key=lambda x: (-x[0], x[1]))
    words.sort(key=lambda x: (-x[0], x[1]))

    def spell(key):
        """The real spelling people actually wrote, not the counting key."""
        options = surface.get(key)
        if not options:
            return key
        return max(options.items(), key=lambda kv: kv[1])[0]

    picked = []
    for _n, term in phrases:
        picked.append(spell(term))
        if len(picked) >= limit:
            return picked
    for _n, term in words:
        # skip a word already contained in a phrase we took
        if any(term in p.split() for p in picked):
            continue
        picked.append(spell(term))
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Locality discovery
# ---------------------------------------------------------------------------

def _looks_like_locality(segment, city_forms):
    """Rejects PIN codes, plot numbers, building names and the city itself."""
    if not segment or len(segment) < 3:
        return False
    if PIN_RE.search(segment):
        return False
    if JUNK_PREFIX_RE.match(segment) or JUNK_WORD_RE.search(segment):
        return False

    letters = sum(c.isalpha() for c in segment)
    if letters < max(3, len(segment) // 2):
        return False

    n = _norm(segment)
    if n == "india" or n in city_forms:
        return False
    if any(form in n for form in city_forms):
        return False
    return True


def _extract_locality(address, city):
    """
    Pulls the locality out of a Maps address by finding the city segment and
    taking the nearest sensible segment before it.

        "Astral Tower, Sector 45, Gurugram, Haryana 122003"
                                  ^^^^^^^^ city (typed as "Gurgaon")
                       ^^^^^^^^^ locality; the tower name is skipped

    These names come from Google's own data, so feeding them back as search
    terms is reliable in a way hand-written guesses are not.
    """
    parts = [p.strip() for p in _clean_address(address).split(",") if p.strip()]
    if len(parts) < 2:
        return ""

    forms = _city_forms(city)

    # Exact segment match first, so "Navi Mumbai" cannot outrank "Mumbai"
    idx = None
    for i, part in enumerate(parts):
        if _norm(part) in forms:
            idx = i
            break
    if idx is None:
        for i, part in enumerate(parts):
            if any(form in _norm(part) for form in forms):
                idx = i
                break

    if idx is None or idx == 0:
        return ""

    for j in range(idx - 1, -1, -1):
        if _looks_like_locality(parts[j], forms):
            return parts[j]
    return ""


def _rank_localities(addresses, city, limit):
    """
    Counts localities across a batch of addresses, most common first.

    The frequency floor is what separates a real locality from a building
    name that slipped through: dozens of businesses share "Sector 45", but
    only one sits in "Aravali Retreat".
    """
    counts = {}
    for address in addresses:
        locality = _extract_locality(address, city)
        if not locality:
            continue
        key = _norm(locality)
        entry = counts.setdefault(key, {"label": locality, "n": 0})
        entry["n"] += 1

    # Same reasoning as trade terms: a small result set still deserves
    # locality coverage, so the frequency bar drops with the sample size.
    min_count = 2 if len(addresses) >= 20 else 1

    ranked = sorted(
        (e for e in counts.values() if e["n"] >= min_count),
        key=lambda e: -e["n"],
    )
    return [e["label"] for e in ranked][:limit]


# ---------------------------------------------------------------------------
# Query planning
# ---------------------------------------------------------------------------

def _build_query_list(keyword, city, areas=None, variants=None):
    """
    A single Google Maps search only returns a limited slice of what exists
    -- scrolling harder does not change that. More businesses come from more,
    narrower searches:

      * variants -> different words for the same trade
      * areas    -> localities inside the city
    """
    terms = [t.strip() for t in (variants or [keyword]) if t and t.strip()]
    localities = [a.strip() for a in (areas or []) if a and a.strip()]

    queries = []
    if localities:
        for term in terms:
            for area in localities:
                queries.append(f"{term} in {area}, {city}")
    else:
        for term in terms:
            queries.append(f"{term} in {city}")
    return queries


# ---------------------------------------------------------------------------
# Scrolling + collecting
# ---------------------------------------------------------------------------

def _scroll_results_panel(driver, max_scrolls=config.MAX_SCROLLS):
    """
    Scrolls the results panel until one of three things happens, and says
    WHICH one -- each has a different fix:

      * Google prints "You've reached the end of the list"
            -> no more exist for this query; narrower searches are the answer
      * the count stops growing
            -> Google throttled; raise SCROLL_DELAY
      * max_scrolls runs out
            -> your own limit; raise MAX_SCROLLS in config.py
    """
    try:
        panel = driver.find_element(By.CSS_SELECTOR, RESULTS_FEED)
    except NoSuchElementException:
        print("  Could not find the results panel to scroll.")
        return

    last_count = 0
    stagnant_rounds = 0

    for _ in range(max_scrolls):
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollHeight", panel
        )
        time.sleep(config.SCROLL_DELAY)

        try:
            if END_OF_LIST_TEXT in panel.text.lower():
                count = len(driver.find_elements(By.CSS_SELECTOR, PLACE_LINK))
                print(f"  Google says end of list -- {count} listings.")
                return
        except Exception:
            pass

        count = len(driver.find_elements(By.CSS_SELECTOR, PLACE_LINK))

        if count == last_count:
            stagnant_rounds += 1
            # Loading routinely lags behind scrolling, so wait longer before
            # concluding nothing more is coming.
            time.sleep(config.SCROLL_DELAY)
            if stagnant_rounds >= 5:
                print(f"  Stopped growing at {count} listings.")
                return
        else:
            stagnant_rounds = 0
        last_count = count

    print(f"  Hit your MAX_SCROLLS limit ({max_scrolls}) at {last_count}.")
    print("  ^ raise MAX_SCROLLS in config.py if you expected more.")


def _collect_place_urls(driver, seen_urls):
    """
    Collects listing URLs, skipping any already seen in an earlier query.

    Grabbing URLs first and visiting them afterwards -- rather than clicking
    each card -- avoids StaleElementReferenceException, because clicking makes
    Maps re-render the panel and invalidate every element reference we hold.

    Deduplicating HERE, before opening anything, is what stops overlapping
    searches from re-opening the same business page over and over.
    """
    urls = []
    for link in driver.find_elements(By.CSS_SELECTOR, PLACE_LINK):
        try:
            href = link.get_attribute("href")
        except Exception:
            continue
        if href and href not in seen_urls:
            seen_urls.add(href)
            urls.append(href)
    return urls


# ---------------------------------------------------------------------------
# Detail extraction
# ---------------------------------------------------------------------------

def _parse_address(address):
    """
    Best-effort split of a Maps address into City / State / Pin Code.

    The trailing ", India" is sometimes present and sometimes not, so counting
    backwards from the end breaks unpredictably. Finding the six-digit PIN
    first and working outwards from it is far more robust.
    """
    out = {"City": "", "State": "", "Pin Code": ""}
    if not address:
        return out

    parts = [p.strip() for p in _clean_address(address).split(",") if p.strip()]
    if not parts:
        return out

    pin_index = None
    for i, part in enumerate(parts):
        match = PIN_RE.search(part)
        if match:
            out["Pin Code"] = match.group(1)
            pin_index = i
            out["State"] = PIN_RE.sub("", part).strip(" ,-")
            break

    if pin_index is None:
        if len(parts) >= 2:
            out["State"] = parts[-1]
            out["City"] = parts[-2]
        return out

    if out["State"]:
        if pin_index > 0:
            out["City"] = parts[pin_index - 1]
    else:
        if pin_index > 0:
            out["State"] = parts[pin_index - 1]
        if pin_index > 1:
            out["City"] = parts[pin_index - 2]
    return out


def _extract_place_details(driver, url):
    """
    Opens one place URL and pulls the business details out of the panel.

    Navigating to the URL is slower than clicking a card, but it is stateless:
    nothing goes stale, no back-button dance, and a misbehaving listing can be
    pasted straight into a normal browser to see what the scraper saw.
    """
    data = {
        "Company Name": "",
        "Category": "",
        "Phone Number": "",
        "Address": "",
        "Website": "",
        "Rating": "",
        "City": "",
        "State": "",
        "Pin Code": "",
    }

    try:
        driver.get(url)
    except InvalidSessionIdException:
        raise ScrapingHalted("the browser session ended")
    except WebDriverException as err:
        if _session_is_dead(err):
            raise ScrapingHalted("lost the connection to Chrome")
        print(f"      Could not open listing: {err}")
        return data

    try:
        WebDriverWait(driver, config.PAGE_LOAD_WAIT).until(
            lambda d: bool(_read_business_name(d))
        )
    except TimeoutException:
        # The wait failing no longer means the page is unusable -- read it
        # anyway, because the details are often all present and only the
        # heading was slow or missing.
        name = _read_business_name(driver)
        if not name:
            print("      Timed out waiting for the listing to render.")
            return data
        print("      (heading was slow; read the rest anyway)")
    except InvalidSessionIdException:
        raise ScrapingHalted("the browser session ended")
    except WebDriverException as err:
        if _session_is_dead(err):
            raise ScrapingHalted("lost the connection to Chrome")
        print(f"      Could not read the listing: {err}")
        return data

    # The heading renders first; the rest of the panel (address, phone,
    # rating, website) streams in a beat later. Reading immediately after
    # the heading check above caught the panel mid-render on a chunk of
    # listings -- everything else on the row would be present except the
    # Address, because that field happened to render last. Give the panel
    # a short extra moment to finish before reading anything off it.
    try:
        WebDriverWait(driver, min(5, config.PAGE_LOAD_WAIT)).until(
            lambda d: _panel_has_rendered(d)
        )
    except TimeoutException:
        # Still nothing after the extra wait -- this is a genuine
        # service-area business with no address button, not a slow render.
        pass
    except InvalidSessionIdException:
        raise ScrapingHalted("the browser session ended")
    except WebDriverException as err:
        if _session_is_dead(err):
            raise ScrapingHalted("lost the connection to Chrome")

    data["Company Name"] = _read_business_name(driver)
    data["Category"] = _read_category(driver)

    # aria-label reads like "4.3 stars"; a bare number sorts properly in Excel
    try:
        rating_el = driver.find_element(By.CSS_SELECTOR, RATING_SPAN)
        label = rating_el.get_attribute("aria-label") or ""
        match = re.search(r"[\d.]+", label)
        data["Rating"] = match.group(0) if match else label.strip()
    except NoSuchElementException:
        pass

    data["Address"] = _read_address(driver)

    # data-item-id looks like "phone:tel:+919876543210" -- already normalised
    try:
        phone_el = driver.find_element(By.CSS_SELECTOR, PHONE_BTN)
        item_id = phone_el.get_attribute("data-item-id") or ""
        if ":" in item_id:
            data["Phone Number"] = item_id.split(":")[-1].strip()
        else:
            data["Phone Number"] = _strip_icons(phone_el.text)
    except NoSuchElementException:
        pass

    try:
        website = driver.find_element(
            By.CSS_SELECTOR, WEBSITE_LINK
        ).get_attribute("href")
        if _is_company_website(website):
            data["Website"] = website
        elif website:
            # Not their own site -- but if it is a social page it still has
            # value, so route it to the right column instead of discarding it
            data["_listed_link"] = website
            host = urlparse(website).netloc
            print(f"      (listed site is {host}, not their own website)")
    except NoSuchElementException:
        pass

    data.update(_parse_address(data["Address"]))
    return data


def _panel_has_rendered(driver):
    """
    True once the panel has at least one of the fields we care about, beyond
    just the heading. Used to give the panel a moment to catch up after the
    heading appears -- reading Address/Phone/Rating/Website too early is
    what left Address blank on listings that had every other field filled.
    """
    for selector in (ADDRESS_BTN, PHONE_BTN, WEBSITE_LINK, RATING_SPAN, CATEGORY_BTN):
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            pass
    return False


def _read_category(driver):
    """
    Reads the business type badge (e.g. "Hospital", "Dental clinic").

    Button text is the usual home for it; a couple of listings only carried
    it in the aria-label, same quirk as the address button.
    """
    try:
        element = driver.find_element(By.CSS_SELECTOR, CATEGORY_BTN)
        text = _strip_icons(element.text).strip()
        if text:
            return text
        label = (element.get_attribute("aria-label") or "").strip()
        if label:
            return label
    except NoSuchElementException:
        pass
    return ""


def _read_address(driver):
    """
    Reads the address, trying several sources.

    The data-item-id="address" button is the usual home for it, but Google
    omits that button for service-area businesses and sometimes renders the
    address only as an aria-label. Depending on the button alone left a third
    of one run with no address at all -- and with it, no City, State or Pin.
    """
    # 1. the address button, when present
    try:
        element = driver.find_element(By.CSS_SELECTOR, ADDRESS_BTN)
        text = _clean_address(element.text)
        if text:
            return text
        # the button sometimes carries the text only in its aria-label
        label = element.get_attribute("aria-label") or ""
        label = re.sub(r"^\s*Address:\s*", "", label, flags=re.I)
        text = _clean_address(label)
        if text:
            return text
    except NoSuchElementException:
        pass
    except Exception:
        pass

    # 2. any element whose aria-label announces itself as an address
    for selector in ('button[aria-label^="Address"]',
                     'div[aria-label^="Address"]',
                     '[data-tooltip="Copy address"]'):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                label = element.get_attribute("aria-label") or element.text or ""
                label = re.sub(r"^\s*Address:\s*", "", label, flags=re.I)
                text = _clean_address(label)
                if text:
                    return text
        except Exception:
            pass

    return ""


def _read_business_name(driver):
    """
    Reads the business name, trying several sources in order of reliability.

    Relying on <h1> alone was a mistake: Google moves that heading around,
    and when it does, every listing "times out" even though the page loaded
    perfectly. The URL is the sturdiest fallback -- Maps puts the business
    name in the path of every place URL, and that has held for years.
    """
    # 1. the heading, when it is there
    try:
        for heading in driver.find_elements(By.TAG_NAME, "h1"):
            text = _strip_icons(heading.text)
            if text:
                return text
    except Exception:
        pass

    # 2. the panel's own aria-label, which mirrors the heading
    for selector in ('div[role="main"][aria-label]',
                     'div[role="region"][aria-label]'):
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                label = _strip_icons(element.get_attribute("aria-label") or "")
                if label and label.lower() not in ("main content", "map"):
                    return label
        except Exception:
            pass

    # 3. the page title, minus the "- Google Maps" suffix
    try:
        title = _strip_icons(driver.title or "")
        title = re.sub(r"\s*[-–]\s*Google Maps\s*$", "", title).strip()
        if title and title.lower() != "google maps":
            return title
    except Exception:
        pass

    # 4. the URL, which normally carries the name
    try:
        url = driver.current_url or ""
        match = re.search(r"/maps/place/([^/@?]+)", url)
        if match:
            name = unquote(match.group(1)).replace("+", " ").strip()
            # Maps also uses this slot for opaque payloads like
            # "data=!4m5!3m4!1s0x..." and bare plus-codes. Accepting those
            # would fill the sheet with 30 rows of gibberish and, worse,
            # convince the bot it was working while genuinely blocked.
            # Plus-codes ("2R6P+33G") are Google's coordinate shorthand and
            # appear where a name would when a place has no name at all.
            is_plus_code = bool(re.fullmatch(r"[23456789CFGHJMPQRVWX+]{4,}\s*\w*",
                                             name.replace(" ", "")))
            if (name
                    and not name.lower().startswith("data=")
                    and not name.startswith("!")
                    and not is_plus_code
                    and len(name) > 2
                    and sum(c.isalpha() for c in name) >= 3):
                return _strip_icons(name)
    except Exception:
        pass

    return ""


def _diagnose_blank_page(driver):
    """
    Works out why a listing page came back without a business name.

    The distinction matters because the fixes are opposites: throttling wants
    you to stop and wait, a dead browser wants a restart, and a consent wall
    wants a click. Reporting all three as "Google is throttling" sends you
    away for an hour when the fix was thirty seconds.
    """
    try:
        source = driver.page_source or ""
        url = driver.current_url or ""
    except Exception as err:
        print(f"      Diagnosis: cannot read the page at all ({err}).")
        print("      This usually means Chrome has died -- restart the run.")
        return

    lowered = source.lower()
    print(f"      Diagnosis: page is {len(source):,} bytes, url {url[:70]}")

    if any(s in lowered for s in ("unusual traffic", "captcha", "recaptcha",
                                  "are you a robot", "/sorry/")):
        print("      -> Google is showing a CAPTCHA. This is a real block;")
        print("         wait an hour, and raise SCROLL_DELAY before retrying.")
        return

    if any(s in lowered for s in ("before you continue", "consent.google",
                                  "accept all", "i agree")):
        print("      -> a consent screen is in the way. Run with")
        print("         HEADLESS = False in config.py and accept it once.")
        return

    if len(source) < 5000:
        print("      -> the page is nearly empty, so nothing rendered.")
        print("         Most often a Chrome/ChromeDriver version mismatch.")
        print("         Update Chrome, then delete the webdriver cache:")
        print("         C:\\Users\\<you>\\.wdm")
        return

    if "/maps/place/" not in url:
        print("      -> the browser is not on a place page; the click or")
        print("         navigation did not land. Retry once.")
        return

    print("      -> the page loaded but the business name never appeared.")
    print("         Usually throttling, sometimes just a slow connection.")
    print(f"         Try raising PAGE_LOAD_WAIT (currently "
          f"{getattr(config, 'PAGE_LOAD_WAIT', 10)}s) before assuming a block.")


def _business_key(record):
    """
    Identity for a business, used as the second dedup net.

    URL dedup catches the same listing reached twice. This catches the same
    BUSINESS published under two different Maps listings, which happens often
    with franchises and re-registered shops. Phone is the strongest signal;
    without one, name + address is the fallback.
    """
    name = _norm(record.get("Company Name", ""))
    phone = _digits(record.get("Phone Number", ""))
    if phone:
        return f"{name}|{phone}"
    return f"{name}|{_norm(record.get('Address', ''))}"


# ---------------------------------------------------------------------------
# One search
# ---------------------------------------------------------------------------

def _scrape_one_query(driver, query, seen_urls, seen_business, remaining, health, sink):
    """
    Runs one search on an already-open browser and returns its NEW listings.

    Reusing a single browser across every query is deliberate: launching
    Chrome per search would add ~10 seconds each time.

    Records go straight into `sink` as they are extracted rather than being
    returned at the end. If Chrome dies halfway through a search, everything
    pulled up to that moment is already safe in the caller's list.
    """
    duplicates = 0

    driver.get(_build_search_url(query))

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, RESULTS_FEED))
        )
        has_feed = True
    except TimeoutException:
        has_feed = False

    if not has_feed:
        # Google sometimes decides a query matches exactly one business and
        # jumps straight to that place's page -- there is no feed at all.
        if "/maps/place/" in driver.current_url:
            if driver.current_url not in seen_urls:
                seen_urls.add(driver.current_url)
                single = _extract_place_details(driver, driver.current_url)
                key = _business_key(single)
                if single["Company Name"] and key not in seen_business:
                    seen_business.add(key)
                    sink.append(single)
            return

        print("  No results panel appeared for this query.")
        _save_debug_snapshot(driver)
        return

    _scroll_results_panel(driver)

    place_urls = _collect_place_urls(driver, seen_urls)[:remaining]
    if not place_urls:
        print("  Nothing new here (all already collected).")
        return

    print(f"  {len(place_urls)} new listings, extracting...")

    limit = getattr(config, "STOP_AFTER_FAILED_LISTINGS", 6)

    for idx, place_url in enumerate(place_urls, start=1):
        print(f"    -> {idx}/{len(place_urls)}")
        details = _extract_place_details(driver, place_url)

        if not details["Company Name"]:
            print("       (skipped -- no business name found)")
            health["fails"] += 1

            # Diagnose once, on the first failure of the run. "Empty detail
            # page" has several causes that need opposite responses, and
            # calling them all throttling sends you off to wait an hour when
            # the real problem is a browser that needs restarting.
            if health["fails"] == 1 and not health.get("diagnosed"):
                health["diagnosed"] = True
                _diagnose_blank_page(driver)
            # A few failures are normal. A long unbroken run of them means
            # Google is serving stubs, and nothing after this will work
            # either -- so stop rather than burn the remaining queries.
            if health["fails"] >= limit:
                raise ScrapingHalted(
                    f"{health['fails']} listings in a row failed to load"
                )
            continue

        health["fails"] = 0

        key = _business_key(details)
        if key in seen_business:
            duplicates += 1
            print(f"       (duplicate business: {details['Company Name'][:40]})")
            continue

        seen_business.add(key)
        details["_url"] = place_url
        sink.append(details)
        time.sleep(config.SCROLL_DELAY)

    if duplicates:
        print(f"  Skipped {duplicates} duplicate business(es).")
    return


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape_google_maps(keyword, city, max_results=None, areas=None, variants=None):
    """
    Main entry point for this module.

    Nothing here is tied to a particular industry or city -- both come from
    what the user types, and the bot works out the rest in three stages:

      1. search exactly what was typed
      2. read the trade's real vocabulary out of the business names it found,
         and search those terms city-wide
      3. read locality names out of the addresses, and search every
         term x locality combination until the caps are reached

    Args:
        keyword (str): whatever the user typed, e.g. "Education Industries"
        city (str): whatever the user typed, e.g. "Gurgaon"
        max_results (int): soft cap on TOTAL businesses
        areas (list[str] | None): manual localities, skips discovery
        variants (list[str] | None): manual search terms, skips discovery

    Returns:
        list[dict]: one dict per unique business
    """
    if max_results is None:
        max_results = getattr(config, "MAX_RESULTS", 500)
    if variants is None:
        variants = getattr(config, "KEYWORD_VARIANTS", None)
    if areas is None:
        by_city = getattr(config, "AREAS_BY_CITY", None) or {}
        for name, locality_list in by_city.items():
            if name.strip().lower() == city.strip().lower():
                areas = locality_list
                print(f"Using the AREAS_BY_CITY list configured for {city}.")
                break
        if areas is None:
            areas = getattr(config, "AREAS", None)

    find_keywords = getattr(config, "AUTO_DISCOVER_KEYWORDS", True) and not variants
    find_areas = getattr(config, "AUTO_DISCOVER_AREAS", True) and not areas
    max_terms = getattr(config, "MAX_KEYWORD_VARIANTS", 6)
    max_areas = getattr(config, "MAX_AREAS", 15)
    max_queries = getattr(config, "MAX_QUERIES", 60)

    # Everything remembered is filed per industry AND city, so a healthcare
    # run in Mumbai neither skips nor learns from a coaching run in Noida.
    scope = _scope_key(keyword, city)
    seen_urls, seen_business = _load_seen(scope=scope)
    prior = _load_prior_records(city=city, keyword=keyword)

    if seen_urls:
        print(f"Skipping {len(seen_urls)} '{keyword}' businesses already "
              f"collected in {city}.")
        print("(other industries and cities are tracked separately)\n")
    if prior:
        print(f"Learning from {len(prior)} '{keyword}' businesses already "
              f"found in {city}.\n")

    driver = _create_driver()
    results = []
    searches_run = 0
    health = {"fails": 0}
    save_every = getattr(config, "RECOVERY_SAVE_EVERY", 25)
    last_saved = 0

    def run(query):
        """One search, with the shared caps and counters applied."""
        nonlocal searches_run, last_saved
        if searches_run >= max_queries or len(results) >= max_results:
            return []
        searches_run += 1
        print(f"[search {searches_run}/{max_queries}] {query}")
        before = len(results)
        try:
            _scrape_one_query(
                driver, query, seen_urls, seen_business,
                remaining=max_results - len(results),
                health=health, sink=results,
            )
        finally:
            # runs even when the search is cut short, so a halt still reports
            # and saves whatever that search managed to collect
            found = results[before:]
            if found or searches_run:
                print(f"  Running total: {len(results)} unique businesses\n")
            if len(results) - last_saved >= save_every:
                _save_recovery(results)
                last_saved = len(results)
        return found

    try:
        # --- Stage 1: exactly what the user asked for ------------------
        seed = f"{keyword} in {city}"
        driver.get(_build_search_url(seed))
        _handle_consent_popup(driver)
        run(seed)

        # --- Stage 2: the trade's own vocabulary -----------------------
        terms = list(variants or [])
        if find_keywords:
            terms = _rank_trade_terms(
                [r["Company Name"] for r in results + prior],
                keyword, city, max_terms,
            )
            if terms:
                print("Discovered search terms from the business names found:")
                for term in terms:
                    print(f"   {term}")
                print()
            else:
                print("No extra search terms stood out; using the keyword alone.\n")

        for term in terms:
            run(f"{term} in {city}")

        # A thin seed plus few discovered terms means this city simply does
        # not use the words the user typed. Fall back to the generic trade
        # words for the industry they clearly meant -- that is what finds the
        # businesses a literal search misses.
        if len(results) < 40:
            keyword_group = _industry_group(
                {_singular(t) for t in _tokens(keyword)}
            )
            if keyword_group is not None:
                extra = [
                    w for w in sorted(INDUSTRY_GROUPS[keyword_group])
                    if _norm(w) not in {_norm(t) for t in terms}
                    and _norm(w) not in _norm(keyword)
                ][:max_terms]
                if extra:
                    print("Few results so far -- trying the industry's common "
                          "terms as well:")
                    print("   " + ", ".join(extra) + "\n")
                for term in extra:
                    run(f"{term} in {city}")

        # --- Stage 3: locality by locality -----------------------------
        localities = list(areas or [])
        if find_areas:
            localities = _rank_localities(
                [r["Address"] for r in results + prior], city, limit=max_areas
            )
            if localities:
                print(f"Discovered {len(localities)} localities in {city}:")
                print("   " + ", ".join(localities) + "\n")
            else:
                print("No localities could be read from the addresses.\n")

        # Interleave by locality, not by term: if the caps cut us off early,
        # partial coverage of the whole city beats total coverage of one word.
        all_terms = [keyword] + [t for t in terms if _norm(t) != _norm(keyword)]
        for locality in localities:
            for term in all_terms:
                if searches_run >= max_queries or len(results) >= max_results:
                    break
                run(f"{term} in {locality}, {city}")

        if len(results) >= max_results:
            print(f"Reached max_results ({max_results}).")
        elif searches_run >= max_queries:
            print(f"Reached MAX_QUERIES ({max_queries}). Raise it for wider coverage.")

    except ScrapingHalted as reason:
        print(f"\nStopping early: {reason}.")

        if results:
            # Pages were loading and then stopped: that is throttling.
            print("Google throttles a session once it has served a few hundred")
            print("pages quickly. Searches still return, but the detail pages")
            print("come back empty, so continuing would collect nothing.")
            print("\nWhat helps, in order:")
            print("  1. keep what you have (below) and rerun later -- an hour")
            print("     is usually enough for the block to lift")
            print("  2. raise SCROLL_DELAY in config.py so the next run lasts")
            print("     longer before hitting the same wall")
            print("  3. lower MAX_RESULTS so a run finishes inside the window")
        else:
            # Nothing loaded at all -- a block is only one possibility, and
            # not the most likely one when the search itself returned results.
            print("Nothing loaded at all this run, which is different from")
            print("being throttled part-way. See the diagnosis printed above.")
            print("\nMost likely causes, in order:")
            print("  1. Chrome and ChromeDriver versions do not match --")
            print("     update Chrome, then delete the C:\\Users\\<you>\\.wdm")
            print("     folder so the right driver is downloaded again")
            print("  2. set HEADLESS = False in config.py and watch one run;")
            print("     a consent screen or captcha becomes obvious instantly")
            print("  3. raise PAGE_LOAD_WAIT if your connection is slow")
            print("  4. only then assume a block and wait an hour")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    _save_recovery(results)
    _append_seen(results, scope=scope)
    _append_master(results, keyword=keyword, city=city)
    for rec in results:
        rec.pop("_url", None)

    print(f"\nDone. {len(results)} new businesses from {searches_run} search(es).")
    if results:
        folder = getattr(config, "OUTPUT_DIR", "output")
        print(f"Everything so far, across all runs, is in "
              f"{os.path.join(folder, '_all_leads.csv')}")
        print("Run again later for the next batch -- these ones will be skipped.")
    return results