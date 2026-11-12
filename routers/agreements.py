"""
routers/agreements.py - agreement documents for a tenant + shop.

Admins upload / list / delete the signed agreement file(s) for an assignment;
the tenant can list, view and download their own. Files are stored on disk by
services/document_storage.py and are only ever streamed through the download
endpoint below - there is no public URL.

Step 26 of the router/service split (new module, not an extraction).
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from core.database import get_db
from core.logger import get_logger
from core.security import get_current_user, require_admin, require_tenant
from models.schema import AgreementDocument, Shop, User, UserShop
from services import document_storage
from services import settings as settings_service
from services.audit import write_audit

logger = get_logger("app")

router = APIRouter(tags=["Agreements"])

_TITLE_MAX = 150


# ── helpers ───────────────────────────────────────────────────────────────

def document_to_dict(doc: AgreementDocument, uploader_names: Optional[dict] = None) -> dict:
    """Public shape of a document. Never includes the storage path."""
    return {
        "id": doc.id,
        "user_id": doc.user_id,
        "shop_id": doc.shop_id,
        "title": doc.title,
        "original_name": doc.original_name,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
        "uploaded_by_name": (uploader_names or {}).get(doc.uploaded_by),
    }


def documents_for(db: Session, user_id: int, shop_id: Optional[int] = None) -> List[AgreementDocument]:
    q = db.query(AgreementDocument).filter(AgreementDocument.user_id == user_id)
    if shop_id is not None:
        q = q.filter(AgreementDocument.shop_id == shop_id)
    return q.order_by(AgreementDocument.uploaded_at.desc(), AgreementDocument.id.desc()).all()


def documents_by_shop(db: Session, user_id: int) -> dict:
    """{shop_id: [doc dict, ...]} for one tenant - used by /api/tenant/home."""
    out: dict = {}
    for d in documents_for(db, user_id):
        out.setdefault(d.shop_id, []).append(document_to_dict(d))
    return out


def _uploader_names(db: Session, docs: List[AgreementDocument]) -> dict:
    ids = {d.uploaded_by for d in docs if d.uploaded_by}
    if not ids:
        return {}
    return {u.id: u.name for u in db.query(User.id, User.name).filter(User.id.in_(ids)).all()}


def _download_name(doc: AgreementDocument, shop: Optional[Shop]) -> str:
    """ASCII-safe filename for Content-Disposition, with the right extension."""
    ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}.get(doc.mime_type, "bin")
    name = document_storage.safe_original_name(doc.original_name or "")
    if name:
        if not name.lower().endswith("." + ext) and not (ext == "jpg" and name.lower().endswith(".jpeg")):
            name = f"{name}.{ext}"
        return name
    shop_part = document_storage.safe_original_name(shop.shop_number) if shop else str(doc.shop_id)
    return f"agreement-{shop_part}-{doc.id}.{ext}"


# ── admin ─────────────────────────────────────────────────────────────────

@router.get("/api/user/{user_id}/shop/{shop_id}/agreement-documents")
def list_agreement_documents(
    user_id: int,
    shop_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """All agreement documents for one tenant + shop, newest first. Admin only."""
    docs = documents_for(db, user_id, shop_id)
    names = _uploader_names(db, docs)
    return {"success": True, "documents": [document_to_dict(d, names) for d in docs]}


@router.get("/api/shops/{shop_id}/agreement-documents")
def shop_agreement_history(
    shop_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """
    Every agreement document ever uploaded for a shop - current AND past
    tenants - newest first. Admin only.

    The per-assignment list above can only reach the tenant assigned right
    now; once a tenant leaves, their signed agreement is still kept but would
    otherwise have no screen to open it from. This is that screen.
    """
    shop = db.query(Shop).filter(Shop.id == shop_id).first()
    if not shop:
        raise HTTPException(404, detail="Shop not found")

    docs = (
        db.query(AgreementDocument)
        .filter(AgreementDocument.shop_id == shop_id)
        .order_by(AgreementDocument.uploaded_at.desc(), AgreementDocument.id.desc())
        .all()
    )
    names = _uploader_names(db, docs)
    tenant_ids = {d.user_id for d in docs}
    tenants = {
        u.id: (u.name, u.mobile)
        for u in db.query(User.id, User.name, User.mobile).filter(User.id.in_(tenant_ids)).all()
    } if tenant_ids else {}
    current_ids = {
        r.user_id for r in db.query(UserShop.user_id).filter(UserShop.shop_id == shop_id).all()
    }

    out = []
    for d in docs:
        row = document_to_dict(d, names)
        name, mobile = tenants.get(d.user_id, (None, None))
        row["tenant_name"] = name
        row["tenant_mobile"] = mobile
        row["is_current_tenant"] = d.user_id in current_ids
        out.append(row)
    return {"success": True, "shop_id": shop.id, "shop_number": shop.shop_number, "documents": out}


@router.post("/api/user/{user_id}/shop/{shop_id}/agreement-documents", status_code=201)
async def upload_agreement_document(
    user_id: int,
    shop_id: int,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Upload a signed agreement (PDF, JPG or PNG) for a tenant's shop. Admin only.

    The shop must currently be assigned to that tenant - uploading against an
    assignment that doesn't exist would create a document no tenant can see.
    """
    assignment = (
        db.query(UserShop)
        .filter(UserShop.user_id == user_id, UserShop.shop_id == shop_id)
        .first()
    )
    if not assignment:
        raise HTTPException(404, detail="This shop is not assigned to this tenant.")

    if not file or not file.filename:
        raise HTTPException(400, detail="Please choose a file to upload.")

    cfg = settings_service.get_all(db)
    content = await file.read()
    try:
        ext, mime = document_storage.validate(content, int(cfg.get("agreement.max_mb") or 15))
    except document_storage.DocumentValidationError as exc:
        raise HTTPException(400, detail=str(exc))

    clean_title = (title or "").strip()[:_TITLE_MAX] or None
    key = document_storage.build_key(user_id, shop_id, ext)

    doc = AgreementDocument(
        user_id       = user_id,
        shop_id       = shop_id,
        title         = clean_title,
        original_name = document_storage.safe_original_name(file.filename) or None,
        file_path     = key,
        mime_type     = mime,
        size_bytes    = len(content),
        uploaded_by   = current_user.id,
    )
    db.add(doc)
    db.flush()

    # Write the file only once the row is valid, so a failed insert never
    # leaves an orphan file on disk.
    try:
        document_storage.save(str(cfg.get("agreement.storage_dir")), key, content)
    except document_storage.DocumentStorageError as exc:
        db.rollback()
        logger.error("Agreement file save failed (user %s shop %s): %s", user_id, shop_id, exc)
        raise HTTPException(500, detail="Could not save the file on the server.")

    write_audit(db, current_user.id, "UPLOAD", "agreement_documents", doc.id, new_data={
        "user_id": user_id, "shop_id": shop_id, "title": clean_title,
        "original_name": doc.original_name, "size_bytes": doc.size_bytes, "mime_type": mime,
    })
    db.commit()
    db.refresh(doc)
    return {
        "success": True,
        "message": "Agreement uploaded",
        "document": document_to_dict(doc, {current_user.id: current_user.name}),
    }


@router.delete("/api/agreement-documents/{id}")
def delete_agreement_document(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete an agreement document and its file. Admin only."""
    doc = db.query(AgreementDocument).filter(AgreementDocument.id == id).first()
    if not doc:
        raise HTTPException(404, detail="Document not found")

    old = document_to_dict(doc)
    key = doc.file_path
    db.delete(doc)
    write_audit(db, current_user.id, "DELETE", "agreement_documents", id, old_data=old)
    db.commit()

    # File removed after the commit: if the commit failed the row (and so the
    # file) must still be there. A missing file is not an error here.
    cfg = settings_service.get_all(db)
    document_storage.delete(str(cfg.get("agreement.storage_dir")), key)
    return {"success": True, "message": "Document deleted"}


# ── tenant ────────────────────────────────────────────────────────────────

@router.get("/api/tenant/agreement-documents", tags=["Tenant"])
def tenant_agreement_documents(
    shop_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_tenant),
):
    """The caller's own agreement documents (optionally for one shop)."""
    docs = documents_for(db, current_user.id, shop_id)
    return {"success": True, "documents": [document_to_dict(d) for d in docs]}


# ── shared: view / download ───────────────────────────────────────────────

@router.get("/api/agreement-documents/{id}/download")
def download_agreement_document(
    id: int,
    inline: bool = Query(False, description="true = open in the browser, false = download"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Stream an agreement file. An admin may fetch any document; a tenant only
    their own. Someone else's document answers 404 (not 403), so the endpoint
    does not reveal which ids exist.
    """
    doc = db.query(AgreementDocument).filter(AgreementDocument.id == id).first()
    if not doc or (current_user.role != "admin" and doc.user_id != current_user.id):
        raise HTTPException(404, detail="Document not found")

    cfg = settings_service.get_all(db)
    try:
        content = document_storage.read(str(cfg.get("agreement.storage_dir")), doc.file_path)
    except document_storage.DocumentStorageError:
        raise HTTPException(404, detail="The file is missing from the server. Please contact the office.")

    shop = db.query(Shop).filter(Shop.id == doc.shop_id).first()
    filename = _download_name(doc, shop)
    disposition = "inline" if inline else "attachment"
    return Response(
        content=content,
        media_type=doc.mime_type or "application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
