"""
SKEIN Web UI - HTMX-based browser interface.

Launch with: skein web
"""

from .app import create_app, run_server

__all__ = ["create_app", "run_server"]
