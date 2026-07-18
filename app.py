"""Streamlit dashboard for email_scraper.py.

Wraps the existing scraper logic (sitemap discovery, contact-page ranking,
email extraction, optional Playwright fallback) in a one-page web UI: paste
domains, hit Run, watch results populate live, download the CSV.

Run locally:   streamlit run app.py
Deploy free:   push this folder to a GitHub repo, connect it at
               https://streamlit.io/cloud
"""

import asyncio
import os
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

# Pre-fills the Proxies box from an env var on first load of a new browser
# session (a page reload otherwise loses whatever was pasted in, since that
# textarea's value only lives in Streamlit's session_state, not on disk).
# Set SCRAPER_PROXIES in Railway's Variables tab (comma- or newline-
# separated) — never hardcode real proxy credentials into this file, since
# it's committed to a public repo. Still editable/overridable per-session
# in the box itself; this only seeds the initial value.
if "proxies_text" not in st.session_state:
    _default_proxies_env = os.environ.get("SCRAPER_PROXIES", "")
    st.session_state.proxies_text = "\n".join(
        p.strip() for p in _default_proxies_env.replace(",", "\n").splitlines() if p.strip()
    )

with st.sidebar:
    st.header("Settings")
    workers = st.slider(
        "Concurrent domains", 1, 50, 10,
        help="Bounded by asyncio tasks now, not OS threads, so this can go "
             "meaningfully higher than the old threaded version could — "
             "pushing it up is mostly a free speed win as long as your host "
             "has the outbound bandwidth/connections to match.",
    )
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
        "Proxies (optional, one per line)", height=80, key="proxies_text",
        placeholder="http://user:pass@host:port",
        help="Free datacenter proxy lists are usually already blacklisted and won't help much. "
             "Leave empty unless you have a paid rotating endpoint. Pre-filled from the "
             "SCRAPER_PROXIES environment variable if set — edit here to override for just "
             "this session.",
    )
    health_check_interval = st.selectbox(
        "Proxy health check interval", [15, 30, 60, 120], index=1,
        format_func=lambda s: f"every {s}s",
        help="How often the Proxy Health panel below re-checks each proxy.",
    )

@st.fragment(run_every=health_check_interval)
def _proxy_health_panel():
    """Re-checks every proxy on its own timer, independent of the rest of
    the page — a plain st.rerun() loop would also restart any in-progress
    scrape, which this must not do. st.fragment scopes the auto-refresh to
    just this panel. Reads the proxies textarea via session_state (not a
    closured variable) since a fragment-only rerun doesn't re-execute the
    surrounding script, so session_state is the only way to see its current
    value on each tick."""
    proxies_raw = st.session_state.get("proxies_text", "")
    proxies = [p.strip() for p in proxies_raw.splitlines() if p.strip()]
    if not proxies:
        st.caption("No proxies entered in the sidebar — add some to monitor their health here.")
        return

    results = asyncio.run(es.check_all_proxies_health(proxies))
    df = pd.DataFrame(results)
    df["status"] = df["alive"].map({True: "✅ alive", False: "❌ down"})
    alive_count = int(df["alive"].sum())

    st.caption(f"{alive_count}/{len(proxies)} alive — last checked {time.strftime('%H:%M:%S')}")
    st.dataframe(
        df[["proxy", "status", "latency_ms", "egress_ip", "error"]],
        width='stretch', hide_index=True,
    )


st.subheader("🩺 Proxy Health")
_proxy_health_panel()

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

    if proxies:
        original_count = len(proxies)
        with st.spinner("Checking proxy health before starting..."):
            proxies, dead_count = asyncio.run(es.filter_alive_proxies(proxies))
        if dead_count == original_count:
            st.warning(f"All {original_count} proxies failed the health check — using them anyway "
                       "since there's no healthy fallback (expect connection errors).")
        elif dead_count:
            st.info(f"{dead_count} of {original_count} proxies failed the health check "
                    "and will be skipped for this run.")

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
