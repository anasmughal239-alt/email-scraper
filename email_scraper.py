"""
Email Scraper — sitemap-aware contact-page email finder.

Pipeline per input URL:
  1. Normalize URL -> bare domain, build base https URL.
  2. Discover sitemaps via robots.txt "Sitemap:" lines, plus common path guesses
     (sitemap.xml, sitemap_index.xml, sitemap-index.xml, wp-sitemap.xml, sitemap.xml.gz).
  3. Recursively walk sitemap indexes to collect page URLs.
  4. Rank URLs by contact-likelihood (contains contact/about/support/get-in-touch/...)
     and fetch the top candidates.
  5. If no sitemap / no contact URL found, fetch the homepage and look for
     footer/contact links there instead.
  6. On every fetched page: extract emails via
       - plain regex
       - Cloudflare "email-protection" obfuscation (cdn-cgi/l/email-protection)
       - human-obfuscated text patterns ("name [at] domain [dot] com")
  7. Filter out placeholder/tracking/false-positive addresses.
  8. Write results to CSV incrementally (crash/disconnect-safe).

Works as a plain CLI script and as a Google Colab cell (see README.md).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import random
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from typing import Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap/sitemap.xml",
    "/sitemaps.xml",
]

CONTACT_KEYWORDS = [
    "contact", "contact-us", "contactus", "about", "about-us", "aboutus",
    "support", "help", "get-in-touch", "getintouch", "reach-us", "reach-out",
    "connect", "enquiry", "inquiry", "customer-service", "customer-support",
    # Spanish
    "contacto", "contactenos", "contactanos", "sobre-nosotros", "quienes-somos",
    "soporte", "ayuda",
    # French
    "contactez-nous", "nous-contacter", "a-propos", "apropos", "assistance",
    # German
    "kontakt", "kontaktiere-uns", "ueber-uns", "uber-uns", "hilfe",
    # Italian
    "contatti", "chi-siamo", "assistenza",
    # Portuguese
    "contato", "fale-conosco", "sobre-nos", "suporte",
    # Dutch
    "neem-contact-op", "over-ons",
]

# Unambiguous "this page is for contacting us" phrases — trustworthy at any
# path depth. The single generic words below (about/support/help/connect/
# enquiry/inquiry) are only trustworthy on shallow pages: large SaaS sites
# have huge self-serve help-center/doc trees where "help" or "connect"
# appears in hundreds of unrelated tutorial slugs (e.g. Notion's
# /help/connect-a-custom-domain-with-notion-sites).
HARD_CONTACT_KEYWORDS = {
    "contact", "contact-us", "contactus", "about-us", "aboutus",
    "get-in-touch", "getintouch", "reach-us", "reach-out",
    "customer-service", "customer-support",
    "contacto", "contactenos", "contactanos", "sobre-nosotros", "quienes-somos",
    "contactez-nous", "nous-contacter", "a-propos", "apropos",
    "kontakt", "kontaktiere-uns", "ueber-uns", "uber-uns",
    "contatti", "chi-siamo",
    "contato", "fale-conosco", "sobre-nos",
    "neem-contact-op", "over-ons",
}
MAX_SOFT_MATCH_DEPTH = 2  # soft keywords only count on paths this shallow or less

# NOTE ON LANGUAGE COVERAGE: the above covers Latin-script European languages,
# which fit this scraper's path/token-matching approach. CJK, Arabic, and
# other non-Latin-script sites are NOT covered — their URL slugs are often
# percent-encoded or transliterated inconsistently, which this substring/token
# approach can't reliably handle without a much larger rework (URL-decoding
# every path, language-specific tokenization, etc.).

# Path segments that mean "this is content ABOUT a topic", not a real contact
# page — even though the page may otherwise score high on CONTACT_KEYWORDS
# (e.g. a policy doc at /gmail/about/policy/, or a blog post titled
# "customer-support-vs-customer-service").
NEGATIVE_PATH_KEYWORDS = {
    "policy", "privacy", "terms", "legal", "license", "gdpr", "cookie",
    "cookies", "blog", "weblog", "news", "press", "article", "articles",
}
DATED_PERMALINK_RE = re.compile(
    r"/\d{4}/(?:\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/", re.IGNORECASE
)

# Domains/patterns that are near-always noise, not a real contact address.
EMAIL_BLOCKLIST_DOMAINS = {
    "example.com", "example.org", "example.net",
    "sentry.io", "sentry-next.wixpress.com", "wixpress.com",
    "godaddy.com", "domain.com", "yourdomain.com", "yoursite.com", "yourbusiness.com",
    "schema.org", "w3.org", "adobe.com",
    "acme.com", "acmeinc.com",  # classic tutorial/placeholder company name
    "company.com",  # extremely common placeholder in SaaS demo/marketing copy
    "hostname.com",  # placeholder domain used in sysadmin/nginx-style tutorials
    "encom.com",  # fictional company from Tron, common tech-demo placeholder
    "foo.com", "bar.com", "test.com", "placeholder.com", "notarealemail.com",
    "noemail.com", "yourcompany.com", "mycompany.com", "email.com", "website.com",
    "host.com", "doe.net", "doe.com",  # generic placeholder / "Jane Doe" domains
    "xyzcorp.com",  # "XYZ Corp" — classic placeholder company name in docs/examples
    "mail.com",  # classic HTML placeholder text, e.g. <input placeholder="you@mail.com">
    "email.de",  # German equivalent placeholder domain (paired with "beispiel" = "example")
    "lovelace.app", "lovelace.com",  # "Ada Lovelace" — recurring tech-demo placeholder person
    # Multi-language "your company" placeholder domains, matching yourcompany.com.
    "ihrefirma.com", "tuempresa.com", "suaempresa.com",
    # Disposable/temporary email services — never a real long-term contact.
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org",
    "tempmail.com", "throwawaymail.com", "mailnesia.com", "trashmail.com",
    "fakeinbox.com", "dispostable.com", "yopmail.com", "sharklasers.com",
    "getnada.com", "maildrop.cc",
    "2x.png", "1x.png",  # guards against image-name false positives slipping through
}
EMAIL_BLOCKLIST_LOCALPARTS = {
    "no-reply", "noreply", "donotreply", "test", "example",
    "youremail", "email", "yourname",
    "john.appleseed", "jane.appleseed", "john.doe", "jane.doe",
    "beispiel",  # German for "example" — placeholder-form pattern
}
# Matches a whole local part that is just a rendered-without-backslash JS
# unicode escape (e.g. a non-breaking space) leaking outside a <script>
# tag we'd otherwise strip — same bug category as the earlier Shopify
# "u003e" prefix leak, just as a whole local part instead of a prefix.
ESCAPED_UNICODE_LOCALPART_RE = re.compile(r"^u[0-9a-f]{4}$", re.IGNORECASE)
# Retina-asset filenames often mistaken for emails, e.g. logo@2x.png,
# icon@3x.webm — the "@Nx." convention applies to any asset type, so match
# on the convention itself rather than a fixed extension list.
IMAGE_LIKE_SUFFIX_RE = re.compile(r"@\d+x\.[a-zA-Z]{1,5}$", re.IGNORECASE)

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9][a-zA-Z0-9._%+\-]{0,63}@[a-zA-Z0-9][a-zA-Z0-9.\-]{0,253}\.[a-zA-Z]{2,24}"
)

# "name [at] domain [dot] com" / "name (at) domain (dot) com" / "name AT domain DOT com"
OBFUSCATED_EMAIL_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]{1,64})\s*[\[\(]?\s*(?:at|AT|@)\s*[\]\)]?\s*"
    r"([a-zA-Z0-9.\-]{1,253})\s*[\[\(]?\s*(?:dot|DOT)\s*[\]\)]?\s*"
    r"([a-zA-Z]{2,24})"
)

REQUEST_TIMEOUT = 12
# A domain that's dead/blocked at the network level (not just a slow real
# site) usually fails just as fast at 6s as at 12s — this shorter timeout
# is used only for the initial reachability probe, so we don't wait the
# full REQUEST_TIMEOUT (x up to 3 attempts with retries) before deciding
# whether it's even worth trying the 7 guessed sitemap paths.
FAST_FAIL_PROBE_TIMEOUT = 6
# Hard ceiling on how long a single domain's worker thread may run before
# a batch gives up on it. Found via a real hang: artstation.com's thread
# ran 5+ minutes with zero CPU activity — well past what our own per-request
# timeouts (REQUEST_TIMEOUT x retries) should ever allow — pointing to a
# DNS/TLS-level stall outside requests' timeout coverage. Without this, one
# such domain blocks an entire batch indefinitely, even with everything
# else already done. Python cannot forcibly kill a stuck thread, so the
# abandoned worker may keep running in the background, but this stops it
# from blocking the batch's visible progress, CSV output, or completion.
DOMAIN_HARD_TIMEOUT_SECONDS = 180
MAX_SITEMAP_DEPTH = 3
MAX_PAGES_PER_DOMAIN = 10
# Large e-commerce/blog sites can have sitemap trees with thousands of child
# files (per-locale, per-collection, per-year archives). Cap total sitemap
# fetches per domain so one domain can't stall the whole batch.
MAX_SITEMAP_FETCHES_PER_DOMAIN = 50
# The fetch-count cap above bounds sitemap *files*, but a single file can
# list tens of thousands of URLs (e.g. figma.com's community-file index
# sitemap has 367,890 entries) — that alone made sitemap discovery take
# 100+ seconds for one domain, dwarfing everything else in the pipeline.
# This caps the total *page* count collected, so a handful of huge leaf
# sitemaps can't blow up runtime even while staying under the file cap.
#
# Regression found in a live run: an earlier value of 5000 was too tight —
# fastly.com has 5525 legitimate pages, and its real /contact-us page got
# cut off before being collected, silently losing a real result. 20000
# gives ordinary large sites comfortable headroom while still taming
# figma.com's extreme case (367,890 -> 20,000, an 18x cut).
MAX_TOTAL_SITEMAP_PAGES = 20000
# Sitemap *files* used to be fetched one at a time, recursively — a sitemap
# index with many locale/category children (fastly.com's tree needed ~50
# files to assemble its 5525 pages) took as long as the SUM of every file
# fetch. This bounds how many sitemap-file fetches run concurrently via a
# shared work-queue (BFS), so the same tree takes roughly as long as its
# slowest single fetch instead.
SITEMAP_FETCH_WORKERS = 6
# Extra same-domain links tried when sitemap + footer both find nothing —
# a shallow second hop rather than a full crawl, to catch contact pages
# that aren't sitemap-listed and aren't linked from the footer specifically.
MAX_SECOND_HOP_LINKS = 5
# How many candidate pages to fetch concurrently within a single domain —
# these fetches are independent of each other, so there's no reason to
# fetch them one at a time.
PER_DOMAIN_FETCH_WORKERS = 4
# User-agent token used when matching a site's robots.txt rules. We present
# as a generic bot ("*"), so we obey the rules any site sets for all crawlers.
ROBOTS_USER_AGENT = "*"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

def _brand_label(domain: str) -> str:
    """The leading label of a domain, e.g. 'mozilla' from 'mozilla.org' or
    'makenotion' from 'makenotion.com'. A crude stand-in for "company name"
    without needing a public-suffix-list dependency."""
    return domain.split(".")[0].lower()


def email_matches_domain(email: str, bare_domain: str) -> bool:
    """True if the email's domain is the target domain, a subdomain of it, or
    plausibly the same company under a different TLD/brand domain — e.g.
    mozilla.org's site with a mozilla.com email, or notion.so's site with a
    makenotion.com email. Distinguishes the company's own contact address
    from a third party's email incidentally mentioned on one of its pages
    (e.g. an integration partner's support address on a marketplace listing)."""
    email_domain = email.rpartition("@")[2].lower()
    bare_domain = bare_domain.lower()
    if email_domain == bare_domain or email_domain.endswith("." + bare_domain):
        return True

    # Conservative fuzzy fallback: only when both labels are long enough that
    # a match isn't coincidental, and lengths are close enough that a short
    # brand name isn't just happening to appear inside an unrelated longer
    # domain (e.g. "linear" inside "linear-algebra-tutors").
    email_label = _brand_label(email_domain)
    bare_label = _brand_label(bare_domain)
    shorter, longer = sorted([email_label, bare_label], key=len)
    if len(shorter) >= 5 and len(longer) / len(shorter) <= 2.5 and shorter in longer:
        return True
    # Prefix-specific case: catches short brand names with a product-suite
    # suffix (zoho.com site, zohocorp.com/zohoinvoice.com/zohobilling.com
    # emails) that the ratio rule's 5-char minimum would otherwise miss.
    # The suffix must be hyphen-free — a real sub-brand/product suffix is a
    # single compact word (invoice, billing, financeplus); the false-match
    # case ("linear" inside "linear-algebra-tutors") has a hyphen right at
    # the join, which this rejects regardless of length.
    if len(shorter) >= 4 and longer.startswith(shorter):
        remainder = longer[len(shorter):]
        if remainder and "-" not in remainder and len(remainder) <= 15:
            return True
    return False


# --------------------------------------------------------------------------
# Role classification: label each email by its local-part function and rank
# them so the most useful outreach contact surfaces first.
# --------------------------------------------------------------------------

# Checked in insertion order; first matching role wins. Order also mirrors
# ROLE_PRIORITY below (general/sales/support are the most useful for generic
# outreach; press/careers/billing/legal are specialized).
ROLE_KEYWORDS = {
    "general": {"info", "hello", "hi", "hey", "contact", "contactus", "hola",
                "enquiries", "enquiry", "inquiries", "inquiry", "mail", "office",
                "general", "ask", "team", "reception", "admin"},
    "sales": {"sales", "business", "bizdev", "biz", "partnerships", "partner",
              "partners", "deals", "newbusiness", "reseller"},
    "support": {"support", "help", "helpdesk", "care", "customercare",
                "customerservice", "service", "success", "customersuccess",
                "feedback"},
    "press": {"press", "media", "pr", "news", "communications", "comms",
              "publicity", "marketing"},
    "careers": {"careers", "career", "jobs", "job", "hr", "recruiting",
                "recruitment", "talent", "hiring", "work", "people", "apply"},
    "billing": {"billing", "accounts", "accounting", "finance", "invoices",
                "invoice", "payments", "payment", "orders"},
    "legal": {"legal", "privacy", "dpo", "gdpr", "compliance", "abuse",
              "security", "trust", "copyright", "dmca", "fraud", "coc", "conduct"},
}

# Lower number = higher priority (surfaced first / chosen as primary).
ROLE_PRIORITY = {
    "general": 0, "sales": 1, "support": 2, "personal": 3,
    "press": 4, "careers": 5, "billing": 6, "legal": 7, "other": 8,
}

# Single-token local parts that are system/automated addresses, NOT people —
# so a lone alpha token like these isn't mistaken for a first name.
SYSTEM_LOCALPARTS = {
    "webmaster", "postmaster", "hostmaster", "newsletter", "newsletters",
    "notifications", "notification", "mailer", "daemon", "bounce", "bounces",
    "root", "administrator", "automated", "system", "do-not-reply",
}


def classify_email_role(email: str) -> str:
    """Label an email by the function of its local part, e.g.
    info@x.com -> 'general', support@x.com -> 'support', jane.doe@x.com and
    alex@x.com -> 'personal'. Returns 'other' when nothing matches."""
    local = email.partition("@")[0].lower()
    tokens = set(re.split(r"[._\-+]", local))
    for role, keywords in ROLE_KEYWORDS.items():
        if local in keywords or tokens & keywords:
            return role
    parts = [p for p in re.split(r"[._\-]", local) if p]
    # firstname.lastname / firstname_lastname style -> an individual person.
    if len(parts) >= 2 and all(p.isalpha() and len(p) >= 2 for p in parts[:2]):
        return "personal"
    # A single alpha-only token (no digits) is most likely a first name or
    # vanity handle — treat as a person unless it's a known system address.
    if len(parts) == 1 and parts[0].isalpha() and 2 <= len(parts[0]) <= 20 \
            and parts[0] not in SYSTEM_LOCALPARTS:
        return "personal"
    return "other"


def rank_emails_by_role(emails) -> list:
    """Sort emails best-outreach-contact-first by role priority, tie-broken
    alphabetically for stable output."""
    return sorted(
        emails,
        key=lambda e: (ROLE_PRIORITY.get(classify_email_role(e), 99), e),
    )


def pick_primary_email(own_emails: list, other_emails: list) -> tuple:
    """Choose the single best contact email (prefer the company's own domain
    over third-party), returning (email, role) or ('', '')."""
    ranked_own = rank_emails_by_role(own_emails)
    ranked_other = rank_emails_by_role(other_emails)
    best = (ranked_own or ranked_other)
    if not best:
        return "", ""
    email = best[0]
    return email, classify_email_role(email)


@dataclass
class DomainResult:
    input_url: str
    domain: str = ""
    emails: set = field(default_factory=set)
    source_pages: set = field(default_factory=set)
    method: str = ""       # sitemap | homepage-fallback | none
    error: str = ""


# --------------------------------------------------------------------------
# HTTP session helpers
# --------------------------------------------------------------------------

def build_session(proxies: Optional[list] = None) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if proxies:
        proxy = random.choice(proxies)
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def fetch(session: requests.Session, url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[requests.Response]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            return resp
    except requests.RequestException:
        return None
    return None


def probe_domain_reachable(base_url: str) -> bool:
    """Quick, short-timeout connectivity check, used to skip a dead/blocked
    domain fast instead of paying for a full robots.txt fetch plus up to 7
    guessed sitemap paths — each through the main session's retry-with-
    backoff logic — before discovering the same thing. A site that responds
    at all (even with an error status like 403) counts as reachable, since
    error responses return immediately without retries; it's a fully failed
    connection (timeout/refused/DNS failure) that's slow today, and this
    probe targets exactly that case.

    Allows one retry: a single-attempt version produced a real false
    negative in testing (mysql.com flagged unreachable once, then answered
    normally on the very next attempt — an ordinary transient network blip,
    not a dead site). Worst case for a genuinely dead domain is now
    2x FAST_FAIL_PROBE_TIMEOUT instead of 1x, still far cheaper than the
    multi-attempt path it's replacing."""
    for _ in range(2):
        try:
            requests.get(
                base_url,
                headers={"User-Agent": random.choice(USER_AGENTS)},
                timeout=FAST_FAIL_PROBE_TIMEOUT,
                allow_redirects=True,
            )
            return True
        except requests.RequestException:
            continue
    return False


# --------------------------------------------------------------------------
# Step 1: URL/domain normalization
# --------------------------------------------------------------------------

def normalize_to_base_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urlparse(raw)
    netloc = parsed.netloc or parsed.path.split("/")[0]
    return f"https://{netloc}"


def get_bare_domain(base_url: str) -> str:
    netloc = urlparse(base_url).netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


# --------------------------------------------------------------------------
# Step 2-3: sitemap discovery
# --------------------------------------------------------------------------

class RobotsPolicy:
    """Holds a site's robots.txt rules: which URLs may be fetched, plus any
    declared sitemaps. Fetched once per domain and threaded through the
    pipeline so we can skip Disallow'd URLs before requesting them."""

    def __init__(self, parser: Optional[robotparser.RobotFileParser], sitemaps: list, enabled: bool = True):
        self._parser = parser
        self.sitemaps = sitemaps
        self.enabled = enabled

    def can_fetch(self, url: str) -> bool:
        # When compliance is off, or there's no usable robots.txt, allow all.
        if not self.enabled or self._parser is None:
            return True
        try:
            return self._parser.can_fetch(ROBOTS_USER_AGENT, url)
        except Exception:
            return True  # never let a robotparser quirk block the whole run


def fetch_robots_policy(session: requests.Session, base_url: str, respect_robots: bool = True) -> RobotsPolicy:
    """Fetch and parse robots.txt once. Extracts both the Disallow rules
    (for can_fetch checks) and the declared Sitemap: URLs. A missing or
    unreadable robots.txt fails open (allow all) — the conventional
    behaviour for a well-behaved but functional crawler."""
    resp = fetch(session, urljoin(base_url, "/robots.txt"))
    if not resp:
        return RobotsPolicy(None, [], enabled=respect_robots)

    lines = resp.text.splitlines()
    parser: Optional[robotparser.RobotFileParser] = robotparser.RobotFileParser()
    try:
        parser.parse(lines)
    except Exception:
        parser = None  # malformed robots.txt -> don't enforce, just skip

    sitemaps = [
        line.split(":", 1)[1].strip()
        for line in lines
        if line.lower().startswith("sitemap:")
    ]
    return RobotsPolicy(parser, sitemaps, enabled=respect_robots)


def parse_sitemap_xml(raw_bytes: bytes) -> tuple:
    """Returns (child_sitemap_urls, page_urls)."""
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError:
        return [], []

    tag = root.tag.lower()
    child_sitemaps, page_urls = [], []

    for elem in root:
        loc = None
        for child in elem:
            if child.tag.lower().endswith("loc"):
                loc = (child.text or "").strip()
                break
        if not loc:
            continue
        if tag.endswith("sitemapindex"):
            child_sitemaps.append(loc)
        else:
            page_urls.append(loc)

    return child_sitemaps, page_urls


def _fetch_one_sitemap(session: requests.Session, sitemap_url: str) -> tuple:
    """Fetches and parses a single sitemap file. Returns (child_sitemap_urls,
    page_urls) — no recursion, no shared state; the caller (discover_all_page_urls)
    owns the BFS traversal and bookkeeping."""
    resp = fetch(session, sitemap_url)
    if not resp:
        return [], []

    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass

    return parse_sitemap_xml(raw)


def discover_all_page_urls(session: requests.Session, base_url: str,
                            robots: Optional["RobotsPolicy"] = None) -> list:
    """Breadth-first, concurrent sitemap discovery. A sitemap index with many
    locale/category child files used to be fetched one at a time, recursively
    — taking as long as the SUM of every file fetch. This processes one
    "wave" of URLs at a time through a shared thread pool, so a wide sitemap
    tree takes roughly as long as its slowest single fetch instead."""
    # Sitemaps declared in robots.txt are already known; add common guesses.
    candidates = list(robots.sitemaps) if robots else []
    candidates += [urljoin(base_url, p) for p in COMMON_SITEMAP_PATHS]

    all_pages: list = []
    seen: set = set()
    fetch_count = 0
    page_count = 0
    frontier = [u for u in dict.fromkeys(candidates) if u not in seen]

    with ThreadPoolExecutor(max_workers=SITEMAP_FETCH_WORKERS) as executor:
        depth = 0
        while frontier and fetch_count < MAX_SITEMAP_FETCHES_PER_DOMAIN and page_count < MAX_TOTAL_SITEMAP_PAGES:
            wave = frontier[:MAX_SITEMAP_FETCHES_PER_DOMAIN - fetch_count]
            for u in wave:
                seen.add(u)
            fetch_count += len(wave)

            futures = {executor.submit(_fetch_one_sitemap, session, u): u for u in wave}
            next_frontier: list = []
            for future in as_completed(futures):
                children, pages = future.result()
                if pages:
                    all_pages.extend(pages)
                    page_count += len(pages)
                if depth < MAX_SITEMAP_DEPTH:
                    next_frontier.extend(c for c in children if c not in seen)

            frontier = list(dict.fromkeys(next_frontier))
            depth += 1

    pages = list(dict.fromkeys(all_pages))  # dedupe, preserve order
    if robots:
        pages = [u for u in pages if robots.can_fetch(u)]
    return pages


# --------------------------------------------------------------------------
# Step 4: rank + pick contact-like URLs
# --------------------------------------------------------------------------

def _path_tokens(path: str) -> tuple:
    """Split a URL path into segments and hyphen-split sub-tokens, e.g.
    '/blog/customer-support-vs-service' -> segments incl. 'blog', and tokens
    incl. 'customer', 'support', 'vs', 'service'."""
    segments = [p for p in path.strip("/").split("/") if p]
    tokens = set(segments)
    for seg in segments:
        tokens.update(seg.split("-"))
    return segments, tokens


def rank_contact_urls(urls: list, base_url: str, limit: int = MAX_PAGES_PER_DOMAIN) -> list:
    scored = []
    for url in urls:
        path = urlparse(url).path.lower()
        segments, tokens = _path_tokens(path)

        # Reject content/blog/policy pages outright, even if a keyword like
        # "support" or "about" happens to appear in the slug.
        if tokens & NEGATIVE_PATH_KEYWORDS:
            continue
        if DATED_PERMALINK_RE.search(path):
            continue

        matched = {kw for kw in CONTACT_KEYWORDS if kw in tokens}
        if not matched:
            continue

        # A deep path (help-center article, doc page, etc.) only counts as
        # contact-like if it matched an unambiguous phrase, not a generic
        # word that also shows up in ordinary tutorial/doc slugs.
        if len(segments) > MAX_SOFT_MATCH_DEPTH and not (matched & HARD_CONTACT_KEYWORDS):
            continue

        # Prefer shallow, dedicated pages (e.g. /contact) over deep ones.
        depth_bonus = max(0, 3 - len(segments)) * 0.5
        scored.append((len(matched) + depth_bonus, url))

    scored.sort(key=lambda t: -t[0])
    return [u for _, u in scored[:limit]]


# --------------------------------------------------------------------------
# Step 5: homepage fallback — footer/contact link discovery
# --------------------------------------------------------------------------

def find_contact_links_on_page(html: str, base_url: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    footer = soup.find("footer")
    scope = footer if footer else soup

    links = []
    for a in scope.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        haystack = (href + " " + text).lower()
        if any(kw in haystack for kw in CONTACT_KEYWORDS):
            links.append(urljoin(base_url, href))
    return list(dict.fromkeys(links))


def find_second_hop_links(html: str, base_url: str, already_tried: set,
                           limit: int = MAX_SECOND_HOP_LINKS) -> list:
    """When sitemap discovery and keyword-based footer-link discovery both
    come up empty, take a broader (but still shallow and bounded) look at
    the homepage's other same-domain navigation links — e.g. a 'Team' or
    'Locations' page that doesn't happen to contain any contact keyword."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc

    candidates = []
    seen = set(already_tried)
    for a in soup.find_all("a", href=True):
        full_url = urljoin(base_url, a["href"])
        parsed = urlparse(full_url)
        if parsed.netloc != base_domain or full_url in seen:
            continue
        path = parsed.path.lower()
        segments, tokens = _path_tokens(path)
        if tokens & NEGATIVE_PATH_KEYWORDS or DATED_PERMALINK_RE.search(path):
            continue
        if len(segments) > 2:  # stay shallow — nav links, not deep content
            continue
        seen.add(full_url)
        candidates.append((len(segments), full_url))

    candidates.sort(key=lambda t: t[0])
    return [u for _, u in candidates[:limit]]


# --------------------------------------------------------------------------
# Step 6: email extraction (plain + obfuscated + Cloudflare)
# --------------------------------------------------------------------------

def decode_cloudflare_email(encoded: str) -> Optional[str]:
    """Cloudflare's /cdn-cgi/l/email-protection obfuscation is a single-byte XOR."""
    try:
        raw = bytes.fromhex(encoded)
    except ValueError:
        return None
    if len(raw) < 2:
        return None
    key = raw[0]
    decoded = bytes(b ^ key for b in raw[1:])
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_cloudflare_emails(html: str) -> set:
    found = set()
    for m in re.finditer(r'data-cfemail="([a-fA-F0-9]+)"', html):
        decoded = decode_cloudflare_email(m.group(1))
        if decoded:
            found.add(decoded)
    for m in re.finditer(r"/cdn-cgi/l/email-protection#([a-fA-F0-9]+)", html):
        decoded = decode_cloudflare_email(m.group(1))
        if decoded:
            found.add(decoded)
    return found


def extract_obfuscated_emails(text: str) -> set:
    found = set()
    for m in OBFUSCATED_EMAIL_RE.finditer(text):
        local, domain, tld = m.groups()
        found.add(f"{local}@{domain}.{tld}".lower())
    return found


def is_valid_candidate(email: str) -> bool:
    email = email.strip().strip(".,;:")
    if IMAGE_LIKE_SUFFIX_RE.search(email):
        return False
    local, _, domain = email.partition("@")
    if not local or not domain:
        return False
    domain = domain.lower()
    # A real hostname never contains whitespace or URL-special characters —
    # guards against a leftover query string (e.g. a mailto: href whose "?"
    # was percent-encoded as "%3F" and so slipped past our literal-"?" split)
    # ending up attached to the domain.
    if re.search(r"[\s?=&#/%]", domain):
        return False
    if domain.split(".")[0] == "example":  # example.<any TLD>, not just .com/.org/.net
        return False
    if any(domain == d or domain.endswith("." + d) for d in EMAIL_BLOCKLIST_DOMAINS):
        return False
    if local.lower() in EMAIL_BLOCKLIST_LOCALPARTS:
        return False
    if ESCAPED_UNICODE_LOCALPART_RE.match(local):
        return False
    if domain.endswith((
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",  # images
        ".mjs", ".js", ".css", ".map", ".json", ".wasm",   # JS/build asset filenames, e.g. house.js@0.0.<hash>.mjs
    )):
        return False
    return True


def extract_emails_from_html(html: str) -> set:
    emails = set()
    # Cloudflare-obfuscated emails live in a data-cfemail attribute or
    # cdn-cgi link even inside <script>, so decode these from the raw HTML
    # before stripping scripts below.
    emails |= extract_cloudflare_emails(html)

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if addr:
                emails.add(addr)

    # Strip <script>/<style> content before any further text/HTML scan —
    # otherwise JSON payloads (e.g. Next.js data blobs) leak HTML-escaped
    # fragments like ">plus@shopify.com" into the match.
    for tag in soup(["script", "style"]):
        tag.decompose()

    visible_text = soup.get_text(" ", strip=True)
    emails |= set(EMAIL_RE.findall(visible_text))
    emails |= set(EMAIL_RE.findall(str(soup)))
    emails |= extract_obfuscated_emails(visible_text)

    return {e.lower() for e in emails if is_valid_candidate(e)}


# --------------------------------------------------------------------------
# Optional: MX record sanity check (soft dependency on dnspython)
# --------------------------------------------------------------------------

_dns_cache: dict = {}

def domain_has_mx(domain: str) -> bool:
    if domain in _dns_cache:
        return _dns_cache[domain]
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        _dns_cache[domain] = True  # dnspython not installed -> skip the check, don't discard
        return True
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        result = len(answers) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        result = False  # domain doesn't exist or genuinely has no mail server
    except Exception:
        result = True  # transient/network error on our end -> don't punish the email for it
    _dns_cache[domain] = result
    return result


# --------------------------------------------------------------------------
# Optional: Playwright fallback for JS-rendered footers (soft dependency)
# --------------------------------------------------------------------------
# Some sites inject their footer/contact info entirely client-side, so a
# plain `requests` GET sees an empty shell. This fallback only runs when the
# static pass already found zero emails for a domain — Playwright is much
# heavier than a plain HTTP request (spins up a real Chromium instance), so
# it's not worth paying that cost on every domain.

PLAYWRIGHT_PAGE_TIMEOUT_MS = 20000
# Each PlaywrightFetcher launches a real Chromium process (typically
# 200-500MB+). Domain processing runs across many concurrent worker
# threads, so without a cap, several domains needing the fallback at the
# same time launch that many Chromium instances simultaneously — this is
# exactly what caused an out-of-memory crash on a resource-constrained
# host (Railway) even though the CLI/local runs never hit it. This
# semaphore bounds concurrent Chromium instances independently of the
# batch's overall --workers setting.
MAX_CONCURRENT_PLAYWRIGHT_INSTANCES = 1
_playwright_semaphore = threading.Semaphore(MAX_CONCURRENT_PLAYWRIGHT_INSTANCES)


class PlaywrightFetcher:
    """Launches one headless Chromium instance and reuses it across every
    page fetched for a single domain, closing it when done."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # raises ImportError if not installed
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as exc:
            # Hosts like Streamlit Cloud install apt deps via packages.txt but
            # have no build hook to download the actual browser binary, so the
            # first Playwright-enabled run on a fresh container needs to fetch
            # it here — a one-time ~300MB download, slow but self-healing.
            if "Executable doesn't exist" not in str(exc):
                self._pw.stop()
                raise
            import subprocess
            import sys
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            self._browser = self._pw.chromium.launch(headless=True)
        return self

    def fetch(self, url: str) -> Optional[str]:
        try:
            page = self._browser.new_page(user_agent=random.choice(USER_AGENTS))
            try:
                # "networkidle" is unreliable on real sites — analytics beacons,
                # chat widgets, and websockets keep the network busy forever on
                # many pages, turning this into a guaranteed timeout. Wait for
                # the DOM instead, then give client-side rendering a moment.
                page.goto(url, timeout=PLAYWRIGHT_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                return page.content()
            finally:
                page.close()
        except Exception:
            return None

    def __exit__(self, *exc_info):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def playwright_fallback(result: "DomainResult", base_url: str, pages_to_check: list, delay: float) -> None:
    """Re-fetches pages with a real headless browser when the static pass
    found nothing, to catch footers/contact info rendered by client-side JS.
    Only MAX_CONCURRENT_PLAYWRIGHT_INSTANCES domains can be running a
    Chromium instance at any moment — other domains needing the fallback
    block here until a slot frees up, trading some wall-clock time for a
    bounded memory ceiling regardless of --workers."""
    with _playwright_semaphore:
        try:
            with PlaywrightFetcher() as pf:
                urls_to_try = pages_to_check or [base_url]
                extra_links: list = []

                for page_url in urls_to_try[:MAX_PAGES_PER_DOMAIN]:
                    html = pf.fetch(page_url)
                    time.sleep(delay)
                    if not html:
                        continue
                    if not pages_to_check:
                        # Homepage was previously unreachable/empty; now that we
                        # have JS-rendered HTML, look for contact links in it too.
                        extra_links = find_contact_links_on_page(html, base_url)
                    found = extract_emails_from_html(html)
                    if found:
                        result.emails |= found
                        result.source_pages.add(page_url)

                for link_url in extra_links[:MAX_PAGES_PER_DOMAIN - 1]:
                    html = pf.fetch(link_url)
                    time.sleep(delay)
                    if not html:
                        continue
                    found = extract_emails_from_html(html)
                    if found:
                        result.emails |= found
                        result.source_pages.add(link_url)

            if result.emails:
                result.method = f"{result.method}+playwright"
                result.error = ""
            else:
                result.error = result.error or "no emails found on checked pages (incl. JS-rendered)"

        except ImportError:
            result.error = (
                (result.error + "; " if result.error else "")
                + "playwright not installed — run: pip install playwright && playwright install chromium"
            )


# --------------------------------------------------------------------------
# Per-domain pipeline
# --------------------------------------------------------------------------

def fetch_and_extract_pages(session: requests.Session, urls: list, robots: "RobotsPolicy",
                             delay: float) -> tuple:
    """Fetches a domain's candidate pages concurrently (they're independent
    of each other, so there's no reason to wait for one before starting the
    next) and extracts emails from each. `delay` staggers when each fetch
    *starts* rather than serializing them — a soft rate limit that still
    lets slow pages run in the background instead of blocking faster ones."""
    emails: set = set()
    source_pages: set = set()
    allowed_urls = [u for u in urls if robots.can_fetch(u)]
    if not allowed_urls:
        return emails, source_pages

    def _fetch_one(url: str) -> tuple:
        resp = fetch(session, url)
        if not resp:
            return url, None
        return url, extract_emails_from_html(resp.text)

    max_workers = min(PER_DOMAIN_FETCH_WORKERS, len(allowed_urls))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for url in allowed_urls:
            futures.append(executor.submit(_fetch_one, url))
            if delay:
                time.sleep(delay)
        for future in as_completed(futures):
            url, found = future.result()
            if found:
                emails |= found
                source_pages.add(url)
    return emails, source_pages


def process_domain(raw_url: str, delay: float, proxies: Optional[list], verify_mx: bool,
                    use_playwright: bool = False, respect_robots: bool = True) -> DomainResult:
    result = DomainResult(input_url=raw_url)
    base_url = normalize_to_base_url(raw_url)
    result.domain = get_bare_domain(base_url)
    if not result.domain:
        result.error = "could not parse domain"
        return result

    if not probe_domain_reachable(base_url):
        result.method = "none"
        result.error = "domain unreachable (fast-fail connectivity probe)"
        if use_playwright:
            playwright_fallback(result, base_url, [], delay)
        return result

    session = build_session(proxies)

    try:
        robots = fetch_robots_policy(session, base_url, respect_robots=respect_robots)

        all_pages = discover_all_page_urls(session, base_url, robots)
        contact_urls = rank_contact_urls(all_pages, base_url)

        home_resp = None
        pages_to_check = []
        if contact_urls:
            result.method = "sitemap"
            pages_to_check = contact_urls
        else:
            result.method = "homepage-fallback"
            if robots.can_fetch(base_url):
                home_resp = fetch(session, base_url)
            if home_resp:
                pages_to_check = [base_url]
                extra_links = find_contact_links_on_page(home_resp.text, base_url)
                pages_to_check += [u for u in extra_links if robots.can_fetch(u)][:MAX_PAGES_PER_DOMAIN - 1]

        if not pages_to_check:
            result.method = "none"
            result.error = "no sitemap, no contact links, homepage unreachable"
            if use_playwright:
                playwright_fallback(result, base_url, pages_to_check, delay)
            return result

        found, source_pages = fetch_and_extract_pages(
            session, pages_to_check[:MAX_PAGES_PER_DOMAIN], robots, delay
        )
        result.emails |= found
        result.source_pages |= source_pages

        # Shallow second hop: sitemap + footer-keyword discovery both failed
        # to surface anything useful, so try a few more same-domain nav links
        # from the homepage before giving up or falling back to Playwright.
        if not result.emails and result.method == "homepage-fallback" and home_resp:
            second_hop_urls = find_second_hop_links(home_resp.text, base_url, set(pages_to_check))
            found, source_pages = fetch_and_extract_pages(session, second_hop_urls, robots, delay)
            result.emails |= found
            result.source_pages |= source_pages

        if not result.emails and use_playwright:
            playwright_fallback(result, base_url, pages_to_check, delay)

        if verify_mx and result.emails:
            result.emails = {
                e for e in result.emails
                if domain_has_mx(e.partition("@")[2])
            }

        if not result.emails:
            result.error = result.error or "no emails found on checked pages"

    except Exception as exc:  # noqa: BLE001 - keep batch running on a single-domain failure
        result.error = f"unexpected error: {exc}"

    return result


# --------------------------------------------------------------------------
# Batch runner + CSV writer
# --------------------------------------------------------------------------

def load_domains(path: str) -> list:
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line.split(",")[0].strip())
    return urls


def load_proxies(path: Optional[str]) -> Optional[list]:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    return proxies or None


CSV_FIELDNAMES = [
    "input_url", "domain", "primary_email", "primary_role",
    "own_domain_emails", "other_domain_emails",
    "method", "source_pages", "error",
]


def result_to_row(r: "DomainResult") -> dict:
    """Turn a DomainResult into a flat output row: split own/other-domain
    emails, rank each list by role, and pick the single best primary contact.
    Shared by the CLI CSV writer and the Streamlit dashboard so both stay in
    sync."""
    own = [e for e in r.emails if email_matches_domain(e, r.domain)]
    other = [e for e in r.emails if e not in own]
    own_ranked = rank_emails_by_role(own)
    other_ranked = rank_emails_by_role(other)
    primary_email, primary_role = pick_primary_email(own_ranked, other_ranked)
    return {
        "input_url": r.input_url,
        "domain": r.domain,
        "primary_email": primary_email,
        "primary_role": primary_role,
        "own_domain_emails": "; ".join(own_ranked),
        "other_domain_emails": "; ".join(other_ranked),
        "method": r.method,
        "source_pages": "; ".join(sorted(r.source_pages)),
        "error": r.error,
    }


def drain_futures_with_hard_timeout(futures_to_url: dict, per_domain_timeout: float = DOMAIN_HARD_TIMEOUT_SECONDS):
    """Yields (url, DomainResult) as domain-processing futures complete, but
    gives up on any single future that's been pending longer than
    per_domain_timeout — rather than letting one stuck domain block every
    other (already-finished-or-finishing) domain in the batch forever.
    Shared by the CLI batch runner and the Streamlit dashboard.

    Python has no way to forcibly kill a running thread, so an abandoned
    worker may keep executing in the background after being given up on —
    this only stops it from blocking the caller's progress/output."""
    pending = dict(futures_to_url)  # future -> url
    started_at = {f: time.time() for f in pending}
    while pending:
        done, _ = wait(pending.keys(), timeout=5, return_when=FIRST_COMPLETED)
        now = time.time()
        for f in list(done):
            url = pending.pop(f)
            started_at.pop(f, None)
            yield url, f.result()
        for f in list(pending.keys()):
            if now - started_at[f] >= per_domain_timeout:
                url = pending.pop(f)
                started_at.pop(f, None)
                r = DomainResult(input_url=url)
                r.domain = get_bare_domain(normalize_to_base_url(url))
                r.method = "none"
                r.error = (
                    f"gave up after {per_domain_timeout:.0f}s - domain appears stuck "
                    "beyond normal timeouts (e.g. a DNS/TLS-level stall)"
                )
                yield url, r


def run_batch(input_path: str, output_path: str, max_workers: int = 10, delay: float = 0.3,
              proxies_path: Optional[str] = None, verify_mx: bool = False,
              use_playwright: bool = False, respect_robots: bool = True) -> None:
    domains = load_domains(input_path)
    proxies = load_proxies(proxies_path)

    if use_playwright:
        # Fail fast with a clear message rather than letting every domain's
        # fallback silently no-op with an ImportError buried in its error column.
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "use_playwright=True but the 'playwright' package (and its browser "
                "binaries) is not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        f.flush()

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {
                executor.submit(process_domain, url, delay, proxies, verify_mx,
                                use_playwright, respect_robots): url
                for url in domains
            }
            done_count = 0
            for _url, r in drain_futures_with_hard_timeout(futures):
                writer.writerow(result_to_row(r))
                f.flush()
                done_count += 1
                status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
                print(f"[{done_count}/{len(domains)}] {r.domain or r.input_url}: {status}", flush=True)
        finally:
            # wait=False: if a domain was abandoned above for exceeding the
            # hard timeout, its thread may still be running — don't let our
            # own shutdown block on it too.
            executor.shutdown(wait=False)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sitemap-aware contact email scraper")
    parser.add_argument("--input", "-i", required=True, help="Path to file with one URL/domain per line")
    parser.add_argument("--output", "-o", default="results.csv", help="Path to output CSV")
    parser.add_argument("--workers", "-w", type=int, default=10, help="Concurrent domains to process")
    parser.add_argument("--delay", "-d", type=float, default=0.3, help="Seconds to stagger page-fetch starts within a domain")
    parser.add_argument("--proxies", "-p", default=None, help="Optional path to a file of proxy URLs, one per line")
    parser.add_argument("--verify-mx", action="store_true", help="Drop emails whose domain has no MX record (requires dnspython)")
    parser.add_argument("--use-playwright", action="store_true",
                         help="Retry with a headless browser (requires: pip install playwright && "
                              "playwright install chromium) when a domain's static fetch finds zero emails")
    parser.add_argument("--ignore-robots", action="store_true",
                         help="Do NOT honor robots.txt Disallow rules (default: honored). "
                              "Only use on sites you own or have permission to crawl.")
    args = parser.parse_args()

    run_batch(
        input_path=args.input,
        output_path=args.output,
        max_workers=args.workers,
        delay=args.delay,
        proxies_path=args.proxies,
        verify_mx=args.verify_mx,
        use_playwright=args.use_playwright,
        respect_robots=not args.ignore_robots,
    )


if __name__ == "__main__":
    main()
