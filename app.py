"""Streamlit dashboard for email_scraper.py.

Wraps the existing scraper logic (sitemap discovery, contact-page ranking,
email extraction, optional Playwright fallback) in a one-page web UI: paste
domains, hit Run, watch results populate live, download the CSV.

Run locally:   streamlit run app.py
Deploy free:   push this folder to a GitHub repo, connect it at
               https://streamlit.io/cloud
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

import email_scraper as es

st.set_page_config(page_title="Email Scraper Dashboard", page_icon="📧", layout="wide")
st.title("📧 Email Scraper Dashboard")
st.caption(
    "Finds contact emails via sitemap discovery, contact-page ranking, and footer parsing. "
    "Enter one domain or URL per line."
)

if "results" not in st.session_state:
    st.session_state.results = []

with st.sidebar:
    st.header("Settings")
    workers = st.slider("Concurrent domains", 1, 20, 8)
    delay = st.slider("Delay between page fetches (s)", 0.0, 3.0, 0.5, 0.1)
    verify_mx = st.checkbox(
        "Verify MX records", value=False,
        help="Drops emails whose domain has no mail server. Requires dnspython.",
    )
    use_playwright = st.checkbox(
        "Use Playwright fallback (JS-rendered footers)", value=False,
        help="Retries with a headless browser when a domain finds zero emails. "
             "Much slower per domain, and requires 'playwright install chromium' "
             "to have been run on this machine — not available on most free cloud hosts.",
    )
    proxies_text = st.text_area(
        "Proxies (optional, one per line)", height=80,
        placeholder="http://user:pass@host:port",
        help="Free datacenter proxy lists are usually already blacklisted and won't help much. "
             "Leave empty unless you have a paid rotating endpoint.",
    )

domains_text = st.text_area(
    "Domains", height=200,
    placeholder="example.com\nhttps://www.example.org\n# lines starting with # are ignored",
)

run_clicked = st.button("Run scraper", type="primary")

if run_clicked:
    domains = [
        line.split(",")[0].strip()
        for line in domains_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    proxies = [p.strip() for p in proxies_text.splitlines() if p.strip()] or None

    if not domains:
        st.warning("Enter at least one domain.")
        st.stop()

    if use_playwright:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            st.error(
                "Playwright fallback is checked but not installed on this host. "
                "Run `pip install playwright && playwright install chromium`, "
                "or uncheck the option to continue without it."
            )
            st.stop()

    progress_bar = st.progress(0.0)
    status_text = st.empty()
    table_placeholder = st.empty()

    rows = []
    total = len(domains)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(es.process_domain, url, delay, proxies, verify_mx, use_playwright): url
            for url in domains
        }
        for done, future in enumerate(as_completed(futures), start=1):
            r = future.result()
            own = sorted({e for e in r.emails if es.email_matches_domain(e, r.domain)})
            other = sorted(r.emails - set(own))
            rows.append({
                "input_url": r.input_url,
                "domain": r.domain,
                "own_domain_emails": "; ".join(own),
                "other_domain_emails": "; ".join(other),
                "method": r.method,
                "source_pages": "; ".join(sorted(r.source_pages)),
                "error": r.error,
            })

            progress_bar.progress(done / total)
            status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
            status_text.text(f"[{done}/{total}] {r.domain or r.input_url}: {status}")
            table_placeholder.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.session_state.results = rows
    st.success(f"Done — {total} domain(s) processed.")

if st.session_state.results:
    df = pd.DataFrame(st.session_state.results)
    st.subheader("Results")
    st.dataframe(df, use_container_width=True)
    st.download_button(
        "Download results.csv",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="results.csv",
        mime="text/csv",
    )
