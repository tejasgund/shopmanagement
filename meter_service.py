"""
meter_service.py - Submeter reading business logic

Kept out of app.py so the rules that decide money can be read, reasoned about
and unit-tested on their own.

The one rule that governs everything here:

    The bill is calculated from the ADMIN's verified reading, never from what
    the tenant typed. The tenant's number and their photo are evidence that
    helps the admin decide; they are never the billing authority.

Flow:
    tenant submits photo + reading  ->  status "pending"
    admin looks at the photo, types the reading they can see
    admin approves                  ->  approved_reading = admin's reading
                                        units = approved - previous approved
                                        bill  = units x tariff live at that date
    admin rejects                   ->  reason recorded, no bill, tenant can resubmit
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from sqlalchemy.orm import Session

import settings_service
from create_tables import Bill, Meter, MeterReading, MeterTariff
from log import get_logger

logger = get_logger("app")


class MeterError(Exception):
    """
    A business-rule failure the caller should surface to the user as a 4xx.

    `status_code` lets app.py translate it straight into an HTTPException
    without every endpoint re-deciding what the right code is.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _money(value) -> Decimal:
    """Round to 2 dp, half-up - the way money is rounded on a paper bill."""
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _units(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ══════════════════════════════════════════════════════════════════════════════
# PREVIOUS READING
# ══════════════════════════════════════════════════════════════════════════════

def latest_approved_reading(db: Session, meter_id: int) -> Optional[MeterReading]:
    """
    The most recent APPROVED reading for this meter - the only thing allowed to
    act as the "previous reading". Pending and rejected rows are deliberately
    excluded: billing from an unverified number is exactly what this whole
    workflow exists to prevent.
    """
    return (
        db.query(MeterReading)
        .filter(
            MeterReading.meter_id == meter_id,
            MeterReading.status == "approved",
        )
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .first()
    )


def previous_reading_value(db: Session, meter: Meter) -> Decimal:
    """
    The number this meter's next bill should count up from.

    Falls back to the meter's initial_reading when nothing has been approved
    yet, so the very first bill charges only what was consumed since the meter
    was installed - not the whole lifetime value on the meter's face.
    """
    last = latest_approved_reading(db, meter.id)
    if last is not None and last.approved_reading is not None:
        return _units(last.approved_reading)
    return _units(meter.initial_reading)


# ══════════════════════════════════════════════════════════════════════════════
# TARIFF
# ══════════════════════════════════════════════════════════════════════════════

def applicable_tariff(db: Session, meter_type: str, when: datetime) -> Optional[MeterTariff]:
    """
    The tariff that was live on `when` - the newest row whose effective_from is
    on or before that date. Looking it up by date (rather than "the current
    price") is what makes an old bill reproducible after a price rise.
    """
    return (
        db.query(MeterTariff)
        .filter(
            MeterTariff.meter_type == (meter_type or "electricity"),
            MeterTariff.effective_from <= when,
        )
        .order_by(MeterTariff.effective_from.desc(), MeterTariff.id.desc())
        .first()
    )


# ══════════════════════════════════════════════════════════════════════════════
# CONSUMPTION + ESTIMATE
# ══════════════════════════════════════════════════════════════════════════════

def calculate_units(previous: Decimal, current: Decimal) -> Decimal:
    """
    Units consumed. Raises rather than returning a negative number: a current
    reading below the previous one means the photo was misread, the wrong meter
    was photographed, or the meter was replaced - all of which need a human,
    not a negative bill.
    """
    previous = _units(previous)
    current = _units(current)
    if current < previous:
        raise MeterError(
            f"Current reading ({current}) is lower than the last approved reading "
            f"({previous}). Check the photo - if the meter was replaced or has "
            f"rolled over, reject this reading and register a new meter instead.",
            status_code=400,
        )
    return _units(current - previous)


def estimate_bill(db: Session, meter: Meter, units: Decimal, when: datetime) -> dict:
    """
    What the bill would come to, using the tariff live on `when`.
    Returns the full breakdown so the admin sees the numbers before approving.
    """
    tariff = applicable_tariff(db, meter.meter_type, when)
    if tariff is None:
        return {
            "tariff_id": None,
            "unit_price": None,
            "fixed_charge": None,
            "tax_percent": None,
            "energy_charge": None,
            "subtotal": None,
            "tax_amount": None,
            "total": None,
            "error": (
                f"No unit price has been set for {meter.meter_type} on or before "
                f"{when:%d %b %Y}. Add a tariff before approving this reading."
            ),
        }

    unit_price = Decimal(str(tariff.unit_price))
    fixed = _money(tariff.fixed_charge)
    tax_percent = Decimal(str(tariff.tax_percent or 0))

    energy_charge = _money(_units(units) * unit_price)
    subtotal = _money(energy_charge + fixed)
    tax_amount = _money(subtotal * tax_percent / Decimal("100"))
    total = _money(subtotal + tax_amount)

    return {
        "tariff_id": tariff.id,
        "unit_price": float(unit_price),
        "fixed_charge": float(fixed),
        "tax_percent": float(tax_percent),
        "energy_charge": float(energy_charge),
        "subtotal": float(subtotal),
        "tax_amount": float(tax_amount),
        "total": float(total),
        "effective_from": tariff.effective_from,
        "error": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# COMPARISON + ANOMALY FLAGS
# ══════════════════════════════════════════════════════════════════════════════

def recent_average_units(db: Session, meter_id: int, limit: int = 6) -> Optional[Decimal]:
    """Average consumption over the last few approved readings, for spike detection."""
    rows = (
        db.query(MeterReading.calculated_units)
        .filter(
            MeterReading.meter_id == meter_id,
            MeterReading.status == "approved",
            MeterReading.calculated_units.isnot(None),
        )
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .limit(limit)
        .all()
    )
    values = [Decimal(str(r[0])) for r in rows if r[0] is not None]
    if not values:
        return None
    return _units(sum(values) / len(values))


def build_comparison(customer_reading, admin_reading) -> dict:
    """
    Side-by-side of what the tenant said versus what the admin read off the
    photo. A mismatch is surfaced loudly rather than silently resolved - the
    admin's number still wins, but they should know they are overriding.
    """
    customer = _units(customer_reading) if customer_reading is not None else None
    admin = _units(admin_reading) if admin_reading is not None else None

    if admin is None:
        return {
            "customer_reading": float(customer) if customer is not None else None,
            "admin_reading": None,
            "matches": None,
            "difference": None,
            "message": "Waiting for the admin to enter the reading shown in the photo.",
        }

    matches = customer is not None and customer == admin
    difference = float(admin - customer) if customer is not None else None

    return {
        "customer_reading": float(customer) if customer is not None else None,
        "admin_reading": float(admin),
        "matches": matches,
        "difference": difference,
        "message": (
            "Tenant's reading matches your verified reading."
            if matches else
            f"Your reading differs from the tenant's by {difference:+.2f}. "
            "Your verified reading will be used for the bill."
        ),
    }


def detect_anomalies(db: Session, meter: Meter, previous: Decimal, current: Decimal,
                     units: Decimal) -> List[dict]:
    """
    Warnings for the admin - never blocks. Every one of these is a case where a
    human looking at the photo can tell instantly whether it is real (a shop
    running extra freezers all summer) or a misread.
    """
    warnings: List[dict] = []
    cfg = settings_service.get_all(db)

    if cfg.get("meter.warn_zero_consumption") and _units(units) == 0:
        warnings.append({
            "code": "zero_consumption",
            "severity": "warning",
            "message": (
                "No consumption - the reading is identical to the last approved one. "
                "Check the photo is the current reading and not a re-submitted old photo."
            ),
        })

    threshold = cfg.get("meter.high_consumption_units") or 0
    if threshold and _units(units) > Decimal(str(threshold)):
        warnings.append({
            "code": "high_consumption",
            "severity": "warning",
            "message": (
                f"Unusually high consumption ({units} units, above the "
                f"{threshold}-unit alert level). Please double-check the photo."
            ),
        })

    multiplier = cfg.get("meter.high_consumption_multiplier") or 0
    if multiplier:
        average = recent_average_units(db, meter.id)
        if average and average > 0 and _units(units) > average * Decimal(str(multiplier)):
            warnings.append({
                "code": "consumption_spike",
                "severity": "warning",
                "message": (
                    f"This is {float(_units(units) / average):.1f}x this meter's recent "
                    f"average of {average} units. Verify the digits carefully."
                ),
            })

    return warnings


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL  ->  BILL
# ══════════════════════════════════════════════════════════════════════════════

def build_bill_for_reading(db: Session, reading: MeterReading, meter: Meter,
                           units: Decimal, when: datetime) -> Bill:
    """
    Create the Bill for an approved reading, using the tariff that was live on
    the reading date. The rate is copied onto both the bill description and the
    reading row, so a later price change can never rewrite this bill.

    The caller owns the transaction - this only stages the Bill.
    """
    cfg = settings_service.get_all(db)
    breakdown = estimate_bill(db, meter, units, when)
    if breakdown["error"]:
        raise MeterError(breakdown["error"], status_code=400)

    total = _money(breakdown["total"])
    due_days = int(cfg.get("meter.bill_due_days") or 0)

    description_parts = [
        f"Meter {meter.meter_number}",
        f"{reading.previous_reading} to {reading.approved_reading}",
        f"{units} units @ {breakdown['unit_price']}/unit",
    ]
    if breakdown["fixed_charge"]:
        description_parts.append(f"fixed {breakdown['fixed_charge']}")
    if breakdown["tax_percent"]:
        description_parts.append(f"tax {breakdown['tax_percent']}%")

    bill = Bill(
        user_id        = reading.user_id,
        shop_id        = reading.shop_id,
        bill_type      = str(cfg.get("meter.bill_type_label") or "Electricity"),
        description    = " | ".join(description_parts),
        amount         = total,
        paid_amount    = Decimal("0"),
        pending_amount = total,
        bill_date      = when,
        due_date       = when + timedelta(days=due_days),
        status         = "pending",
    )
    db.add(bill)
    return bill


def approve_reading(db: Session, reading: MeterReading, meter: Meter,
                    admin_reading: Decimal, admin_id: int,
                    override_reason: Optional[str] = None,
                    admin_note: Optional[str] = None) -> dict:
    """
    Approve a pending reading and (unless switched off in settings) raise the
    bill, inside the caller's transaction.

    Guarantees:
      - Only a pending reading can be approved. Re-approving an already
        approved row is refused, so a double-clicked Approve button cannot
        produce a second bill. The UNIQUE constraint on meter_readings.bill_id
        is the database-level backstop for the same thing.
      - approved_reading is always the ADMIN's number.
      - If bill creation fails for any reason the caller rolls back, so the
        reading never ends up approved-but-unbilled.
    """
    if reading.status == "approved":
        raise MeterError(
            "This reading has already been approved and billed.", status_code=409,
        )
    if reading.status == "rejected":
        raise MeterError(
            "This reading was rejected. Ask the tenant to submit a new photo.",
            status_code=409,
        )

    cfg = settings_service.get_all(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    admin_value = _units(admin_reading)
    previous = _units(reading.previous_reading)
    units = calculate_units(previous, admin_value)   # raises if negative

    # If the admin's reading differs from the tenant's, insist on a reason when
    # the setting requires it - that reason is what makes the override auditable.
    customer_value = _units(reading.customer_reading)
    differs = admin_value != customer_value
    if differs and cfg.get("meter.require_override_reason") and not (override_reason or "").strip():
        raise MeterError(
            "Your reading differs from the tenant's submitted reading. "
            "Please give a short reason before approving.",
            status_code=400,
        )

    reading.admin_verified_reading = admin_value
    reading.admin_verified_by = admin_id
    reading.admin_verified_at = now
    reading.admin_note = (admin_note or "").strip() or None
    reading.override_reason = (override_reason or "").strip() or None

    # THE rule: the approved (billable) reading is the admin's verified value.
    reading.approved_reading = admin_value
    reading.approved_by = admin_id
    reading.approved_at = now
    reading.status = "approved"
    reading.calculated_units = units

    result = {
        "units": float(units),
        "previous_reading": float(previous),
        "approved_reading": float(admin_value),
        "override": differs,
        "bill_id": None,
        "bill_amount": None,
        "bill_created": False,
    }

    if not cfg.get("meter.auto_create_bill"):
        logger.info(
            "Reading %s approved without a bill (auto_create_bill is off).", reading.id,
        )
        return result

    bill = build_bill_for_reading(db, reading, meter, units, now)
    db.flush()   # assigns bill.id

    reading.bill_id = bill.id
    breakdown = estimate_bill(db, meter, units, now)
    reading.unit_price_applied = Decimal(str(breakdown["unit_price"]))
    reading.tariff_id = breakdown["tariff_id"]

    result.update({
        "bill_id": bill.id,
        "bill_amount": float(bill.amount),
        "bill_created": True,
        "unit_price": breakdown["unit_price"],
    })
    return result


def reject_reading(db: Session, reading: MeterReading, admin_id: int, reason: str) -> None:
    """
    Reject a pending reading. The photo and the tenant's number are kept for
    audit; the tenant can then submit a fresh photo.
    """
    if reading.status == "approved":
        raise MeterError(
            "This reading has already been approved and billed. Use a bill "
            "adjustment instead of rejecting it.",
            status_code=409,
        )
    if reading.status == "rejected":
        raise MeterError("This reading has already been rejected.", status_code=409)

    if not (reason or "").strip():
        raise MeterError("Please tell the tenant why the reading was rejected.", status_code=400)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reading.status = "rejected"
    reading.rejection_reason = reason.strip()
    reading.rejected_by = admin_id
    reading.rejected_at = now
