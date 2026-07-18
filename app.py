"""Streamlit dashboard for email_scraper.py.

Wraps the existing scraper logic (sitemap discovery, contact-page ranking,
email extraction, optional Playwright fallback) in a one-page web UI: paste
domains, hit Run, watch results populate live, download the CSV.

Run locally:   streamlit run app.py
Deploy free:   push this folder to a GitHub repo, connect it at
               https://streamlit.io/cloud
"""

import asyncio
import csv
import hashlib
import os
import time
from collections import deque

import pandas as pd
import streamlit as st

import email_scraper as es

# Per-job result CSVs are streamed here as each domain finishes, so an
# interrupted run (browser disconnect, Streamlit rerun, container hiccup)
# loses nothing and can be resumed. Gitignored; ephemeral on Railway across
# a full redeploy, but survives the common case of the script/session dying.
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")


def _job_csv_path(domains: list) -> str:
    """A stable path for a given domain list, so re-running the same list
    resumes the same file. Hashed over the sorted domains, so ordering
    changes don't spawn a different file."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    key = hashlib.sha1("\n".join(sorted(domains)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(RUNS_DIR, f"run_{key}.csv")

st.set_page_config(page_title="Email Scraper Dashboard", page_icon="📧", layout="wide")
st.title("📧 Email Scraper Dashboard")
st.caption(
    "Finds contact emails via sitemap discovery, contact-page ranking, and footer parsing. "
    "Enter one domain or URL per line."
)

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

col_run, col_fresh = st.columns([1, 3])
with col_run:
    run_clicked = st.button("Run scraper", type="primary")
with col_fresh:
    start_fresh = st.checkbox(
        "Start fresh (ignore any saved progress for this exact list)", value=False,
        help="By default, re-running the same domain list resumes where it left off, "
             "skipping domains already saved to disk and appending the rest. Tick this "
             "to discard that saved progress and scrape the whole list again.",
    )


async def _run_batch_live(domains, max_workers, delay, proxies, verify_mx, use_playwright,
                           respect_robots, csv_path, write_header, prior_done, total_all,
                           progress_bar, status_text, timer_text, table_placeholder,
                           start_time):
    """Streams each result to csv_path on disk (flushing every row) as it
    completes, so a crash / browser disconnect / Streamlit rerun loses
    nothing — the file is the source of truth, and a later run of the same
    list resumes from it. Holds only a small rolling window of recent rows
    in memory (not the whole batch) so memory and per-iteration render time
    stay flat across thousands of domains, instead of re-rendering an
    ever-growing table on every completion.

    `prior_done`/`total_all` let the progress bar and counter reflect the
    whole list (including domains already saved from an earlier run), while
    rate/ETA are measured over just this run's newly-processed domains."""
    recent = deque(maxlen=40)
    total_new = len(domains)
    done = 0
    emails_found = 0
    errors = 0
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=es.CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
            f.flush()

        async for _done, _total, r in es.process_domains_streaming(
            domains, max_workers, delay, proxies, verify_mx, use_playwright, respect_robots
        ):
            row = es.result_to_row(r)
            writer.writerow(row)
            f.flush()

            done += 1
            if r.emails:
                emails_found += 1
            if r.error:
                errors += 1
            recent.appendleft(row)

            completed_all = prior_done + done
            elapsed = time.time() - start_time
            progress_bar.progress(completed_all / total_all)
            status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
            status_text.text(f"[{completed_all}/{total_all}] {r.domain or r.input_url}: {status}")
            rate = done / elapsed if elapsed > 0 else 0
            remaining = (total_new - done) / rate if rate > 0 else 0
            # Below ~0.1/s, "0.0/s" rounds to something that reads as broken —
            # show domains/min instead so a slow batch still reports a real rate.
            rate_str = f"{rate:.1f}/s" if rate >= 0.1 else f"{rate * 60:.1f}/min"
            timer_text.text(
                f"Elapsed: {elapsed:.0f}s | Rate: {rate_str} | "
                f"Est. remaining: {remaining:.0f}s | "
                f"{emails_found} with email, {errors} errors (this run)"
            )
            table_placeholder.dataframe(pd.DataFrame(list(recent)), width='stretch')
    return done


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
            proxies = None
            st.warning(f"All {original_count} proxies failed the health check (often a bandwidth/"
                       "quota issue, not a real outage) — running this batch WITHOUT a proxy "
                       "(direct connection) instead of through connections already known to be dead.")
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

    csv_path = _job_csv_path(domains)
    st.session_state.job_csv_path = csv_path

    if start_fresh and os.path.exists(csv_path):
        os.remove(csv_path)

    prior_completed = es._load_completed_input_urls(csv_path)
    remaining = [d for d in domains if d not in prior_completed]
    total_all = len(domains)
    prior_done = len(prior_completed)
    # Write a header only when starting a brand-new (or just-cleared) file.
    write_header = (not os.path.exists(csv_path)) or os.path.getsize(csv_path) == 0

    if prior_completed:
        st.info(f"Resuming this list: {prior_done} domain(s) already saved, "
                f"{len(remaining)} remaining.")

    if not remaining:
        st.success("Every domain in this list is already saved — nothing left to run. "
                   "Tick 'Start fresh' to scrape the whole list again.")
    else:
        progress_bar = st.progress(prior_done / total_all if total_all else 0.0)
        status_text = st.empty()
        timer_text = st.empty()
        table_placeholder = st.empty()

        start_time = time.time()
        done = asyncio.run(_run_batch_live(
            remaining, workers, delay, proxies, verify_mx, use_playwright, respect_robots,
            csv_path, write_header, prior_done, total_all,
            progress_bar, status_text, timer_text, table_placeholder, start_time,
        ))

        total_elapsed = time.time() - start_time
        st.session_state.last_run_seconds = total_elapsed
        per = total_elapsed / done if done else 0
        st.success(f"Done — {done} domain(s) processed this run in {total_elapsed:.1f}s "
                   f"({per:.1f}s/domain average). "
                   f"{prior_done + done}/{total_all} saved in total.")

# Results are read back from the on-disk job CSV (the source of truth),
# not from memory — so they're complete even though the live run only held
# a rolling window, and they survive a page reload or a run that was
# interrupted partway.
_job_path = st.session_state.get("job_csv_path")
if _job_path and os.path.exists(_job_path) and os.path.getsize(_job_path) > 0:
    df = pd.read_csv(_job_path).fillna("")
    st.subheader("Results")
    caption = f"{len(df)} domain(s) saved for this list."
    if st.session_state.get("last_run_seconds"):
        secs = st.session_state.last_run_seconds
        caption = f"Last run took {secs:.1f}s. " + caption
    st.caption(caption)
    # For very large result sets, only render a preview in the UI (rendering
    # thousands of rows is slow) but let the download serve the full file.
    if len(df) > 500:
        st.caption("Showing the first 500 rows below; the download has all of them.")
        st.dataframe(df.head(500), width='stretch')
    else:
        st.dataframe(df, width='stretch')
    with open(_job_path, "rb") as f:
        st.download_button(
            "Download results.csv",
            data=f.read(),
            file_name="results.csv",
            mime="text/csv",
        )
