"""
Shared pytest fixtures.

Tests run against a throwaway SQLite database and a temporary photo folder, so
they never touch the real MySQL data and leave nothing behind. The DATABASE_URL
override is read by db_config.py; everything else in the app is untouched.
"""

import os
import tempfile

import pytest

# Must be set BEFORE db_config/app are imported for the first time.
_TMP_DB = os.path.join(tempfile.gettempdir(), "tms_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ.setdefault("JWT_SECRET", "test-secret-key")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy.orm import Session                 # noqa: E402

import app as app_module                           # noqa: E402
import settings_service                            # noqa: E402
from create_tables import (                        # noqa: E402
    Base, Meter, MeterTariff, Shop, User, UserShop, hash_password,
)
from db_config import SessionLocal, engine         # noqa: E402


@pytest.fixture(scope="function")
def db() -> Session:
    """A clean schema for every test - no state leaks between tests."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    settings_service.invalidate_cache()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def photo_dir(db):
    """
    Point photo storage at a throwaway folder for the duration of one test.

    Deliberately not pytest's tmp_path: the repo may live on a mounted/network
    filesystem where its cleanup misbehaves. A plain mkdtemp under the system
    temp dir is predictable everywhere.
    """
    import pathlib
    import shutil
    import tempfile as _tempfile

    target = pathlib.Path(_tempfile.mkdtemp(prefix="tms_photos_"))
    settings_service.set_many(db, {"meter.photo_storage_dir": str(target)})
    db.commit()
    settings_service.invalidate_cache()
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def client(db):
    return TestClient(app_module.app)


# ── Data builders ─────────────────────────────────────────────────────────

@pytest.fixture
def admin(db) -> User:
    user = User(
        name="Admin One", mobile="9000000001", email="admin1@test.com",
        password_hash=hash_password("admin123"), role="admin", is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def tenant(db) -> User:
    user = User(
        name="Tenant One", mobile="9000000002", email="tenant1@test.com",
        password_hash=hash_password("tenant123"), role="tenant", is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def other_tenant(db) -> User:
    """A second tenant, used to prove one tenant cannot see another's data."""
    user = User(
        name="Tenant Two", mobile="9000000003", email="tenant2@test.com",
        password_hash=hash_password("tenant123"), role="tenant", is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def shop(db, tenant) -> Shop:
    s = Shop(shop_number="A-101", status="occupied", shop_rent=10000, shop_deposit=50000)
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(UserShop(user_id=tenant.id, shop_id=s.id))
    db.commit()
    return s


@pytest.fixture
def other_shop(db, other_tenant) -> Shop:
    s = Shop(shop_number="B-202", status="occupied", shop_rent=8000, shop_deposit=40000)
    db.add(s)
    db.commit()
    db.refresh(s)
    db.add(UserShop(user_id=other_tenant.id, shop_id=s.id))
    db.commit()
    return s


@pytest.fixture
def meter(db, shop) -> Meter:
    m = Meter(
        shop_id=shop.id, meter_number="MTR-001", meter_type="electricity",
        initial_reading=12450, is_active=True,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


@pytest.fixture
def tariff(db) -> MeterTariff:
    from datetime import datetime
    t = MeterTariff(
        meter_type="electricity", unit_price=9.50, fixed_charge=0,
        tax_percent=0, effective_from=datetime(2000, 1, 1),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── Auth helpers ──────────────────────────────────────────────────────────

def auth_header(user: User) -> dict:
    return {"Authorization": f"Bearer {app_module.create_access_token({'sub': str(user.id)})}"}


@pytest.fixture
def admin_auth(admin):
    return auth_header(admin)


@pytest.fixture
def tenant_auth(tenant):
    return auth_header(tenant)


@pytest.fixture
def other_tenant_auth(other_tenant):
    return auth_header(other_tenant)


# ── Fake photo bytes ──────────────────────────────────────────────────────

def make_jpeg(size_bytes: int = 2048) -> bytes:
    """Minimal bytes that pass the JPEG magic-byte check."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size_bytes - 4)


def make_png(size_bytes: int = 2048) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size_bytes - 8)
