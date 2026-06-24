# interskein web instance — the content-hash station read surface (port 9001).
# Separate from the legacy ./Dockerfile (which builds the frozen 8001 API server).
#
# Multistage: a builder compiles the venv, the runtime carries only the venv.
# Debian slim (glibc) so fastapi/uvicorn/pydantic-core/cryptography install as
# prebuilt manylinux wheels — no compiler in the image, fast reproducible builds.

# --- builder: resolve deps into a self-contained venv -----------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# Only what's needed to install the package; .dockerignore keeps the context
# lean (no .git, no stores, no worktrees).
COPY pyproject.toml ./
COPY skein ./skein

# knurl resolves from PyPI; the rest are manylinux wheels on glibc.
RUN pip install .

# --- runtime: just the venv + a non-root user ------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SKEIN_DATA_DIR=/data

COPY --from=builder /opt/venv /opt/venv

# Run unprivileged; the corpus is mounted read-only at /data.
RUN useradd --create-home --uid 10001 interskein
USER interskein

EXPOSE 9001

# The station data dir is a read-only volume mount at /data (a project's
# .skein). v0 is read-only, so the container never writes the corpus.
VOLUME ["/data"]

# Binds plainly on 9001; the reverse proxy terminates TLS for darkive.org /
# interskein.com and forwards here.
CMD ["skein", "--data-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "9001"]
