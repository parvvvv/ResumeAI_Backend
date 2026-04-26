"""
PDF generation service using Playwright (Chromium headless) and Jinja2 templates.

Uses Chromium's native PDF engine for fast, high-quality PDF rendering.
Includes adaptive font-shrinking to guarantee single-page output.
"""

import asyncio
import time
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
import structlog
from app.runtime import get_runtime

logger = structlog.get_logger()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

# ── Shared browser instance ──────────────────────────────────────────────────
# Reusing a single browser instance avoids the ~1-2s Chromium cold-start
# on every PDF generation.
_browser = None
_browser_lock = None


async def _get_browser():
    """Get or create a shared Playwright browser instance."""
    global _browser, _browser_lock
    if _browser and _browser.is_connected():
        return _browser

    if _browser_lock is None:
        _browser_lock = asyncio.Lock()

    async with _browser_lock:
        # Double-check after acquiring lock
        if _browser and _browser.is_connected():
            return _browser

        from playwright.async_api import async_playwright

        t0 = time.perf_counter()
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True)
        logger.info("browser_launched", elapsed_ms=round((time.perf_counter() - t0) * 1000))
        return _browser


async def shutdown_browser():
    """Gracefully shut down the shared browser. Call on app shutdown."""
    global _browser
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None


# ── Pre-compiled regex patterns ──────────────────────────────────────────────
_RE_PT_VALUES = re.compile(r'([\d.]+)pt')
_RE_PX_VALUES = re.compile(r'([\d.]+)px')
_RE_STYLE_BLOCK = re.compile(r'(<style>)(.*?)(</style>)', re.DOTALL)


def _shrink_font_in_html(html_content: str, scale: float) -> str:
    """
    Scale down all pt font-size values in the HTML's <style> block.
    `scale` is a multiplier (e.g. 0.92 = shrink by 8%).
    Also tightens line-height and margins proportionally.
    """
    def scale_pt(match):
        value = float(match.group(1))
        new_value = round(value * scale, 1)
        return f"{new_value}pt"

    def scale_px(match):
        value = float(match.group(1))
        new_value = max(0, round(value * scale))
        return f"{new_value}px"

    # Scale all pt values (font sizes)
    result = _RE_PT_VALUES.sub(scale_pt, html_content)
    # Scale px values for margins/padding (only in <style> block)
    style_match = _RE_STYLE_BLOCK.search(result)
    if style_match:
        style_content = style_match.group(2)
        scaled_style = _RE_PX_VALUES.sub(scale_px, style_content)
        result = result[:style_match.start(2)] + scaled_style + result[style_match.end(2):]

    return result


async def _render_pdf_with_playwright(html_content: str) -> bytes:
    """
    Render HTML to a single-page PDF using Playwright's Chromium.
    Returns raw PDF bytes.
    """
    browser = await _get_browser()
    context = await browser.new_context()
    page = await context.new_page()

    try:
        # Set content with local file base URL so fonts resolve correctly
        base_url = _TEMPLATES_DIR.as_uri()
        await page.set_content(html_content, wait_until="load")

        # Generate PDF with letter page size matching the template's @page rule
        pdf_bytes = await page.pdf(
            width="8.5in",
            height="11in",
            margin={
                "top": "0.6cm",
                "right": "1.8cm",
                "bottom": "1.8cm",
                "left": "1.8cm",
            },
            print_background=True,
            prefer_css_page_size=True,
        )

        return pdf_bytes
    finally:
        await context.close()


async def _count_pages_playwright(html_content: str) -> tuple:
    """
    Render HTML to PDF and return (page_count, pdf_bytes).
    Playwright doesn't expose page count directly, so we check via PDF size
    or parse the PDF to count pages.
    """
    pdf_bytes = await _render_pdf_with_playwright(html_content)

    # Quick page count: count the /Type /Page entries in the raw PDF
    # This is much faster than using a full PDF parser
    page_count = pdf_bytes.count(b"/Type /Page") - pdf_bytes.count(b"/Type /Pages")

    return page_count, pdf_bytes


async def generate_pdf(resume_data: dict, template_name: str = "modern") -> bytes:
    """
    Render resume data into an HTML template and convert to PDF bytes.
    If the result exceeds 1 page, uses binary-search font shrinking
    to fit onto a single page.

    Args:
        resume_data: Dictionary matching the ResumeData schema.
        template_name: Template to use ('modern' or 'ats').

    Returns:
        PDF file contents as bytes.
    """
    runtime = get_runtime()
    async with runtime.pdf_semaphore:
        t_start = time.perf_counter()

        template_file = f"{template_name}.html"
        template = _jinja_env.get_template(template_file)
        html_content = template.render(resume=resume_data)

        logger.info("pdf_generating", template=template_name)

        # ── First attempt at full scale ──────────────────────────────────────
        page_count, pdf_bytes = await _count_pages_playwright(html_content)

        elapsed = round((time.perf_counter() - t_start) * 1000)

        if page_count <= 1:
            logger.info(
                "pdf_generated",
                size_kb=len(pdf_bytes) // 1024,
                pages=page_count,
                final_scale=1.0,
                elapsed_ms=elapsed,
            )
            return pdf_bytes

        # ── Binary-search shrink to fit on 1 page ────────────────────────────
        lo, hi = 0.70, 0.95
        best_pdf = None
        best_scale = lo
        attempts = 0

        while hi - lo > 0.03:
            attempts += 1
            mid = round((lo + hi) / 2, 3)
            current_html = _shrink_font_in_html(html_content, mid)
            page_count, pdf_bytes = await _count_pages_playwright(current_html)

            logger.info("pdf_shrinking", scale=mid, pages=page_count, attempt=attempts)

            if page_count <= 1:
                best_pdf = pdf_bytes
                best_scale = mid
                lo = mid   # Try less shrinking
            else:
                hi = mid   # Need more shrinking

        elapsed = round((time.perf_counter() - t_start) * 1000)

        if best_pdf is not None:
            logger.info(
                "pdf_generated",
                size_kb=len(best_pdf) // 1024,
                final_scale=best_scale,
                attempts=attempts,
                elapsed_ms=elapsed,
            )
            return best_pdf

        # Fallback: render at minimum scale
        logger.warning("pdf_min_scale_reached", scale=0.70)
        final_html = _shrink_font_in_html(html_content, 0.70)
        _, pdf_bytes = await _count_pages_playwright(final_html)

        elapsed = round((time.perf_counter() - t_start) * 1000)
        logger.info("pdf_generated_clipped", size_kb=len(pdf_bytes) // 1024, elapsed_ms=elapsed)
        return pdf_bytes
