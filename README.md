# Shop Electricity Bill Management System - Backend

## Files
- `dbconfig.py`  - MySQL connection pool (mysql-connector-python)
- `log.py`       - Centralized logger (`get_logger`)
- `main.py`      - All models, schemas, auth, and API routes
- `createMN.py`  - Stand-alone script that creates/updates all DB tables
- `requirements.txt`

## Setup
```bash
pip install -r requirements.txt
```

Edit `dbconfig.py` with your actual MySQL host/user/password/database before running.

## Run
```bash
# 1. Create all tables (only creates what's missing - safe to re-run)
python createMN.py

# 2. Start the API server
uvicorn main:app --reload
```

API docs available at: http://127.0.0.1:8000/docs

## Default admin (auto-created on first startup)
- username: `admin`
- password: `admin123`

## Notes
- `main.py` also runs `Base.metadata.create_all()` on startup as a safety net,
  so `createMN.py` is optional but recommended as the explicit/CI-friendly way
  to initialize the schema.
- Change `SECRET_KEY` in `main.py` before deploying to production.
- bcrypt is pinned to `4.0.1` in requirements.txt - newer bcrypt 4.1+ releases
  break passlib's backend auto-detection.
