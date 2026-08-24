"""
schemas/api.py - Pydantic request/response models for the Tenant Management System API.

Extracted verbatim from app.py as step 1 of the router/service split (see the
"Split app.py into routers/services" effort). This file contains ONLY data
shape definitions - no business logic, no DB access, no FastAPI routes - so it
is safe to import from anywhere (routers, services, tests) without risking
circular imports.

IMPORTANT: every class body below is an exact, unmodified copy of what lived
in app.py. Do not "clean up" field ordering, formatting, or validators here -
any difference, however small, is a behavior change to a live API contract.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════════
# ── Auth ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    mobile:   str = Field(..., example="9999999999")
    password: str = Field(..., example="admin@123")


class LoginResponse(BaseModel):
    success: bool
    token:   str
    role:    str


# ── Complex ───────────────────────────────────
class ComplexCreate(BaseModel):
    name:        str  = Field(..., min_length=1, max_length=150)
    address:     Optional[str] = None
    description: Optional[str] = None


class ComplexUpdate(BaseModel):
    name:        Optional[str] = Field(None, min_length=1, max_length=150)
    address:     Optional[str] = None
    description: Optional[str] = None


class ComplexResponse(BaseModel):
    id:          int
    name:        str
    address:     Optional[str]
    description: Optional[str]
    created_at:  datetime
    updated_at:  datetime

    class Config:
        from_attributes = True


# ── Shop ──────────────────────────────────────
class ShopCreate(BaseModel):
    shop_number:  str             = Field(..., min_length=1, max_length=50)
    area_sqft:    Optional[float] = None
    status:       Optional[str]   = Field("available", pattern="^(available|occupied|maintenance)$")
    complex_id:   Optional[int]   = None
    shop_rent:    Optional[float] = Field(0, ge=0)
    shop_deposit: Optional[float] = Field(0, ge=0)


class ShopUpdate(BaseModel):
    shop_number:  Optional[str]   = Field(None, min_length=1, max_length=50)
    area_sqft:    Optional[float] = None
    status:       Optional[str]   = Field(None, pattern="^(available|occupied|maintenance)$")
    complex_id:   Optional[int]   = None
    shop_rent:    Optional[float] = Field(None, ge=0)
    shop_deposit: Optional[float] = Field(None, ge=0)


class ShopOwnerInfo(BaseModel):
    id:     int
    name:   str
    mobile: str
    agreement_start_date: Optional[datetime] = None
    agreement_end_date:   Optional[datetime] = None


class ShopResponse(BaseModel):
    id:           int
    shop_number:  str
    area_sqft:    Optional[float]
    status:       str
    complex_id:   Optional[int]
    shop_rent:    float
    shop_deposit: float
    created_at:   datetime
    updated_at:   datetime
    assigned_to:  Optional[ShopOwnerInfo] = None

    class Config:
        from_attributes = True


class AssignComplexRequest(BaseModel):
    complex_id: int


# ── User ──────────────────────────────────────
class UserCreate(BaseModel):
    name:     str            = Field(..., min_length=1, max_length=120)
    mobile:   str            = Field(..., min_length=10, max_length=15)
    email:    Optional[str]  = None
    password: str            = Field(..., min_length=6)
    role:     Optional[str]  = Field("tenant", pattern="^(admin|tenant)$")
    rent_bill_date: Optional[int] = Field(None, ge=1, le=28)
    auto_rent_bill_enabled: bool = Field(False, description="If true, the nightly scheduler auto-generates this user's Rent bill on rent_bill_date each month.")


class UserUpdate(BaseModel):
    name:     Optional[str] = Field(None, min_length=1, max_length=120)
    mobile:   Optional[str] = Field(None, min_length=10, max_length=15)
    email:    Optional[str] = None
    password: Optional[str] = Field(None, min_length=6)
    role:     Optional[str] = Field(None, pattern="^(admin|tenant)$")
    is_active:Optional[bool]= None
    rent_bill_date: Optional[int] = Field(None, ge=1, le=28)
    auto_rent_bill_enabled: Optional[bool] = None


class UserResponse(BaseModel):
    id:        int
    name:      str
    mobile:    str
    email:     Optional[str]
    role:      str
    is_active: bool
    rent_bill_date: Optional[int]
    auto_rent_bill_enabled: bool
    created_at:datetime
    updated_at:datetime

    class Config:
        from_attributes = True


class AssignShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)
    force: bool = Field(False, description="If true, reassign shops already owned by another active tenant.")
    agreement_start_date: Optional[datetime] = None
    agreement_end_date:   Optional[datetime] = None


class UpdateAgreementRequest(BaseModel):
    agreement_start_date: Optional[datetime] = None
    agreement_end_date:   Optional[datetime] = None


class DetachShopsRequest(BaseModel):
    shop_ids: List[int] = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4)


# ── Bill ──────────────────────────────────────
class BillCreate(BaseModel):
    user_id:     int
    shop_id:     int
    bill_type:   str               = Field(..., min_length=1, max_length=80,
                                            description='Use "Rent" to auto-fill amount from the agreed rent for this tenant/shop, or any other value (e.g. "Electricity", "Maintenance", "Other") for manual entry.')
    amount:      Optional[float]   = Field(None, gt=0,
                                            description="Required when bill_type is not Rent. Ignored (recomputed) when bill_type is Rent.")
    description: Optional[str]     = None
    due_date:    Optional[datetime] = None
    bill_date: Optional[datetime] = None


class BillResponse(BaseModel):
    id:             int
    user_id:        int
    shop_id:        int
    bill_type:      str
    description:    Optional[str]
    amount:         float
    paid_amount:    float
    pending_amount: float
    bill_date:      datetime
    due_date:       Optional[datetime]
    status:         str
    created_at:     datetime

    class Config:
        from_attributes = True


# ── Payment ───────────────────────────────────
class PaymentCreate(BaseModel):
    bill_id:        int
    amount:         float  = Field(..., gt=0)
    payment_method: str    = Field(..., min_length=1, max_length=60)
    remarks:        Optional[str] = None
    payment_date: Optional[datetime] = None


class PaymentResponse(BaseModel):
    id:             int
    bill_id:        int
    amount:         float
    payment_method: str
    payment_date:   datetime
    remarks:        Optional[str]
    created_at:     datetime

    class Config:
        from_attributes = True


class AutoAllocatePreviewRequest(BaseModel):
    user_id: int
    shop_id: Optional[int] = None   # None = across all shops for this user
    amount:  float = Field(..., gt=0)


class AllocationRow(BaseModel):
    bill_id:        int
    bill_type:      str
    description:    Optional[str]
    shop_number:    Optional[str]
    due_date:       Optional[datetime]
    bill_amount:    float
    outstanding:    float   # pending balance before this allocation
    allocated:      float   # FIFO-suggested amount (admin can edit before confirming)
    resulting_status: str   # what the bill's status would become


class AutoAllocatePreviewResponse(BaseModel):
    user_id:            int
    user_name:           str
    shop_id:            Optional[int]
    rows:                List[AllocationRow]
    amount_received:     float
    total_allocated:     float
    unallocated_amount:  float


class ConfirmAllocationItem(BaseModel):
    bill_id: int
    amount:  float = Field(..., gt=0)


class AutoAllocateConfirmRequest(BaseModel):
    user_id:         int
    amount_received: float = Field(..., gt=0)
    payment_method:  str   = Field(..., min_length=1, max_length=60)
    remarks:         Optional[str] = None
    allocations:     List[ConfirmAllocationItem] = Field(..., min_length=1)
    payment_date: Optional[datetime] = None


class AutoAllocateResult(BaseModel):
    bill_id:     int
    allocated:   float
    bill_status: str
    note:        Optional[str] = None   # e.g. "capped to outstanding balance"


class AutoAllocateResponse(BaseModel):
    payments:           List[PaymentResponse]
    allocations:        List[AutoAllocateResult]
    total_allocated:    float
    unallocated_amount: float


# ── Deposit Payment ───────────────────────────
class DepositPaymentCreate(BaseModel):
    user_id:      int
    shop_id:      int
    amount:       float           = Field(..., gt=0)
    payment_date: Optional[datetime] = None
    remarks:      Optional[str]   = None


class DepositPaymentResponse(BaseModel):
    id:           int
    user_id:      int
    shop_id:      int
    shop_number:  str
    amount:       float
    payment_date: datetime
    remarks:      Optional[str]
    created_at:   datetime

    class Config:
        from_attributes = True


# ── Razorpay (online tenant payments) ─────────
class RazorpayCreateOrderRequest(BaseModel):
    # Omit to pay across the tenant's WHOLE pending balance (every unpaid
    # bill, oldest due date first) rather than one specific bill.
    bill_id: Optional[int] = None
    # Rupees. Omit to pay the full amount owed (that one bill, or the whole
    # balance). Server always caps this at what's actually pending - this
    # is a ceiling the tenant can pay less than, never a promise to exceed.
    amount: Optional[float] = Field(None, gt=0)


class RazorpayCreateOrderResponse(BaseModel):
    order_id: str
    amount:   int    # paise - what checkout.js needs
    currency: str
    key_id:   str
    bill_id:  Optional[int]   # None when this order pays across all bills


class RazorpayVerifyRequest(BaseModel):
    razorpay_order_id:   str = Field(..., min_length=1)
    razorpay_payment_id: str = Field(..., min_length=1)
    razorpay_signature:  str = Field(..., min_length=1)


# ══════════════════════════════════════════════════════════════════════════════
# ── Bill / Payment / Deposit Payment partial-update schemas
# (originally appended later in app.py, near the audit log routes)
# ══════════════════════════════════════════════════════════════════════════════

class BillUpdate(BaseModel):
    """Partial-update schema for bills.  Only supplied fields are changed."""
    bill_type:   Optional[str]      = Field(None, min_length=1, max_length=80)
    description: Optional[str]      = None
    amount:      Optional[float]    = Field(None, gt=0)
    # The date the bill is *for*. Editable because bills are often entered a
    # few days after the fact, and the month a bill belongs to drives the
    # tenant's monthly view and every month-wise report.
    bill_date:   Optional[datetime] = None
    due_date:    Optional[datetime] = None
    status:      Optional[str]      = Field(None, pattern="^(pending|partial|paid|cancelled)$")


class PaymentUpdate(BaseModel):
    """Partial-update schema for payments."""
    amount:         Optional[float] = Field(None, gt=0)
    payment_method: Optional[str]   = Field(None, min_length=1, max_length=60)
    payment_date:   Optional[datetime] = None
    remarks:        Optional[str]   = None


class DepositPaymentUpdate(BaseModel):
    """Partial-update schema for deposit payments."""
    amount:       Optional[float]    = Field(None, gt=0)
    payment_date: Optional[datetime] = None
    remarks:      Optional[str]      = None


# ══════════════════════════════════════════════════════════════════════════════
# ── Submeter readings / settings
# (originally defined just above the meter routes in app.py)
# ══════════════════════════════════════════════════════════════════════════════

class MeterCreate(BaseModel):
    # Optional: a meter can be registered before it is fitted to a shop and
    # assigned later from the Submeters screen.
    shop_id:           Optional[int] = None
    meter_number:      str = Field(..., min_length=1, max_length=60)
    meter_type:        Optional[str] = Field("electricity", max_length=40)
    initial_reading:   Optional[float] = Field(0, ge=0)
    installation_date: Optional[datetime] = None
    notes:             Optional[str] = None
    is_active:         Optional[bool] = True


class MeterUpdate(BaseModel):
    meter_number:      Optional[str] = Field(None, min_length=1, max_length=60)
    meter_type:        Optional[str] = Field(None, max_length=40)
    initial_reading:   Optional[float] = Field(None, ge=0)
    installation_date: Optional[datetime] = None
    notes:             Optional[str] = None
    is_active:         Optional[bool] = None


class TariffCreate(BaseModel):
    meter_type:     Optional[str]  = Field("electricity", max_length=40)
    unit_price:     float          = Field(..., gt=0)
    fixed_charge:   Optional[float] = Field(0, ge=0)
    tax_percent:    Optional[float] = Field(0, ge=0, le=100)
    effective_from: datetime
    notes:          Optional[str]  = None


class VerifyReadingRequest(BaseModel):
    """Saves the admin's reading without approving - lets them come back to it."""
    admin_verified_reading: float = Field(..., ge=0)
    admin_note:             Optional[str] = None


class ApproveReadingRequest(BaseModel):
    admin_verified_reading: float = Field(..., ge=0)
    override_reason:        Optional[str] = None
    admin_note:             Optional[str] = None


class RejectReadingRequest(BaseModel):
    reason: str = Field(..., min_length=1)


class AssignMeterShopRequest(BaseModel):
    shop_id: int


class SettingsUpdateRequest(BaseModel):
    values: dict
