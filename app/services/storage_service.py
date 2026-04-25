"""
Storage service: abstracts file storage.
Uses Supabase Storage when configured, falls back to local disk.
"""

import uuid
from typing import Optional
import structlog
from app.config import settings
from app.runtime import run_blocking

logger = structlog.get_logger()

# Lazy-init Supabase client
_supabase_client = None


def _get_supabase():
    """Initialize Supabase client lazily."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        _supabase_client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _supabase_client


def is_supabase_configured() -> bool:
    """Check if Supabase credentials are provided."""
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


async def upload_pdf(pdf_bytes: bytes, filename: Optional[str] = None) -> str:
    """
    Upload a PDF and return its public URL.
    Uses Supabase if configured, otherwise saves to local disk.
    """
    if filename is None:
        filename = f"{uuid.uuid4().hex}.pdf"

    if is_supabase_configured():
        return await run_blocking(_upload_to_supabase, pdf_bytes, filename)
    return await run_blocking(_save_local, pdf_bytes, filename)


def _upload_to_supabase(pdf_bytes: bytes, filename: str) -> str:
    """Upload to Supabase Storage and return public URL."""
    client = _get_supabase()
    bucket = settings.SUPABASE_BUCKET

    # Upload the file
    client.storage.from_(bucket).upload(
        path=filename,
        file=pdf_bytes,
        file_options={"content-type": "application/pdf"},
    )

    # Get the public URL
    public_url = client.storage.from_(bucket).get_public_url(filename)

    logger.info(
        "pdf_uploaded_supabase",
        filename=filename,
        size_kb=len(pdf_bytes) // 1024,
    )

    return public_url


def _save_local(pdf_bytes: bytes, filename: str) -> str:
    """Save to local disk and return relative URL."""
    filepath = settings.PDF_DIR / filename
    filepath.write_bytes(pdf_bytes)

    logger.info(
        "pdf_saved_local",
        filename=filename,
        size_kb=len(pdf_bytes) // 1024,
    )

    return f"/api/resume/pdf/{filename}"


async def delete_pdf(pdf_url: str) -> None:
    """
    Delete a PDF by its URL.
    Handles both Supabase URLs and local paths.
    """
    if not pdf_url:
        return

    if is_supabase_configured() and "supabase" in pdf_url:
        await run_blocking(_delete_from_supabase, pdf_url)
    elif pdf_url.startswith("/api/resume/pdf/"):
        await run_blocking(_delete_local, pdf_url)


def _delete_from_supabase(pdf_url: str) -> None:
    """Delete from Supabase Storage."""
    try:
        client = _get_supabase()
        bucket = settings.SUPABASE_BUCKET

        # Extract filename from the public URL
        # URL format: https://xxx.supabase.co/storage/v1/object/public/bucket/filename.pdf
        filename = pdf_url.split("/")[-1]

        client.storage.from_(bucket).remove([filename])
        logger.info("pdf_deleted_supabase", filename=filename)
    except Exception as e:
        logger.warning("pdf_delete_supabase_failed", error=str(e), url=pdf_url)


def _delete_local(pdf_url: str) -> None:
    """Delete from local disk."""
    try:
        filename = pdf_url.split("/")[-1]
        filepath = settings.PDF_DIR / filename
        if filepath.exists():
            filepath.unlink()
            logger.info("pdf_deleted_local", filename=filename)
    except Exception as e:
        logger.warning("pdf_delete_local_failed", error=str(e), url=pdf_url)
