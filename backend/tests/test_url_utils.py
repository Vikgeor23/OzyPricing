"""Tests for URL/domain normalization."""

import unittest

from app.utils.url_utils import (
    TECHNOPOLIS_DEFAULT_START_URL,
    TECHNOPOLIS_DOMAIN,
    is_technopolis,
    normalize_domain,
    normalize_url,
    technopolis_category_start_url,
)


class UrlUtilsTests(unittest.TestCase):
    def test_normalize_domain_bare(self) -> None:
        self.assertEqual(normalize_domain("technopolis.bg"), TECHNOPOLIS_DOMAIN)

    def test_normalize_domain_www(self) -> None:
        self.assertEqual(normalize_domain("www.technopolis.bg"), TECHNOPOLIS_DOMAIN)

    def test_normalize_domain_full_url_with_path(self) -> None:
        self.assertEqual(normalize_domain("https://www.technopolis.bg/bg/"), TECHNOPOLIS_DOMAIN)

    def test_normalize_domain_https_no_www(self) -> None:
        self.assertEqual(normalize_domain("https://technopolis.bg/bg/"), TECHNOPOLIS_DOMAIN)

    def test_is_technopolis_variants(self) -> None:
        self.assertTrue(is_technopolis("technopolis.bg"))
        self.assertTrue(is_technopolis("www.technopolis.bg"))
        self.assertTrue(is_technopolis("https://www.technopolis.bg/bg/"))
        self.assertTrue(is_technopolis("https://technopolis.bg/bg/"))
        self.assertFalse(is_technopolis("emag.bg"))

    def test_normalize_url_adds_scheme(self) -> None:
        self.assertEqual(normalize_url("www.technopolis.bg/bg/"), "https://www.technopolis.bg/bg/")

    def test_technopolis_start_url_defaults(self) -> None:
        self.assertEqual(technopolis_category_start_url(None), TECHNOPOLIS_DEFAULT_START_URL)
        self.assertEqual(technopolis_category_start_url("technopolis.bg"), TECHNOPOLIS_DEFAULT_START_URL)
        self.assertEqual(technopolis_category_start_url(TECHNOPOLIS_DOMAIN), TECHNOPOLIS_DEFAULT_START_URL)

    def test_technopolis_start_url_explicit_path(self) -> None:
        self.assertEqual(
            technopolis_category_start_url("https://www.technopolis.bg/bg/"),
            "https://www.technopolis.bg/bg/",
        )
        self.assertEqual(
            technopolis_category_start_url("https://technopolis.bg/bg/"),
            "https://technopolis.bg/bg/",
        )


if __name__ == "__main__":
    unittest.main()
