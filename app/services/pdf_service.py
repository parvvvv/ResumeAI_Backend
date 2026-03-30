"""
PDF generation service using WeasyPrint and Jinja2 templates.
WeasyPrint requires native libraries (pango, gobject).
Import is lazy to avoid blocking app startup if libs are not available.

Includes adaptive font-shrinking to guarantee single-page output.
"""

import os
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
import structlog

logger = structlog.get_logger()

# Ensure Homebrew libraries are discoverable on macOS
_homebrew_lib = "/opt/homebrew/lib"
if os.path.isdir(_homebrew_lib):
    _current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    if _homebrew_lib not in _current:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = f"{_homebrew_lib}:{_current}" if _current else _homebrew_lib

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _get_weasyprint():
    """Lazy import of WeasyPrint to avoid import-time failure."""
    try:
        import weasyprint
        return weasyprint
    except (OSError, ImportError) as e:
        logger.error("weasyprint_unavailable", error=str(e))
        raise RuntimeError(
            "WeasyPrint is not available. Install native dependencies: "
            "brew install pango glib gobject-introspection"
        ) from e


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
    result = re.sub(r'([\d.]+)pt', scale_pt, html_content)
    # Scale px values for margins/padding (only in <style> block)
    style_match = re.search(r'(<style>)(.*?)(</style>)', result, re.DOTALL)
    if style_match:
        style_content = style_match.group(2)
        scaled_style = re.sub(r'([\d.]+)px', scale_px, style_content)
        result = result[:style_match.start(2)] + scaled_style + result[style_match.end(2):]

    return result


async def generate_pdf(resume_data: dict, template_name: str = "modern") -> bytes:
    """
    Render resume data into an HTML template and convert to PDF bytes.
    If the result exceeds 1 page, progressively shrink font sizes until
    it fits on a single page.

    Args:
        resume_data: Dictionary matching the ResumeData schema.
        template_name: Template to use ('modern' or 'ats').

    Returns:
        PDF file contents as bytes.
    """
    template_file = f"{template_name}.html"
    template = _jinja_env.get_template(template_file)

    html_content = template.render(resume=resume_data)

    logger.info("pdf_generating", template=template_name)

    weasyprint = _get_weasyprint()

    # Try rendering, shrink if more than 1 page
    # Start at 100% scale, shrink by 5% each iteration, minimum 70%
    scale = 1.0
    min_scale = 0.70
    step = 0.05
    attempt = 0

    while scale >= min_scale:
        attempt += 1
        if scale < 1.0:
            current_html = _shrink_font_in_html(html_content, scale)
            logger.info("pdf_shrinking", scale=round(scale, 2), attempt=attempt)
        else:
            current_html = html_content

        doc = weasyprint.HTML(
            string=current_html, base_url=str(_TEMPLATES_DIR)
        ).render()

        page_count = len(doc.pages)

        if page_count <= 1:
            pdf_bytes = doc.write_pdf()
            logger.info(
                "pdf_generated",
                size_kb=len(pdf_bytes) // 1024,
                pages=page_count,
                final_scale=round(scale, 2),
            )
            return pdf_bytes

        # Too many pages — shrink and retry
        logger.warning("pdf_overflow", pages=page_count, scale=round(scale, 2))
        scale -= step

    # If we hit minimum scale and still > 1 page, just render at min scale
    # (the CSS overflow:hidden will clip the rest)
    logger.warning("pdf_min_scale_reached", scale=round(min_scale, 2))
    final_html = _shrink_font_in_html(html_content, min_scale)
    pdf_bytes = weasyprint.HTML(
        string=final_html, base_url=str(_TEMPLATES_DIR)
    ).write_pdf()

    logger.info("pdf_generated_clipped", size_kb=len(pdf_bytes) // 1024)
    return pdf_bytes
