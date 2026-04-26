"""
Application configuration.
Loads settings from environment variables with sensible defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from backend directory
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


class Settings:
    """Centralized application settings loaded from environment variables."""

    # --- Environment ---
    APP_ENV: str = os.getenv("APP_ENV", "local")

    # --- AI ---
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = "gemini-3-flash-preview"
    GEMINI_TIMEOUT_SECONDS: float = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "45"))
    SUPABASE_RPC_TIMEOUT_SECONDS: float = float(os.getenv("SUPABASE_RPC_TIMEOUT_SECONDS", "15"))
    JSEARCH_TIMEOUT_SECONDS: float = float(os.getenv("JSEARCH_TIMEOUT_SECONDS", "15"))
    BLOCKING_OPERATION_TIMEOUT_SECONDS: float = float(os.getenv("BLOCKING_OPERATION_TIMEOUT_SECONDS", "60"))

    # --- Database ---
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB_NAME: str = "resumeai"

    # --- Authentication ---
    JWT_SECRET: str = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "6"))
    JWT_REFRESH_EXPIRY_DAYS: int = int(os.getenv("JWT_REFRESH_EXPIRY_DAYS", "7"))
    BCRYPT_ROUNDS: int = 12
    MIN_PASSWORD_LENGTH: int = 8
    ADMIN_EMAILS: list = []

    # --- CORS ---
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")

    # --- File uploads ---
    MAX_UPLOAD_SIZE_MB: int = 5
    UPLOAD_DIR: Path = Path(__file__).resolve().parent.parent / "uploads"
    PDF_DIR: Path = UPLOAD_DIR / "pdfs"
    RESUME_UPLOAD_DIR: Path = UPLOAD_DIR / "resumes"

    # --- Supabase Storage ---
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")
    SUPABASE_BUCKET: str = os.getenv("SUPABASE_BUCKET", "resumes")

    # --- JSearch API (RapidAPI) ---
    JSEARCH_API_KEYS: list = []
    JSEARCH_HOST: str = "jsearch.p.rapidapi.com"

    # --- Rate limits ---
    RATE_LIMIT_AUTH: str = "5/minute"
    RATE_LIMIT_AI: str = "10/minute"
    RATE_LIMIT_PDF: str = "20/minute"
    RATE_LIMIT_GENERAL: str = "60/minute"
    RATE_LIMIT_JOBS: str = "5/minute"

    # --- Concurrency ---
    AI_PIPELINE_CONCURRENCY: int = int(os.getenv("AI_PIPELINE_CONCURRENCY", "2"))
    CHAT_CONCURRENCY: int = int(os.getenv("CHAT_CONCURRENCY", "4"))
    PDF_RENDER_CONCURRENCY: int = int(os.getenv("PDF_RENDER_CONCURRENCY", "2"))
    BLOCKING_IO_CONCURRENCY: int = int(os.getenv("BLOCKING_IO_CONCURRENCY", "4"))

    def __init__(self) -> None:
        # Ensure upload directories exist
        self.PDF_DIR.mkdir(parents=True, exist_ok=True)
        self.RESUME_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # Parse comma-separated JSearch API keys
        raw_keys = os.getenv("JSEARCH_API_KEYS", "")
        self.JSEARCH_API_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]

        # Parse comma-separated admin bootstrap emails
        raw_admin_emails = os.getenv("ADMIN_EMAILS", "")
        self.ADMIN_EMAILS = [
            email.strip().lower()
            for email in raw_admin_emails.split(",")
            if email.strip()
        ]

        # Warn about insecure defaults
        if self.JWT_SECRET == "CHANGE_ME_IN_PRODUCTION":
            import warnings
            warnings.warn(
                "JWT_SECRET is using the default value. "
                "Set a strong secret in the .env file for production.",
                stacklevel=2,
            )


settings = Settings()
