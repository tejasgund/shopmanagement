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
# Copy application source
#
# The backend is packages now, not a pile of loose modules, so this is one
# COPY per package instead of one per file - adding a module no longer means
# remembering to add a COPY line (the bug that caused
# "ModuleNotFoundError: No module named 'razorpay_service'" in an earlier
# image).
#
#   core/     config, database, logging, security
#   models/   SQLAlchemy models + schema creation
#   schemas/  Pydantic request/response models
#   services/ business logic (settings, meters, photos, payments, audit)
#   helpers/  small shared helpers used by routers
#   routers/  HTTP endpoints, one module per resource
# ──────────────────────────────────────────────
COPY app.py .
COPY core/     ./core/
COPY models/   ./models/
COPY schemas/  ./schemas/
COPY services/ ./services/
COPY helpers/  ./helpers/
COPY routers/  ./routers/

# The two scheduler scripts. Nothing in this image imports them and nothing
# here runs them: the container has no cron daemon on purpose. They are
# shipped so the HOST crontab can invoke them:
#   0 2 * * * docker exec <container> sh -c "cd /app/scheduler/auto_rent_generation && python auto_rent_generation.py"
# They need only pymysql (already installed above via requirements.txt), so
# they can equally be copied to a box of their own. See docs/SCHEDULER.md.
COPY scheduler/ ./scheduler/

# ──────────────────────────────────────────────
# Log directories. All are auto-created at runtime, but pre-creating ensures
# correct ownership - the schedulers may be invoked as a different user via
# docker exec, and each writes only into its own folder.
# ──────────────────────────────────────────────
RUN mkdir -p /app/logs \
    /app/scheduler/auto_rent_generation/logs \
    /app/scheduler/due_bill_penalty/logs

# ──────────────────────────────────────────────
# Environment variables – override at runtime via
# docker run -e or docker-compose environment block
# ──────────────────────────────────────────────
ENV DB_HOST=172.31.52.221 \
    DB_PORT=3306 \
#    DB_NAME=tenant_management \
    DB_USER=admin \
    DB_PASSWORD=admin \
    JWT_SECRET=CHANGE_ME_IN_PRODUCTION_SECRET_KEY \
    JWT_ALGORITHM=HS256 \
    JWT_EXPIRE_MINUTES=1440 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Kolkata

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
# Entrypoint:
#   1. python -m models.schema  → migrate / seed DB (was create_tables.py)
#   2. Start Uvicorn
# Using shell form so environment variables expand correctly
# ──────────────────────────────────────────────
CMD python -m models.schema && \
    uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2
