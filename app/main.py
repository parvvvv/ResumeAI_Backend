"""
FastAPI application entry point.
Sets up CORS, middleware, rate limiting, and mounts all routers.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
import structlog

from app.config import settings
from app.database import connect_db, disconnect_db, get_database
from app.runtime import get_runtime, init_runtime, shutdown_runtime
from app.security import SecurityHeadersMiddleware, RequestIDMiddleware, AuthContextMiddleware
from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler, SlowAPIMiddleware
from app.routers import auth, resume, pdf, dashboard, notifications, jobs, chat, admin

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
    logger.info("app_starting", environment=settings.APP_ENV)
    await connect_db()
    app.state.runtime = await init_runtime()
    logger.info("app_started", database=settings.MONGO_DB_NAME)
    yield
    # Shutdown: close shared Playwright browser
    from app.services.pdf_service import shutdown_browser
    await shutdown_browser()
    await shutdown_runtime()
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

# Auth context for user-aware rate limiting
app.add_middleware(AuthContextMiddleware)

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
app.include_router(chat.router)
app.include_router(admin.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["Health"])
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok", "service": "resumeai"}


@app.get("/api/health/live", tags=["Health"])
async def live_health_check():
    """Liveness probe: confirms the process is serving requests."""
    return {"status": "ok", "service": "resumeai", "check": "live"}


@app.get("/api/health/ready", tags=["Health"])
async def ready_health_check():
    """Readiness probe: confirms core app resources are initialized."""
    mongo_status = "unknown"
    runtime_status = "unknown"

    try:
        database = get_database()
        await database.command("ping")
        mongo_status = "ok"
    except Exception:
        mongo_status = "error"

    try:
        runtime = get_runtime()
        runtime_status = "ok" if runtime.http_client else "error"
    except Exception:
        runtime_status = "error"

    ready = mongo_status == "ok" and runtime_status == "ok"
    return {
        "status": "ok" if ready else "degraded",
        "service": "resumeai",
        "check": "ready",
        "dependencies": {
            "mongo": mongo_status,
            "runtime": runtime_status,
        },
    }
