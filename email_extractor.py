"""
email_extractor.py
-------------------
Given a company's website URL, this module tries to find a publicly
listed email address by:
  1. Fetching the homepage
  2. Fetching a handful of common "contact"/"about" sub-pages
  3. Scanning the page text/HTML with a regex for anything that looks
     like an email address
  4. Filtering out obvious junk/false positives
  5. Preferring addresses that actually belong to that company's domain

Main functions to use from outside this file:
    find_email(website_url) -> str
    find_contacts(website_url) -> dict with "email", "whatsapp" and the
                                  social profiles found on the site
"""

import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import config


# Suffixes where the registrable name is the third-last label, not the
# second-last: in "svgindia.co.in" the company is svgindia, not "co".
TWO_PART_SUFFIXES = {
    "co.in", "net.in", "org.in", "gen.in", "firm.in", "ind.in", "ac.in",
    "edu.in", "gov.in", "co.uk", "org.uk", "me.uk", "com.au", "net.au",
    "co.za", "com.br", "co.jp", "com.sg", "com.my", "co.nz", "com.pk",
    "com.bd", "co.ke", "com.tr",
}


def _registrable_domain(host):
    """Reduces a hostname to the part that identifies the organisation."""
    host = (host or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]

    labels = host.split(".")
    if len(labels) < 2:
        return host
    if ".".join(labels[-2:]) in TWO_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _site_domain(website_url):
    if not website_url:
        return ""
    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url
    return _registrable_domain(urlparse(website_url).netloc)


def _email_domain(email):
    if "@" not in (email or ""):
        return ""
    return _registrable_domain(email.rsplit("@", 1)[-1])


# --- WhatsApp -------------------------------------------------------------
# Sites link WhatsApp half a dozen ways, so match on the host rather than one
# fixed URL shape.
WA_LINK_RE = re.compile(
    r"(wa\.me|api\.whatsapp\.com|web\.whatsapp\.com|whatsapp://)", re.I)

# chat.whatsapp.com is a group invite, not a number -- no use for outreach.
WA_GROUP_RE = re.compile(r"chat\.whatsapp\.com", re.I)

# "WhatsApp: 98765 43210" written as plain text, with the label close by so a
# random phone number elsewhere on the page is not mistaken for one.
WA_TEXT_RE = re.compile(
    r"(whats\s*app|whatsapp)\D{0,20}((?:\+?\d[\d\s\-()]{7,16}\d))", re.I)


def _normalise_number(raw, default_cc=None):
    """
    Turns whatever a site wrote into +<country><number>, or "" if it cannot
    be a WhatsApp number.

    The landline check matters: plenty of institutes print "Call 0120-4567890"
    right beside a WhatsApp icon, and a landline on a WhatsApp column is worse
    than a blank one -- it looks usable and is not.
    """
    if default_cc is None:
        default_cc = str(getattr(config, "DEFAULT_COUNTRY_CODE", "91"))

    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]

    # Bare 10-digit Indian mobile, or one written with a leading 0
    if len(digits) == 10 and digits[0] in "6789":
        digits = default_cc + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = default_cc + digits[1:]

    if not (10 <= len(digits) <= 15):
        return ""

    # Indian numbers must be a mobile series; landlines cannot use WhatsApp
    if digits.startswith("91"):
        rest = digits[2:]
        if len(rest) != 10 or rest[0] not in "6789":
            return ""

    return "+" + digits


def _whatsapp_from_href(href):
    """Reads a number out of a WhatsApp link. Returns "" for group invites."""
    if not href or WA_GROUP_RE.search(href):
        return ""
    if not WA_LINK_RE.search(href):
        return ""

    # whatsapp:// is not a URL urlparse understands, so make it look like one
    probe = href.replace("whatsapp://", "https://placeholder/")
    query = parse_qs(urlparse(probe).query)
    if "phone" in query:
        return _normalise_number(unquote(query["phone"][0]))

    match = re.search(r"wa\.me/([^/?#]+)", href, re.I)
    if match:
        return _normalise_number(unquote(match.group(1)))
    return ""


def _extract_whatsapp_from_html(html):
    """
    Finds a WhatsApp number on one page.

    Links are tried before page text because they are unambiguous: a number
    in a wa.me href is definitely WhatsApp, whereas text next to the word
    "WhatsApp" is only probably WhatsApp.
    """
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        number = _whatsapp_from_href(link["href"])
        if number:
            return number

    # Some sites put the link in a button's onclick or a data- attribute
    for tag in soup.find_all(True):
        for value in tag.attrs.values():
            if isinstance(value, str) and WA_LINK_RE.search(value):
                match = re.search(r"https?://[^\s\"\']+", value)
                if match:
                    number = _whatsapp_from_href(match.group(0))
                    if number:
                        return number

    match = WA_TEXT_RE.search(soup.get_text(" "))
    if match:
        return _normalise_number(match.group(2))
    return ""


# --- Social profiles ------------------------------------------------------
# Platforms worth collecting, in the order the columns should appear.
SOCIAL_PLATFORMS = ("Instagram", "Facebook", "LinkedIn", "YouTube")

SOCIAL_PATTERNS = {
    "Instagram": r"(?:www\.)?instagram\.com/([A-Za-z0-9._]+)",
    "Facebook": r"(?:[a-z]{2,3}\.)?(?:www\.)?facebook\.com/([A-Za-z0-9.\-]+)",
    "LinkedIn": r"(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(company|school|in)"
                r"/([A-Za-z0-9._\-]+)",
    "YouTube": r"(?:www\.)?youtube\.com/(?:(@[A-Za-z0-9._\-]+)"
               r"|c/([A-Za-z0-9._\-]+)|channel/([A-Za-z0-9_\-]+)"
               r"|user/([A-Za-z0-9._\-]+))",
}

# First path segments that are never a profile. Sites are full of these --
# share buttons, tracking pixels, embedded videos -- and they all sit on the
# same domains as the real links, so matching the domain alone would fill the
# columns with facebook.com/sharer and instagram.com/p/... for every company.
SOCIAL_JUNK_PATHS = {
    "sharer", "share", "sharearticle", "sharing", "tr", "intent", "plugins",
    "embed", "p", "reel", "reels", "explore", "hashtag", "tags", "dialog",
    "watch", "login", "signup", "policies", "help", "about", "pages",
    "groups", "events", "story", "stories", "posts", "photo", "video",
    "permalink.php", "profile.php", "home", "search", "results",
}


def _classify_social(url):
    """
    Works out which platform a link belongs to and pulls out the handle.

    Returns (platform, canonical_url) or (None, None) when the link is not a
    usable profile.
    """
    if not url or "//" not in url:
        return None, None

    parsed = urlparse(url if url.startswith("http") else "https://" + url)
    path = parsed.path.strip("/")
    first_segment = path.split("/")[0].lower() if path else ""

    if not first_segment or first_segment in SOCIAL_JUNK_PATHS:
        return None, None

    for platform, pattern in SOCIAL_PATTERNS.items():
        match = re.search(pattern, url, re.I)
        if not match:
            continue

        groups = [g for g in match.groups() if g]
        if not groups:
            continue

        if platform == "LinkedIn":
            # /in/ is a person's own profile, not the business
            if groups[0].lower() == "in":
                return None, None
            handle = groups[-1]
            return platform, f"https://www.linkedin.com/{groups[0]}/{handle}"

        handle = groups[-1]
        if handle.lower() in SOCIAL_JUNK_PATHS:
            return None, None

        if platform == "Instagram":
            return platform, f"https://www.instagram.com/{handle}"
        if platform == "Facebook":
            return platform, f"https://www.facebook.com/{handle}"
        if platform == "YouTube":
            return platform, f"https://www.youtube.com/{handle}"

    return None, None


def classify_social_link(url):
    """
    Public wrapper around the social-link classifier.

    main.py uses this for the link Google lists as a "website" when it is
    actually a Facebook or Instagram page.

    Returns (column_name, profile_url) or (None, "").
    """
    platform, profile = _classify_social(url)
    return (platform, profile) if platform else (None, "")


def _extract_socials_from_html(html):
    """
    Collects one profile link per platform from a page.

    Only <a href> links are read, not raw text: scripts and embeds mention
    these domains constantly, and matching those would return a share widget
    for nearly every site.
    """
    found = {}
    if not html:
        return found

    soup = BeautifulSoup(html, "html.parser")
    for link in soup.find_all("a", href=True):
        platform, profile = _classify_social(link["href"])
        if platform and platform not in found:
            found[platform] = profile
    return found


def _fetch_page(url):
    """
    Fetches a single page and returns its HTML text, or None on failure.
    Wrapped in a try/except because company websites are unpredictable
    (dead links, slow servers, SSL issues, etc.) and we don't want one
    bad site to crash the whole bot.
    """
    try:
        headers = {"User-Agent": config.USER_AGENT}
        response = requests.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.text
    except requests.RequestException:
        pass
    return None


def _extract_emails_from_html(html):
    """
    Scans raw HTML/text for anything matching an email pattern,
    then filters out common false positives (image files, tracking
    pixels, placeholder domains, etc.).
    """
    if not html:
        return set()

    # Parse with BeautifulSoup so we can search visible text AND
    # attributes like mailto: links, which is where emails often hide
    soup = BeautifulSoup(html, "html.parser")

    candidates = set()

    # 1) mailto: links are the most reliable source
    for link in soup.find_all("a", href=True):
        if link["href"].lower().startswith("mailto:"):
            email = link["href"].split("mailto:")[1].split("?")[0].strip()
            candidates.add(email)

    # 2) Regex over the raw page text as a fallback
    page_text = soup.get_text(" ")
    found = re.findall(config.EMAIL_REGEX, page_text)
    candidates.update(found)

    # Filter out junk
    clean = set()
    for email in candidates:
        email_lower = email.lower()
        if any(bad in email_lower for bad in config.EMAIL_BLOCKLIST_SUBSTRINGS):
            continue
        clean.add(email_lower)

    return clean


def _pick_best(emails, site_domain):
    """
    Chooses which address to keep, and reports whether it belongs to the
    company or came from somewhere else on the page.

    Order of preference:
      1. role address on the company's own domain  (info@theirsite.com)
      2. any address on the company's own domain
      3. role address on some other domain
      4. anything else

    The domain check is the important part. A regex over page text picks up
    every address on the page -- the web designer's credit in the footer, a
    partner brand, a directory link -- so without it two unrelated companies
    can end up sharing one email, and the lead list looks fine while being
    quietly wrong.

    Returns (email, is_own_domain).
    """
    prefixes = tuple(getattr(
        config, "PREFERRED_PREFIXES",
        ("info@", "contact@", "sales@", "hello@", "support@"),
    ))

    own = sorted(e for e in emails if site_domain and _email_domain(e) == site_domain)
    other = sorted(e for e in emails if e not in own)

    for email in own:
        if email.startswith(prefixes):
            return email, True
    if own:
        return own[0], True

    for email in other:
        if email.startswith(prefixes):
            return email, False
    if other:
        return other[0], False

    return "", False


def find_contacts(website_url):
    """
    Fetches each page once and pulls both an email and a WhatsApp number
    out of it.

    Doing them together rather than in two passes matters: separate passes
    would double the requests to every company site, which both doubles the
    run time and doubles the chance of being rate-limited.

    Returns a dict with "email", "whatsapp", and one key per social platform
    ("instagram", "facebook", "linkedin", "youtube"). Any of them may be "".
    """
    result = {"email": "", "whatsapp": ""}
    for platform in SOCIAL_PLATFORMS:
        result[platform.lower()] = ""
    if not website_url:
        return result

    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    site_domain = _site_domain(website_url)
    keep_hunting = getattr(config, "PREFER_OWN_DOMAIN_EMAIL", False)
    want_whatsapp = getattr(config, "EXTRACT_WHATSAPP", True)

    pages_to_try = [website_url] + [
        urljoin(website_url, path) for path in config.CONTACT_PAGE_PATHS
    ]

    want_social = getattr(config, "EXTRACT_SOCIAL_LINKS", True)

    all_found_emails = set()
    whatsapp = ""
    socials = {}

    for page_url in pages_to_try:
        html = _fetch_page(page_url)
        all_found_emails.update(_extract_emails_from_html(html))

        if want_whatsapp and not whatsapp:
            whatsapp = _extract_whatsapp_from_html(html)

        if want_social:
            for platform, profile in _extract_socials_from_html(html).items():
                socials.setdefault(platform, profile)

        have_email = bool(all_found_emails)
        if keep_hunting:
            have_email = any(
                _email_domain(e) == site_domain for e in all_found_emails
            )

        # Stop only when nothing more is wanted from further pages
        if have_email and (whatsapp or not want_whatsapp):
            break

        time.sleep(config.WEBSITE_REQUEST_DELAY)

    email, is_own = _pick_best(all_found_emails, site_domain)

    if email and not is_own:
        print(f"      Note: {email} is not on {site_domain} "
              "-- may belong to someone else on the page.")

    result["email"] = email
    result["whatsapp"] = whatsapp
    for platform in SOCIAL_PLATFORMS:
        result[platform.lower()] = socials.get(platform, "")
    return result


def find_email(website_url):
    """
    Main entry point for this module.

    Kept for compatibility -- it simply returns the email half of
    find_contacts(). Use find_contacts() if you also want the WhatsApp number,
    since it gets both without fetching anything twice.

    When does it stop? That depends on PREFER_OWN_DOMAIN_EMAIL in config:

      False (default) -- stop at the first page that yields any address.
                         Fast. If several appear on that page, the domain
                         check still picks the most likely one.
      True            -- keep going until an address on the company's own
                         domain turns up. Slower, but avoids recording a
                         web designer's footer address as the company's.

    Either way an off-domain address is still returned if that is all there
    is; nothing gets discarded, it just gets flagged.

    Args:
        website_url (str): the company's website (may be empty/None)

    Returns:
        str: the best email address found, or "" if none found
    """
    return find_contacts(website_url)["email"]