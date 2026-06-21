# ==============================================================================
# Dockerfile - Shop Electricity Bill Management System (FastAPI backend)
# ==============================================================================

FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
# (so log.py output shows up immediately in `docker logs`)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System packages needed to build mysql-connector-python's optional C
# extension and bcrypt on platforms without prebuilt wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (separate layer so code changes
# don't force a full dependency reinstall on every rebuild)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY dbconfig.py log.py main.py createMN.py ./

EXPOSE 8000

# Basic container health check against the FastAPI docs endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs')" || exit 1

# Create/verify all DB tables, then start the API server.
# (main.py also runs create_all() on startup as a safety net, so this is belt-and-braces.)
CMD ["sh", "-c", "python createMN.py && uvicorn main:app --host 0.0.0.0 --port 8000"]
