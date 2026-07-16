"""Offline regression tests for email_scraper.

Every case here corresponds to a real bug or false-positive we hit during
development and fixed — the suite exists so those fixes can't silently
regress. All tests are deterministic and network-free (no live HTTP), so
they run fast and anywhere.

Run with either:
    python -m unittest test_email_scraper -v
    pytest test_email_scraper.py -v      (if pytest is installed)
"""

import unittest
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


class TestRankContactUrls(unittest.TestCase):
    def test_contact_page_over_blog(self):
        urls = [
            "https://x.com/blog/how-to-contact-support",
            "https://x.com/contact",
        ]
        picked = es.rank_contact_urls(urls, "https://x.com")
        self.assertIn("https://x.com/contact", picked)
        self.assertNotIn("https://x.com/blog/how-to-contact-support", picked)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
