"""PDF generation from history records, with dual backend support.

Backends:
    - weasyprint  Pure-Python CSS Paged Media renderer (precise typography).
                  Requires native libs on Windows (pango/cairo) — often hard to install.
    - chrome      Chrome headless via subprocess. Uses the user's installed Chrome.
                  Zero extra deps on machines that already have Chrome.

The "auto" mode tries weasyprint first, then falls back to chrome.
"""
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from src.output.html_standalone import generate_html_from_history

logger = logging.getLogger(__name__)


def generate_pdf_from_history(base_name: str, backend: str = "auto") -> bytes | None:
    """Render a history record to PDF bytes.

    Returns None if the record doesn't exist. Raises RuntimeError if all backends fail.
    """
    html = generate_html_from_history(base_name)
    if html is None:
        return None

    if backend == "auto":
        last_err = None
        for candidate in ("weasyprint", "chrome"):
            try:
                return _render(html, candidate)
            except Exception as e:
                logger.warning("PDF backend %s failed: %s", candidate, e)
                last_err = e
        raise RuntimeError(f"All PDF backends failed; last error: {last_err}")
    return _render(html, backend)


def _render(html: str, backend: str) -> bytes:
    if backend == "weasyprint":
        return _render_weasyprint(html)
    if backend == "chrome":
        return _render_chrome(html)
    raise ValueError(f"Unknown PDF backend: {backend}")


def _render_weasyprint(html: str) -> bytes:
    # Lazy import — module may not be installable on Windows.
    from weasyprint import HTML  # type: ignore[import-not-found]
    return HTML(string=html).write_pdf()


def _render_chrome(html: str) -> bytes:
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("Chrome / Chromium executable not found")
    with tempfile.TemporaryDirectory(prefix="va_pdf_") as td:
        html_path = Path(td) / "input.html"
        pdf_path = Path(td) / "output.pdf"
        html_path.write_text(html, encoding="utf-8")
        # --print-to-pdf wants a posix-style file:// URL even on Windows
        file_url = f"file:///{html_path.as_posix().lstrip('/')}"
        try:
            result = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-pdf-header-footer",
                    "--hide-scrollbars",
                    f"--print-to-pdf={pdf_path}",
                    file_url,
                ],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Chrome headless timed out after {e.timeout}s") from e
        if not pdf_path.exists():
            stderr = result.stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Chrome did not produce PDF (exit {result.returncode}): {stderr}")
        return pdf_path.read_bytes()


def _find_chrome() -> str | None:
    """Locate Chrome / Chromium executable cross-platform."""
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    return (
        shutil.which("chrome")
        or shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("msedge")
    )
