"""
Security utilities: input sanitization, file validation, security headers, request ID.
"""

import re
import uuid
from fastapi import UploadFile, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from jose import JWTError
import bleach
import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

_ALLOWED_TAGS: list[str] = []  # Strip ALL HTML tags
_ALLOWED_ATTRIBUTES: dict = {}


def sanitize_input(text: str) -> str:
    """Strip all HTML/JS from user-provided text."""
    if not text:
        return text
    return bleach.clean(text, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, strip=True)


# ---------------------------------------------------------------------------
# Template HTML sanitization (whitelist-based)
# ---------------------------------------------------------------------------

_TEMPLATE_ALLOWED_TAGS = [
    # Structure
    "html", "head", "body", "div", "span", "section", "header", "footer",
    "main", "article", "aside", "nav",
    # Text
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i",
    "u", "small", "br", "hr", "blockquote", "pre", "code",
    # Lists
    "ul", "ol", "li",
    # Tables
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    # Media (images only — no video/audio/embed)
    "img",
    # Links
    "a",
    # Inline styling (allowed, but content is further sanitized)
    "style",
    # Meta (needed for charset/viewport in templates)
    "meta", "title",
    # Font faces
    "link",
]

_TEMPLATE_ALLOWED_ATTRIBUTES = {
    "*": ["class", "id", "style", "data-section", "data-field"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
    "meta": ["charset", "name", "content"],
    "link": ["rel", "href", "type"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "col": ["span"],
    "colgroup": ["span"],
}

# Patterns to strip from <style> blocks and inline styles
_DANGEROUS_CSS_PATTERNS = [
    re.compile(r'@import\s', re.IGNORECASE),
    re.compile(r'url\s*\(\s*["\']?https?://', re.IGNORECASE),
    re.compile(r'expression\s*\(', re.IGNORECASE),
    re.compile(r'javascript\s*:', re.IGNORECASE),
    re.compile(r'-moz-binding', re.IGNORECASE),
    re.compile(r'behavior\s*:', re.IGNORECASE),
]

# JS event attributes to strip
_JS_EVENT_ATTRS = re.compile(
    r'\s+on\w+\s*=\s*["\'][^"\']*["\']',
    re.IGNORECASE,
)

# Blocked tags that should be completely removed including content
_BLOCKED_TAGS_WITH_CONTENT = re.compile(
    r'<\s*(script|iframe|object|embed|form|input|textarea|button|select|applet)\b[^>]*>.*?</\s*\1\s*>',
    re.IGNORECASE | re.DOTALL,
)

_BLOCKED_SELF_CLOSING = re.compile(
    r'<\s*(script|iframe|object|embed|form|input|textarea|button|select|applet)\b[^>]*/?\s*>',
    re.IGNORECASE,
)

# External resource references in link tags (only allow fonts/local)
_EXTERNAL_LINK_PATTERN = re.compile(
    r'<link\b[^>]*href\s*=\s*["\']https?://[^"\']*["\'][^>]*/?\s*>',
    re.IGNORECASE,
)


def sanitize_template_html(raw_html: str) -> tuple[str, list[str]]:
    """
    Sanitize user-uploaded template HTML using a whitelist approach.
    Returns (sanitized_html, warnings[]).
    Blocks: script, iframe, JS events, external JS, dangerous CSS.
    Preserves: Jinja2 template syntax ({{ }}, {% %}).
    """
    warnings: list[str] = []

    if not raw_html or not raw_html.strip():
        return "", ["Empty HTML provided."]

    html = raw_html

    # 1. Strip blocked tags with their content
    blocked_found = _BLOCKED_TAGS_WITH_CONTENT.findall(html)
    if blocked_found:
        tags_found = list(set(t.lower() for t in blocked_found))
        warnings.append(f"Removed blocked tags: {', '.join(tags_found)}")
        html = _BLOCKED_TAGS_WITH_CONTENT.sub("", html)

    html = _BLOCKED_SELF_CLOSING.sub("", html)

    # 2. Strip JS event attributes (onclick, onerror, onload, etc.)
    js_events = _JS_EVENT_ATTRS.findall(html)
    if js_events:
        warnings.append(f"Removed {len(js_events)} JavaScript event attribute(s).")
        html = _JS_EVENT_ATTRS.sub("", html)

    # 3. Strip external link tags (keep local/relative font refs)
    external_links = _EXTERNAL_LINK_PATTERN.findall(html)
    if external_links:
        warnings.append(f"Removed {len(external_links)} external resource link(s).")
        html = _EXTERNAL_LINK_PATTERN.sub("", html)

    # 4. Sanitize CSS in <style> blocks
    def _sanitize_style_block(match: re.Match) -> str:
        style_content = match.group(0)
        for pattern in _DANGEROUS_CSS_PATTERNS:
            if pattern.search(style_content):
                warnings.append(f"Removed dangerous CSS pattern from <style> block.")
                style_content = pattern.sub("/* removed */", style_content)
        return style_content

    html = re.sub(
        r'<style[^>]*>.*?</style>',
        _sanitize_style_block,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 5. Sanitize inline style attributes
    def _sanitize_inline_style(match: re.Match) -> str:
        style_val = match.group(1)
        for pattern in _DANGEROUS_CSS_PATTERNS:
            if pattern.search(style_val):
                warnings.append("Removed dangerous CSS from inline style.")
                style_val = pattern.sub("/* removed */", style_val)
        return f'style="{style_val}"'

    html = re.sub(
        r'style\s*=\s*"([^"]*)"',
        _sanitize_inline_style,
        html,
        flags=re.IGNORECASE,
    )

    return html, warnings


# ---------------------------------------------------------------------------
# Jinja template safety validation
# ---------------------------------------------------------------------------

# Allowed Jinja variable patterns — only resume.*, extras.*, templateMeta.*
# Allowed Jinja variable patterns — resume.*, extras.*, templateMeta.*, loop.*,
# and any loop iteration variable (detected dynamically from {% for %} blocks).
_SAFE_JINJA_VAR_BASE = re.compile(
    r'\{\{\s*'
    r'(?:'
    r'resume\.\w[\w.]*'          # {{ resume.personalInfo.fullName }}
    r'|extras\.\w[\w.]*'         # {{ extras.tagline }}
    r'|templateMeta\.\w[\w.]*'   # {{ templateMeta.title }}
    r'|loop\.\w+'                # {{ loop.index }}
    r')'
    r'(?:\s*\|\s*[\w]+(?:\([^)]*\))?)*'  # Jinja filters like | join(", ")
    r'\s*\}\}'
)

# Regex to extract loop variable names from {% for VAR in ... %}
_FOR_LOOP_VAR = re.compile(r'\{%[-\s]+for\s+(\w+)\s+in\s+')

# Allowed Jinja block patterns
_SAFE_JINJA_BLOCK = re.compile(
    r'\{%[-\s]+'
    r'(?:'
    r'if\s+'                     # {% if ... %}
    r'|elif\s+'                  # {% elif ... %}
    r'|else\s*'                  # {% else %}
    r'|endif\s*'                 # {% endif %}
    r'|for\s+\w+\s+in\s+'       # {% for x in ... %}
    r'|endfor\s*'                # {% endfor %}
    r')'
    r'.*?'
    r'[-\s]+%\}'
)

# Dangerous patterns that indicate SSTI attempts
_DANGEROUS_JINJA_PATTERNS = [
    re.compile(r'\{\{.*__\w+__.*\}\}'),                    # {{ x.__class__ }}
    re.compile(r'\{%\s*set\s', re.IGNORECASE),             # {% set %}
    re.compile(r'\{%\s*import\s', re.IGNORECASE),          # {% import %}
    re.compile(r'\{%\s*include\s', re.IGNORECASE),         # {% include %}
    re.compile(r'\{%\s*extends\s', re.IGNORECASE),         # {% extends %}
    re.compile(r'\{%\s*macro\s', re.IGNORECASE),           # {% macro %}
    re.compile(r'\{\{\s*config\b', re.IGNORECASE),         # {{ config }}
    re.compile(r'\{\{\s*self\b', re.IGNORECASE),           # {{ self }}
    re.compile(r'\{\{\s*lipsum\b', re.IGNORECASE),         # {{ lipsum }}
    re.compile(r'\{\{\s*cycler\b', re.IGNORECASE),         # {{ cycler }}
    re.compile(r'\{\{\s*joiner\b', re.IGNORECASE),         # {{ joiner }}
    re.compile(r'\{\{\s*namespace\b', re.IGNORECASE),      # {{ namespace }}
    re.compile(r'\{\{\s*request\b', re.IGNORECASE),        # {{ request }}
    re.compile(r'subprocess|popen|os\.', re.IGNORECASE),   # OS-level access
]


def _build_loop_var_pattern(loop_vars: set[str]) -> re.Pattern:
    """Build a regex that matches {{ loopVar }} or {{ loopVar.prop.sub }}
    for any loop iteration variable found in the template."""
    if not loop_vars:
        return None
    escaped = '|'.join(re.escape(v) for v in loop_vars)
    return re.compile(
        r'\{\{\s*'
        r'(?:' + escaped + r')'
        r'(?:\.[\w]+)*'                          # optional .prop.sub chaining
        r'(?:\s*\|\s*[\w]+(?:\([^)]*\))?)*'      # optional | filter(args)
        r'\s*\}\}'
    )


def validate_jinja_safety(html_content: str) -> tuple[bool, list[str]]:
    """
    Validate that a template's Jinja expressions are safe.
    Allows: {{ resume.* }}, {{ extras.* }}, {{ templateMeta.* }},
    {{ loop.* }}, and any loop iteration variable from {% for %} blocks.
    Returns (is_safe, violations[]).
    """
    violations: list[str] = []

    # Check for dangerous patterns first
    for pattern in _DANGEROUS_JINJA_PATTERNS:
        matches = pattern.findall(html_content)
        if matches:
            violations.append(f"Dangerous Jinja pattern detected: {matches[0][:80]}")

    # Extract loop variable names from {% for VAR in ... %} blocks
    loop_vars = set(_FOR_LOOP_VAR.findall(html_content))
    loop_var_pattern = _build_loop_var_pattern(loop_vars)

    # Extract all {{ ... }} expressions and verify each is safe
    all_vars = re.findall(r'\{\{.*?\}\}', html_content, re.DOTALL)
    for var_expr in all_vars:
        stripped = var_expr.strip()
        # Check base patterns (resume.*, extras.*, templateMeta.*, loop.*)
        if _SAFE_JINJA_VAR_BASE.match(stripped):
            continue
        # Check loop iteration variables (exp.role, item, point, etc.)
        if loop_var_pattern and loop_var_pattern.match(stripped):
            continue
        violations.append(f"Unsafe Jinja variable: {stripped[:80]}")

    # Extract all {% ... %} blocks and verify each is safe
    all_blocks = re.findall(r'\{%.*?%\}', html_content, re.DOTALL)
    for block_expr in all_blocks:
        if not _SAFE_JINJA_BLOCK.match(block_expr.strip()):
            violations.append(f"Unsafe Jinja block: {block_expr.strip()[:80]}")

    return len(violations) == 0, violations


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------

_ALLOWED_MIME_TYPES = {"application/pdf"}
_MAX_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

_TEMPLATE_HTML_MIME_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
_MAX_TEMPLATE_HTML_BYTES = 500 * 1024  # 500 KB

_TEMPLATE_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_MAX_TEMPLATE_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


async def validate_pdf_upload(file: UploadFile) -> bytes:
    """
    Validate an uploaded file is a PDF within size limits.
    Returns the file contents as bytes.
    """
    # Check MIME type
    if file.content_type not in _ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type '{file.content_type}'. Only PDF files are accepted.",
        )

    # Read and check size
    content = await file.read()
    if len(content) > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {_MAX_SIZE_BYTES // (1024 * 1024)} MB.",
        )

    return content


async def validate_template_html_upload(file: UploadFile) -> str:
    """
    Validate an uploaded HTML file for template generation.
    Returns the HTML content as a string.
    """
    if file.content_type not in _TEMPLATE_HTML_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type '{file.content_type}'. HTML or text files are accepted.",
        )

    content = await file.read()
    if len(content) > _MAX_TEMPLATE_HTML_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {_MAX_TEMPLATE_HTML_BYTES // 1024} KB.",
        )

    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is not valid UTF-8 encoded text.",
        )


async def validate_template_image_upload(file: UploadFile) -> bytes:
    """
    Validate an uploaded image file for template generation.
    Returns the image content as bytes.
    """
    if file.content_type not in _TEMPLATE_IMAGE_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type '{file.content_type}'. PNG, JPEG, or WebP images are accepted.",
        )

    content = await file.read()
    if len(content) > _MAX_TEMPLATE_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {_MAX_TEMPLATE_IMAGE_BYTES // (1024 * 1024)} MB.",
        )

    return content


# ---------------------------------------------------------------------------
# Security Headers Middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
        return response


class AuthContextMiddleware(BaseHTTPMiddleware):
    """Attach authenticated user context when a valid bearer token is present."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :].strip()
            if token:
                from app.services.auth_service import decode_jwt

                try:
                    payload = decode_jwt(token)
                    request.state.user_id = payload.get("sub")
                except JWTError:
                    request.state.user_id = None
        else:
            request.state.user_id = None

        return await call_next(request)


# ---------------------------------------------------------------------------
# Request ID Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request for tracing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        # Bind to structlog context for this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        logger.info(
            "request_started",
            method=request.method,
            path=str(request.url.path),
        )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "request_completed",
            status_code=response.status_code,
        )
        return response
