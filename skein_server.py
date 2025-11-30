#!/usr/bin/env python3
"""
SKEIN Server - Structured Knowledge Exchange & Integration Nexus

Standalone server for inter-agent collaboration.
"""

import logging
import os
import json
import uuid
import contextvars
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from skein.routes import router as skein_router

# Context variable for request ID - accessible throughout the request lifecycle
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def get_config():
    """Load configuration from environment variables and config file."""
    config = {
        "host": "0.0.0.0",
        "port": 8001,
        "log_level": "info"
    }

    # Try to load from config file
    config_file = Path(__file__).parent / "config" / "config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                file_config = json.load(f)
                if "server" in file_config:
                    config.update(file_config["server"])
        except Exception:
            pass

    # Environment variables take precedence
    if os.getenv("SKEIN_HOST"):
        config["host"] = os.getenv("SKEIN_HOST")
    if os.getenv("SKEIN_PORT"):
        config["port"] = int(os.getenv("SKEIN_PORT"))
    if os.getenv("SKEIN_LOG_LEVEL"):
        config["log_level"] = os.getenv("SKEIN_LOG_LEVEL")

    return config


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add request ID tracking to all API calls.

    - Uses X-Request-ID header if provided by client
    - Otherwise generates a new UUID
    - Sets request ID in context var for use in logging
    - Returns X-Request-ID header in response
    """

    async def dispatch(self, request: Request, call_next):
        # Get request ID from header or generate new one
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Store in context var for access throughout request
        request_id_var.set(request_id)

        # Also attach to request state for easy access in handlers
        request.state.request_id = request_id

        # Log the incoming request with request ID
        logger.info(f"[{request_id}] {request.method} {request.url.path}")

        # Process the request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        return response


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="SKEIN API",
    description="Structured Knowledge Exchange & Integration Nexus - Agent collaboration infrastructure",
    version="0.2.0"
)

# Global exception handler for unhandled errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all exception handler to prevent 500 errors from crashing requests.
    Logs full stack trace and returns structured error response with request ID.
    """
    request_id = getattr(request.state, "request_id", None) or request_id_var.get() or "unknown"

    logger.error(
        f"[{request_id}] Unhandled exception on {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        exc_info=True
    )

    response = JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path,
            "request_id": request_id
        }
    )
    response.headers["X-Request-ID"] = request_id
    return response

# Request ID middleware - must be added before CORS so it runs first
app.add_middleware(RequestIDMiddleware)

# CORS middleware (allow all origins for development)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],  # Expose request ID header to clients
)

# Include SKEIN routes
app.include_router(skein_router, prefix="/skein", tags=["skein"])


@app.get("/")
async def root():
    """Root endpoint - API info."""
    return {
        "name": "SKEIN API",
        "version": "0.2.0",
        "description": "Structured Knowledge Exchange & Integration Nexus",
        "docs": "/docs"
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    config = get_config()

    logger.info("=" * 80)
    logger.info("🧵 Starting SKEIN Server")
    logger.info("=" * 80)
    logger.info(f"Host: {config['host']}")
    logger.info(f"Port: {config['port']}")
    logger.info(f"Docs: http://localhost:{config['port']}/docs")
    logger.info("=" * 80)

    uvicorn.run(
        app,
        host=config["host"],
        port=config["port"],
        log_level=config["log_level"]
    )
