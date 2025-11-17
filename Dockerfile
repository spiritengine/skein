# SKEIN Server Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY skein/ ./skein/
COPY client/ ./client/
COPY skein_server.py .
COPY config/ ./config/

# Create data directory
RUN mkdir -p /app/.skein/data

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV SKEIN_HOST=0.0.0.0
ENV SKEIN_PORT=8001

# Expose port
EXPOSE 8001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

# Run the server
CMD ["python", "skein_server.py"]
