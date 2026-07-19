# Email Scraper

Finds public contact emails for a list of websites by:

1. Reading `robots.txt` for declared sitemaps **and honoring its `Disallow`
   rules** — pages a site asks crawlers not to fetch are skipped (can be
   turned off with `--ignore-robots` for sites you own). Also guesses common
   sitemap paths (`sitemap.xml`, `sitemap_index.xml`, `wp-sitemap.xml`, ...).
2. Recursively walking sitemap indexes to collect page URLs.
3. Ranking URLs for contact-page likelihood (`contact`, `about`, `support`,
   `get-in-touch`, plus Spanish/French/German/Italian/Portuguese/Dutch
   equivalents) and fetching the top candidates.
4. Falling back to the homepage + footer link discovery, then a shallow
   second-hop crawl of same-domain nav links, if no sitemap/contact page is found.
5. Extracting emails via `mailto:` links, plain-text regex, Cloudflare's
   `email-protection` obfuscation, and human-obfuscated text (`name [at] domain [dot] com`).
6. Filtering out placeholder/tracking/false-positive addresses, and splitting
   results into the company's own-domain emails vs. third-party emails.
7. Optionally retrying with a headless browser (Playwright) when a domain's static
   fetch finds zero emails, to catch footers/contact info rendered by client-side JS.
8. Writing results to CSV incrementally, so a crash or Colab disconnect doesn't lose progress.

## Local / repo usage

```bash
pip install -r requirements.txt
cp domains.example.txt domains.txt   # add your real list, one URL/domain per line
python email_scraper.py --input domains.txt --output results.csv --workers 10 --delay 0.3
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--input, -i` | required | file with one URL/domain per line |
| `--output, -o` | `results.csv` | output CSV path |
| `--workers, -w` | `10` | domains processed concurrently. Bounded by asyncio tasks, not OS threads, so this can reasonably go well past 10-20 if your host has the bandwidth/connections to match |
| `--delay, -d` | `0.3` | seconds to stagger page-fetch starts *within* a domain (politeness) |
| `--proxies, -p` | none | optional file of proxy URLs, one per line |
| `--verify-mx` | off | drop emails whose domain has no MX record (needs `dnspython`) |
| `--use-playwright` | off | retry with headless Chromium when a domain's static fetch finds zero emails (needs `playwright`, see below) |
| `--ignore-robots` | off (robots honored) | do **not** honor `robots.txt` Disallow rules — only for sites you own or have permission to crawl |
| `--resume` | off | if `--output` already has rows from a previous (e.g. interrupted) run, skip domains already recorded in it and append new results instead of overwriting the file |
| `--retry-failed` | off | after the main batch, re-attempt (once, at lower concurrency, after a cooldown) domains that failed to connect at all — catches transient network blips and temporary rate-limiting. Does not retry domains that connected fine but had no email |

Output CSV columns: `input_url, domain, primary_email, primary_role, own_domain_emails, other_domain_emails, method, source_pages, error`.

### Role-based ranking

Each email is classified by the function of its local part —
`general` (info@, hello@, contact@), `sales`, `support`, `personal`
(a named individual like jane.doe@ or alex@), `press`, `careers`,
`billing`, `legal`, or `other` — and the email lists are sorted
best-outreach-contact-first using that classification. The
`primary_email`/`primary_role` columns give the single best contact to
use per domain (preferring the company's own domain over any third-party
address), so you don't have to eyeball a flat list.

## Running the tests

The suite is offline and dependency-free (stdlib `unittest`) — every case
corresponds to a real bug/false-positive fixed during development, so it
guards against regressions:

```bash
python -m unittest test_email_scraper -v
# or, if you have pytest:
pytest test_email_scraper.py -v
```

### Enabling the Playwright (JS-rendered footer) fallback

Some sites inject their footer/contact info entirely client-side, so the
default `requests`-based fetch sees an empty shell. `--use-playwright` retries
those domains with a real headless Chromium instance. It only kicks in when a
domain's static pass already found zero emails, since spinning up a browser
is much slower than a plain HTTP request — expect roughly 5-15s extra per
affected domain, not per whole batch.

`playwright` is already in `requirements.txt`, so `pip install -r requirements.txt`
covers the Python package. The browser binary is a separate download:

```bash
playwright install chromium
python email_scraper.py --input domains.txt --output results.csv --use-playwright
```

### Playwright on Streamlit Community Cloud

`packages.txt` in this repo lists the apt-level system libraries Chromium
needs (fonts, `libnss3`, `libatk-bridge2.0-0`, etc.) — Streamlit Cloud installs
these automatically during build. It does **not**, however, run
`playwright install chromium` for you, since it only supports `packages.txt`
(apt) and `requirements.txt` (pip), not arbitrary build commands. To cover
this, `PlaywrightFetcher` self-heals: if Chromium's binary is missing on first
use, it downloads it automatically (~300MB) before continuing. This means the
**first** Playwright-enabled scrape after a cold start or redeploy is slow
(1-2 extra minutes), since Streamlit Cloud's filesystem is ephemeral and the
download isn't cached across container restarts/sleeps.

## Google Colab usage

Paste this into a Colab cell:

```python
!pip install -q requests beautifulsoup4 lxml dnspython
!wget -q https://raw.githubusercontent.com/<your-username>/<your-repo>/main/email_scraper.py

import email_scraper as es

domains = [
    "example.com",
    "https://www.wikipedia.org",
]
with open("domains.txt", "w") as f:
    f.write("\n".join(domains))

es.run_batch(
    input_path="domains.txt",
    output_path="results.csv",
    max_workers=5,
    delay=1.0,
    proxies_path=None,   # or path to an uploaded proxies.txt
    verify_mx=False,
    use_playwright=False,  # set True after installing playwright + chromium (see below)
)
```

To enable the JS-rendered-footer fallback in Colab, add this before `run_batch`:

```python
!pip install -q playwright
!playwright install --with-deps chromium
```

then pass `use_playwright=True` to `run_batch(...)`.

Then download `results.csv` from the Colab file browser, or:

```python
from google.colab import files
files.download("results.csv")
```

If you don't want to publish to GitHub first, you can instead just paste the
full contents of `email_scraper.py` into a Colab cell (or upload the file via
the Colab file-browser "Upload" button) and `import email_scraper as es` will
work the same way.

## Deploying an always-on version (Railway)

Streamlit Community Cloud (above) is free but sleeps the app after
inactivity and has no persistent job state. For an always-on dashboard,
this repo includes a `Dockerfile` for Railway (or any Docker host):

1. Push this repo to GitHub (already done if you're reading this from there).
2. On [railway.app](https://railway.app), connect the GitHub repo — Railway
   auto-detects the `Dockerfile` and uses it to build.
3. Railway assigns a `$PORT` env var at runtime; the `Dockerfile`'s `CMD`
   already binds Streamlit to it (`--server.port $PORT --server.address 0.0.0.0`).
4. Railway gives you a public URL once the build finishes.

The `Dockerfile`'s apt package list is the same one already debugged for
Streamlit Cloud (see the packages.txt section above for that story), but
Railway's build environment is a plain `python:3.11-slim` (Debian bookworm)
rather than Streamlit Cloud's mixed-repo image, so **the exact same package
names aren't guaranteed to work first try** — if the build fails on an apt
package, share the build log and it can be adjusted the same way we fixed
`packages.txt`, by name.

This does **not** speed up scraping itself — that's bound by target-site
response times and worker/delay settings, not hosting location. What it
does give you: no cold-start sleep, and a stable place to run long batches
without tying up your own machine.

## Notes, limits, and things to know before relying on this

- **Free datacenter proxies are not recommended.** Most public free-proxy
  lists are already blacklisted by the same anti-bot systems (Cloudflare,
  etc.) you're trying to get past, and they're short-lived. The script
  supports `--proxies` if you have a paid rotating endpoint, but by default
  it just uses polite rate-limiting, retries with backoff, and rotating
  User-Agents — which will get you further than free datacenter IPs.
- **Proxy health.** The dashboard (`app.py`) has a live "🩺 Proxy Health"
  panel below the settings — it re-checks every proxy in the sidebar's
  Proxies box on a timer (default every 30s, configurable) by fetching a
  lightweight IP-echo endpoint through each one, and shows alive/dead,
  latency, and the egress IP each proxy actually reports. Dead proxies are
  also automatically skipped by the actual scraper run itself (a fresh
  health check runs right before a batch starts), not just flagged in the
  panel.
- **Proxy persistence.** The Proxies box is normally per-browser-session —
  a page reload loses whatever was pasted in. Set a `SCRAPER_PROXIES`
  environment variable (comma- or newline-separated proxy URLs) on your
  host (e.g. Railway's Variables tab) to pre-fill it automatically on every
  new session, without ever putting real credentials in this repo. It's
  still editable/overridable in the box itself for a one-off session.
- **Streaming results + resume (dashboard).** Each domain's result is
  written to a per-job CSV under `runs/` (path derived from a hash of the
  domain list) and flushed as soon as it completes — so an interrupted run
  (browser disconnect, laptop sleep, Streamlit rerun, container hiccup)
  loses nothing. Re-running the same list picks up where it left off,
  skipping domains already saved and appending the rest; tick **Start
  fresh** to discard that progress and rescrape. Only a small rolling
  window of recent rows is held in memory during a run, so memory and
  render time stay flat even across thousands of domains — this is what
  makes a single large (e.g. 2000+ domain) job safe. `runs/` is gitignored;
  note it's ephemeral across a full Railway redeploy, so download the CSV
  once a big job finishes. Both the CLI (`--retry-failed`) and the
  dashboard ("Retry failed domains after this run") support an optional
  end-of-batch retry pass: after the main run, domains that failed to
  connect at all (not ones that connected fine but had no email) are
  re-attempted once, at lower concurrency, after a 15s cooldown — catches
  transient network blips and temporary rate-limiting without retrying
  domains where a retry can't change the outcome.
- **Respectful of 429/rate-limiting.** A 429 response is retried using the
  server's actual `Retry-After` value (capped at 15s) rather than a fixed
  backoff — the standard-compliant behavior for a well-behaved crawler,
  distinct from techniques meant to evade rate limiting.
- **Large response headers are handled.** aiohttp's default header-size
  limit (8190 bytes) is too small for some real e-commerce sites with
  large Content-Security-Policy headers (many third-party script domains
  listed) — this previously caused a fully reachable site to be reported
  as unreachable. Raised to 64KB.
- **JavaScript-rendered footers** are handled by the optional `--use-playwright`
  / `use_playwright=True` fallback (see above). It's opt-in and only triggers
  per-domain when the static pass finds nothing, since it's much slower.
- **Respect `robots.txt` disallow rules and rate limits** if you plan to run
  this at real scale — this script does not currently enforce `Disallow`
  rules itself, only reads the `Sitemap:` directive.
- **Legal/ethical note:** scraping public contact info is generally fine;
  what you do with it afterward (bulk email, etc.) may fall under
  CAN-SPAM/GDPR depending on jurisdiction and audience — keep that decision
  separate from the scraping step.
