"""
FastAPI application entry point.
Sets up CORS, middleware, rate limiting, and mounts all routers.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
import structlog

from app.config import settings
from app.database import connect_db, disconnect_db
from app.security import SecurityHeadersMiddleware, RequestIDMiddleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler, SlowAPIMiddleware
from app.routers import auth, resume, pdf, dashboard, notifications, jobs


# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("app_starting", mongo_uri=settings.MONGO_URI)
    await connect_db()
    logger.info("app_started", database=settings.MONGO_DB_NAME)
    yield
    await disconnect_db()
    logger.info("app_stopped")


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ResumeAI API",
    description="AI-powered resume parsing, tailoring, and PDF generation",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Middleware (order matters — outermost first)
# ---------------------------------------------------------------------------

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers
app.add_middleware(SecurityHeadersMiddleware)

# Request ID tracing
app.add_middleware(RequestIDMiddleware)

# Rate limiting (internal middleware)
app.add_middleware(SlowAPIMiddleware)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(resume.router)
app.include_router(pdf.router)
app.include_router(dashboard.router)
app.include_router(notifications.router)
app.include_router(jobs.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["Health"])
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok", "service": "resumeai"}
