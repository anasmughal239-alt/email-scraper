"""Streamlit dashboard for email_scraper.py.

Wraps the existing scraper logic (sitemap discovery, contact-page ranking,
email extraction, optional Playwright fallback) in a one-page web UI: paste
domains, hit Run, watch results populate live, download the CSV.

Run locally:   streamlit run app.py
Deploy free:   push this folder to a GitHub repo, connect it at
               https://streamlit.io/cloud
"""

import asyncio
import time

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
    workers = st.slider("Concurrent domains", 1, 20, 10)
    delay = st.slider(
        "Page-fetch stagger (s)", 0.0, 3.0, 0.3, 0.1,
        help="Pages within one domain are fetched concurrently, not one at a time. "
             "This staggers when each fetch starts, as a soft rate limit — it no "
             "longer serializes the whole domain the way it used to.",
    )
    verify_mx = st.checkbox(
        "Verify MX records", value=False,
        help="Drops emails whose domain has no mail server. Requires dnspython.",
    )
    respect_robots = st.checkbox(
        "Respect robots.txt", value=True,
        help="Honor each site's robots.txt Disallow rules and skip pages it "
             "asks crawlers not to fetch. Recommended; leave on unless you own "
             "the sites or have permission to crawl them.",
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


async def _run_batch_live(domains, max_workers, delay, proxies, verify_mx, use_playwright,
                           respect_robots, progress_bar, status_text, timer_text,
                           table_placeholder, start_time):
    """Drives process_domains_streaming() and pushes each result into the
    live Streamlit UI as it arrives. Runs inside a single asyncio.run() call
    from the synchronous Streamlit script — verified safe in isolation
    before this rewrite (progress_bar/status_text updates render correctly
    mid-run, and a stuck domain is genuinely cancelled at its timeout rather
    than blocking the rest of the batch)."""
    rows = []
    total = len(domains)
    async for done, _total, r in es.process_domains_streaming(
        domains, max_workers, delay, proxies, verify_mx, use_playwright, respect_robots
    ):
        rows.append(es.result_to_row(r))
        elapsed = time.time() - start_time

        progress_bar.progress(done / total)
        status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
        status_text.text(f"[{done}/{total}] {r.domain or r.input_url}: {status}")
        rate = done / elapsed if elapsed > 0 else 0
        remaining = (total - done) / rate if rate > 0 else 0
        timer_text.text(
            f"Elapsed: {elapsed:.0f}s | Rate: {rate:.1f} domains/s | "
            f"Est. remaining: {remaining:.0f}s"
        )
        table_placeholder.dataframe(pd.DataFrame(rows), width='stretch')
    return rows


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
            import playwright.async_api  # noqa: F401
        except ImportError:
            st.error(
                "Playwright fallback is checked but not installed on this host. "
                "Run `pip install playwright && playwright install chromium`, "
                "or uncheck the option to continue without it."
            )
            st.stop()

    progress_bar = st.progress(0.0)
    status_text = st.empty()
    timer_text = st.empty()
    table_placeholder = st.empty()

    start_time = time.time()
    rows = asyncio.run(_run_batch_live(
        domains, workers, delay, proxies, verify_mx, use_playwright, respect_robots,
        progress_bar, status_text, timer_text, table_placeholder, start_time,
    ))

    total_elapsed = time.time() - start_time
    st.session_state.results = rows
    st.session_state.last_run_seconds = total_elapsed
    st.success(f"Done — {len(domains)} domain(s) processed in {total_elapsed:.1f}s "
               f"({total_elapsed / len(domains):.1f}s/domain average).")

if st.session_state.results:
    df = pd.DataFrame(st.session_state.results)
    st.subheader("Results")
    if st.session_state.get("last_run_seconds"):
        secs = st.session_state.last_run_seconds
        st.caption(f"Last run took {secs:.1f}s for {len(df)} domain(s) "
                   f"({secs / len(df):.1f}s/domain average).")
    st.dataframe(df, width='stretch')
    st.download_button(
        "Download results.csv",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="results.csv",
        mime="text/csv",
    )
