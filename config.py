"""
config.py
---------
Every tunable setting for the bot lives here, so you never edit the logic
files to change behaviour.

Nothing in here is tied to a particular industry or city. You type both at
the prompt when you run the bot, and it works the rest out on its own.

If you ever see "module 'config' has no attribute ...", run:
    python check_config.py
"""

# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

# False = you watch Chrome work (useful while debugging)
# True  = runs invisibly in the background (faster)
HEADLESS = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
# These are politeness settings, not just speed knobs. Lowering them shortens
# the run but makes throttling far more likely -- and a throttled run quietly
# returns fewer results instead of raising an error, which is the worst kind
# of failure because it looks like success.

SCROLL_DELAY = 3.0            # between scrolls, and between listings
PAGE_LOAD_WAIT = 10           # how long to wait for a page to render
WEBSITE_REQUEST_DELAY = 1.5   # between hits on company websites


# ---------------------------------------------------------------------------
# How wide to search
# ---------------------------------------------------------------------------

MAX_SCROLLS = 40      # each scroll loads roughly 5-7 more listings

# The bot searches in three stages:
#   1. exactly what you typed
#   2. trade terms it reads out of the business names it found
#   3. every term across every locality it read out of the addresses
#
# Both discoveries come from Google's own data, so they fit whatever industry
# and city you enter -- no lists to maintain here.
AUTO_DISCOVER_KEYWORDS = True
AUTO_DISCOVER_AREAS = True

MAX_KEYWORD_VARIANTS = 6      # extra trade terms to try
MAX_AREAS = 15                # localities to explore
MAX_QUERIES = 80             # hard ceiling on total searches
MAX_RESULTS = 180             # hard ceiling on businesses PER RUN

# Google starts serving stub pages once a session has pulled a few hundred
# listings quickly. When that happens every listing fails to render, so a
# long unbroken run of failures means stop -- not try harder.
STOP_AFTER_FAILED_LISTINGS = 6

# Remember across runs which businesses have already been collected, so a
# second run picks up where the first stopped instead of re-scraping the
# same city-wide results. This is what makes a modest MAX_RESULTS work:
# three 180-lead runs give you 540 leads, not the same 180 three times.
# Delete output/_seen.csv to start a city over from scratch.
RESUME_ACROSS_RUNS = True

# How often to write output/_recovery.csv. A crash then costs a few minutes
# of work instead of the entire run.
RECOVERY_SAVE_EVERY = 25

# Whichever ceiling is reached first ends the run. MAX_RESULTS is set below
# the point where Google starts throttling (around 200 listings pulled in
# quick succession), so a run finishes cleanly instead of dying partway.
#
# This is not a limit on your total leads. RESUME_ACROSS_RUNS below means
# the next run skips everything already collected, so three runs an hour
# apart give roughly 500 -- and each one completes.


# ---------------------------------------------------------------------------
# Optional manual overrides
# ---------------------------------------------------------------------------
# Leave these alone unless discovery gets something wrong for a specific job.
# Anything set here is used INSTEAD of discovery.

KEYWORD_VARIANTS = None   # e.g. ["coaching institute", "tuition centre"]

AREAS_BY_CITY = {}        # e.g. {"Gurgaon": ["Sector 45", "DLF Phase 1"]}


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------
# All names below now match email_extractor.py exactly (verified against
# the real file, not guessed).

EMAIL_REGEX = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"

REQUEST_TIMEOUT = 10          # seconds before giving up on a company website

CONTACT_PAGE_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/reach-us"]

PREFERRED_PREFIXES = ["info@", "contact@", "sales@", "enquiry@", "hello@"]

# How hard to look before settling for an address.
#   False -- take the first address found. Fast, usually right.
#   True  -- keep checking /contact, /about etc. until one appears on the
#            company's own domain. Slower, but stops a web designer's
#            footer address being recorded as the company's.
# Either way, an off-domain address is still used when it is the only one
# available -- it just prints a note so you can spot it in the log.
PREFER_OWN_DOMAIN_EMAIL = False


# ---------------------------------------------------------------------------
# WhatsApp numbers
# ---------------------------------------------------------------------------
# Pulled from the same page fetches as the email, so switching this on costs
# almost no extra time. Worth having: in trades like coaching, most sites
# carry a WhatsApp button but no email address at all.
EXTRACT_WHATSAPP = True

# Used when a site prints a bare 10-digit number with no country code.
# 91 = India.
DEFAULT_COUNTRY_CODE = "91"


# ---------------------------------------------------------------------------
# Social profiles
# ---------------------------------------------------------------------------
# Instagram, Facebook, LinkedIn and YouTube links, each in its own column.
# Read from the same page fetches as the email, so this costs no extra time.
#
# Worth having for the businesses that have a website but publish neither an
# email nor a WhatsApp number -- a social profile is often the only way in.
EXTRACT_SOCIAL_LINKS = True

EMAIL_BLOCKLIST_SUBSTRINGS = [
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    "example.com", "yourdomain", "sentry.io", "wixpress.com",
    "@2x", "@3x",
]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

OUTPUT_DIR = "output"
