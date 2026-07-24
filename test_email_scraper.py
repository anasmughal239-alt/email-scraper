"""Offline regression tests for email_scraper.

Every case here corresponds to a real bug or false-positive we hit during
development and fixed — the suite exists so those fixes can't silently
regress. All tests are deterministic and network-free (no live HTTP), so
they run fast and anywhere.

Run with either:
    python -m unittest test_email_scraper -v
    pytest test_email_scraper.py -v      (if pytest is installed)
"""

import asyncio
import csv
import os
import tempfile
import time
import unittest
import unittest.mock
from urllib import robotparser

import email_scraper as es


def cf_encode(email: str, key: int = 0x42) -> str:
    """Independent implementation of Cloudflare's single-byte-XOR email
    obfuscation, used to test the decoder against a separate encoder."""
    out = [key]
    for ch in email.encode():
        out.append(ch ^ key)
    return bytes(out).hex()


class TestEmailExtraction(unittest.TestCase):
    def test_mailto_link(self):
        html = '<a href="mailto:hello@company-x.com">Email us</a>'
        self.assertIn("hello@company-x.com", es.extract_emails_from_html(html))

    def test_plain_text_email(self):
        html = "<p>Reach us at support@company-x.com any time.</p>"
        self.assertIn("support@company-x.com", es.extract_emails_from_html(html))

    def test_cloudflare_data_cfemail(self):
        enc = cf_encode("protected@company-x.com")
        html = f'<a href="/cdn-cgi/l/email-protection" data-cfemail="{enc}">[email protected]</a>'
        self.assertIn("protected@company-x.com", es.extract_emails_from_html(html))

    def test_cloudflare_cdn_cgi_link(self):
        enc = cf_encode("info@company-x.com")
        html = f'<a href="/cdn-cgi/l/email-protection#{enc}">contact</a>'
        self.assertIn("info@company-x.com", es.extract_emails_from_html(html))

    def test_obfuscated_at_dot(self):
        for text in [
            "sales [at] company-x [dot] com",
            "sales (at) company-x (dot) com",
            "sales AT company-x DOT com",
        ]:
            with self.subTest(text=text):
                self.assertIn("sales@company-x.com", es.extract_emails_from_html(f"<p>{text}</p>"))

    def test_script_json_leak_stripped(self):
        # Regression: a Next.js JSON blob leaked HTML-escaped ">email" prefixes.
        html = '<script>{"contact":"\\u003eplus@company-x.com"}</script>' \
               '<footer>plus@company-x.com</footer>'
        emails = es.extract_emails_from_html(html)
        self.assertIn("plus@company-x.com", emails)
        self.assertNotIn("u003eplus@company-x.com", emails)


class TestJsonHydrationExtraction(unittest.TestCase):
    """Real contact emails embedded in a framework's page-data JSON blob
    (Next.js __NEXT_DATA__, JSON-LD, etc.) rather than rendered into visible
    HTML — parsed as real JSON, not regexed as raw text, so it can't
    reintroduce the escaped-fragment leak the plain script-stripping guards
    against (see test_script_json_leak_stripped above)."""

    def test_extracts_email_from_next_data_script(self):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"contactEmail": "hi@nextsite.com"}}}'
            "</script>"
        )
        self.assertIn("hi@nextsite.com", es.extract_emails_from_json_hydration(html))

    def test_extracts_email_from_ld_json(self):
        html = (
            '<script type="application/ld+json">'
            '{"@type": "Organization", "email": "contact@ldsite.com"}'
            "</script>"
        )
        self.assertIn("contact@ldsite.com", es.extract_emails_from_json_hydration(html))

    def test_nested_arrays_and_objects_are_walked(self):
        html = (
            '<script type="application/json">'
            '{"a": [1, 2, {"b": {"c": ["deep@nested.com"]}}]}'
            "</script>"
        )
        self.assertIn("deep@nested.com", es.extract_emails_from_json_hydration(html))

    def test_malformed_json_does_not_crash(self):
        html = '<script type="application/json">{not valid json</script>'
        self.assertEqual(es.extract_emails_from_json_hydration(html), set())

    def test_ordinary_script_without_json_type_or_id_is_ignored(self):
        html = '<script>var x = "plain@js.com";</script>'
        self.assertEqual(es.extract_emails_from_json_hydration(html), set())

    def test_folded_into_extract_emails_from_html(self):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"email": "found@viahydration.com"}'
            "</script>"
        )
        self.assertIn("found@viahydration.com", es.extract_emails_from_html(html))


class TestCloudflareDecode(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(es.decode_cloudflare_email(cf_encode("a@b.com")), "a@b.com")

    def test_invalid_hex_returns_none(self):
        self.assertIsNone(es.decode_cloudflare_email("nothex!!"))

    def test_too_short_returns_none(self):
        self.assertIsNone(es.decode_cloudflare_email("2a"))


class TestValidCandidate(unittest.TestCase):
    def test_real_emails_pass(self):
        for email in ["support@stripe.com", "press@figma.com", "hello@linear.app"]:
            with self.subTest(email=email):
                self.assertTrue(es.is_valid_candidate(email))

    def test_placeholder_domains_blocked(self):
        for email in [
            "a@example.com", "b@company.com", "c@hostname.com",
            "d@acme.com", "e@encom.com", "f@yourcompany.com", "g@test.com",
            "you@mail.com",       # classic HTML placeholder text
            "beispiel@email.de",  # German "example@" placeholder pattern
        ]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))

    def test_escaped_unicode_localpart_blocked(self):
        # Regression: a JS-escaped non-breaking space rendered as "u00a0"
        # (no backslash) leaked into a whole local part outside a <script>
        # tag, same bug family as the earlier Shopify "u003e" prefix leak.
        self.assertFalse(es.is_valid_candidate("u00a0@konfront.io"))
        self.assertTrue(es.is_valid_candidate("sales@konfront.io"))

    def test_malformed_domain_with_query_string_blocked(self):
        # Regression (lemonsqueezy.com): a mailto: href's "?subject=..."
        # query string ended up attached to the domain instead of being
        # stripped. is_valid_candidate now rejects any domain containing
        # whitespace or URL-special characters, regardless of how it got
        # there, rather than relying solely on the mailto-parsing split.
        self.assertFalse(es.is_valid_candidate("hello@lemonsqueezy.com?subject=product tour request"))
        self.assertTrue(es.is_valid_candidate("hello@lemonsqueezy.com"))

    def test_example_any_tld_blocked(self):
        # Regression (gatsbyjs.com): the blocklist only covered
        # example.com/.org/.net explicitly, missing example.xyz and others.
        for email in ["you@example.xyz", "a@example.io", "b@example.co"]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))

    def test_more_placeholder_domains_blocked(self):
        # Found in a live 200-domain batch: laravel.com (Ada Lovelace,
        # recurring placeholder person), grafana.com, copy.ai, hover.com,
        # and vwo.com's multi-language "your company" placeholders.
        for email in [
            "ada@lovelace.com", "name@host.com", "name@website.com",
            "jane@doe.net", "name@ihrefirma.com", "nombre@tuempresa.com",
            "nome@suaempresa.com",
        ]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))

    def test_disposable_domains_blocked(self):
        for email in [
            "a@mailinator.com", "b@yopmail.com", "c@guerrillamail.com",
            "d@10minutemail.com", "e@maildrop.cc",
        ]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))

    def test_subdomain_of_blocked_domain(self):
        # Regression: a Sentry tracking pixel on a subdomain slipped past an
        # exact-match blocklist.
        self.assertFalse(es.is_valid_candidate("abc123@o1069899.ingest.sentry.io"))

    def test_asset_filenames_blocked(self):
        for email in ["logo@2x.png", "icon@3x.webp", "house.js@0.0.dqygl43c.mjs"]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))

    def test_blocklist_localparts(self):
        for email in ["noreply@stripe.com", "john.doe@stripe.com", "test@stripe.com"]:
            with self.subTest(email=email):
                self.assertFalse(es.is_valid_candidate(email))


class TestObfuscatedExtraction(unittest.TestCase):
    def test_variants(self):
        got = es.extract_obfuscated_emails("Write to jane [at] example-corp [dot] org please")
        self.assertIn("jane@example-corp.org", got)

    def test_two_part_cctld(self):
        got = es.extract_obfuscated_emails("Email hello [at] example [dot] co [dot] uk for support")
        self.assertIn("hello@example.co.uk", got)

    def test_single_part_tld_still_works_without_trailing_group(self):
        # Regression guard: adding the optional second-dot group must not
        # break the plain single-TLD case it already handled.
        got = es.extract_obfuscated_emails("Contact us at team [at] widgets [dot] io today")
        self.assertIn("team@widgets.io", got)


class TestRankContactUrls(unittest.TestCase):
    def test_contact_page_over_blog(self):
        urls = [
            "https://x.com/blog/how-to-contact-support",
            "https://x.com/contact",
        ]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertIn("https://x.com/contact", picked)
        self.assertNotIn("https://x.com/blog/how-to-contact-support", picked)

    def test_contact_outranks_about_us_at_same_depth(self):
        # Both are "hard" whole-segment matches at depth 1 — contact must
        # still win reliably, not by sitemap-order coincidence.
        urls = ["https://x.com/about-us", "https://x.com/contact"]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertEqual(picked[0], "https://x.com/contact")

    def test_contact_outranks_about_us_regardless_of_input_order(self):
        urls = ["https://x.com/contact", "https://x.com/about-us"]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertEqual(picked[0], "https://x.com/contact")

    def test_multi_language(self):
        urls = [
            "https://x.es/contacto",
            "https://x.fr/contactez-nous",
            "https://x.de/kontakt",
            "https://x.it/contatti",
        ]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertEqual(len(picked), 4)

    def test_negative_and_dated_excluded(self):
        urls = [
            "https://x.com/privacy-policy",
            "https://x.com/2024/06/contact-form-tips",
        ]
        self.assertEqual(es.rank_contact_urls(urls, "https://x.com"), [])

    def test_deep_soft_keyword_excluded_hard_included(self):
        # Regression (Notion): a deep help-center doc whose slug merely
        # contains a soft keyword like "help"/"connect" must NOT be treated
        # as a contact page. Real offending URL was 3+ segments deep.
        soft = ["https://x.com/es-es/help/connect-a-custom-domain-with-x-sites"]
        self.assertEqual(es.rank_contact_urls(soft, "https://x.com"), [])
        # A deep path with an unambiguous phrase like "contact-us" is included.
        hard = ["https://x.com/company/global/contact-us"]
        self.assertEqual(es.rank_contact_urls(hard, "https://x.com"), hard)

    def test_hard_match_outranks_soft_sub_token_pileup(self):
        # Regression: a page whose slug happens to contain several
        # contact-adjacent words as sub-tokens of a compound segment (here:
        # "about", "about-us", "contact", "support" — 4 soft hits) used to
        # outscore a real, dedicated /contact page (1 hard hit), crowding it
        # out of the top-N candidates. Same root pattern as the zendesk
        # contact-center bug (sub-tokens of an irrelevant compound phrase
        # treated as independent evidence), different mechanism: that fix
        # only closed the depth-bypass loophole, not the scoring itself.
        urls = [
            "https://x.com/about-us/contact-support-team",
            "https://x.com/contact",
        ]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertEqual(picked[0], "https://x.com/contact")


class TestEmailMatchesDomain(unittest.TestCase):
    def test_exact_and_subdomain(self):
        self.assertTrue(es.email_matches_domain("sales@stripe.com", "stripe.com"))
        self.assertTrue(es.email_matches_domain("a@mail.stripe.com", "stripe.com"))

    def test_same_brand_different_tld(self):
        self.assertTrue(es.email_matches_domain("hello@mozilla.com", "mozilla.org"))

    def test_brand_in_subsidiary_domain(self):
        self.assertTrue(es.email_matches_domain("team@makenotion.com", "notion.so"))

    def test_false_match_guard(self):
        # A short brand name must not match inside an unrelated longer domain.
        self.assertFalse(es.email_matches_domain("x@linear-algebra-tutors.com", "linear.app"))

    def test_unrelated(self):
        self.assertFalse(es.email_matches_domain("random@totally-different.com", "shopify.com"))

    def test_prefix_product_suite_domains(self):
        # Regression: zoho.com's own product-suite domains (zohocorp.com,
        # zohoinvoice.com, etc.) were missed because "zoho" is only 4 chars,
        # below the ratio rule's 5-char minimum. The hyphen-free prefix rule
        # should catch all of these without reopening the guard above (that
        # false match has a hyphen right at the join; these don't).
        for email in [
            "sales@zohocorp.com", "support@zohobilling.com", "support@zohobooks.com",
            "support@zohoinventory.com", "support@zohoexpense.com",
            "support@zohofinanceplus.com", "support@zohoinvoice.com",
        ]:
            with self.subTest(email=email):
                self.assertTrue(es.email_matches_domain(email, "zoho.com"))


class TestRoleClassification(unittest.TestCase):
    def test_role_labels(self):
        cases = {
            "info@x.com": "general",
            "hello@x.com": "general",
            "sales@x.com": "sales",
            "partnerships@x.com": "sales",
            "support@x.com": "support",
            "help@x.com": "support",
            "press@x.com": "press",
            "careers@x.com": "careers",
            "billing@x.com": "billing",
            "legal@x.com": "legal",
            "abuse@x.com": "legal",
            "jane.doe@x.com": "personal",
            "john_smith@x.com": "personal",
            "alex@x.com": "personal",       # single first name
            "patrick@x.com": "personal",
            "webmaster@x.com": "other",     # single token but a system address
            "postmaster@x.com": "other",
            "xq7z@x.com": "other",          # has a digit -> not a name
        }
        for email, expected in cases.items():
            with self.subTest(email=email):
                self.assertEqual(es.classify_email_role(email), expected)

    def test_ranking_orders_general_first(self):
        emails = ["legal@x.com", "support@x.com", "info@x.com", "sales@x.com"]
        ranked = es.rank_emails_by_role(emails)
        self.assertEqual(ranked[0], "info@x.com")   # general wins
        self.assertEqual(ranked[-1], "legal@x.com")  # legal last

    def test_transactional_addresses_not_misclassified_as_personal(self):
        # Regression: these single alpha-only tokens fell through
        # classify_email_role's "not a known keyword, so assume it's a
        # person's first name" heuristic before SYSTEM_LOCALPARTS covered
        # them — meaning a scraped unsubscribe@/confirm@-style address
        # (priority 3, "personal") could outrank a genuine press/careers/
        # billing/legal contact (priority 4-7) and become primary_email.
        for local in ["unsubscribe", "confirm", "verify", "activate",
                      "signup", "register", "optout", "subscribe"]:
            with self.subTest(local=local):
                self.assertEqual(es.classify_email_role(f"{local}@x.com"), "other")

    def test_pick_primary_prefers_own_domain(self):
        own = ["support@x.com"]
        other = ["info@partner.com"]  # 'general' outranks 'support' by role,
        # but own-domain must still be preferred over a third-party address.
        email, role = es.pick_primary_email(own, other)
        self.assertEqual(email, "support@x.com")
        self.assertEqual(role, "support")

    def test_pick_primary_falls_back_to_other(self):
        email, role = es.pick_primary_email([], ["hello@partner.com"])
        self.assertEqual(email, "hello@partner.com")
        self.assertEqual(role, "general")

    def test_pick_primary_empty(self):
        self.assertEqual(es.pick_primary_email([], []), ("", ""))

    def test_mailto_sourced_email_preferred_within_same_role(self):
        # Both "general" role, tied by ROLE_PRIORITY — the mailto-sourced
        # one should win the tiebreak over one that merely appeared in text.
        emails = ["hello@x.com", "info@x.com"]
        ranked = es.rank_emails_by_role(emails, mailto_emails={"info@x.com"})
        self.assertEqual(ranked[0], "info@x.com")

    def test_mailto_priority_does_not_override_role_priority(self):
        # A mailto-sourced legal@ address must not outrank a plain-text
        # info@ — role still wins first, mailto is only a same-role tiebreak.
        emails = ["legal@x.com", "info@x.com"]
        ranked = es.rank_emails_by_role(emails, mailto_emails={"legal@x.com"})
        self.assertEqual(ranked[0], "info@x.com")

    def test_pick_primary_prefers_mailto_source(self):
        own = ["hello@x.com", "info@x.com"]
        email, role = es.pick_primary_email(own, [], mailto_emails={"info@x.com"})
        self.assertEqual(email, "info@x.com")
        self.assertEqual(role, "general")


class TestRobotsPolicy(unittest.TestCase):
    def _policy(self, body, enabled=True):
        parser = robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        return es.RobotsPolicy(parser, [], enabled=enabled)

    def test_disallow_blocks(self):
        p = self._policy("User-agent: *\nDisallow: /private/\n")
        self.assertFalse(p.can_fetch("https://x.com/private/x"))
        self.assertTrue(p.can_fetch("https://x.com/contact"))

    def test_disabled_allows_all(self):
        p = self._policy("User-agent: *\nDisallow: /\n", enabled=False)
        self.assertTrue(p.can_fetch("https://x.com/anything"))

    def test_no_parser_allows_all(self):
        p = es.RobotsPolicy(None, [], enabled=True)
        self.assertTrue(p.can_fetch("https://x.com/anything"))


class TestUrlNormalization(unittest.TestCase):
    def test_adds_scheme(self):
        self.assertEqual(es.normalize_to_base_url("example.com"), "https://example.com")

    def test_strips_path(self):
        self.assertEqual(es.normalize_to_base_url("https://example.com/foo/bar"),
                         "https://example.com")

    def test_bare_domain_strips_www(self):
        self.assertEqual(es.get_bare_domain("https://www.example.com"), "example.com")


class TestProcessDomainsHardTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_stuck_domain_does_not_block_others(self):
        # Regression: a genuinely hung domain (artstation.com, 5+ minutes
        # with zero CPU activity — a DNS/TLS-level stall beyond the
        # scraper's own request timeouts) blocked an entire 300-domain
        # batch indefinitely, even with 299/300 already done. This proves
        # asyncio.wait_for genuinely cancels a stuck domain's task instead
        # of waiting for it, without disturbing a concurrently-completing
        # fast one. process_domain itself is faked out here so the test
        # stays network-free/deterministic; only the timeout/cancellation
        # plumbing in process_domains_streaming is under test.
        import time

        async def fake_process_domain(url, delay, proxies, verify_mx, use_playwright, respect_robots):
            if url == "stuck.com":
                await asyncio.sleep(30)
                return es.DomainResult(input_url=url, error="should never be reached within the test's timeout")
            r = es.DomainResult(input_url=url)
            r.emails = {"hello@fast.com"}
            return r

        with unittest.mock.patch.object(es, "process_domain", fake_process_domain), \
                unittest.mock.patch.object(es, "DOMAIN_HARD_TIMEOUT_SECONDS", 1):
            t0 = time.time()
            results = {}
            async for _done, _total, r in es.process_domains_streaming(
                ["fast.com", "stuck.com"], max_workers=2, delay=0,
                proxies=None, verify_mx=False, use_playwright=False, respect_robots=True,
            ):
                results[r.input_url] = r
            elapsed = time.time() - t0

        # Must return well before the stuck task's 30s sleep would finish.
        self.assertLess(elapsed, 15)
        self.assertEqual(results["fast.com"].emails, {"hello@fast.com"})
        self.assertIn("gave up after", results["stuck.com"].error)


class TestParseRetryAfter(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(es._parse_retry_after(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(es._parse_retry_after(""))

    def test_integer_seconds(self):
        self.assertEqual(es._parse_retry_after("120"), 120.0)

    def test_integer_seconds_with_whitespace(self):
        self.assertEqual(es._parse_retry_after("  30  "), 30.0)

    def test_http_date_in_future(self):
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        header_value = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = es._parse_retry_after(header_value)
        self.assertIsNotNone(delay)
        # Allow a few seconds of slack for test execution time.
        self.assertTrue(50 <= delay <= 65, f"expected ~60s, got {delay}")

    def test_http_date_in_past_clamps_to_zero(self):
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        header_value = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = es._parse_retry_after(header_value)
        self.assertEqual(delay, 0.0)

    def test_garbage_value_returns_none(self):
        self.assertIsNone(es._parse_retry_after("not a valid value at all"))


class TestDecideEmptyResultStatus(unittest.TestCase):
    """Regression: a domain where every candidate page failed to fetch
    (403/timeout/connection reset/exhausted retries) and a domain that
    fetched fine and genuinely had no email used to produce the identical
    error="no emails found on checked pages" — indistinguishable, which
    silently broke --retry-failed (built specifically to retry the former
    case) since it only ever checked method=="none"."""

    def test_all_pages_failed_to_fetch(self):
        fetch_failed, message = es._decide_empty_result_status(
            pages_fetched_ok=0, pages_fetch_failed=3
        )
        self.assertTrue(fetch_failed)
        self.assertIn("failed to fetch", message)

    def test_pages_fetched_fine_no_email(self):
        fetch_failed, message = es._decide_empty_result_status(
            pages_fetched_ok=3, pages_fetch_failed=0
        )
        self.assertFalse(fetch_failed)
        self.assertEqual(message, "no emails found on checked pages")

    def test_mixed_outcome_prefers_genuine_result(self):
        # At least one page DID fetch successfully, so this is a genuine
        # "checked and empty" result even though other candidates failed.
        fetch_failed, _ = es._decide_empty_result_status(
            pages_fetched_ok=1, pages_fetch_failed=2
        )
        self.assertFalse(fetch_failed)

    def test_no_pages_attempted_at_all(self):
        # Neither fetched nor failed (e.g. an empty candidate list reached
        # this point somehow) — shouldn't claim a fetch failure that didn't
        # happen.
        fetch_failed, _ = es._decide_empty_result_status(
            pages_fetched_ok=0, pages_fetch_failed=0
        )
        self.assertFalse(fetch_failed)


class TestFetchAndExtractPagesDistinguishesFailureFromEmpty(unittest.IsolatedAsyncioTestCase):
    """The counting half of the same regression: fetch_and_extract_pages()
    must report *how many* candidate pages fetched successfully versus
    failed outright, not just which pages happened to contain an email."""

    async def test_every_fetch_fails(self):
        async def fake_fetch_none(session, url, proxy=None, blocked_sink=None):
            return None

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch_none):
            emails, source_pages, fetched_ok, fetch_failed, phones, whatsapp, social, _blocked, _extras = (
                await es.fetch_and_extract_pages(
                    None, ["https://x.com/contact", "https://x.com/about"], robots, delay=0
                )
            )

        self.assertEqual(emails, set())
        self.assertEqual(source_pages, set())
        self.assertEqual(fetched_ok, 0)
        self.assertEqual(fetch_failed, 2)
        self.assertEqual(phones, set())
        self.assertEqual(whatsapp, set())
        self.assertEqual(social, set())

    async def test_fetches_succeed_but_page_has_no_email(self):
        class FakeResp:
            text = "<html><body>Just some ordinary page content, no address here.</body></html>"

        async def fake_fetch_ok(session, url, proxy=None, blocked_sink=None):
            return FakeResp()

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch_ok):
            emails, source_pages, fetched_ok, fetch_failed, phones, whatsapp, social, _blocked, _extras = (
                await es.fetch_and_extract_pages(
                    None, ["https://x.com/contact"], robots, delay=0
                )
            )

        self.assertEqual(emails, set())
        self.assertEqual(source_pages, set())
        self.assertEqual(fetched_ok, 1)
        self.assertEqual(fetch_failed, 0)
        self.assertEqual(phones, set())

    async def test_mixed_some_fetch_some_dont(self):
        class FakeResp:
            text = "<p>reach us at hello@company-x.com</p>"

        async def fake_fetch_mixed(session, url, proxy=None, blocked_sink=None):
            if "works" in url:
                return FakeResp()
            return None

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch_mixed):
            emails, source_pages, fetched_ok, fetch_failed, phones, whatsapp, social, _blocked, _extras = (
                await es.fetch_and_extract_pages(
                    None, ["https://x.com/works", "https://x.com/broken"], robots, delay=0
                )
            )

        self.assertEqual(emails, {"hello@company-x.com"})
        self.assertEqual(fetched_ok, 1)
        self.assertEqual(fetch_failed, 1)


class TestResolverSelection(unittest.IsolatedAsyncioTestCase):
    """Regression: ThreadedResolver's blocking socket.getaddrinfo runs in a
    background thread that can't be forcibly killed if a lookup hangs — a
    zombie thread occupies an executor slot until the OS-level call itself
    returns. AsyncResolver (aiodns) resolves DNS natively async with no
    thread at all, but aiodns's c-ares resolver is known broken on Windows
    in this dev environment, so the choice must be platform-conditional
    rather than an unconditional swap. Async since AsyncResolver.__init__
    requires a running event loop."""

    async def test_windows_uses_threaded_resolver(self):
        with unittest.mock.patch.object(es.sys, "platform", "win32"):
            resolver = es._new_resolver()
        self.assertIsInstance(resolver, es.ThreadedResolver)

    async def test_non_windows_uses_async_resolver(self):
        with unittest.mock.patch.object(es.sys, "platform", "linux"):
            resolver = es._new_resolver()
        self.assertIsInstance(resolver, es.AsyncResolver)

    async def test_falls_back_to_threaded_if_async_resolver_unavailable(self):
        with unittest.mock.patch.object(es.sys, "platform", "linux"), \
                unittest.mock.patch.object(es, "AsyncResolver", side_effect=RuntimeError("no aiodns")):
            resolver = es._new_resolver()
        self.assertIsInstance(resolver, es.ThreadedResolver)


class TestSalvageExtraction(unittest.TestCase):
    """Phone/WhatsApp/social salvage — used only when a domain ends up with
    no email but its pages fetched fine. Each extractor is deliberately
    conservative (tel: hrefs and a leading "+" required for phone; the two
    literal WhatsApp link shapes; known social-platform URL patterns with
    share/intent noise excluded) to avoid false positives, the same
    "trust a hard signal over a loose regex" discipline used for email
    extraction elsewhere in this module."""

    def test_phone_tel_href(self):
        html = '<a href="tel:+923001234567">Call us</a>'
        self.assertIn("+923001234567", es.extract_phone_numbers_from_html(html))

    def test_phone_standard_text_format(self):
        html = "<p>Reach us at +1 (555) 123-4567 anytime.</p>"
        self.assertIn("+1 (555) 123-4567", es.extract_phone_numbers_from_html(html))

    def test_phone_does_not_false_positive_on_dates_prices_codes(self):
        html = "<p>Published 2024-06-15, price $123.45, order #98765432.</p>"
        self.assertEqual(es.extract_phone_numbers_from_html(html), set())

    def test_whatsapp_wa_me_link(self):
        html = '<a href="https://wa.me/447577305736">Chat on WhatsApp</a>'
        self.assertIn("https://wa.me/447577305736", es.extract_whatsapp_links_from_html(html))

    def test_whatsapp_send_link(self):
        html = '<a href="https://api.whatsapp.com/send?phone=447577305736&text=hi">WhatsApp</a>'
        found = es.extract_whatsapp_links_from_html(html)
        self.assertTrue(any("whatsapp.com/send" in u for u in found))

    def test_social_profile_link(self):
        html = '<footer><a href="https://www.instagram.com/mybrand/">Instagram</a></footer>'
        self.assertIn("https://www.instagram.com/mybrand/", es.extract_social_links_from_html(html))

    def test_social_excludes_share_widget_noise(self):
        # Regression guard: a "Share on Facebook" button's sharer.php URL is
        # not the business's own profile — must not be mistaken for one.
        html = '<a href="https://www.facebook.com/sharer.php?u=https://x.com/post">Share</a>'
        self.assertEqual(es.extract_social_links_from_html(html), set())

    def test_page_with_none_of_the_above_returns_empty(self):
        html = "<html><body><h1>Welcome</h1><p>We sell widgets.</p></body></html>"
        self.assertEqual(es.extract_phone_numbers_from_html(html), set())
        self.assertEqual(es.extract_whatsapp_links_from_html(html), set())
        self.assertEqual(es.extract_social_links_from_html(html), set())


class TestMaybeApplySalvage(unittest.TestCase):
    """The gating logic: salvage should only ever surface for a domain that
    genuinely ended up with no email despite its pages fetching
    successfully — never when an email was found, never when every page
    failed to fetch (nothing real to salvage from in that case)."""

    def test_applies_when_no_email_and_fetch_succeeded(self):
        result = es.DomainResult(input_url="https://x.com")
        result.fetch_failed = False
        phones, whatsapp, social = {"+1234"}, {"https://wa.me/1234"}, {"https://instagram.com/x"}
        es._maybe_apply_salvage(result, phones, whatsapp, social)
        self.assertEqual(result.phones, phones)
        self.assertEqual(result.whatsapp_links, whatsapp)
        self.assertEqual(result.social_links, social)

    def test_does_not_apply_when_email_was_found(self):
        result = es.DomainResult(input_url="https://x.com")
        result.emails = {"hello@x.com"}
        result.fetch_failed = False
        es._maybe_apply_salvage(result, {"+1234"}, set(), set())
        self.assertEqual(result.phones, set())

    def test_does_not_apply_when_fetch_failed(self):
        # Every candidate page failed to fetch — there's no real page
        # content to have salvaged anything from in the first place.
        result = es.DomainResult(input_url="https://x.com")
        result.fetch_failed = True
        es._maybe_apply_salvage(result, {"+1234"}, set(), set())
        self.assertEqual(result.phones, set())


class TestClassifyDomainResult(unittest.TestCase):
    """The four hit-rate buckets must be mutually exclusive and cover every
    real outcome a domain can land in."""

    def test_email_found(self):
        r = es.DomainResult(input_url="https://x.com")
        r.emails = {"hello@x.com"}
        self.assertEqual(es.classify_domain_result(r), "email_found")

    def test_connection_failed_method_none(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "none"
        self.assertEqual(es.classify_domain_result(r), "connection_failed")

    def test_connection_failed_fetch_failed_flag(self):
        # method is "sitemap" (candidates were found) but every one of them
        # failed to fetch — still a connection failure, not a genuine result.
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.fetch_failed = True
        self.assertEqual(es.classify_domain_result(r), "connection_failed")

    def test_salvage_only(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.phones = {"+1234567890"}
        self.assertEqual(es.classify_domain_result(r), "salvage_only")

    def test_genuine_no_result(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        self.assertEqual(es.classify_domain_result(r), "genuine_no_result")

    def test_permanently_blocked(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "none"
        r.blocked_by = "cloudflare"
        self.assertEqual(es.classify_domain_result(r), "permanently_blocked")

    def test_email_found_outranks_blocked_by(self):
        # A partial block (some pages blocked, one candidate still yielded
        # an email) is a real result — it shouldn't be demoted to
        # permanently_blocked just because a signature was seen somewhere.
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.emails = {"hi@x.com"}
        r.blocked_by = "akamai"
        self.assertEqual(es.classify_domain_result(r), "email_found")

    def test_has_contact_form(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.has_contact_form = True
        self.assertEqual(es.classify_domain_result(r), "has_contact_form")

    def test_salvage_only_outranks_has_contact_form(self):
        # A phone/social number is a stronger signal than "there's a form
        # somewhere" — salvage_only should win if both are present.
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.phones = {"+1234567890"}
        r.has_contact_form = True
        self.assertEqual(es.classify_domain_result(r), "salvage_only")


class TestBlockedSignatureDetection(unittest.TestCase):
    """Detection only — these tests confirm a known WAF/bot-challenge
    block-page is correctly labeled, never that anything is bypassed."""

    def test_detects_cloudflare_challenge_page(self):
        html = (
            "<html><head><title>Just a moment...</title></head><body>"
            "<div class='cf-browser-verification'>Checking your browser...</div>"
            "</body></html>"
        )
        self.assertEqual(es.detect_blocked_signature(html), "cloudflare")

    def test_detects_akamai_block_page(self):
        html = (
            "<html><body><h1>Access Denied</h1>"
            "<p>You don't have permission to access this resource.</p>"
            "<p>Reference #18.7c5e1002.1690000000.1a2b3c4</p></body></html>"
        )
        self.assertEqual(es.detect_blocked_signature(html), "akamai")

    def test_detects_perimeterx_block_page(self):
        html = '<html><body><div id="px-captcha"></div>Please verify you are a human</body></html>'
        self.assertEqual(es.detect_blocked_signature(html), "perimeterx")

    def test_ordinary_page_not_flagged(self):
        html = "<html><body><h1>Welcome to Acme Corp</h1><p>Contact us at hi@acme.com</p></body></html>"
        self.assertEqual(es.detect_blocked_signature(html), "")

    def test_generic_access_denied_alone_not_flagged(self):
        # "access denied" alone is too generic (plenty of ordinary pages use
        # the phrase) — only counts paired with Akamai's "reference #" marker.
        html = "<html><body>Access Denied: you need to log in first.</body></html>"
        self.assertEqual(es.detect_blocked_signature(html), "")

    def test_empty_html_returns_empty(self):
        self.assertEqual(es.detect_blocked_signature(""), "")


class TestNoteBlockedSignature(unittest.IsolatedAsyncioTestCase):
    class FakeResp:
        def __init__(self, text, charset="utf-8"):
            self._content = text.encode(charset)
            self.charset = charset

        async def read(self):
            return self._content

    async def test_appends_vendor_when_signature_present(self):
        resp = self.FakeResp("<div class='cf-browser-verification'>blocked</div>")
        sink: list = []
        await es._note_blocked_signature(resp, sink)
        self.assertEqual(sink, ["cloudflare"])

    async def test_no_append_when_sink_is_none(self):
        resp = self.FakeResp("<div class='cf-browser-verification'>blocked</div>")
        await es._note_blocked_signature(resp, None)  # must not raise

    async def test_no_append_when_no_signature(self):
        resp = self.FakeResp("<h1>Ordinary page</h1>")
        sink: list = []
        await es._note_blocked_signature(resp, sink)
        self.assertEqual(sink, [])


class TestContactFormDetection(unittest.TestCase):
    def test_detects_wpcf7_form(self):
        html = '<form id="wpcf7-form-123" action="/contact"><input type="email" name="your-email"></form>'
        self.assertTrue(es.detect_contact_form(html))

    def test_detects_hubspot_form(self):
        html = '<form class="hs-form stacked"><input type="email" name="email"></form>'
        self.assertTrue(es.detect_contact_form(html))

    def test_detects_generic_email_plus_message_shape(self):
        html = (
            '<form action="/submit">'
            '<input type="email" name="from">'
            '<textarea name="message"></textarea>'
            '<button type="submit">Send</button>'
            "</form>"
        )
        self.assertTrue(es.detect_contact_form(html))

    def test_email_input_alone_not_flagged(self):
        # A newsletter-signup form (email input, no message field) isn't a
        # contact form — it's a different intent entirely.
        html = '<form action="/subscribe"><input type="email" name="email"><button>Subscribe</button></form>'
        self.assertFalse(es.detect_contact_form(html))

    def test_page_with_no_form_not_flagged(self):
        html = "<html><body><h1>About us</h1><p>We make things.</p></body></html>"
        self.assertFalse(es.detect_contact_form(html))


class TestPageMetadataExtraction(unittest.TestCase):
    def test_extracts_title_and_meta_description(self):
        html = (
            "<html><head><title>Acme Corp | Home</title>"
            '<meta name="description" content="We build widgets."></head></html>'
        )
        meta = es.extract_page_metadata(html)
        self.assertEqual(meta["page_title"], "Acme Corp | Home")
        self.assertEqual(meta["meta_description"], "We build widgets.")

    def test_extracts_og_tags(self):
        html = (
            "<html><head>"
            '<meta property="og:title" content="Acme Corp">'
            '<meta property="og:description" content="Widgets for everyone.">'
            "</head></html>"
        )
        meta = es.extract_page_metadata(html)
        self.assertEqual(meta["og_title"], "Acme Corp")
        self.assertEqual(meta["og_description"], "Widgets for everyone.")

    def test_missing_tags_default_to_empty_string(self):
        meta = es.extract_page_metadata("<html><head></head><body></body></html>")
        self.assertEqual(meta, {
            "page_title": "", "meta_description": "", "og_title": "", "og_description": "",
        })


class TestFetchAndExtractPagesPageExtras(unittest.IsolatedAsyncioTestCase):
    """has_contact_form and page metadata must surface through the same
    return value as blocked_by, reusing already-fetched HTML — no extra
    requests for either."""

    async def test_has_contact_form_true_if_any_page_has_one(self):
        class FormResp:
            text = '<form class="hs-form"><input type="email" name="email"></form>'

        class PlainResp:
            text = "<h1>No form here</h1>"

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            return FormResp() if url == "https://x.com/contact" else PlainResp()

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch):
            *_rest, page_extras = await es.fetch_and_extract_pages(
                None, ["https://x.com/about", "https://x.com/contact"], robots, delay=0
            )
        self.assertTrue(page_extras["has_contact_form"])

    async def test_metadata_comes_from_first_successful_page(self):
        class FirstResp:
            text = "<html><head><title>First Page</title></head></html>"

        class SecondResp:
            text = "<html><head><title>Second Page</title></head></html>"

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            return FirstResp() if url == "https://x.com/a" else SecondResp()

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch), \
                unittest.mock.patch.object(es, "PER_DOMAIN_FETCH_WORKERS", 1):
            *_rest, page_extras = await es.fetch_and_extract_pages(
                None, ["https://x.com/a", "https://x.com/b"], robots, delay=0
            )
        self.assertEqual(page_extras["page_title"], "First Page")

    async def test_no_form_and_no_metadata_when_nothing_fetched(self):
        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            return None

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch):
            *_rest, page_extras = await es.fetch_and_extract_pages(
                None, ["https://x.com/dead"], robots, delay=0
            )
        self.assertEqual(page_extras, {
            "has_contact_form": False, "page_title": "", "meta_description": "",
            "og_title": "", "og_description": "", "mailto_emails": set(),
        })


class TestFetchAndExtractPagesBlockedSignature(unittest.IsolatedAsyncioTestCase):
    """blocked_by must surface from either a rejected (non-200) response
    whose body matched a signature, or a 200 response whose body itself is
    a challenge page — fetch_and_extract_pages sees both through the same
    per-page results loop."""

    async def test_propagates_from_rejected_response(self):
        class FakeResp:
            text = "<h1>Welcome to Acme Corp</h1><p>hi@acme.com</p>"

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            if url == "https://x.com/blocked":
                if blocked_sink is not None:
                    blocked_sink.append("akamai")
                return None
            return FakeResp()

        urls = ["https://x.com/blocked", "https://x.com/contact"]
        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch):
            _e, _sp, _ok, _ff, _p, _w, _s, blocked_by, _extras = await es.fetch_and_extract_pages(
                None, urls, robots, delay=0
            )
        self.assertEqual(blocked_by, "akamai")

    async def test_propagates_from_successful_challenge_page_body(self):
        class ChallengeResp:
            text = "<div class='cf-browser-verification'>Checking your browser...</div>"

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            return ChallengeResp()

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch):
            _e, _sp, _ok, _ff, _p, _w, _s, blocked_by, _extras = await es.fetch_and_extract_pages(
                None, ["https://x.com/"], robots, delay=0
            )
        self.assertEqual(blocked_by, "cloudflare")

    async def test_no_signature_leaves_blocked_by_empty(self):
        class FakeResp:
            text = "<h1>Ordinary page</h1>"

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            return FakeResp()

        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch):
            *_rest, blocked_by, _extras = await es.fetch_and_extract_pages(
                None, ["https://x.com/"], robots, delay=0
            )
        self.assertEqual(blocked_by, "")


class TestIsRetryable(unittest.TestCase):
    def test_connection_failed_is_retryable(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "none"
        self.assertTrue(es._is_retryable(r))

    def test_fetch_failed_is_retryable(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.fetch_failed = True
        self.assertTrue(es._is_retryable(r))

    def test_blocked_domain_is_not_retryable(self):
        # A confirmed WAF/bot-challenge signature means retrying is pointless
        # — it'll fail the same way every time.
        r = es.DomainResult(input_url="https://x.com")
        r.method = "none"
        r.blocked_by = "cloudflare"
        self.assertFalse(es._is_retryable(r))

    def test_genuine_result_is_not_retryable(self):
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        self.assertFalse(es._is_retryable(r))


class TestExtractMailtoEmails(unittest.TestCase):
    def test_finds_mailto_link(self):
        html = '<a href="mailto:hi@x.com">Email us</a>'
        self.assertEqual(es.extract_mailto_emails(html), {"hi@x.com"})

    def test_plain_text_email_not_included(self):
        html = "<p>Reach us at hi@x.com or see our mailto link.</p>"
        self.assertEqual(es.extract_mailto_emails(html), set())

    def test_no_mailto_links_returns_empty_set(self):
        self.assertEqual(es.extract_mailto_emails("<p>No links here.</p>"), set())


class TestIsLowQualityEmailResult(unittest.TestCase):
    def test_no_email_is_not_low_quality(self):
        r = es.DomainResult(input_url="https://x.com")
        r.domain = "x.com"
        self.assertFalse(es._is_low_quality_email_result(r))

    def test_own_domain_classified_email_is_not_low_quality(self):
        r = es.DomainResult(input_url="https://x.com")
        r.domain = "x.com"
        r.emails = {"support@x.com"}
        self.assertFalse(es._is_low_quality_email_result(r))

    def test_third_party_only_is_low_quality(self):
        # No email on the business's own domain at all — e.g. only a
        # Zendesk/helpdesk-widget address was found.
        r = es.DomainResult(input_url="https://x.com")
        r.domain = "x.com"
        r.emails = {"help@some-helpdesk-widget.com"}
        self.assertTrue(es._is_low_quality_email_result(r))

    def test_unclassifiable_own_domain_role_is_low_quality(self):
        r = es.DomainResult(input_url="https://x.com")
        r.domain = "x.com"
        r.emails = {"xq7z@x.com"}  # classify_email_role -> "other"
        self.assertTrue(es._is_low_quality_email_result(r))


class TestSummarizeBatch(unittest.TestCase):
    def test_counts_each_bucket(self):
        latest_category_by_url = {
            "a.com": "email_found",
            "b.com": "email_found",
            "c.com": "connection_failed",
            "d.com": "salvage_only",
            "e.com": "genuine_no_result",
            "f.com": "permanently_blocked",
            "g.com": "has_contact_form",
        }
        summary = es.summarize_batch(latest_category_by_url)
        self.assertEqual(summary["total"], 7)
        self.assertEqual(summary["email_found"], 2)
        self.assertEqual(summary["connection_failed"], 1)
        self.assertEqual(summary["salvage_only"], 1)
        self.assertEqual(summary["genuine_no_result"], 1)
        self.assertEqual(summary["permanently_blocked"], 1)
        self.assertEqual(summary["has_contact_form"], 1)

    def test_empty_batch(self):
        summary = es.summarize_batch({})
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["email_found"], 0)


class TestBatchHistoryLog(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        os.remove(self.path)  # start from a genuinely non-existent file

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_log_then_load_round_trip(self):
        summary1 = {"total": 10, "email_found": 4, "connection_failed": 3,
                    "salvage_only": 2, "genuine_no_result": 1}
        summary2 = {"total": 5, "email_found": 5, "connection_failed": 0,
                    "salvage_only": 0, "genuine_no_result": 0}
        es.log_batch_summary(summary1, self.path)
        es.log_batch_summary(summary2, self.path)

        history = es.load_batch_history(self.path)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["total"], 10)
        self.assertEqual(history[1]["total"], 5)
        self.assertIn("timestamp", history[0])

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(es.load_batch_history(self.path), [])

    def test_load_respects_limit(self):
        for i in range(5):
            es.log_batch_summary({"total": i, "email_found": 0, "connection_failed": 0,
                                   "salvage_only": 0, "genuine_no_result": 0}, self.path)
        history = es.load_batch_history(self.path, limit=2)
        self.assertEqual(len(history), 2)
        # Most recent last.
        self.assertEqual(history[-1]["total"], 4)

    def test_load_skips_corrupt_lines(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not valid json\n")
            f.write('{"total": 1, "email_found": 1}\n')
        history = es.load_batch_history(self.path)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total"], 1)


class TestPlaywrightSecondPassTargeting(unittest.IsolatedAsyncioTestCase):
    """The automatic default second pass must apply Playwright ONLY to
    domains whose final category is genuine_no_result — not connection
    failures (nothing to retry Playwright against usefully) and not
    salvage_only (already found something, don't waste a browser launch)."""

    async def test_only_genuine_no_result_domains_get_playwright(self):
        email_found = es.DomainResult(input_url="https://a.com")
        email_found.emails = {"hi@a.com"}

        connection_failed = es.DomainResult(input_url="https://b.com")
        connection_failed.method = "none"

        salvage_only = es.DomainResult(input_url="https://c.com")
        salvage_only.method = "sitemap"
        salvage_only.phones = {"+1234"}

        genuine_no_result = es.DomainResult(input_url="https://d.com")
        genuine_no_result.method = "sitemap"
        genuine_no_result.base_url = "https://d.com"
        genuine_no_result.pages_checked = ["https://d.com/contact"]

        all_results = [email_found, connection_failed, salvage_only, genuine_no_result]
        targets = [r for r in all_results if es.classify_domain_result(r) == "genuine_no_result"]
        self.assertEqual(targets, [genuine_no_result])

        calls = []

        class FakePlaywrightFetcher:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                pass

        async def fake_fetch_and_extract(pf, result, base_url, pages_to_check, delay,
                                          salvage_phones, salvage_whatsapp, salvage_social):
            calls.append(result.input_url)

        with unittest.mock.patch.object(es, "PlaywrightFetcher", FakePlaywrightFetcher), \
             unittest.mock.patch.object(es, "_playwright_fetch_and_extract", fake_fetch_and_extract):
            processed = [r async for r in es.run_playwright_second_pass(targets, delay=0)]

        self.assertEqual(calls, ["https://d.com"])
        self.assertEqual([r.input_url for r in processed], ["https://d.com"])

    async def test_shares_one_browser_instance_across_all_targets(self):
        # The whole point of Phase 0.2's fix: a batch second-pass must launch
        # exactly ONE PlaywrightFetcher (one Chromium process) and reuse it
        # across every domain, not construct a fresh one per domain.
        d1 = es.DomainResult(input_url="https://d1.com")
        d1.method = "sitemap"
        d1.base_url = "https://d1.com"
        d1.pages_checked = ["https://d1.com/contact"]

        d2 = es.DomainResult(input_url="https://d2.com")
        d2.method = "sitemap"
        d2.base_url = "https://d2.com"
        d2.pages_checked = ["https://d2.com/contact"]

        instances_created = []

        class FakePlaywrightFetcher:
            def __init__(self):
                instances_created.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                pass

        seen_pf_per_call = []

        async def fake_fetch_and_extract(pf, result, base_url, pages_to_check, delay,
                                          salvage_phones, salvage_whatsapp, salvage_social):
            seen_pf_per_call.append(pf)

        with unittest.mock.patch.object(es, "PlaywrightFetcher", FakePlaywrightFetcher), \
             unittest.mock.patch.object(es, "_playwright_fetch_and_extract", fake_fetch_and_extract):
            processed = [r async for r in es.run_playwright_second_pass([d1, d2], delay=0)]

        self.assertEqual(len(instances_created), 1)
        self.assertEqual(len(seen_pf_per_call), 2)
        self.assertIs(seen_pf_per_call[0], seen_pf_per_call[1])
        self.assertIs(seen_pf_per_call[0], instances_created[0])
        self.assertEqual([r.input_url for r in processed], ["https://d1.com", "https://d2.com"])

    async def test_reuses_stored_pages_checked_not_rediscovered(self):
        # The whole point of storing base_url/pages_checked is that the
        # second pass doesn't redo sitemap discovery — verify it's actually
        # passed through to playwright_fallback unchanged.
        r = es.DomainResult(input_url="https://x.com")
        r.method = "sitemap"
        r.base_url = "https://x.com"
        r.pages_checked = ["https://x.com/contact", "https://x.com/about"]

        seen_args = {}

        async def fake_playwright_fallback(result, base_url, pages_to_check, delay,
                                            salvage_phones, salvage_whatsapp, salvage_social):
            seen_args["base_url"] = base_url
            seen_args["pages_to_check"] = pages_to_check

        with unittest.mock.patch.object(es, "playwright_fallback", fake_playwright_fallback):
            await es.apply_playwright_to_result(r, delay=0)

        self.assertEqual(seen_args["base_url"], "https://x.com")
        self.assertEqual(seen_args["pages_to_check"], ["https://x.com/contact", "https://x.com/about"])


class TestDomainRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_enforces_minimum_interval(self):
        limiter = es.DomainRateLimiter(min_interval=0.2)
        t0 = time.monotonic()
        await limiter.wait()
        await limiter.wait()
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.2)

    async def test_first_wait_does_not_block(self):
        limiter = es.DomainRateLimiter(min_interval=5.0)
        t0 = time.monotonic()
        await limiter.wait()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)

    async def test_ensure_min_interval_widens_not_narrows(self):
        limiter = es.DomainRateLimiter(min_interval=1.0)
        limiter.ensure_min_interval(0.1)  # smaller -- must not narrow
        self.assertEqual(limiter.min_interval, 1.0)
        limiter.ensure_min_interval(5.0)  # larger -- must widen
        self.assertEqual(limiter.min_interval, 5.0)

    async def test_concurrent_waiters_still_serialize(self):
        # Three concurrent callers on the SAME limiter must still be spaced
        # out by min_interval cumulatively -- the lock across the await
        # point inside wait() is what prevents them from all reading a
        # stale last_request_time and proceeding together.
        limiter = es.DomainRateLimiter(min_interval=0.15)
        t0 = time.monotonic()
        await asyncio.gather(limiter.wait(), limiter.wait(), limiter.wait())
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.15 * 2)


class TestPickUserAgent(unittest.TestCase):
    def test_always_returns_one_of_the_known_agents(self):
        for _ in range(20):
            self.assertIn(es._pick_user_agent(), es.USER_AGENTS)

    def test_more_than_one_agent_available(self):
        # The whole point of rotation is variety across requests.
        self.assertGreater(len(es.USER_AGENTS), 1)


class TestGetUserAgentForDomain(unittest.TestCase):
    """A single domain must see the same UA across every request in a
    crawl — switching UAs page-to-page within one domain's session is a
    pattern no real browser exhibits and is more suspicious than either a
    stable spoofed UA or a stable honest one."""

    def setUp(self):
        es._domain_user_agents.clear()

    def test_same_domain_gets_same_agent_every_call(self):
        first = es.get_user_agent_for_domain("https://x.com/a")
        second = es.get_user_agent_for_domain("https://x.com/b")
        third = es.get_user_agent_for_domain("https://www.x.com/c")  # www-stripped, same bare domain
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_different_domains_get_independent_cache_entries(self):
        es.get_user_agent_for_domain("https://a.com/")
        es.get_user_agent_for_domain("https://b.com/")
        self.assertEqual(set(es._domain_user_agents.keys()), {"a.com", "b.com"})


class TestGetDomainRateLimiter(unittest.TestCase):
    def test_same_bare_domain_returns_same_limiter(self):
        a = es.get_domain_rate_limiter("https://www.example-rl-test.com/foo")
        b = es.get_domain_rate_limiter("https://example-rl-test.com/bar")
        self.assertIs(a, b)

    def test_different_domains_get_different_limiters(self):
        a = es.get_domain_rate_limiter("https://one-rl-test.com/")
        b = es.get_domain_rate_limiter("https://two-rl-test.com/")
        self.assertIsNot(a, b)


class TestRobotsCrawlDelay(unittest.TestCase):
    def _policy(self, body, enabled=True):
        parser = robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        return es.RobotsPolicy(parser, [], enabled=enabled)

    def test_crawl_delay_present(self):
        p = self._policy("User-agent: *\nCrawl-delay: 7\nDisallow: /private/\n")
        self.assertEqual(p.crawl_delay(), 7.0)

    def test_crawl_delay_absent(self):
        p = self._policy("User-agent: *\nDisallow: /private/\n")
        self.assertIsNone(p.crawl_delay())

    def test_crawl_delay_none_when_disabled(self):
        p = self._policy("User-agent: *\nCrawl-delay: 7\n", enabled=False)
        self.assertIsNone(p.crawl_delay())

    def test_crawl_delay_none_when_no_parser(self):
        p = es.RobotsPolicy(None, [], enabled=True)
        self.assertIsNone(p.crawl_delay())


class TestExponentialBackoffWithJitter(unittest.TestCase):
    def test_within_bounds(self):
        for attempt in range(6):
            for _ in range(20):
                delay = es._exponential_backoff_with_jitter(attempt, base=1.0, cap=10.0)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, min(10.0, 1.0 * (2 ** attempt)))

    def test_capped_for_large_attempt(self):
        for _ in range(20):
            delay = es._exponential_backoff_with_jitter(attempt=10, base=1.0, cap=10.0)
            self.assertLessEqual(delay, 10.0)

    def test_produces_variation_not_a_fixed_value(self):
        # Full jitter means repeated calls at the same attempt number
        # shouldn't all return the identical value.
        samples = {es._exponential_backoff_with_jitter(3, base=1.0, cap=10.0) for _ in range(30)}
        self.assertGreater(len(samples), 1)


class TestFetchAndExtractPagesEarlyStop(unittest.IsolatedAsyncioTestCase):
    """Politeness, not just speed: once a high-confidence (top-ranked)
    candidate yields an email and the configured minimum has been checked,
    the remaining lower-ranked candidates must never be requested at all —
    not just have their results discarded."""

    async def test_stops_after_first_wave_when_email_found(self):
        requested = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            requested.append(url)
            if url == "https://x.com/p1":
                return FakeResp("<p>reach us at hello@x.com</p>")
            return FakeResp("<p>nothing here</p>")

        urls = [f"https://x.com/p{i}" for i in range(1, 7)]  # 6 candidates
        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch), \
                unittest.mock.patch.object(es, "PER_DOMAIN_FETCH_WORKERS", 4):
            emails, _sp, fetched_ok, _ff, *_ = await es.fetch_and_extract_pages(
                None, urls, robots, delay=0, min_pages_before_stop=3
            )

        self.assertEqual(emails, {"hello@x.com"})
        # Only the first wave (4 pages) should ever have been requested —
        # the remaining 2 (lower-ranked) candidates must not be fetched.
        self.assertEqual(len(requested), 4)
        self.assertNotIn("https://x.com/p5", requested)
        self.assertNotIn("https://x.com/p6", requested)

    async def test_respects_minimum_pages_before_stopping(self):
        # With a wave size of 1, an email on the very first page still
        # shouldn't stop the whole fetch if min_pages_before_stop requires
        # more pages to be checked first.
        requested = []

        class FakeResp:
            def __init__(self, text):
                self.text = text

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            requested.append(url)
            if url == "https://x.com/p1":
                return FakeResp("<p>reach us at hello@x.com</p>")
            return FakeResp("<p>nothing here</p>")

        urls = [f"https://x.com/p{i}" for i in range(1, 6)]
        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch), \
                unittest.mock.patch.object(es, "PER_DOMAIN_FETCH_WORKERS", 1):
            await es.fetch_and_extract_pages(None, urls, robots, delay=0, min_pages_before_stop=3)

        # Should stop right at the minimum (3 waves of 1), not continue to
        # p4/p5, and not stop after just p1 either.
        self.assertEqual(len(requested), 3)

    async def test_cancellation_during_stagger_sleep_cancels_orphaned_tasks(self):
        # Regression test: a real batch run surfaced "RuntimeError: Session
        # is closed" from tasks created via create_task() in the stagger
        # loop, before gather() was ever reached, when the per-domain hard
        # timeout cancelled fetch_and_extract_pages mid-stagger. Those tasks
        # were orphaned (never gathered, so gather's cancellation-propagation
        # never applied) and kept running after the caller's session closed.
        started = []
        cancelled = []

        async def fake_fetch(session, url, proxy=None, blocked_sink=None):
            started.append(url)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(url)
                raise
            return None

        urls = [f"https://x.com/p{i}" for i in range(1, 6)]
        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch), \
                unittest.mock.patch.object(es, "PER_DOMAIN_FETCH_WORKERS", 5):
            with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                await asyncio.wait_for(
                    es.fetch_and_extract_pages(None, urls, robots, delay=0.05),
                    timeout=0.12,
                )

        # Let the cancellation fully propagate before asserting.
        await asyncio.sleep(0.05)

        self.assertGreater(len(started), 0)
        self.assertEqual(set(started), set(cancelled))

    async def test_checks_all_pages_when_nothing_found(self):
        async def fake_fetch_empty(session, url, proxy=None, blocked_sink=None):
            class FakeResp:
                text = "<p>nothing here at all</p>"
            return FakeResp()

        urls = [f"https://x.com/p{i}" for i in range(1, 8)]
        robots = es.RobotsPolicy(None, [], enabled=False)
        with unittest.mock.patch.object(es, "fetch", fake_fetch_empty), \
                unittest.mock.patch.object(es, "PER_DOMAIN_FETCH_WORKERS", 4):
            emails, _sp, fetched_ok, _ff, *_ = await es.fetch_and_extract_pages(
                None, urls, robots, delay=0, min_pages_before_stop=3
            )

        self.assertEqual(emails, set())
        self.assertEqual(fetched_ok, 7)  # every candidate checked, none skipped


class TestFinalizeCsv(unittest.TestCase):
    """A domain touched by a retry or Playwright second pass gets a fresh
    writerow() call on top of its main-pass row — without a final collapse
    step, the CSV ends up with duplicate rows per domain. Confirmed for
    real against an 81-domain batch (4 duplicate rows, one per domain that
    went through the Playwright second pass)."""

    def _make_result(self, url, method, error, emails=None):
        r = es.DomainResult(input_url=url)
        r.domain = es.get_bare_domain(url)
        r.method = method
        r.error = error
        if emails:
            r.emails = set(emails)
        return r

    def test_collapses_duplicate_rows_to_latest_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            first_pass = self._make_result(
                "https://a.com", "sitemap", "no emails found on checked pages"
            )
            second_pass = self._make_result(
                "https://a.com", "sitemap",
                "no emails found on checked pages (incl. JS-rendered)",
            )
            other = self._make_result("https://b.com", "none", "domain unreachable")

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=es.CSV_FIELDNAMES)
                writer.writeheader()
                writer.writerow(es.result_to_row(first_pass))
                writer.writerow(es.result_to_row(other))
                writer.writerow(es.result_to_row(second_pass))  # duplicate for a.com

            es._finalize_csv(path, {"https://a.com": second_pass, "https://b.com": other})

            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 2)
        a_row = next(r for r in rows if r["input_url"] == "https://a.com")
        self.assertEqual(a_row["error"], "no emails found on checked pages (incl. JS-rendered)")

    def test_leaves_untouched_resume_rows_alone(self):
        # Rows for domains not in latest_result_by_url (carried over from a
        # prior --resume run) must survive the collapse unchanged.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            prior_run = self._make_result("https://old.com", "sitemap", "", emails=["hi@old.com"])

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=es.CSV_FIELDNAMES)
                writer.writeheader()
                writer.writerow(es.result_to_row(prior_run))

            es._finalize_csv(path, {})  # this run touched nothing from the prior file

            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_url"], "https://old.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
