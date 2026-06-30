# Post-Login Report Features with Filters & PDF Download

## Overview
After user login, admin users can now generate and download filtered reports in PDF format. This document outlines the new endpoints and their usage.

## New Endpoints

### 1. Pending Bills Report (JSON)
**Endpoint:** `GET /api/reports/pending-bills`

**Authentication:** Admin only (JWT required)

**Parameters:**
- `start_date` (optional): ISO 8601 datetime - Filter bills from this date
- `end_date` (optional): ISO 8601 datetime - Filter bills until this date
- `complex_id` (optional): Integer - Filter by complex
- `user_id` (optional): Integer - Filter by user/tenant

**Response:**
```json
{
  "success": true,
  "period": {
    "start_date": "2026-01-01T00:00:00",
    "end_date": "2026-01-31T23:59:59"
  },
  "filters": {
    "complex_id": null,
    "user_id": null
  },
  "summary": {
    "total_bills": 45,
    "total_pending_amount": 125000.50
  },
  "records": [
    {
      "bill_id": 123,
      "user_id": 5,
      "user_name": "John Doe",
      "mobile": "9876543210",
      "shop_id": 10,
      "shop_number": "A-101",
      "complex_id": 1,
      "complex_name": "Downtown Complex",
      "bill_type": "Rent",
      "bill_date": "2026-01-15T10:30:00",
      "due_date": "2026-02-15T00:00:00",
      "amount": 5000.00,
      "paid_amount": 2000.00,
      "pending_amount": 3000.00,
      "status": "partial"
    }
  ]
}
```

**Example Requests:**
```bash
# All pending bills
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills"

# Pending bills from January 2026
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills?start_date=2026-01-01&end_date=2026-01-31"

# Pending bills for complex ID 1
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills?complex_id=1"

# Pending bills for user ID 5
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills?user_id=5"

# Combined filters
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills?start_date=2026-01-01&end_date=2026-01-31&complex_id=1"
```

---

### 2. Download Pending Bills Report as PDF
**Endpoint:** `GET /api/reports/pending-bills/download`

**Authentication:** Admin only (JWT required)

**Parameters:** Same as pending bills report endpoint
- `start_date` (optional): ISO 8601 datetime
- `end_date` (optional): ISO 8601 datetime
- `complex_id` (optional): Integer
- `user_id` (optional): Integer

**Response:** PDF file with formatted table

**Features:**
- Professional table layout with headers
- All pending and partial bills included
- Total pending amount calculated at bottom
- Generated timestamp included
- Responsive column widths
- Color-coded headers

**Example Request:**
```bash
# Download pending bills PDF
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills/download?start_date=2026-01-01&end_date=2026-01-31" \
  -o pending_bills.pdf

# Download with complex filter
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/pending-bills/download?complex_id=1" \
  -o pending_bills_complex_1.pdf
```

---

### 3. Payments Report (JSON)
**Endpoint:** `GET /api/reports/payments`

**Authentication:** Admin only (JWT required)

**Parameters:**
- `start_date` (optional): ISO 8601 datetime - Filter payments from this date
- `end_date` (optional): ISO 8601 datetime - Filter payments until this date
- `complex_id` (optional): Integer - Filter by complex
- `user_id` (optional): Integer - Filter by user/tenant
- `payment_method` (optional): String - Filter by payment method (e.g., "Cash", "Cheque", "Online")

**Response:**
```json
{
  "success": true,
  "period": {
    "start_date": "2026-01-01T00:00:00",
    "end_date": "2026-01-31T23:59:59"
  },
  "filters": {
    "complex_id": null,
    "user_id": null,
    "payment_method": null
  },
  "summary": {
    "total_payments": 32,
    "total_collected": 250000.00
  },
  "records": [
    {
      "payment_id": 456,
      "bill_id": 123,
      "user_id": 5,
      "user_name": "John Doe",
      "mobile": "9876543210",
      "shop_id": 10,
      "shop_number": "A-101",
      "complex_id": 1,
      "complex_name": "Downtown Complex",
      "bill_type": "Rent",
      "payment_method": "Online Transfer",
      "amount": 5000.00,
      "payment_date": "2026-01-20T14:30:00",
      "remarks": "Partial payment for January rent"
    }
  ]
}
```

**Example Requests:**
```bash
# All payments in January 2026
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments?start_date=2026-01-01&end_date=2026-01-31"

# Payments by cash only
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments?payment_method=Cash"

# Payments from complex 1 in January
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments?complex_id=1&start_date=2026-01-01&end_date=2026-01-31"

# Payments from user 5
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments?user_id=5"
```

---

### 4. Download Payments Report as PDF
**Endpoint:** `GET /api/reports/payments/download`

**Authentication:** Admin only (JWT required)

**Parameters:** Same as payments report endpoint
- `start_date` (optional): ISO 8601 datetime
- `end_date` (optional): ISO 8601 datetime
- `complex_id` (optional): Integer
- `user_id` (optional): Integer
- `payment_method` (optional): String

**Response:** PDF file with formatted table

**Features:**
- Professional table layout with headers
- All payments in selected period
- Total collected amount at bottom
- Generated timestamp included
- Sortable by payment date

**Example Request:**
```bash
# Download all payments as PDF
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments/download" \
  -o payments_all.pdf

# Download January payments with complex filter
curl -H "Authorization: Bearer <TOKEN>" \
  "http://localhost:8000/api/reports/payments/download?start_date=2026-01-01&end_date=2026-01-31&complex_id=1" \
  -o payments_jan_complex1.pdf
```

---

## Date Format

Use ISO 8601 format for date parameters:
- `YYYY-MM-DD` (date only)
- `YYYY-MM-DDTHH:MM:SS` (datetime with time)

Examples:
- `2026-01-15` - January 15, 2026 (00:00:00)
- `2026-01-15T14:30:00` - January 15, 2026 at 2:30 PM
- `2026-01-01T00:00:00` - January 1, 2026 at midnight

---

## Filter Combinations

All filters are optional and can be combined:

**Scenario 1:** Get pending bills for a specific tenant in January
```
GET /api/reports/pending-bills?start_date=2026-01-01&end_date=2026-01-31&user_id=5
```

**Scenario 2:** Get all payments by cash from a complex
```
GET /api/reports/payments?complex_id=1&payment_method=Cash
```

**Scenario 3:** Get pending bills for a complex (without date range - all time)
```
GET /api/reports/pending-bills?complex_id=1
```

**Scenario 4:** Download payments report for Dec 2025 to Jan 2026
```
GET /api/reports/payments/download?start_date=2025-12-01&end_date=2026-01-31
```

---

## PDF Download Features

### Filename Convention
Downloaded PDF files follow this naming pattern:
- Pending bills: `pending_bills_YYYYMMDD_HHMMSS.pdf`
- Payments: `payment_report_YYYYMMDD_HHMMSS.pdf`

Example: `pending_bills_20260120_143000.pdf`

### PDF Content
Each PDF includes:
- **Title:** Report name with applied date range
- **Table Header:** Column names with blue background
- **Data Rows:** Formatted data with alternating row colors
- **Summary Row:** Total amounts (for pending/collected)
- **Timestamp:** Generation date and time

### Columns in Pending Bills PDF
| Column | Description |
|--------|-------------|
| Bill ID | Unique bill identifier |
| User Name | Tenant name |
| Mobile | Tenant mobile number |
| Shop | Shop number/identifier |
| Complex | Complex name |
| Amount | Total bill amount |
| Paid | Amount already paid |
| Pending | Outstanding amount |
| Status | pending/partial/paid |
| Date | Bill date |

### Columns in Payments PDF
| Column | Description |
|--------|-------------|
| Payment ID | Unique payment identifier |
| User Name | Tenant name |
| Mobile | Tenant mobile number |
| Shop | Shop number/identifier |
| Complex | Complex name |
| Bill Type | Type of bill (Rent, Other, etc.) |
| Amount | Payment amount |
| Method | Payment method (Cash, Check, etc.) |
| Date | Payment date |

---

## Security & Access Control

- **Authentication Required:** All endpoints require valid JWT token
- **Admin Only:** All report endpoints require admin role
- **Tenant Restrictions:** Tenants cannot access these endpoints
- **Audit Logging:** All report access is logged (future enhancement)

---

## Dependencies

The following dependencies are required (already included in requirements.txt):

- `reportlab==4.0.9` - PDF generation
- `fastapi==0.115.0` - Web framework
- `sqlalchemy==2.0.35` - Database ORM
- `python-jose[cryptography]==3.3.0` - JWT handling

---

## Technical Implementation

### PDF Generation
- Uses ReportLab library for creating professional PDFs
- Supports table formatting with colors and borders
- Generates in-memory PDF (no server-side file storage)
- Returns as downloadable file response

### Query Filters
- Date filtering uses SQL comparison operators
- Complex filtering via JOIN operations
- Efficient database queries with indexed lookups
- Results ordered by most recent first

---

## Future Enhancements

1. **Deposit Report** - Similar filtering for deposit payments
2. **User-wise Financial Summary PDF** - Complete financial report per tenant
3. **Rent Collection Report PDF** - Enhanced rent collection analytics
4. **Email Reports** - Scheduled report delivery via email
5. **Chart/Graph Reports** - Visual representations of data
6. **Custom Report Builder** - Admin UI for custom queries

---

## Support

For questions or issues with report features:
1. Check the endpoint documentation above
2. Verify your date format (ISO 8601)
3. Ensure JWT token is valid and has admin role
4. Check database connectivity

