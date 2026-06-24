"""``python -m skein.web`` -> serve the read surface on port 9001."""

from .app import run_server

if __name__ == "__main__":
    run_server()
