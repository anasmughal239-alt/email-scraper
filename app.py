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
# Same directory/persistence caveats as the per-job CSVs above: survives
# the container's lifetime, wiped on a full redeploy.
BATCH_HISTORY_PATH = os.path.join(RUNS_DIR, "batch_history.jsonl")


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
        "Force Playwright on every no-email domain (manual override)", value=False,
        help="Forces a headless-browser retry on EVERY domain that finds zero emails "
             "during the main pass, including connection failures it can't help with. "
             "Much slower and heavier at batch scale — the automatic second pass below "
             "is the targeted, cheaper default. Requires 'playwright install chromium' "
             "to have been run on this machine — not available on most free cloud hosts. "
             "Enabling this disables the automatic second pass (redundant otherwise).",
    )
    playwright_second_pass = st.checkbox(
        "Auto-retry genuinely-empty domains with Playwright after the batch", value=True,
        help="After the batch (and any --retry-failed pass) completes, domains whose "
             "pages fetched fine but truly found nothing (no email, no salvage) get "
             "Playwright applied once, targeted just at them — not connection failures, "
             "not domains that already salvaged a phone/social. This is the recommended "
             "default; disable if this host doesn't have Playwright/Chromium installed.",
        disabled=use_playwright,
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

col_run, col_fresh, col_retry = st.columns([1, 2, 2])
with col_run:
    run_clicked = st.button("Run scraper", type="primary")
with col_fresh:
    start_fresh = st.checkbox(
        "Start fresh (ignore any saved progress for this exact list)", value=False,
        help="By default, re-running the same domain list resumes where it left off, "
             "skipping domains already saved to disk and appending the rest. Tick this "
             "to discard that saved progress and scrape the whole list again.",
    )
with col_retry:
    retry_failed = st.checkbox(
        "Retry failed domains after this run", value=False,
        help="After the main run, re-attempt (once, at lower concurrency, after a "
             "cooldown) domains that failed to connect at all — catches transient "
             "network blips and temporary rate-limiting. Does not retry domains that "
             "connected fine but genuinely had no email.",
    )


async def _run_batch_live(domains, max_workers, delay, proxies, verify_mx, use_playwright,
                           respect_robots, csv_path, write_header, prior_done, total_all,
                           progress_bar, status_text, timer_text, table_placeholder,
                           start_time, retry_failed=False, playwright_second_pass=False):
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
    latest_result_by_url: dict = {}
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=es.CSV_FIELDNAMES)
        if write_header:
            writer.writeheader()
            f.flush()

        retryable_urls = []
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
            latest_result_by_url[r.input_url] = r
            if retry_failed and (r.method == "none" or r.fetch_failed):
                retryable_urls.append(r.input_url)
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

        if retry_failed and retryable_urls:
            # method == "none" covers connection-class failures (unreachable,
            # homepage fetch failed, hard timeout) — the kind that can be a
            # transient blip rather than a permanent block. Domains that
            # connected fine but genuinely had no email are excluded, since
            # retrying wouldn't change that outcome.
            status_text.text(f"Cooling down {es.RETRY_COOLDOWN_SECONDS:.0f}s before retrying "
                              f"{len(retryable_urls)} domain(s) that failed to connect...")
            await asyncio.sleep(es.RETRY_COOLDOWN_SECONDS)
            retry_workers = max(1, min(max_workers, es.RETRY_MAX_WORKERS))
            retry_total = len(retryable_urls)
            retry_done = 0
            async for _done, _total, r in es.process_domains_streaming(
                retryable_urls, retry_workers, delay, proxies, verify_mx, use_playwright, respect_robots
            ):
                row = es.result_to_row(r)
                writer.writerow(row)
                f.flush()
                retry_done += 1
                if r.emails:
                    emails_found += 1
                latest_result_by_url[r.input_url] = r
                recent.appendleft(row)
                status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
                status_text.text(f"[retry {retry_done}/{retry_total}] {r.domain or r.input_url}: {status}")
                table_placeholder.dataframe(pd.DataFrame(list(recent)), width='stretch')

        if playwright_second_pass:
            no_result_targets = [
                r for r in latest_result_by_url.values()
                if es.classify_domain_result(r) == "genuine_no_result"
            ]
            low_quality_targets = [
                r for r in latest_result_by_url.values()
                if es.classify_domain_result(r) == "email_found" and es._is_low_quality_email_result(r)
            ]
            pw_targets = no_result_targets + low_quality_targets
            if pw_targets:
                status_text.text(f"Running Playwright on {len(pw_targets)} domain(s): "
                                  f"{len(no_result_targets)} with no result, "
                                  f"{len(low_quality_targets)} with a weak email to try to upgrade...")
                pw_done = 0
                async for r in es.run_playwright_second_pass(pw_targets, delay):
                    row = es.result_to_row(r)
                    writer.writerow(row)
                    f.flush()
                    pw_done += 1
                    if r.emails:
                        emails_found += 1
                    latest_result_by_url[r.input_url] = r
                    recent.appendleft(row)
                    status = f"{len(r.emails)} email(s)" if r.emails else (r.error or "no result")
                    status_text.text(f"[playwright {pw_done}/{len(pw_targets)}] "
                                      f"{r.domain or r.input_url}: {status}")
                    table_placeholder.dataframe(pd.DataFrame(list(recent)), width='stretch')

    latest_category_by_url = {u: es.classify_domain_result(r) for u, r in latest_result_by_url.items()}
    return done, latest_category_by_url


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
        # Already forced on every no-email domain during the main pass —
        # the automatic second pass afterward would just redo the same work.
        effective_second_pass = False
    elif playwright_second_pass:
        try:
            import playwright.async_api  # noqa: F401
            effective_second_pass = True
        except ImportError:
            st.info("Playwright isn't installed on this host — skipping the automatic "
                     "no-result second pass (the rest of the batch runs normally).")
            effective_second_pass = False
    else:
        effective_second_pass = False

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
        done, latest_category_by_url = asyncio.run(_run_batch_live(
            remaining, workers, delay, proxies, verify_mx, use_playwright, respect_robots,
            csv_path, write_header, prior_done, total_all,
            progress_bar, status_text, timer_text, table_placeholder, start_time,
            retry_failed=retry_failed, playwright_second_pass=effective_second_pass,
        ))

        total_elapsed = time.time() - start_time
        st.session_state.last_run_seconds = total_elapsed
        per = total_elapsed / done if done else 0
        st.success(f"Done — {done} domain(s) processed this run in {total_elapsed:.1f}s "
                   f"({per:.1f}s/domain average). "
                   f"{prior_done + done}/{total_all} saved in total.")

        if latest_category_by_url:
            batch_summary = es.summarize_batch(latest_category_by_url)
            es.log_batch_summary(batch_summary, BATCH_HISTORY_PATH)
            st.session_state.last_batch_summary = batch_summary

if st.session_state.get("last_batch_summary"):
    summary = st.session_state.last_batch_summary
    st.subheader("📊 Batch summary")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("With email", summary["email_found"])
    col2.metric("Salvage only", summary["salvage_only"])
    col3.metric("Contact form only", summary["has_contact_form"])
    col4.metric("Genuinely no result", summary["genuine_no_result"])
    col5.metric("Permanently blocked", summary["permanently_blocked"])
    col6.metric("Connection failed", summary["connection_failed"])

    history = es.load_batch_history(BATCH_HISTORY_PATH)
    if len(history) > 1:
        with st.expander(f"📈 History ({len(history)} recent batch(es))"):
            st.dataframe(pd.DataFrame(history), width='stretch', hide_index=True)

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
