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

Concurrency is asyncio/aiohttp-based throughout: fetching, sitemap discovery,
and per-domain batching. This lets a stuck domain (DNS/TLS-level stall) be
genuinely cancelled via asyncio.wait_for, rather than merely stopped-watching
the way a stuck OS thread would have to be.

Works as a plain CLI script and as a Google Colab cell (see README.md).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import csv
import gzip
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional
from urllib import robotparser
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import aiohttp
from aiohttp.resolver import ThreadedResolver
from bs4 import BeautifulSoup

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
# Hard ceiling on how long a single domain's processing may run before a
# batch gives up on it. Found via a real hang: artstation.com ran 5+ minutes
# with zero CPU activity — well past what our own per-request timeouts
# (REQUEST_TIMEOUT x retries) should ever allow — pointing to a DNS/TLS-level
# stall outside aiohttp's own timeout coverage. Without this, one such
# domain blocks an entire batch indefinitely, even with everything else
# already done. Enforced via asyncio.wait_for, which genuinely cancels the
# stuck coroutine at the next await point (unlike an OS thread, which can't
# be forcibly killed) — so the abandoned domain's task actually stops
# rather than merely being ignored.
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
# fetch. This bounds how many sitemap-file fetches run concurrently via an
# asyncio.Semaphore over one "wave" of URLs at a time, so the same tree
# takes roughly as long as its slowest single fetch instead.
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
# HTTP fetch helpers (aiohttp-based)
# --------------------------------------------------------------------------

@dataclass
class _FetchResponse:
    text: str
    content: bytes


async def _run_cpu(func, *args):
    """Runs a CPU-bound, pure-Python parse (sitemap XML, BeautifulSoup HTML)
    in the default executor thread pool instead of the event loop itself.
    Without this, a single expensive parse — e.g. figma.com's real
    367,890-entry sitemap file — blocks the *shared* event loop for the
    whole parse, stalling every other concurrently-processing domain's
    timers and socket reads along with it (an OS-thread-per-domain model
    doesn't have this failure mode, since each thread's CPU work only ever
    blocked that one thread). Found via a real regression: several
    unrelated domains falsely reported "unreachable" in the same batch
    that hit figma.com's giant sitemap, because their short probe timeouts
    fired while the loop was stuck parsing XML for a different domain."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _new_connector() -> aiohttp.TCPConnector:
    """Explicitly force aiohttp's plain ThreadedResolver (a background-thread
    wrapper around socket.getaddrinfo) instead of letting aiohttp
    auto-select AsyncResolver whenever the optional 'aiodns' package happens
    to be installed. aiodns's c-ares resolver failed outright on this
    project's Windows dev machine (ClientConnectorDNSError: "Could not
    contact DNS servers") even though the same lookups work fine via plain
    socket.getaddrinfo — a known aiohttp+aiodns+Windows footgun. Forcing
    ThreadedResolver keeps behavior identical and reliable across platforms
    rather than depending on which optional packages happen to be present."""
    return aiohttp.TCPConnector(resolver=ThreadedResolver())


# aiohttp's default header-size limits (max_line_size/max_field_size =
# 8190 bytes each) are tuned for ordinary sites, not modern e-commerce
# pages — a large Content-Security-Policy header listing every third-party
# script domain (Klaviyo, Listrak, ad pixels, etc.) routinely exceeds that
# on real Shopify/DTC storefronts. Found via a real batch: mvmt.com raised
# "Got more than 8190 bytes when reading" and was reported as completely
# unreachable, even though the site was up and answering fine — the
# request itself failed on header parsing before any of our own logic
# (probe, robots.txt, sitemap) ever ran. Raised well past what any
# legitimate site should need, rather than tuned to one observed value.
_HEADER_SIZE_LIMIT = 65536


def new_client_session(timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
    """Single construction point for every aiohttp.ClientSession in this
    module, so the header-size fix (and the DNS-resolver fix in
    _new_connector) apply everywhere uniformly instead of needing to be
    remembered at each call site."""
    return aiohttp.ClientSession(
        timeout=timeout,
        connector=_new_connector(),
        max_line_size=_HEADER_SIZE_LIMIT,
        max_field_size=_HEADER_SIZE_LIMIT,
    )


async def fetch(session: aiohttp.ClientSession, url: str, timeout: int = REQUEST_TIMEOUT,
                 proxy: Optional[str] = None) -> Optional[_FetchResponse]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    attempts = 3  # 1 initial try + 2 retries, matching the old urllib3 Retry(total=2, ...)
    for attempt in range(attempts):
        try:
            async with session.get(url, headers=headers, timeout=client_timeout,
                                    allow_redirects=True, proxy=proxy) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    try:
                        text = content.decode(resp.charset or "utf-8", errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        text = content.decode("utf-8", errors="replace")
                    return _FetchResponse(text=text, content=content)
                if resp.status in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt < attempts - 1:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            return None
    return None


async def probe_domain_reachable(base_url: str, proxy: Optional[str] = None) -> bool:
    """Quick, short-timeout connectivity check, used to skip a dead/blocked
    domain fast instead of paying for a full robots.txt fetch plus up to 7
    guessed sitemap paths — each through the main fetch()'s retry-with-
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
    timeout = aiohttp.ClientTimeout(total=FAST_FAIL_PROBE_TIMEOUT)
    for _ in range(2):
        try:
            async with new_client_session(timeout) as session:
                async with session.get(base_url, headers={"User-Agent": random.choice(USER_AGENTS)},
                                        allow_redirects=True, proxy=proxy):
                    return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    return False


# --------------------------------------------------------------------------
# Proxy health checks (used by the Streamlit dashboard's live proxy panel)
# --------------------------------------------------------------------------

# A tiny, fast, plain-text endpoint whose only job is to echo back the
# caller's IP — the standard way to confirm a proxy is both alive AND
# actually being used (a bad proxy config can silently fall through to a
# direct connection, which would otherwise still "work").
PROXY_HEALTH_CHECK_URL = "https://api.ipify.org"
PROXY_HEALTH_CHECK_TIMEOUT = 8


async def check_proxy_health(proxy: str) -> dict:
    """Verifies a single proxy is alive and measures its latency by fetching
    PROXY_HEALTH_CHECK_URL through it. Returns a plain dict (not a dataclass)
    since this is display data for the dashboard, not something threaded
    through the scraping pipeline."""
    timeout = aiohttp.ClientTimeout(total=PROXY_HEALTH_CHECK_TIMEOUT)
    t0 = time.monotonic()
    try:
        async with new_client_session(timeout) as session:
            async with session.get(PROXY_HEALTH_CHECK_URL, proxy=proxy) as resp:
                if resp.status == 200:
                    egress_ip = (await resp.text()).strip()
                    return {
                        "proxy": proxy,
                        "alive": True,
                        "latency_ms": round((time.monotonic() - t0) * 1000),
                        "egress_ip": egress_ip,
                        "error": "",
                    }
                return {
                    "proxy": proxy, "alive": False, "latency_ms": None,
                    "egress_ip": "", "error": f"HTTP {resp.status}",
                }
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return {
            "proxy": proxy, "alive": False, "latency_ms": None,
            "egress_ip": "", "error": str(exc) or type(exc).__name__,
        }


async def check_all_proxies_health(proxies: list) -> list:
    """Checks every proxy concurrently; order matches the input list."""
    return await asyncio.gather(*(check_proxy_health(p) for p in proxies))


async def filter_alive_proxies(proxies: list) -> tuple:
    """Runs a health check over `proxies` and returns (usable_proxies,
    dead_count), so a batch skips proxies already known to be down instead
    of wasting a request routing a new domain through one.

    If every proxy fails the check, returns an empty list (dead_count ==
    len(proxies)) so the caller falls back to a direct connection, rather
    than continuing to route every domain through proxies already known to
    be dead. An earlier version kept using the dead list on the theory that
    silently switching to a direct connection was a bigger behavior change
    than letting requests fail per-domain — that reasoning broke down in
    practice: a real 2118-domain batch ran entirely through 10 Webshare
    proxies that had exhausted their bandwidth quota (health-check-dead),
    and "keep trying anyway" turned a proxy outage into ~1300 false
    "domain unreachable" results, when most of those sites would have
    scraped fine directly. Callers still get a loud, explicit warning when
    this happens — it just no longer means "run the whole batch through
    connections guaranteed to fail." """
    results = await check_all_proxies_health(proxies)
    alive = [r["proxy"] for r in results if r["alive"]]
    dead_count = len(proxies) - len(alive)
    return alive, dead_count


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


async def fetch_robots_policy(session: aiohttp.ClientSession, base_url: str,
                               proxy: Optional[str] = None, respect_robots: bool = True) -> RobotsPolicy:
    """Fetch and parse robots.txt once. Extracts both the Disallow rules
    (for can_fetch checks) and the declared Sitemap: URLs. A missing or
    unreadable robots.txt fails open (allow all) — the conventional
    behaviour for a well-behaved but functional crawler."""
    resp = await fetch(session, urljoin(base_url, "/robots.txt"), proxy=proxy)
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


async def _fetch_one_sitemap(session: aiohttp.ClientSession, sitemap_url: str,
                              proxy: Optional[str] = None) -> tuple:
    """Fetches and parses a single sitemap file. Returns (child_sitemap_urls,
    page_urls) — no recursion, no shared state; the caller (discover_all_page_urls)
    owns the BFS traversal and bookkeeping."""
    resp = await fetch(session, sitemap_url, proxy=proxy)
    if not resp:
        return [], []

    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass

    return await _run_cpu(parse_sitemap_xml, raw)


async def discover_all_page_urls(session: aiohttp.ClientSession, base_url: str,
                                  robots: Optional["RobotsPolicy"] = None,
                                  proxy: Optional[str] = None) -> list:
    """Breadth-first, concurrent sitemap discovery. A sitemap index with many
    locale/category child files used to be fetched one at a time, recursively
    — taking as long as the SUM of every file fetch. This processes one
    "wave" of URLs at a time via an asyncio.Semaphore-bounded gather, so a
    wide sitemap tree takes roughly as long as its slowest single fetch
    instead."""
    # Sitemaps declared in robots.txt are already known; add common guesses.
    candidates = list(robots.sitemaps) if robots else []
    candidates += [urljoin(base_url, p) for p in COMMON_SITEMAP_PATHS]

    all_pages: list = []
    seen: set = set()
    fetch_count = 0
    page_count = 0
    frontier = [u for u in dict.fromkeys(candidates) if u not in seen]

    sem = asyncio.Semaphore(SITEMAP_FETCH_WORKERS)

    async def _bounded_fetch(u: str) -> tuple:
        async with sem:
            return await _fetch_one_sitemap(session, u, proxy)

    depth = 0
    while frontier and fetch_count < MAX_SITEMAP_FETCHES_PER_DOMAIN and page_count < MAX_TOTAL_SITEMAP_PAGES:
        wave = frontier[:MAX_SITEMAP_FETCHES_PER_DOMAIN - fetch_count]
        for u in wave:
            seen.add(u)
        fetch_count += len(wave)

        results = await asyncio.gather(*(_bounded_fetch(u) for u in wave))
        next_frontier: list = []
        for children, pages in results:
            # The cap above only stopped a new *wave* from starting once
            # exceeded — a single file within an already-started wave (e.g.
            # figma.com's real 367,890-URL community index) still added its
            # entire contents before the check ran again. Truncating each
            # file's own contribution to the remaining budget, right as it's
            # merged, means one huge file can no longer blow past the cap by
            # itself — the fetch/parse of that one file still costs what it
            # costs, but its result can't inflate all_pages/downstream work
            # (dedup, rank_contact_urls) past MAX_TOTAL_SITEMAP_PAGES.
            if pages:
                remaining_budget = MAX_TOTAL_SITEMAP_PAGES - page_count
                if remaining_budget <= 0:
                    pages = []
                elif len(pages) > remaining_budget:
                    pages = pages[:remaining_budget]
                if pages:
                    all_pages.extend(pages)
                    page_count += len(pages)
            if depth < MAX_SITEMAP_DEPTH and page_count < MAX_TOTAL_SITEMAP_PAGES:
                next_frontier.extend(c for c in children if c not in seen)

        frontier = list(dict.fromkeys(next_frontier))
        depth += 1

    return await _run_cpu(_dedupe_and_filter_pages, all_pages, robots)


def _dedupe_and_filter_pages(all_pages: list, robots: Optional["RobotsPolicy"]) -> list:
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
        # word that also shows up in ordinary tutorial/doc slugs. The hard
        # match must come from a WHOLE path segment (e.g. /contact/,
        # /contact-us/), not merely a hyphen-split sub-token buried inside a
        # longer compound segment — otherwise product-category pages like
        # /marketplace/.../vonage-contact-center/ falsely bypass the depth
        # limit purely because "contact-center" splits into a "contact"
        # sub-token, crowding out the real /contact/ page from the top-N
        # candidates that actually get fetched. Found via a real batch:
        # zendesk.com's genuine /contact/ and /company/contact-info/ pages
        # existed in its sitemap but never made the candidate list because
        # a dozen unrelated "contact centre" marketplace listings outranked
        # them under the old (sub-token-eligible) rule.
        hard_segment_matches = HARD_CONTACT_KEYWORDS & set(segments)
        if len(segments) > MAX_SOFT_MATCH_DEPTH and not hard_segment_matches:
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

def _domain_has_mx_sync(domain: str) -> bool:
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


async def domain_has_mx(domain: str) -> bool:
    """dnspython has no native asyncio resolver, so the blocking lookup runs
    in the default executor thread pool instead of on the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _domain_has_mx_sync, domain)


# --------------------------------------------------------------------------
# Optional: Playwright fallback for JS-rendered footers (soft dependency)
# --------------------------------------------------------------------------
# Some sites inject their footer/contact info entirely client-side, so a
# plain HTTP GET sees an empty shell. This fallback only runs when the
# static pass already found zero emails for a domain — Playwright is much
# heavier than a plain HTTP request (spins up a real Chromium instance), so
# it's not worth paying that cost on every domain.

PLAYWRIGHT_PAGE_TIMEOUT_MS = 20000
# Each PlaywrightFetcher launches a real Chromium process (typically
# 200-500MB+). Domain processing runs across many concurrent asyncio tasks,
# so without a cap, several domains needing the fallback at the same time
# launch that many Chromium instances simultaneously — this is exactly what
# caused an out-of-memory crash on a resource-constrained host (Railway)
# even though local runs never hit it. This semaphore bounds concurrent
# Chromium instances independently of the batch's overall --workers setting.
MAX_CONCURRENT_PLAYWRIGHT_INSTANCES = 1
_playwright_semaphore = asyncio.Semaphore(MAX_CONCURRENT_PLAYWRIGHT_INSTANCES)


class PlaywrightFetcher:
    """Launches one headless Chromium instance and reuses it across every
    page fetched for a single domain, closing it when done."""

    async def __aenter__(self):
        from playwright.async_api import async_playwright  # raises ImportError if not installed
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:
            # Hosts like Streamlit Cloud install apt deps via packages.txt but
            # have no build hook to download the actual browser binary, so the
            # first Playwright-enabled run on a fresh container needs to fetch
            # it here — a one-time ~300MB download, slow but self-healing.
            if "Executable doesn't exist" not in str(exc):
                await self._pw.stop()
                raise
            import subprocess
            import sys
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            self._browser = await self._pw.chromium.launch(headless=True)
        return self

    async def fetch(self, url: str) -> Optional[str]:
        try:
            page = await self._browser.new_page(user_agent=random.choice(USER_AGENTS))
            try:
                # "networkidle" is unreliable on real sites — analytics beacons,
                # chat widgets, and websockets keep the network busy forever on
                # many pages, turning this into a guaranteed timeout. Wait for
                # the DOM instead, then give client-side rendering a moment.
                await page.goto(url, timeout=PLAYWRIGHT_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                return await page.content()
            finally:
                await page.close()
        except Exception:
            return None

    async def __aexit__(self, *exc_info):
        try:
            await self._browser.close()
        finally:
            await self._pw.stop()


async def playwright_fallback(result: "DomainResult", base_url: str, pages_to_check: list, delay: float) -> None:
    """Re-fetches pages with a real headless browser when the static pass
    found nothing, to catch footers/contact info rendered by client-side JS.
    Only MAX_CONCURRENT_PLAYWRIGHT_INSTANCES domains can be running a
    Chromium instance at any moment — other domains needing the fallback
    wait here until a slot frees up, trading some wall-clock time for a
    bounded memory ceiling regardless of --workers."""
    async with _playwright_semaphore:
        try:
            async with PlaywrightFetcher() as pf:
                urls_to_try = pages_to_check or [base_url]
                extra_links: list = []

                for page_url in urls_to_try[:MAX_PAGES_PER_DOMAIN]:
                    html = await pf.fetch(page_url)
                    await asyncio.sleep(delay)
                    if not html:
                        continue
                    if not pages_to_check:
                        # Homepage was previously unreachable/empty; now that we
                        # have JS-rendered HTML, look for contact links in it too.
                        extra_links = await _run_cpu(find_contact_links_on_page, html, base_url)
                    found = await _run_cpu(extract_emails_from_html, html)
                    if found:
                        result.emails |= found
                        result.source_pages.add(page_url)

                for link_url in extra_links[:MAX_PAGES_PER_DOMAIN - 1]:
                    html = await pf.fetch(link_url)
                    await asyncio.sleep(delay)
                    if not html:
                        continue
                    found = await _run_cpu(extract_emails_from_html, html)
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

async def fetch_and_extract_pages(session: aiohttp.ClientSession, urls: list, robots: "RobotsPolicy",
                                   delay: float, proxy: Optional[str] = None) -> tuple:
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

    sem = asyncio.Semaphore(min(PER_DOMAIN_FETCH_WORKERS, len(allowed_urls)))

    async def _fetch_one(url: str) -> tuple:
        async with sem:
            resp = await fetch(session, url, proxy=proxy)
        if not resp:
            return url, None
        return url, await _run_cpu(extract_emails_from_html, resp.text)

    tasks = []
    for url in allowed_urls:
        tasks.append(asyncio.create_task(_fetch_one(url)))
        if delay:
            await asyncio.sleep(delay)

    # asyncio.gather (not as_completed) is required here: if this function's
    # caller is itself cancelled (the per-domain hard timeout), gather
    # propagates that cancellation to every task above before re-raising —
    # as_completed does NOT, which left orphaned tasks running against an
    # already-closed aiohttp session in testing (RuntimeError: Session is
    # closed) once the parent's `async with ClientSession()` block exited.
    results = await asyncio.gather(*tasks)
    for url, found in results:
        if found:
            emails |= found
            source_pages.add(url)
    return emails, source_pages


async def process_domain(raw_url: str, delay: float, proxies: Optional[list], verify_mx: bool,
                          use_playwright: bool = False, respect_robots: bool = True) -> DomainResult:
    result = DomainResult(input_url=raw_url)
    base_url = normalize_to_base_url(raw_url)
    result.domain = get_bare_domain(base_url)
    if not result.domain:
        result.error = "could not parse domain"
        return result

    # A proxy is picked once per domain (not per request), matching the
    # original per-session proxy assignment.
    proxy = random.choice(proxies) if proxies else None

    if not await probe_domain_reachable(base_url, proxy):
        result.method = "none"
        result.error = "domain unreachable (fast-fail connectivity probe)"
        if use_playwright:
            await playwright_fallback(result, base_url, [], delay)
        return result

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with new_client_session(timeout) as session:
        try:
            robots = await fetch_robots_policy(session, base_url, proxy, respect_robots=respect_robots)

            all_pages = await discover_all_page_urls(session, base_url, robots, proxy)
            # Offloaded like the sitemap/HTML parsing above — a huge sitemap
            # (figma.com's real community-file index has 368k+ URLs) makes
            # this pure-Python scan itself a multi-second event-loop-blocking
            # CPU spike (measured: ~24s at that scale) if run inline.
            contact_urls = await _run_cpu(rank_contact_urls, all_pages, base_url)

            home_resp = None
            pages_to_check = []
            if contact_urls:
                result.method = "sitemap"
                pages_to_check = contact_urls
            else:
                result.method = "homepage-fallback"
                if robots.can_fetch(base_url):
                    home_resp = await fetch(session, base_url, proxy=proxy)
                if home_resp:
                    pages_to_check = [base_url]
                    extra_links = await _run_cpu(find_contact_links_on_page, home_resp.text, base_url)
                    pages_to_check += [u for u in extra_links if robots.can_fetch(u)][:MAX_PAGES_PER_DOMAIN - 1]

            if not pages_to_check:
                result.method = "none"
                result.error = "no sitemap, no contact links, homepage unreachable"
                if use_playwright:
                    await playwright_fallback(result, base_url, pages_to_check, delay)
                return result

            found, source_pages = await fetch_and_extract_pages(
                session, pages_to_check[:MAX_PAGES_PER_DOMAIN], robots, delay, proxy
            )
            result.emails |= found
            result.source_pages |= source_pages

            # Shallow second hop: sitemap + footer-keyword discovery both failed
            # to surface anything useful, so try a few more same-domain nav links
            # from the homepage before giving up or falling back to Playwright.
            if not result.emails and result.method == "homepage-fallback" and home_resp:
                second_hop_urls = await _run_cpu(
                    find_second_hop_links, home_resp.text, base_url, set(pages_to_check)
                )
                found, source_pages = await fetch_and_extract_pages(session, second_hop_urls, robots, delay, proxy)
                result.emails |= found
                result.source_pages |= source_pages

            if not result.emails and use_playwright:
                await playwright_fallback(result, base_url, pages_to_check, delay)

            if verify_mx and result.emails:
                emails_list = list(result.emails)
                mx_checks = await asyncio.gather(*(domain_has_mx(e.partition("@")[2]) for e in emails_list))
                result.emails = {e for e, ok in zip(emails_list, mx_checks) if ok}

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


def _load_completed_input_urls(output_path: str) -> set:
    """Reads whatever input_urls already have a row in an existing output
    CSV, so --resume can skip them rather than re-scraping from scratch.
    Any row present (including a "no emails found" or "gave up after ...s"
    error result) counts as done — those are ordinary terminal outcomes,
    not signs of an interrupted run, exactly like a normal (non-resumed)
    batch never retries them either."""
    if not os.path.exists(output_path):
        return set()
    with open(output_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["input_url"] for row in reader if row.get("input_url")}


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


async def _process_domain_with_hard_timeout(sem: asyncio.Semaphore, url: str, delay: float,
                                             proxies: Optional[list], verify_mx: bool,
                                             use_playwright: bool, respect_robots: bool) -> DomainResult:
    """Bounds overall concurrency to `sem`'s size and gives up on any single
    domain that's still running after DOMAIN_HARD_TIMEOUT_SECONDS — rather
    than letting one stuck domain (e.g. a DNS/TLS-level stall) block every
    other domain in the batch forever. asyncio.wait_for actually cancels the
    coroutine at its next await point, which also frees this domain's
    semaphore slot for the next one — unlike an abandoned OS thread, which
    keeps its worker slot occupied for as long as it keeps running."""
    async with sem:
        try:
            return await asyncio.wait_for(
                process_domain(url, delay, proxies, verify_mx, use_playwright, respect_robots),
                timeout=DOMAIN_HARD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            r = DomainResult(input_url=url)
            r.domain = get_bare_domain(normalize_to_base_url(url))
            r.method = "none"
            r.error = (
                f"gave up after {DOMAIN_HARD_TIMEOUT_SECONDS:.0f}s - domain appears stuck "
                "beyond normal timeouts (e.g. a DNS/TLS-level stall)"
            )
            return r


def _size_default_executor(max_workers: int) -> None:
    """The event loop's default executor backs both our own CPU-bound
    offloading (_run_cpu) and aiohttp's ThreadedResolver DNS lookups. Its
    default size (min(32, os.cpu_count()+4)) is easily oversubscribed once
    several domains run concurrently, each needing several DNS lookups plus
    occasional sitemap/HTML parsing — found via a real regression where
    perfectly reachable domains (figma.com, hubspot.com, slack.com) failed
    the fast-fail probe only when run as part of a concurrent batch, not
    when run one at a time. Sized relative to the batch's own concurrency
    so it can't become the bottleneck."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=max(32, max_workers * 8))
    )


async def process_domains_streaming(domains: list, max_workers: int, delay: float,
                                     proxies: Optional[list], verify_mx: bool,
                                     use_playwright: bool, respect_robots: bool
                                     ) -> AsyncIterator[tuple]:
    """Async generator yielding (done_count, total, DomainResult) as each
    domain finishes, bounded to `max_workers` concurrent domains with a hard
    per-domain timeout. Shared by the CLI batch runner and the Streamlit
    dashboard so both stay in sync."""
    _size_default_executor(max_workers)
    sem = asyncio.Semaphore(max_workers)
    total = len(domains)
    tasks = [
        asyncio.create_task(
            _process_domain_with_hard_timeout(sem, url, delay, proxies, verify_mx, use_playwright, respect_robots)
        )
        for url in domains
    ]
    done_count = 0
    for coro in asyncio.as_completed(tasks):
        r = await coro
        done_count += 1
        yield done_count, total, r


def _check_playwright_installed() -> None:
    # Fail fast with a clear message rather than letting every domain's
    # fallback silently no-op with an ImportError buried in its error column.
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "use_playwright=True but the 'playwright' package (and its browser "
            "binaries) is not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )


async def run_batch(input_path: str, output_path: str, max_workers: int = 10, delay: float = 0.3,
                     proxies_path: Optional[str] = None, verify_mx: bool = False,
                     use_playwright: bool = False, respect_robots: bool = True,
                     resume: bool = False) -> None:
    domains = load_domains(input_path)
    proxies = load_proxies(proxies_path)

    if proxies:
        original_count = len(proxies)
        proxies, dead_count = await filter_alive_proxies(proxies)
        if dead_count == original_count:
            print(f"Proxy health check: all {original_count} proxie(s) failed — "
                  "running this batch WITHOUT a proxy (direct connection) instead of "
                  "through connections already known to be dead.", flush=True)
        elif dead_count:
            print(f"Proxy health check: {dead_count}/{original_count} proxie(s) failed "
                  "and will be skipped for this run.", flush=True)

    if use_playwright:
        _check_playwright_installed()

    file_mode = "w"
    write_header = True
    if resume:
        already_done = _load_completed_input_urls(output_path)
        if already_done:
            remaining = [d for d in domains if d not in already_done]
            print(f"Resuming: {len(already_done)} domain(s) already in {output_path}, "
                  f"{len(remaining)} remaining.", flush=True)
            domains = remaining
            file_mode = "a"
            write_header = False

    with open(output_path, file_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
            f.flush()

        async for done_count, total, r in process_domains_streaming(
            domains, max_workers, delay, proxies, verify_mx, use_playwright, respect_robots
        ):
            writer.writerow(result_to_row(r))
            f.flush()
            status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
            print(f"[{done_count}/{total}] {r.domain or r.input_url}: {status}", flush=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sitemap-aware contact email scraper")
    parser.add_argument("--input", "-i", required=True, help="Path to file with one URL/domain per line")
    parser.add_argument("--output", "-o", default="results.csv", help="Path to output CSV")
    parser.add_argument("--workers", "-w", type=int, default=10,
                         help="Concurrent domains to process. Bounded by asyncio tasks, not OS "
                              "threads, so this can reasonably go well past 10-20 if your host "
                              "has the bandwidth/connections to match.")
    parser.add_argument("--delay", "-d", type=float, default=0.3, help="Seconds to stagger page-fetch starts within a domain")
    parser.add_argument("--proxies", "-p", default=None, help="Optional path to a file of proxy URLs, one per line")
    parser.add_argument("--verify-mx", action="store_true", help="Drop emails whose domain has no MX record (requires dnspython)")
    parser.add_argument("--use-playwright", action="store_true",
                         help="Retry with a headless browser (requires: pip install playwright && "
                              "playwright install chromium) when a domain's static fetch finds zero emails")
    parser.add_argument("--ignore-robots", action="store_true",
                         help="Do NOT honor robots.txt Disallow rules (default: honored). "
                              "Only use on sites you own or have permission to crawl.")
    parser.add_argument("--resume", action="store_true",
                         help="If --output already has rows from a previous (e.g. interrupted) "
                              "run, skip domains already recorded in it and append new results "
                              "instead of overwriting the file.")
    args = parser.parse_args()

    asyncio.run(run_batch(
        input_path=args.input,
        output_path=args.output,
        max_workers=args.workers,
        delay=args.delay,
        proxies_path=args.proxies,
        verify_mx=args.verify_mx,
        use_playwright=args.use_playwright,
        respect_robots=not args.ignore_robots,
        resume=args.resume,
    ))


if __name__ == "__main__":
    main()
