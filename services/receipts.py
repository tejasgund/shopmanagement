"""
services/receipts.py - receipts, advance (credit) and how money meets bills.

THE MODEL
    Receipt  = money a tenant actually handed over (kind="receipt", amount > 0)
               or money the office gave back (kind="refund", amount < 0).
    Payment  = a slice of one receipt applied to one bill (payment.receipt_id).

    advance(tenant) = SUM(receipts.amount) - SUM(payments.amount of receipts)

    The advance is NEVER stored. It is always derived from those two tables, so
    it cannot drift after an edit or a delete - the whole point of the design.

INVARIANTS (checked by check_invariants(), asserted after every test):
    I1  bill.paid_amount == SUM(its payments)              (_reconcile_bill)
    I2  for every receipt: SUM(its payments) <= receipt.amount
    I3  for every tenant: advance >= 0   (refunds never exceed what is unused)
    I4  a refund has no payments

RULES
    - New money is applied to the tenant's unpaid bills OLDEST FIRST (due date,
      then bill date, then id - the same order the old auto-allocate used);
      whatever is left is advance.
    - Advance is applied automatically: settle(tenant) is called after every
      change that could create an unpaid bill or free up money (new bill, bill
      edited/deleted, receipt edited/deleted, refund deleted). Advance belongs
      to the TENANT, so it pays any of their shops' bills.
    - Money freed from a bill (bill deleted / amount lowered) returns to its
      receipt and becomes advance again - it never disappears.
    - Reducing or deleting a receipt (or a payment line) means "this money was
      not received": slices are taken back from the NEWEST bills first.
    - Refunds come out of the advance only, FIFO from the oldest unused
      receipt; a change that would leave refunds larger than the money left is
      refused (ReceiptError, 400) rather than producing a negative advance.

CONCURRENCY
    Every operation first locks the tenant's users row (SELECT ... FOR UPDATE)
    - one mutex per tenant for all money movements - so two workers can never
    spend the same advance twice. SQLite (tests) ignores FOR UPDATE.

Payment rows created before receipts existed have receipt_id NULL. They are
left alone except when money must be released from them (bill deleted /
lowered), when they are first "adopted" into a receipt so the money survives.
`python -m services.receipts backfill` groups them all into receipts.
"""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.logger import get_logger
from helpers.domain import _reconcile_bill, bill_payable
from models.schema import Bill, Payment, Receipt, User
from services.audit import write_audit

logger = get_logger("app")

CENT = Decimal("0.01")
ZERO = Decimal("0")


class ReceiptError(Exception):
    """A requested change would break the money rules. Routers map it to 400."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def D(x) -> Decimal:
    if x is None:
        return ZERO
    return Decimal(str(x)).quantize(CENT)


def receipt_no(receipt_id: int) -> str:
    return f"R-{int(receipt_id):05d}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
# Locking / ordering helpers
# ══════════════════════════════════════════════════════════════════════════

def lock_tenant(db: Session, user_id: int) -> Optional[User]:
    """Serialise all money movements for one tenant (MySQL row lock)."""
    db.flush()   # sessions run with autoflush off: make pending changes visible
    return db.query(User).filter(User.id == user_id).with_for_update().first()


def _unpaid_bills(db: Session, user_id: int, shop_id: Optional[int] = None) -> List[Bill]:
    """Unpaid bills, oldest first. With shop_id, that shop's bills come first."""
    db.flush()
    q = db.query(Bill).filter(Bill.user_id == user_id, Bill.pending_amount > 0)
    bills = q.order_by(
        Bill.due_date.is_(None), Bill.due_date.asc(), Bill.bill_date.asc(), Bill.id.asc()
    ).with_for_update().all()
    if shop_id is not None:
        bills = [b for b in bills if b.shop_id == shop_id] + [b for b in bills if b.shop_id != shop_id]
    return bills


def _receipt_allocated(db: Session, receipt_ids: Iterable[int]) -> Dict[int, Decimal]:
    ids = list(receipt_ids)
    if not ids:
        return {}
    rows = (
        db.query(Payment.receipt_id, func.coalesce(func.sum(Payment.amount), 0))
        .filter(Payment.receipt_id.in_(ids))
        .group_by(Payment.receipt_id)
        .all()
    )
    return {rid: D(total) for rid, total in rows}


# ══════════════════════════════════════════════════════════════════════════
# Balances
# ══════════════════════════════════════════════════════════════════════════

def balances(db: Session, user_id: int) -> dict:
    """
    The tenant's advance, and how much of it sits on each receipt.

    Refunds are taken FIFO from the oldest receipts' unused money, so
    `unused[receipt_id]` is what that receipt can still pay towards bills.
    """
    db.flush()   # autoflush is off in this app's sessions
    receipts = (
        db.query(Receipt)
        .filter(Receipt.user_id == user_id)
        .order_by(Receipt.received_date.asc(), Receipt.id.asc())
        .all()
    )
    alloc = _receipt_allocated(db, [r.id for r in receipts if r.kind == "receipt"])
    raw: Dict[int, Decimal] = {}
    received = ZERO
    refunded = ZERO
    for r in receipts:
        if r.kind == "refund":
            refunded += -D(r.amount)
        else:
            received += D(r.amount)
            raw[r.id] = D(r.amount) - alloc.get(r.id, ZERO)

    to_absorb = refunded
    unused: Dict[int, Decimal] = {}
    for rid, amt in raw.items():          # dict keeps receipt (date) order
        take = min(max(amt, ZERO), to_absorb)
        unused[rid] = amt - take
        to_absorb -= take

    advance = sum(raw.values(), ZERO) - refunded
    return {
        "advance": advance,
        "received": received,
        "refunded": refunded,
        "applied": received - sum(raw.values(), ZERO),
        "unused": unused,
        "receipts": receipts,
        "allocated": alloc,
    }


def advance_of(db: Session, user_id: int) -> Decimal:
    return balances(db, user_id)["advance"]


def advance_by_user(db: Session, user_ids: Optional[Iterable[int]] = None) -> Dict[int, Decimal]:
    """Advance for many tenants in two queries (finance screens)."""
    db.flush()
    rq = db.query(Receipt.user_id, func.coalesce(func.sum(Receipt.amount), 0))
    pq = (
        db.query(Receipt.user_id, func.coalesce(func.sum(Payment.amount), 0))
        .join(Payment, Payment.receipt_id == Receipt.id)
    )
    if user_ids is not None:
        ids = list(user_ids)
        if not ids:
            return {}
        rq = rq.filter(Receipt.user_id.in_(ids))
        pq = pq.filter(Receipt.user_id.in_(ids))
    got = {uid: D(t) for uid, t in rq.group_by(Receipt.user_id).all()}
    for uid, t in pq.group_by(Receipt.user_id).all():
        got[uid] = got.get(uid, ZERO) - D(t)
    return got


def _guard_non_negative(db: Session, user_id: int) -> None:
    adv = advance_of(db, user_id)
    if adv < 0:
        raise ReceiptError(
            f"This change is not possible: ₹{float(-adv):,.2f} of this tenant's advance has "
            f"already been refunded. Delete or reduce the refund first."
        )


# ══════════════════════════════════════════════════════════════════════════
# Applying money to bills
# ══════════════════════════════════════════════════════════════════════════

def _add_slice(db: Session, bill: Bill, receipt: Receipt, amount: Decimal,
               actor_id: Optional[int], action: str = "CREATE") -> Payment:
    pay = Payment(
        bill_id=bill.id, receipt_id=receipt.id, amount=amount,
        payment_method=receipt.payment_method, remarks=receipt.remarks,
        payment_date=receipt.received_date,
        razorpay_payment_id=receipt.razorpay_payment_id,
    )
    db.add(pay)
    db.flush()
    db.refresh(bill)
    _reconcile_bill(bill)
    if actor_id is not None:
        write_audit(db, actor_id, action, "payments", pay.id, new_data={
            "bill_id": bill.id, "receipt_id": receipt.id, "receipt_no": receipt_no(receipt.id),
            "amount": float(amount), "payment_method": receipt.payment_method,
        })
    return pay


def _apply(db: Session, receipt: Receipt, available: Decimal, bills: List[Bill],
           actor_id: Optional[int], action: str) -> Tuple[List[Payment], Decimal]:
    made = []
    for bill in bills:
        if available <= 0:
            break
        outstanding = D(bill.pending_amount)
        if outstanding <= 0:
            continue
        amt = min(available, outstanding)
        made.append(_add_slice(db, bill, receipt, amt, actor_id, action))
        available -= amt
    return made, available


def settle(db: Session, user_id: int, actor_id: Optional[int] = None) -> List[Payment]:
    """
    Use the tenant's advance on their unpaid bills (oldest receipt's money on
    the oldest bill first). Idempotent: does nothing when there is no advance
    or nothing unpaid. Called after every change that could need it.
    """
    lock_tenant(db, user_id)
    bal = balances(db, user_id)
    if bal["advance"] <= 0:
        return []
    bills = _unpaid_bills(db, user_id)
    if not bills:
        return []
    made: List[Payment] = []
    for receipt in bal["receipts"]:
        if receipt.kind != "receipt":
            continue
        unused = bal["unused"].get(receipt.id, ZERO)
        if unused <= 0:
            continue
        got, _left = _apply(db, receipt, unused, bills, actor_id, "AUTO_APPLY_ADVANCE")
        made += got
        if all(D(b.pending_amount) <= 0 for b in bills):
            break
    if made:
        logger.info("Advance applied for tenant %s: %s payment(s)", user_id, len(made))
    return made


def settle_all(db: Session) -> int:
    """
    settle() every tenant who has advance AND an unpaid bill. Cheap when
    there is nothing to do (one aggregate query). Used by the finance screens,
    the tenant portal, and the `settle` CLI the rent cron can call - because
    the cron schedulers write bills with raw SQL and never call settle().
    """
    adv = advance_by_user(db)
    candidates = [uid for uid, a in adv.items() if a > 0]
    if not candidates:
        return 0
    owing = {
        uid for (uid,) in db.query(Bill.user_id)
        .filter(Bill.user_id.in_(candidates), Bill.pending_amount > 0).distinct().all()
    }
    n = 0
    for uid in owing:
        n += len(settle(db, uid))
    return n


# ══════════════════════════════════════════════════════════════════════════
# Creating receipts
# ══════════════════════════════════════════════════════════════════════════

def create_receipt(
    db: Session, *, user_id: int, amount, payment_method: str,
    received_date: Optional[datetime] = None, remarks: Optional[str] = None,
    actor_id: Optional[int] = None, source: str = "manual",
    allocations: Optional[List[Tuple[int, Decimal]]] = None,
    shop_id: Optional[int] = None, razorpay_payment_id: Optional[str] = None,
) -> Tuple[Receipt, List[Payment]]:
    """
    Record money received and apply it.

    allocations=None  -> automatic: oldest unpaid bills first (shop_id's first).
    allocations=[...] -> exactly these (bill_id, amount) slices, each capped at
                         what the bill still owes. Anything left over is
                         advance, which settle() then applies as usual.
    """
    amount = D(amount)
    if amount <= 0:
        raise ReceiptError("Amount must be greater than zero.")
    if not lock_tenant(db, user_id):
        raise ReceiptError("Tenant not found.", 404)

    receipt = Receipt(
        user_id=user_id, kind="receipt", amount=amount,
        received_date=received_date or _now(), payment_method=payment_method,
        remarks=remarks, source=source, razorpay_payment_id=razorpay_payment_id,
        created_by=actor_id,
    )
    db.add(receipt)
    db.flush()

    made: List[Payment] = []
    left = amount
    if allocations is None:
        made, left = _apply(db, receipt, left, _unpaid_bills(db, user_id, shop_id), actor_id, "CREATE")
    else:
        seen = set()
        for bill_id, want in allocations:
            if bill_id in seen:
                raise ReceiptError("The same bill appears twice.")
            seen.add(bill_id)
            bill = db.query(Bill).filter(Bill.id == bill_id).with_for_update().first()
            if not bill or bill.user_id != user_id:
                raise ReceiptError(f"Bill {bill_id} not found for this tenant.")
            want = min(D(want), D(bill.pending_amount), left)
            if want <= 0:
                continue
            made.append(_add_slice(db, bill, receipt, want, actor_id))
            left -= want

    if actor_id is not None:
        write_audit(db, actor_id, "CREATE", "receipts", receipt.id, new_data={
            "receipt_no": receipt_no(receipt.id), "user_id": user_id, "amount": float(amount),
            "payment_method": payment_method, "source": source,
            "applied": float(amount - left), "advance": float(left),
        })
    made += settle(db, user_id, actor_id)
    return receipt, made


def create_refund(db: Session, *, user_id: int, amount, payment_method: str,
                  received_date: Optional[datetime] = None, remarks: Optional[str] = None,
                  actor_id: Optional[int] = None) -> Receipt:
    amount = D(amount)
    if amount <= 0:
        raise ReceiptError("Refund amount must be greater than zero.")
    if not lock_tenant(db, user_id):
        raise ReceiptError("Tenant not found.", 404)
    settle(db, user_id, actor_id)             # advance first pays what is owed
    adv = advance_of(db, user_id)
    if amount > adv:
        raise ReceiptError(f"Refund cannot be more than the advance balance (₹{float(adv):,.2f}).")
    refund = Receipt(
        user_id=user_id, kind="refund", amount=-amount,
        received_date=received_date or _now(), payment_method=payment_method,
        remarks=remarks, source="refund", created_by=actor_id,
    )
    db.add(refund)
    db.flush()
    if actor_id is not None:
        write_audit(db, actor_id, "CREATE", "receipts", refund.id, new_data={
            "receipt_no": receipt_no(refund.id), "kind": "refund", "user_id": user_id,
            "amount": float(amount), "payment_method": payment_method,
        })
    return refund


# ══════════════════════════════════════════════════════════════════════════
# Taking money back off bills
# ══════════════════════════════════════════════════════════════════════════

def adopt_legacy_payment(db: Session, pay: Payment, actor_id: Optional[int] = None) -> Receipt:
    """Give a pre-receipts payment its own receipt so money freed from it survives."""
    if pay.receipt_id is not None:
        return pay.receipt
    bill = pay.bill
    receipt = Receipt(
        user_id=bill.user_id, kind="receipt", amount=D(pay.amount),
        received_date=pay.payment_date, payment_method=pay.payment_method,
        remarks=pay.remarks, source="migrated",
        razorpay_payment_id=pay.razorpay_payment_id, created_by=actor_id,
    )
    db.add(receipt)
    db.flush()
    pay.receipt_id = receipt.id
    db.flush()
    return receipt


def _reduce_payment(db: Session, pay: Payment, by: Decimal) -> Decimal:
    """Take `by` (or as much as it has) off one payment; delete it if it hits 0."""
    take = min(by, D(pay.amount))
    bill = pay.bill
    if take >= D(pay.amount):
        db.delete(pay)
    else:
        pay.amount = D(pay.amount) - take
    db.flush()
    if bill is not None:
        db.refresh(bill)
        _reconcile_bill(bill)
    return take


def release_from_bill(db: Session, bill: Bill, keep_paid: Decimal,
                      actor_id: Optional[int] = None) -> Decimal:
    """
    Bring a bill's payments down to `keep_paid`, newest payment first. The
    released money stays on its receipt (= advance). Returns amount released.
    """
    pays = (
        db.query(Payment).filter(Payment.bill_id == bill.id)
        .order_by(Payment.payment_date.desc(), Payment.id.desc()).all()
    )
    excess = sum((D(p.amount) for p in pays), ZERO) - D(keep_paid)
    released = ZERO
    for p in pays:
        if excess <= 0:
            break
        adopt_legacy_payment(db, p, actor_id)
        got = _reduce_payment(db, p, excess)
        excess -= got
        released += got
    if released > 0 and actor_id is not None:
        write_audit(db, actor_id, "RELEASE_TO_ADVANCE", "bills", bill.id, new_data={
            "released": float(released), "kept_paid": float(keep_paid),
        })
    return released


def release_whole_bill(db: Session, bill: Bill, actor_id: Optional[int] = None) -> Decimal:
    """Before a bill is deleted: free all its money back to receipts."""
    return release_from_bill(db, bill, ZERO, actor_id)


def _take_back_from_receipt(db: Session, receipt: Receipt, by: Decimal) -> Decimal:
    """Remove `by` of this receipt's slices, newest bill first."""
    pays = (
        db.query(Payment).join(Bill, Bill.id == Payment.bill_id)
        .filter(Payment.receipt_id == receipt.id)
        .order_by(Bill.bill_date.desc(), Bill.id.desc(), Payment.id.desc()).all()
    )
    taken = ZERO
    for p in pays:
        if by - taken <= 0:
            break
        taken += _reduce_payment(db, p, by - taken)
    return taken


# ══════════════════════════════════════════════════════════════════════════
# Editing / deleting receipts and payment lines
# ══════════════════════════════════════════════════════════════════════════

def _sync_slices(db: Session, receipt: Receipt) -> None:
    """A receipt's slices always carry its date, method and remarks."""
    for p in db.query(Payment).filter(Payment.receipt_id == receipt.id).all():
        p.payment_date = receipt.received_date
        p.payment_method = receipt.payment_method
        p.remarks = receipt.remarks
    db.flush()


def update_receipt(db: Session, receipt: Receipt, *, amount=None, received_date=None,
                   payment_method=None, remarks=None, actor_id: Optional[int] = None) -> Receipt:
    lock_tenant(db, receipt.user_id)
    old = receipt_dict(db, receipt)
    if received_date is not None:
        receipt.received_date = received_date
    if payment_method is not None:
        receipt.payment_method = payment_method
    if remarks is not None:
        receipt.remarks = remarks

    if amount is not None:
        new = D(amount)
        if new <= 0:
            raise ReceiptError("Amount must be greater than zero. To remove it, delete the receipt instead.")
        if receipt.kind == "refund":
            receipt.amount = -new
        else:
            cur = D(receipt.amount)
            if new < cur:
                applied = _receipt_allocated(db, [receipt.id]).get(receipt.id, ZERO)
                unused_here = cur - applied
                still = (cur - new) - max(unused_here, ZERO)
                if still > 0:
                    _take_back_from_receipt(db, receipt, still)
            receipt.amount = new
    db.flush()
    if receipt.kind == "receipt":
        _sync_slices(db, receipt)
    _guard_non_negative(db, receipt.user_id)
    settle(db, receipt.user_id, actor_id)
    if actor_id is not None:
        write_audit(db, actor_id, "UPDATE", "receipts", receipt.id, old_data=old,
                    new_data=receipt_dict(db, receipt))
    return receipt


def delete_receipt(db: Session, receipt: Receipt, actor_id: Optional[int] = None) -> None:
    user_id = receipt.user_id
    lock_tenant(db, user_id)
    old = receipt_dict(db, receipt)
    for p in db.query(Payment).filter(Payment.receipt_id == receipt.id).all():
        bill = p.bill
        db.delete(p)
        db.flush()
        if bill is not None:
            db.refresh(bill)
            _reconcile_bill(bill)
    db.delete(receipt)
    db.flush()
    _guard_non_negative(db, user_id)
    settle(db, user_id, actor_id)
    if actor_id is not None:
        write_audit(db, actor_id, "DELETE", "receipts", old["id"], old_data=old)


def update_payment_line(db: Session, pay: Payment, changes: dict,
                        actor_id: Optional[int] = None) -> Payment:
    """
    PUT /api/payment/{id}. A payment line is a slice of a receipt:
      - amount changed  => the money received changed by the same delta
                           (receipt amount follows); the bill never gets more
                           than it owes - anything beyond becomes advance.
      - date/method/remarks => a property of the receipt: all its slices follow.
    """
    bill = pay.bill
    lock_tenant(db, bill.user_id)
    receipt = adopt_legacy_payment(db, pay, actor_id)

    if "amount" in changes:
        new = D(changes["amount"])
        if new <= 0:
            raise ReceiptError("Amount must be greater than zero. To remove it, delete the payment instead.")
        old_amt = D(pay.amount)
        receipt.amount = D(receipt.amount) + (new - old_amt)
        db.refresh(bill)
        others = sum((D(p.amount) for p in bill.payments if p.id != pay.id), ZERO)
        room = max(D(bill_payable(bill)) - others, ZERO)
        slice_amt = min(new, room)          # never overpay the bill; the rest is advance
        if slice_amt <= 0:
            db.delete(pay)
        else:
            pay.amount = slice_amt
        db.flush()
        db.refresh(bill)
        _reconcile_bill(bill)

    if "payment_date" in changes:
        receipt.received_date = changes["payment_date"]
    if "payment_method" in changes:
        receipt.payment_method = changes["payment_method"]
    if "remarks" in changes:
        receipt.remarks = changes["remarks"]
    db.flush()
    _sync_slices(db, receipt)
    _guard_non_negative(db, bill.user_id)
    settle(db, bill.user_id, actor_id)
    return pay


def delete_payment_line(db: Session, pay: Payment, actor_id: Optional[int] = None) -> None:
    """DELETE /api/payment/{id}: this money was not received."""
    bill = pay.bill
    user_id = bill.user_id
    lock_tenant(db, user_id)
    receipt = pay.receipt
    amt = D(pay.amount)
    db.delete(pay)
    db.flush()
    db.refresh(bill)
    _reconcile_bill(bill)
    if receipt is not None:
        receipt.amount = D(receipt.amount) - amt
        db.flush()
        remaining = db.query(Payment).filter(Payment.receipt_id == receipt.id).count()
        if D(receipt.amount) <= 0 and remaining == 0:
            db.delete(receipt)
            db.flush()
        elif D(receipt.amount) < 0:
            raise ReceiptError("Receipt amount cannot go below zero.")
    _guard_non_negative(db, user_id)
    settle(db, user_id, actor_id)


# ══════════════════════════════════════════════════════════════════════════
# Read shapes
# ══════════════════════════════════════════════════════════════════════════

def receipt_dict(db: Session, r: Receipt, bal: Optional[dict] = None, with_lines: bool = True) -> dict:
    lines = []
    applied = ZERO
    if with_lines and r.kind == "receipt":
        rows = (
            db.query(Payment, Bill).join(Bill, Bill.id == Payment.bill_id)
            .filter(Payment.receipt_id == r.id)
            .order_by(Bill.bill_date.asc(), Bill.id.asc(), Payment.id.asc()).all()
        )
        for p, b in rows:
            applied += D(p.amount)
            lines.append({
                "payment_id": p.id, "bill_id": b.id, "bill_type": b.bill_type,
                "shop_id": b.shop_id,
                "bill_date": b.bill_date.isoformat() if b.bill_date else None,
                "period": b.bill_date.strftime("%Y-%m") if b.bill_date else None,
                "amount": float(D(p.amount)),
            })
    elif r.kind == "receipt":
        applied = _receipt_allocated(db, [r.id]).get(r.id, ZERO)
    unused = (bal or {}).get("unused", {}).get(r.id) if bal else None
    if unused is None:
        unused = D(r.amount) - applied if r.kind == "receipt" else ZERO
    return {
        "id": r.id,
        "receipt_no": receipt_no(r.id),
        "user_id": r.user_id,
        "kind": r.kind,
        "amount": float(abs(D(r.amount))),
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "payment_method": r.payment_method,
        "remarks": r.remarks,
        "source": r.source,
        "razorpay_payment_id": r.razorpay_payment_id,
        "applied": float(applied),
        "advance_left": float(max(D(unused), ZERO)) if r.kind == "receipt" else 0.0,
        "lines": lines,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════════════
# Consistency check
# ══════════════════════════════════════════════════════════════════════════

def check_invariants(db: Session) -> List[str]:
    """Every rule in the module docstring, across the whole database."""
    problems = []
    paid_by_bill = defaultdict(lambda: ZERO)
    for bid, amt in db.query(Payment.bill_id, Payment.amount).all():
        paid_by_bill[bid] += D(amt)
    for b in db.query(Bill).all():
        if D(b.paid_amount) != paid_by_bill[b.id]:
            problems.append(f"I1 bill {b.id}: paid_amount {b.paid_amount} != payments {paid_by_bill[b.id]}")
        if D(b.pending_amount) != max(D(bill_payable(b)) - paid_by_bill[b.id], ZERO) and b.status in ("pending", "partial", "paid"):
            problems.append(f"I1 bill {b.id}: pending_amount {b.pending_amount} inconsistent")
    alloc = _receipt_allocated(db, [r.id for r in db.query(Receipt.id).all()])
    for r in db.query(Receipt).all():
        a = alloc.get(r.id, ZERO)
        if r.kind == "refund":
            if a != 0:
                problems.append(f"I4 refund {r.id} has payments")
            if D(r.amount) >= 0:
                problems.append(f"refund {r.id} amount must be negative")
        else:
            if a > D(r.amount):
                problems.append(f"I2 receipt {r.id}: applied {a} > amount {r.amount}")
            if D(r.amount) <= 0:
                problems.append(f"receipt {r.id} amount must be positive")
    for uid, adv in advance_by_user(db).items():
        if adv < 0:
            problems.append(f"I3 tenant {uid}: advance {adv} < 0")
    for p in db.query(Payment).filter(Payment.receipt_id.isnot(None)).all():
        if p.receipt and p.bill and p.receipt.user_id != p.bill.user_id:
            problems.append(f"payment {p.id}: receipt tenant != bill tenant")
    return problems


# ══════════════════════════════════════════════════════════════════════════
# Backfill: group pre-receipt payments into receipts
# ══════════════════════════════════════════════════════════════════════════

def plan_backfill(db: Session, window_seconds: int = 10) -> List[dict]:
    """
    Group payments that have no receipt. Same tenant + same payment_date +
    method + remarks, recorded within `window_seconds` of each other (one
    auto-allocate confirm writes all its rows in one transaction) = one
    receipt. Razorpay rows group by their payment id instead.
    """
    rows = (
        db.query(Payment, Bill).join(Bill, Bill.id == Payment.bill_id)
        .filter(Payment.receipt_id.is_(None))
        .order_by(Bill.user_id, Payment.created_at, Payment.id).all()
    )
    groups: List[dict] = []
    open_by_key: Dict[tuple, dict] = {}
    for p, b in rows:
        if p.razorpay_payment_id:
            key = ("rzp", b.user_id, p.razorpay_payment_id)
        else:
            key = ("cash", b.user_id, p.payment_date, p.payment_method, (p.remarks or "").strip())
        g = open_by_key.get(key)
        if g is not None and key[0] == "cash" and p.created_at and g["last_created"] and \
                abs((p.created_at - g["last_created"]).total_seconds()) > window_seconds:
            g = None
        if g is None:
            g = {"user_id": b.user_id, "payment_ids": [], "amount": ZERO,
                 "received_date": p.payment_date, "payment_method": p.payment_method,
                 "remarks": p.remarks, "razorpay_payment_id": p.razorpay_payment_id,
                 "last_created": p.created_at, "bills": []}
            groups.append(g)
            open_by_key[key] = g
        g["payment_ids"].append(p.id)
        g["amount"] += D(p.amount)
        g["bills"].append(b.id)
        g["last_created"] = p.created_at
    return groups


def run_backfill(db: Session, dry_run: bool = True) -> dict:
    groups = plan_backfill(db)
    if not dry_run:
        for g in groups:
            r = Receipt(
                user_id=g["user_id"], kind="receipt", amount=g["amount"],
                received_date=g["received_date"], payment_method=g["payment_method"],
                remarks=g["remarks"], source="migrated",
                razorpay_payment_id=g["razorpay_payment_id"],
            )
            db.add(r)
            db.flush()
            db.query(Payment).filter(Payment.id.in_(g["payment_ids"])).update(
                {Payment.receipt_id: r.id}, synchronize_session=False)
        db.flush()
    return {
        "payments": sum(len(g["payment_ids"]) for g in groups),
        "receipts": len(groups),
        "multi_bill_receipts": sum(1 for g in groups if len(g["payment_ids"]) > 1),
        "groups": groups,
    }


def _cli(argv=None) -> int:
    """
    python -m services.receipts backfill            (dry run: shows the plan)
    python -m services.receipts backfill --apply    (writes the receipts)
    python -m services.receipts settle              (apply advance everywhere)
    python -m services.receipts check               (consistency report)
    """
    import argparse

    from dotenv import load_dotenv
    load_dotenv()
    from core.database import SessionLocal

    ap = argparse.ArgumentParser(prog="python -m services.receipts")
    ap.add_argument("command", choices=["backfill", "settle", "check"])
    ap.add_argument("--apply", action="store_true", help="backfill: actually write (default is a dry run)")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        if args.command == "backfill":
            res = run_backfill(db, dry_run=not args.apply)
            print(f"{'DRY RUN - nothing written. ' if not args.apply else ''}"
                  f"{res['payments']} old payment(s) -> {res['receipts']} receipt(s); "
                  f"{res['multi_bill_receipts']} receipt(s) cover more than one bill.")
            for g in res["groups"][:50]:
                if len(g["payment_ids"]) > 1:
                    print(f"  tenant {g['user_id']}  {g['received_date']}  {g['payment_method']}  "
                          f"Rs {g['amount']}  -> bills {g['bills']}")
            if args.apply:
                db.commit()
                print("Done. Run 'python -m services.receipts check' to verify.")
            else:
                db.rollback()
                print("Run again with --apply to write these receipts.")
        elif args.command == "settle":
            n = settle_all(db)
            db.commit()
            print(f"Advance applied: {n} payment(s) created.")
        else:
            problems = check_invariants(db)
            print("OK - all money rules hold." if not problems else "\n".join(problems))
            return 1 if problems else 0
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(_cli())
