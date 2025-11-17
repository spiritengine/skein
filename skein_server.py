#!/usr/bin/env python3
"""
SKEIN Server - Structured Knowledge Exchange & Integration Nexus

Standalone server for inter-agent collaboration.
"""

import logging
import os
import json
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from skein.routes import router as skein_router


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
    Logs full stack trace and returns structured error response.
    """
    logger.error(
        f"Unhandled exception on {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        exc_info=True
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path
        }
    )

# CORS middleware (allow all origins for development)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
