# Email Scraper

Finds public contact emails for a list of websites by:

1. Reading `robots.txt` for declared sitemaps, plus guessing common sitemap paths
   (`sitemap.xml`, `sitemap_index.xml`, `sitemap-index.xml`, `wp-sitemap.xml`, `sitemap.xml.gz`).
2. Recursively walking sitemap indexes to collect page URLs.
3. Ranking URLs for contact-page likelihood (`contact`, `about`, `support`, `get-in-touch`, ...)
   and fetching the top candidates.
4. Falling back to the homepage + footer link discovery if no sitemap/contact page is found.
5. Extracting emails via `mailto:` links, plain-text regex, Cloudflare's
   `email-protection` obfuscation, and human-obfuscated text (`name [at] domain [dot] com`).
6. Filtering out placeholder/tracking/false-positive addresses.
7. Optionally retrying with a headless browser (Playwright) when a domain's static
   fetch finds zero emails, to catch footers/contact info rendered by client-side JS.
8. Writing results to CSV incrementally, so a crash or Colab disconnect doesn't lose progress.

## Local / repo usage

```bash
pip install -r requirements.txt
cp domains.example.txt domains.txt   # add your real list, one URL/domain per line
python email_scraper.py --input domains.txt --output results.csv --workers 5 --delay 1.0
```

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--input, -i` | required | file with one URL/domain per line |
| `--output, -o` | `results.csv` | output CSV path |
| `--workers, -w` | `5` | domains processed concurrently |
| `--delay, -d` | `1.0` | seconds between page fetches *within* a domain (politeness) |
| `--proxies, -p` | none | optional file of proxy URLs, one per line |
| `--verify-mx` | off | drop emails whose domain has no MX record (needs `dnspython`) |
| `--use-playwright` | off | retry with headless Chromium when a domain's static fetch finds zero emails (needs `playwright`, see below) |

Output CSV columns: `input_url, domain, own_domain_emails, other_domain_emails, method, source_pages, error`.

### Enabling the Playwright (JS-rendered footer) fallback

Some sites inject their footer/contact info entirely client-side, so the
default `requests`-based fetch sees an empty shell. `--use-playwright` retries
those domains with a real headless Chromium instance. It only kicks in when a
domain's static pass already found zero emails, since spinning up a browser
is much slower than a plain HTTP request — expect roughly 5-15s extra per
affected domain, not per whole batch.

```bash
pip install playwright
playwright install chromium
python email_scraper.py --input domains.txt --output results.csv --use-playwright
```

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

## Notes, limits, and things to know before relying on this

- **Free datacenter proxies are not recommended.** Most public free-proxy
  lists are already blacklisted by the same anti-bot systems (Cloudflare,
  etc.) you're trying to get past, and they're short-lived. The script
  supports `--proxies` if you have a paid rotating endpoint, but by default
  it just uses polite rate-limiting, retries with backoff, and rotating
  User-Agents — which will get you further than free datacenter IPs.
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
