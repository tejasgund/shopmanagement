"""
routers/receipts.py - receipts (money received), advance and refunds.

Admin:
    GET    /api/receipts                      list (filter by tenant, dates, search)
    GET    /api/receipts/{id}                 one receipt with its month-by-month split
    POST   /api/receipts                      record money received (auto split, rest = advance)
    PUT    /api/receipts/{id}                 edit amount / date / method / remarks
    DELETE /api/receipts/{id}                 money never came: remove it and its split
    POST   /api/receipts/refund               give advance back to a tenant
    GET    /api/tenants/{user_id}/advance     advance balance for one tenant
    POST   /api/receipts/settle               apply every tenant's advance now
    GET    /api/receipts/check                consistency report (all money rules)
Tenant:
    GET    /api/tenant/receipts               own receipts + advance balance
    GET    /api/tenant/receipts/{id}          one own receipt

All rules live in services/receipts.py - this module only validates input,
calls it, and maps ReceiptError to HTTP 400/404.

Step 28 of the router/service split (new module).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.security import require_admin, require_tenant
from models.schema import Complex, Receipt, Shop, User, UserShop
from services import receipts as receipt_service
from services.receipts import ReceiptError

router = APIRouter(tags=["Receipts"])


# ── request bodies ────────────────────────────────────────────────────────

class ReceiptCreate(BaseModel):
    user_id: int
    amount: float = Field(..., gt=0)
    payment_method: str = Field(..., min_length=1, max_length=60)
    received_date: Optional[datetime] = None
    remarks: Optional[str] = None
    shop_id: Optional[int] = None      # pay this shop's bills first


class ReceiptUpdate(BaseModel):
    amount: Optional[float] = Field(None, gt=0)
    payment_method: Optional[str] = Field(None, min_length=1, max_length=60)
    received_date: Optional[datetime] = None
    remarks: Optional[str] = None


class RefundCreate(BaseModel):
    user_id: int
    amount: float = Field(..., gt=0)
    payment_method: str = Field("Cash", min_length=1, max_length=60)
    received_date: Optional[datetime] = None
    remarks: Optional[str] = None


def _fail(exc: ReceiptError, db: Session):
    db.rollback()
    raise HTTPException(exc.status_code, detail=str(exc))


def _tenant_info(db: Session, user_ids) -> dict:
    ids = list(set(user_ids))
    if not ids:
        return {}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()}
    shops_by_user: dict = {}
    for us, s in (db.query(UserShop, Shop).join(Shop, Shop.id == UserShop.shop_id)
                  .filter(UserShop.user_id.in_(ids)).all()):
        shops_by_user.setdefault(us.user_id, []).append(s.shop_number)
    return {
        uid: {"tenant_name": u.name, "mobile": u.mobile,
              "shops": sorted(shops_by_user.get(uid, []))}
        for uid, u in users.items()
    }


def _rdict(db: Session, r: Receipt, cache: Optional[dict] = None) -> dict:
    """receipt_dict with refunds taken into account (FIFO), per tenant."""
    cache = {} if cache is None else cache
    if r.user_id not in cache:
        cache[r.user_id] = receipt_service.balances(db, r.user_id)
    return receipt_service.receipt_dict(db, r, cache[r.user_id])


def _with_tenant(d: dict, info: dict) -> dict:
    d.update(info.get(d["user_id"], {"tenant_name": None, "mobile": None, "shops": []}))
    return d


def _advance_payload(db: Session, user_id: int) -> dict:
    bal = receipt_service.balances(db, user_id)
    return {
        "user_id": user_id,
        "advance": float(bal["advance"]),
        "total_received": float(bal["received"]),
        "total_refunded": float(bal["refunded"]),
        "total_applied": float(bal["applied"]),
    }


# ══════════════════════════════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/receipts")
def list_receipts(
    user_id: Optional[int] = None,
    complex_id: Optional[int] = None,
    kind: Optional[str] = Query(None, pattern="^(receipt|refund)$"),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Every receipt and refund, newest first, each with its split."""
    q = db.query(Receipt)
    if user_id is not None:
        q = q.filter(Receipt.user_id == user_id)
    if kind:
        q = q.filter(Receipt.kind == kind)
    if start_date is not None:
        q = q.filter(Receipt.received_date >= start_date)
    if end_date is not None:
        q = q.filter(Receipt.received_date <= end_date)
    if complex_id is not None:
        tenant_ids = [uid for (uid,) in db.query(UserShop.user_id).join(Shop, Shop.id == UserShop.shop_id)
                      .filter(Shop.complex_id == complex_id).distinct().all()]
        q = q.filter(Receipt.user_id.in_(tenant_ids or [-1]))
    if search:
        term = f"%{search.strip()}%"
        uids = [uid for (uid,) in db.query(User.id).filter(User.name.ilike(term) | User.mobile.ilike(term)).all()]
        digits = "".join(ch for ch in search if ch.isdigit())
        cond = Receipt.user_id.in_(uids or [-1]) | Receipt.payment_method.ilike(term) | Receipt.remarks.ilike(term)
        if digits:
            cond = cond | (Receipt.id == int(digits))
        q = q.filter(cond)

    total = q.count()
    rows = (q.order_by(Receipt.received_date.desc(), Receipt.id.desc())
            .offset((page - 1) * limit).limit(limit).all())
    info = _tenant_info(db, [r.user_id for r in rows])
    cache: dict = {}
    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "data": [_with_tenant(_rdict(db, r, cache), info) for r in rows],
    }


@router.get("/api/receipts/check")
def check_receipts(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Run every money rule over the whole database. Empty list = all good."""
    problems = receipt_service.check_invariants(db)
    unlinked = db.query(Receipt).count()
    from models.schema import Payment
    legacy = db.query(Payment).filter(Payment.receipt_id.is_(None)).count()
    return {"ok": not problems, "problems": problems, "receipts": unlinked,
            "payments_without_receipt": legacy}


@router.post("/api/receipts/settle")
def settle_everyone(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Apply every tenant's advance to their unpaid bills now."""
    n = receipt_service.settle_all(db)
    db.commit()
    return {"success": True, "payments_created": n}


@router.get("/api/receipts/{receipt_id}")
def get_receipt(receipt_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    r = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not r:
        raise HTTPException(404, detail="Receipt not found")
    d = _with_tenant(_rdict(db, r), _tenant_info(db, [r.user_id]))
    d["tenant_advance"] = float(receipt_service.advance_of(db, r.user_id))
    return d


@router.post("/api/receipts", status_code=201)
def create_receipt(body: ReceiptCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Money received from a tenant: split oldest-first; anything extra is advance."""
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user or user.role != "tenant":
        raise HTTPException(404, detail="Tenant not found")
    try:
        receipt, _made = receipt_service.create_receipt(
            db, user_id=body.user_id, amount=body.amount, payment_method=body.payment_method,
            received_date=body.received_date, remarks=body.remarks, actor_id=actor.id,
            source="manual", shop_id=body.shop_id,
        )
        db.commit()
    except ReceiptError as exc:
        _fail(exc, db)
    db.refresh(receipt)
    d = _with_tenant(_rdict(db, receipt), _tenant_info(db, [receipt.user_id]))
    d["tenant_advance"] = float(receipt_service.advance_of(db, receipt.user_id))
    return d


@router.put("/api/receipts/{receipt_id}")
def update_receipt(receipt_id: int, body: ReceiptUpdate, db: Session = Depends(get_db),
                   actor: User = Depends(require_admin)):
    r = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not r:
        raise HTTPException(404, detail="Receipt not found")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, detail="No fields provided to update")
    try:
        receipt_service.update_receipt(
            db, r, amount=changes.get("amount"), received_date=changes.get("received_date"),
            payment_method=changes.get("payment_method"), remarks=changes.get("remarks"),
            actor_id=actor.id,
        )
        db.commit()
    except ReceiptError as exc:
        _fail(exc, db)
    db.refresh(r)
    d = _with_tenant(_rdict(db, r), _tenant_info(db, [r.user_id]))
    d["tenant_advance"] = float(receipt_service.advance_of(db, r.user_id))
    return d


@router.delete("/api/receipts/{receipt_id}")
def delete_receipt(receipt_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    r = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not r:
        raise HTTPException(404, detail="Receipt not found")
    user_id, no, kind = r.user_id, receipt_service.receipt_no(r.id), r.kind
    try:
        receipt_service.delete_receipt(db, r, actor_id=actor.id)
        db.commit()
    except ReceiptError as exc:
        _fail(exc, db)
    return {"success": True,
            "message": f"{'Refund' if kind == 'refund' else 'Receipt'} {no} deleted",
            "tenant_advance": float(receipt_service.advance_of(db, user_id))}


@router.post("/api/receipts/refund", status_code=201)
def refund_advance(body: RefundCreate, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """Give part or all of a tenant's advance back to them."""
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user or user.role != "tenant":
        raise HTTPException(404, detail="Tenant not found")
    try:
        refund = receipt_service.create_refund(
            db, user_id=body.user_id, amount=body.amount, payment_method=body.payment_method,
            received_date=body.received_date, remarks=body.remarks, actor_id=actor.id,
        )
        db.commit()
    except ReceiptError as exc:
        _fail(exc, db)
    db.refresh(refund)
    d = _with_tenant(_rdict(db, refund), _tenant_info(db, [refund.user_id]))
    d["tenant_advance"] = float(receipt_service.advance_of(db, refund.user_id))
    return d


@router.get("/api/tenants/{user_id}/advance")
def tenant_advance(user_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(404, detail="Tenant not found")
    if receipt_service.settle(db, user_id):
        db.commit()
    return _advance_payload(db, user_id)


# ══════════════════════════════════════════════════════════════════════════
# TENANT
# ══════════════════════════════════════════════════════════════════════════

@router.get("/api/tenant/receipts", tags=["Tenant"])
def my_receipts(db: Session = Depends(get_db), current_user: User = Depends(require_tenant)):
    """The tenant's own receipts and refunds (newest first) and advance balance."""
    if receipt_service.settle(db, current_user.id):
        db.commit()
    bal = receipt_service.balances(db, current_user.id)
    rows = sorted(bal["receipts"], key=lambda r: (r.received_date, r.id), reverse=True)
    shops = {s.id: s.shop_number for s in db.query(Shop).all()}
    out = []
    for r in rows:
        d = receipt_service.receipt_dict(db, r, bal)
        for line in d["lines"]:
            line["shop_number"] = shops.get(line["shop_id"])
        out.append(d)
    return {
        "advance": float(bal["advance"]),
        "receipts": out,
    }


@router.get("/api/tenant/receipts/{receipt_id}", tags=["Tenant"])
def my_receipt(receipt_id: int, db: Session = Depends(get_db), current_user: User = Depends(require_tenant)):
    r = db.query(Receipt).filter(Receipt.id == receipt_id).first()
    if not r or r.user_id != current_user.id:
        raise HTTPException(404, detail="Receipt not found")
    d = _rdict(db, r)
    shops = {s.id: s.shop_number for s in db.query(Shop).all()}
    for line in d["lines"]:
        line["shop_number"] = shops.get(line["shop_id"])
    complexes = {c.id: c.name for c in db.query(Complex).all()}
    d["tenant_name"] = current_user.name
    d["mobile"] = current_user.mobile
    d["complexes"] = sorted({complexes.get(s.complex_id) for s in
                             db.query(Shop).join(UserShop, UserShop.shop_id == Shop.id)
                             .filter(UserShop.user_id == current_user.id).all()} - {None})
    return d
