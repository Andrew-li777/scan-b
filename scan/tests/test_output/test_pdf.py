"""Tests for the PDF generation module.

Covers:
    - _find_chrome() platform paths and fallback to PATH
    - generate_pdf_from_history() returns None for non-existent records
    - Chrome backend end-to-end smoke test (skipped if Chrome not available)
    - Backend selection error for unknown backend

weasyprint is NOT tested here — it's an optional dependency that's often hard
to install on Windows. The auto path is exercised implicitly.
"""
import sys
from unittest.mock import patch

import pytest

from src.output.pdf import (
    _find_chrome,
    _render,
    generate_pdf_from_history,
)


class TestFindChrome:
    def test_returns_string_or_none(self):
        result = _find_chrome()
        assert result is None or isinstance(result, str)

    def test_finds_chrome_via_path_when_no_standard_install(self):
        # Mock all candidate paths to not exist, but PATH lookup succeeds.
        fake_chrome = "/some/path/chrome"
        with patch("src.output.pdf.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            with patch("src.output.pdf.shutil.which", side_effect=lambda n: fake_chrome if n == "chrome" else None):
                result = _find_chrome()
                assert result == fake_chrome

    def test_returns_none_when_nothing_found(self):
        with patch("src.output.pdf.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            with patch("src.output.pdf.shutil.which", return_value=None):
                assert _find_chrome() is None


class TestRenderDispatch:
    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown PDF backend"):
            _render("<html></html>", "no-such-engine")


class TestGenerateFromHistory:
    def test_missing_record_returns_none(self):
        # An obviously non-existent base_name should return None (not raise).
        assert generate_pdf_from_history("__definitely_not_a_real_record__xyz") is None

    def test_explicit_unknown_backend_propagates_error(self):
        # If we explicitly pick an unknown backend on an existing record, ValueError surfaces.
        # We mock generate_html_from_history to bypass the file-system check.
        with patch("src.output.pdf.generate_html_from_history", return_value="<html><body>hi</body></html>"):
            with pytest.raises(ValueError):
                generate_pdf_from_history("any", backend="bogus")


@pytest.mark.skipif(_find_chrome() is None, reason="Chrome/Chromium not installed")
class TestChromeBackendSmoke:
    """Smoke tests that actually invoke Chrome headless.
    Skipped on machines without Chrome (e.g., CI without a browser).
    """

    def test_chrome_renders_valid_pdf(self):
        from src.output.pdf import _render_chrome
        html = """<!DOCTYPE html><html><head><meta charset='utf-8'>
            <title>Test</title></head><body><h1>Hello 你好</h1></body></html>"""
        pdf = _render_chrome(html)
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF-"), f"Not a PDF (got: {pdf[:20]!r})"
        assert b"%%EOF" in pdf[-100:], "PDF trailer missing"
        assert len(pdf) > 500, f"PDF suspiciously small: {len(pdf)} bytes"
