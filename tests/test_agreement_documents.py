"""
Agreement documents: admin uploads, tenant views/downloads its own only.
"""

import pathlib
import shutil
import tempfile

import pytest

from models.schema import AgreementDocument, AuditLog, UserShop
from services import settings as settings_service
from tests.conftest import make_jpeg, make_png


def make_pdf(size_bytes: int = 2048) -> bytes:
    return b"%PDF-1.7\n" + b"0" * (size_bytes - 9)


@pytest.fixture
def doc_dir(db):
    target = pathlib.Path(tempfile.mkdtemp(prefix="tms_agreements_"))
    settings_service.set_many(db, {"agreement.storage_dir": str(target)})
    db.commit()
    settings_service.invalidate_cache()
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def _upload(client, auth, user_id, shop_id, content=None, name="agreement.pdf", title=None):
    data = {"title": title} if title is not None else {}
    return client.post(
        f"/api/user/{user_id}/shop/{shop_id}/agreement-documents",
        headers=auth,
        files={"file": (name, content if content is not None else make_pdf(), "application/pdf")},
        data=data,
    )


# ── upload ────────────────────────────────────────────────────────────────

def test_admin_uploads_pdf_and_file_lands_on_disk(client, db, admin_auth, tenant, shop, doc_dir):
    res = _upload(client, admin_auth, tenant.id, shop.id, title="Signed 2026")
    assert res.status_code == 201, res.text
    doc = res.json()["document"]
    assert doc["title"] == "Signed 2026"
    assert doc["mime_type"] == "application/pdf"
    assert doc["original_name"] == "agreement.pdf"
    assert "file_path" not in doc            # storage key is never exposed

    row = db.query(AgreementDocument).one()
    assert (doc_dir / row.file_path).is_file()
    assert db.query(AuditLog).filter(AuditLog.table_name == "agreement_documents",
                                     AuditLog.action == "UPLOAD").count() == 1


@pytest.mark.parametrize("content,mime", [(make_jpeg(), "image/jpeg"), (make_png(), "image/png")])
def test_admin_can_upload_images(client, admin_auth, tenant, shop, doc_dir, content, mime):
    res = _upload(client, admin_auth, tenant.id, shop.id, content=content, name="scan.img")
    assert res.status_code == 201, res.text
    assert res.json()["document"]["mime_type"] == mime


def test_type_comes_from_content_not_extension(client, admin_auth, tenant, shop, doc_dir):
    res = _upload(client, admin_auth, tenant.id, shop.id, content=b"MZ" + b"\x00" * 500, name="evil.pdf")
    assert res.status_code == 400
    assert "PDF, JPG or PNG" in res.json()["detail"]


def test_oversize_file_rejected(client, db, admin_auth, tenant, shop, doc_dir):
    settings_service.set_many(db, {"agreement.max_mb": 1})
    db.commit()
    settings_service.invalidate_cache()
    res = _upload(client, admin_auth, tenant.id, shop.id, content=make_pdf(1024 * 1024 + 10))
    assert res.status_code == 400
    assert "1 MB" in res.json()["detail"]
    assert not any(doc_dir.rglob("*.pdf"))


def test_upload_requires_an_existing_assignment(client, admin_auth, tenant, other_shop, doc_dir):
    res = _upload(client, admin_auth, tenant.id, other_shop.id)
    assert res.status_code == 404


def test_tenant_cannot_upload(client, tenant_auth, tenant, shop, doc_dir):
    res = _upload(client, tenant_auth, tenant.id, shop.id)
    assert res.status_code == 403


# ── list / view / download ────────────────────────────────────────────────

def test_tenant_sees_own_documents_in_list_and_home(client, admin_auth, tenant_auth, tenant, shop, doc_dir):
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]

    listed = client.get("/api/tenant/agreement-documents", headers=tenant_auth).json()["documents"]
    assert [d["id"] for d in listed] == [doc_id]

    home = client.get("/api/tenant/home", headers=tenant_auth).json()
    shop_row = next(s for s in home["shops"] if s["id"] == shop.id)
    assert [d["id"] for d in shop_row["agreement_documents"]] == [doc_id]


def test_tenant_downloads_own_document(client, admin_auth, tenant_auth, tenant, shop, doc_dir):
    content = make_pdf(4096)
    doc_id = _upload(client, admin_auth, tenant.id, shop.id, content=content).json()["document"]["id"]

    res = client.get(f"/api/agreement-documents/{doc_id}/download", headers=tenant_auth)
    assert res.status_code == 200
    assert res.content == content
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("attachment;")
    assert 'filename="agreement.pdf"' in res.headers["content-disposition"]

    inline = client.get(f"/api/agreement-documents/{doc_id}/download?inline=true", headers=tenant_auth)
    assert inline.headers["content-disposition"].startswith("inline;")


def test_other_tenant_cannot_see_or_download(client, admin_auth, other_tenant_auth, tenant, shop, other_shop, doc_dir):
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]

    assert client.get(f"/api/agreement-documents/{doc_id}/download",
                      headers=other_tenant_auth).status_code == 404
    assert client.get("/api/tenant/agreement-documents",
                      headers=other_tenant_auth).json()["documents"] == []


def test_tenant_cannot_use_admin_list(client, tenant_auth, tenant, shop, doc_dir):
    res = client.get(f"/api/user/{tenant.id}/shop/{shop.id}/agreement-documents", headers=tenant_auth)
    assert res.status_code == 403


def test_admin_list_and_download(client, admin_auth, tenant, shop, doc_dir):
    _upload(client, admin_auth, tenant.id, shop.id, name="first.pdf")
    second = _upload(client, admin_auth, tenant.id, shop.id, name="second.pdf").json()["document"]["id"]

    docs = client.get(f"/api/user/{tenant.id}/shop/{shop.id}/agreement-documents",
                      headers=admin_auth).json()["documents"]
    assert len(docs) == 2
    assert docs[0]["id"] == second                 # newest first
    assert docs[0]["uploaded_by_name"] == "Admin One"
    assert client.get(f"/api/agreement-documents/{second}/download", headers=admin_auth).status_code == 200


def test_missing_file_is_a_clean_404(client, db, admin_auth, tenant_auth, tenant, shop, doc_dir):
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]
    for f in doc_dir.rglob("*.pdf"):
        f.unlink()
    res = client.get(f"/api/agreement-documents/{doc_id}/download", headers=tenant_auth)
    assert res.status_code == 404


# ── delete ────────────────────────────────────────────────────────────────

def test_admin_deletes_document_and_file(client, db, admin_auth, tenant_auth, tenant, shop, doc_dir):
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]
    assert any(doc_dir.rglob("*.pdf"))

    res = client.delete(f"/api/agreement-documents/{doc_id}", headers=admin_auth)
    assert res.status_code == 200
    assert db.query(AgreementDocument).count() == 0
    assert not any(doc_dir.rglob("*.pdf"))
    assert client.get(f"/api/agreement-documents/{doc_id}/download", headers=tenant_auth).status_code == 404


def test_tenant_cannot_delete(client, admin_auth, tenant_auth, tenant, shop, doc_dir):
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]
    assert client.delete(f"/api/agreement-documents/{doc_id}", headers=tenant_auth).status_code == 403


def test_document_survives_shop_unassignment(client, db, admin_auth, tenant_auth, tenant, shop, doc_dir):
    """The signed agreement is a record - unassigning the shop must not wipe it."""
    doc_id = _upload(client, admin_auth, tenant.id, shop.id).json()["document"]["id"]
    db.query(UserShop).filter(UserShop.user_id == tenant.id, UserShop.shop_id == shop.id).delete()
    db.commit()
    assert client.get(f"/api/agreement-documents/{doc_id}/download", headers=tenant_auth).status_code == 200


# ── settings ──────────────────────────────────────────────────────────────

def test_max_mb_setting_is_bounded(db):
    with pytest.raises(ValueError):
        settings_service.set_many(db, {"agreement.max_mb": 0})
    with pytest.raises(ValueError):
        settings_service.set_many(db, {"agreement.max_mb": 51})


# ── shop history (past + current tenants) ─────────────────────────────────

def test_shop_history_shows_past_and_current_tenants(client, db, admin_auth, tenant, other_tenant,
                                                    other_tenant_auth, shop, doc_dir):
    old_doc = _upload(client, admin_auth, tenant.id, shop.id, name="old.pdf").json()["document"]["id"]
    # old tenant leaves, new tenant takes the SAME shop
    db.query(UserShop).filter(UserShop.user_id == tenant.id, UserShop.shop_id == shop.id).delete()
    db.add(UserShop(user_id=other_tenant.id, shop_id=shop.id))
    db.commit()
    new_doc = _upload(client, admin_auth, other_tenant.id, shop.id, name="new.pdf").json()["document"]["id"]

    # the new tenant still sees only their own
    home = client.get("/api/tenant/home", headers=other_tenant_auth).json()
    row = next(s for s in home["shops"] if s["id"] == shop.id)
    assert [d["id"] for d in row["agreement_documents"]] == [new_doc]
    assert client.get(f"/api/agreement-documents/{old_doc}/download",
                      headers=other_tenant_auth).status_code == 404

    # the admin's shop history has both, labelled
    res = client.get(f"/api/shops/{shop.id}/agreement-documents", headers=admin_auth)
    assert res.status_code == 200
    docs = {d["id"]: d for d in res.json()["documents"]}
    assert set(docs) == {old_doc, new_doc}
    assert docs[old_doc]["tenant_name"] == "Tenant One" and docs[old_doc]["is_current_tenant"] is False
    assert docs[new_doc]["tenant_name"] == "Tenant Two" and docs[new_doc]["is_current_tenant"] is True


def test_shop_history_is_admin_only(client, tenant_auth, shop, doc_dir):
    assert client.get(f"/api/shops/{shop.id}/agreement-documents", headers=tenant_auth).status_code == 403


def test_shop_history_unknown_shop(client, admin_auth, doc_dir):
    assert client.get("/api/shops/9999/agreement-documents", headers=admin_auth).status_code == 404
