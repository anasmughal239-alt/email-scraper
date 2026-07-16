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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "noemail.com", "yourcompany.com", "mycompany.com", "email.com",
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
}
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
MAX_SITEMAP_DEPTH = 3
MAX_PAGES_PER_DOMAIN = 10
# Large e-commerce/blog sites can have sitemap trees with thousands of child
# files (per-locale, per-collection, per-year archives). Cap total sitemap
# fetches per domain so one domain can't stall the whole batch.
MAX_SITEMAP_FETCHES_PER_DOMAIN = 50
# Extra same-domain links tried when sitemap + footer both find nothing —
# a shallow second hop rather than a full crawl, to catch contact pages
# that aren't sitemap-listed and aren't linked from the footer specifically.
MAX_SECOND_HOP_LINKS = 5
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
    return False


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


def fetch_sitemap_urls(session: requests.Session, sitemap_url: str, depth: int = 0,
                        seen: Optional[set] = None, fetch_counter: Optional[list] = None) -> list:
    if seen is None:
        seen = set()
    if fetch_counter is None:
        fetch_counter = [0]
    if depth > MAX_SITEMAP_DEPTH or sitemap_url in seen or fetch_counter[0] >= MAX_SITEMAP_FETCHES_PER_DOMAIN:
        return []
    seen.add(sitemap_url)
    fetch_counter[0] += 1

    resp = fetch(session, sitemap_url)
    if not resp:
        return []

    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass

    children, pages = parse_sitemap_xml(raw)
    for child in children:
        if fetch_counter[0] >= MAX_SITEMAP_FETCHES_PER_DOMAIN:
            break
        pages.extend(fetch_sitemap_urls(session, child, depth + 1, seen, fetch_counter))
    return pages


def discover_all_page_urls(session: requests.Session, base_url: str,
                            robots: Optional["RobotsPolicy"] = None) -> list:
    # Sitemaps declared in robots.txt are already known; add common guesses.
    candidates = list(robots.sitemaps) if robots else []
    candidates += [urljoin(base_url, p) for p in COMMON_SITEMAP_PATHS]

    all_pages, tried = [], set()
    fetch_counter = [0]
    for sm_url in candidates:
        if sm_url in tried or fetch_counter[0] >= MAX_SITEMAP_FETCHES_PER_DOMAIN:
            continue
        tried.add(sm_url)
        pages = fetch_sitemap_urls(session, sm_url, fetch_counter=fetch_counter)
        if pages:
            all_pages.extend(pages)

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
    if any(domain == d or domain.endswith("." + d) for d in EMAIL_BLOCKLIST_DOMAINS):
        return False
    if local.lower() in EMAIL_BLOCKLIST_LOCALPARTS:
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
    found nothing, to catch footers/contact info rendered by client-side JS."""
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

def process_domain(raw_url: str, delay: float, proxies: Optional[list], verify_mx: bool,
                    use_playwright: bool = False, respect_robots: bool = True) -> DomainResult:
    result = DomainResult(input_url=raw_url)
    base_url = normalize_to_base_url(raw_url)
    result.domain = get_bare_domain(base_url)
    if not result.domain:
        result.error = "could not parse domain"
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

        for page_url in pages_to_check[:MAX_PAGES_PER_DOMAIN]:
            if not robots.can_fetch(page_url):
                continue
            resp = fetch(session, page_url)
            time.sleep(delay)
            if not resp:
                continue
            found = extract_emails_from_html(resp.text)
            if found:
                result.emails |= found
                result.source_pages.add(page_url)

        # Shallow second hop: sitemap + footer-keyword discovery both failed
        # to surface anything useful, so try a few more same-domain nav links
        # from the homepage before giving up or falling back to Playwright.
        if not result.emails and result.method == "homepage-fallback" and home_resp:
            for page_url in find_second_hop_links(home_resp.text, base_url, set(pages_to_check)):
                if not robots.can_fetch(page_url):
                    continue
                resp = fetch(session, page_url)
                time.sleep(delay)
                if not resp:
                    continue
                found = extract_emails_from_html(resp.text)
                if found:
                    result.emails |= found
                    result.source_pages.add(page_url)

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


def run_batch(input_path: str, output_path: str, max_workers: int = 5, delay: float = 1.0,
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

    fieldnames = [
        "input_url", "domain", "own_domain_emails", "other_domain_emails",
        "method", "source_pages", "error",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_domain, url, delay, proxies, verify_mx,
                                use_playwright, respect_robots): url
                for url in domains
            }
            done_count = 0
            for future in as_completed(futures):
                r = future.result()
                own = {e for e in r.emails if email_matches_domain(e, r.domain)}
                other = r.emails - own
                writer.writerow({
                    "input_url": r.input_url,
                    "domain": r.domain,
                    "own_domain_emails": "; ".join(sorted(own)),
                    "other_domain_emails": "; ".join(sorted(other)),
                    "method": r.method,
                    "source_pages": "; ".join(sorted(r.source_pages)),
                    "error": r.error,
                })
                f.flush()
                done_count += 1
                status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
                print(f"[{done_count}/{len(domains)}] {r.domain or r.input_url}: {status}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sitemap-aware contact email scraper")
    parser.add_argument("--input", "-i", required=True, help="Path to file with one URL/domain per line")
    parser.add_argument("--output", "-o", default="results.csv", help="Path to output CSV")
    parser.add_argument("--workers", "-w", type=int, default=5, help="Concurrent domains to process")
    parser.add_argument("--delay", "-d", type=float, default=1.0, help="Seconds to sleep between page fetches per domain")
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
