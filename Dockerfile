# ──────────────────────────────────────────────
# Tenant Management System – Dockerfile
# Base: Python 3.12 slim (smaller image)
# ──────────────────────────────────────────────
FROM python:3.12-slim

# Metadata
LABEL maintainer="Tenant Management System"
LABEL description="FastAPI + MySQL Tenant Management Backend"

# ──────────────────────────────────────────────
# System dependencies
# default-libmysqlclient-dev  → needed by some MySQL C extensions
# gcc                         → compiles bcrypt C extension
# curl                        → used in HEALTHCHECK
# ──────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    default-libmysqlclient-dev \
    pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ──────────────────────────────────────────────
# Working directory inside the container
# ──────────────────────────────────────────────
WORKDIR /app

# ──────────────────────────────────────────────
# Install Python dependencies first (layer caching)
# Only re-runs when requirements.txt changes
# ──────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ──────────────────────────────────────────────
# Copy application source files
# ──────────────────────────────────────────────
COPY app.py .
COPY db_config.py .
COPY log.py .
COPY create_tables.py .

# ──────────────────────────────────────────────
# Create logs directory (log.py will also auto-create it,
# but pre-creating ensures correct ownership)
# ──────────────────────────────────────────────
RUN mkdir -p /app/logs

# ──────────────────────────────────────────────
# Environment variables – override at runtime via
# docker run -e or docker-compose environment block
# ──────────────────────────────────────────────
ENV DB_HOST=host.docker.internal \
    DB_PORT=3306 \
    DB_NAME=tenant_management \
    DB_USER=root \
    DB_PASSWORD=root \
    JWT_SECRET=CHANGE_ME_IN_PRODUCTION_SECRET_KEY \
    JWT_ALGORITHM=HS256 \
    JWT_EXPIRE_MINUTES=1440 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ──────────────────────────────────────────────
# Expose FastAPI port
# ──────────────────────────────────────────────
EXPOSE 8000

# ──────────────────────────────────────────────
# Health check – hits the /docs endpoint every 30s
# ──────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/docs || exit 1

# ──────────────────────────────────────────────
# Entrypoint script:
#   1. Run create_tables.py to migrate / seed DB
#   2. Start Uvicorn
# Using shell form so environment variables expand correctly
# ──────────────────────────────────────────────
CMD python create_tables.py && \
    uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
