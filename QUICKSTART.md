# Quick Start Guide - Reports & PDF Download Feature

## 🚀 What's New

After user login, admin users can now:
1. ✅ View **Pending Bills Report** with filters
2. ✅ View **Payments Report** with filters  
3. ✅ **Download Reports as PDF** files
4. ✅ Filter by date range, complex, user, payment method

## 📋 Files Added/Modified

### New Files Created:
1. **reports.html** - Standalone reports UI page
2. **REPORT_FEATURES.md** - Complete API documentation
3. **IMPLEMENTATION_SUMMARY.md** - Technical implementation details
4. **FRONTEND_GUIDE.md** - Frontend integration guide
5. **QUICKSTART.md** - This file

### Modified Files:
1. **requirements.txt** - Added `reportlab==4.0.9`
2. **app.py** - Added 4 new endpoints + PDF helper function

## 🎯 Quick Setup (5 minutes)

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2: Start Backend
```bash
python app.py
```
Server will run at: `http://localhost:8000`

### Step 3: Login
1. Open `tenant-management-system.html` in browser
2. Login with admin credentials
3. Get JWT token (stored in localStorage)

### Step 4: Access Reports
Option A - Direct:
```
Open reports.html in browser
```

Option B - From main app:
```
Add link to tenant-management-system.html sidebar
```

## 📊 Using the Reports

### Pending Bills Report

**Steps:**
1. Go to "Pending Bills" tab
2. Set date range (optional)
3. Select Complex (optional)
4. Select User/Tenant (optional)
5. Click "Load Report" → See results
6. Click "Download PDF" → Get PDF file

**What You'll See:**
- Summary: Total bills count + total pending amount
- Table with: Bill ID, Tenant, Shop, Amount, Pending, Status, Date
- Status badges: pending (orange), partial (yellow), paid (green)

### Payments Report

**Steps:**
1. Go to "Payments" tab
2. Set date range (optional)
3. Select Complex (optional)
4. Select User/Tenant (optional)
5. Select Payment Method (optional)
6. Click "Load Report" → See results
7. Click "Download PDF" → Get PDF file

**What You'll See:**
- Summary: Total payments count + total collected amount
- Table with: Payment ID, Tenant, Shop, Bill Type, Amount, Method, Date
- Color-coded amounts (green for collected)

## 🔗 API Endpoints

All endpoints require JWT token in header:
```
Authorization: Bearer <JWT_TOKEN>
```

### Get Pending Bills
```
GET /api/reports/pending-bills
```
**Filters:** start_date, end_date, complex_id, user_id

### Download Pending Bills PDF
```
GET /api/reports/pending-bills/download
```
**Same filters as above, returns PDF file**

### Get Payments
```
GET /api/reports/payments
```
**Filters:** start_date, end_date, complex_id, user_id, payment_method

### Download Payments PDF
```
GET /api/reports/payments/download
```
**Same filters as above, returns PDF file**

## 📅 Date Format

Use ISO format: **YYYY-MM-DD**

Examples:
- January 15, 2026 → `2026-01-15`
- December 31, 2025 → `2025-12-31`

## 🎨 Styling

- Professional design with color-coded status badges
- Responsive layout (desktop, tablet, mobile)
- Dark blue headers (#1F4D38)
- Green accent colors (#2F6F4F)
- Rust/Orange for pending amounts (#C0612B)

## 🔒 Security

✅ JWT authentication required
✅ Admin-only access
✅ Token stored in localStorage
✅ API validates all requests

## 📝 Example Workflows

### Workflow 1: Monthly Pending Bills Report
1. In "Pending Bills" tab
2. Set Start Date: `2026-01-01`
3. Set End Date: `2026-01-31`
4. Click "Load Report"
5. Click "Download PDF" → `pending_bills_20260130_143000.pdf`

### Workflow 2: Complex-specific Collections
1. In "Payments" tab
2. Select Complex: "Downtown Complex"
3. Click "Load Report"
4. See total collected for that complex
5. Download as PDF for record keeping

### Workflow 3: User Financial Summary
1. In "Payments" tab
2. Select User: "John Doe"
3. Set date range (optional)
4. Click "Load Report"
5. See all payments from that user
6. Download for verification

### Workflow 4: Payment Method Analysis
1. In "Payments" tab
2. Select Payment Method: "Online Transfer"
3. Click "Load Report"
4. See only online payments
5. Download for accounting

## 🐛 Troubleshooting

### Issue: "No Pending Bills" message
**Solution:** 
- Check if date range is correct
- Try removing date filters
- Verify data exists in database

### Issue: PDF download shows blank page
**Solution:**
- Check backend is running
- Verify JWT token is valid
- Try refresh and login again

### Issue: Dropdowns are empty
**Solution:**
- Ensure you're logged in as admin
- Check if complexes/users exist in database
- Refresh the page

### Issue: "Invalid token" error
**Solution:**
- Login again to get fresh token
- Clear localStorage: `localStorage.clear()`
- Refresh page

## 💡 Tips & Tricks

1. **Quick Access:** Bookmark `reports.html` for quick access
2. **Filters:** Combine multiple filters for precise reports
3. **Date Range:** Use monthly ranges for better performance
4. **PDF Naming:** PDFs auto-download with timestamp
5. **Reset:** Use "Reset Filters" to clear all selections

## 📞 Need Help?

Check these documents:
1. **REPORT_FEATURES.md** - Complete API reference
2. **IMPLEMENTATION_SUMMARY.md** - Technical details
3. **FRONTEND_GUIDE.md** - Integration guide
4. Backend console for error messages

## ✨ Features Summary

| Feature | Status |
|---------|--------|
| Pending Bills Report | ✅ Complete |
| Pending Bills PDF | ✅ Complete |
| Payment Reports | ✅ Complete |
| Payment PDF | ✅ Complete |
| Date Filters | ✅ Complete |
| Complex Filter | ✅ Complete |
| User Filter | ✅ Complete |
| Payment Method Filter | ✅ Complete |
| Summary Cards | ✅ Complete |
| Responsive Design | ✅ Complete |
| Authentication | ✅ Complete |

## 🎓 Learning Path

1. **Beginner:** Open reports.html and explore UI
2. **Intermediate:** Try different filter combinations
3. **Advanced:** Study REPORT_FEATURES.md for API details
4. **Expert:** Customize FRONTEND_GUIDE.md code

## 📦 Deployment

### Development
```bash
# Terminal 1: Backend
python app.py

# Terminal 2: Serve HTML (optional)
# Python simple server
python -m http.server 8001
```

### Production
1. Set JWT_SECRET in environment variables
2. Update API_BASE in reports.html to production URL
3. Use HTTPS for all connections
4. Secure localStorage token with httpOnly cookies

## 🔄 Workflow Summary

```
User Login (tenant-management-system.html)
    ↓
Get JWT Token
    ↓
Access Reports (reports.html)
    ↓
Select Filters (date, complex, user, method)
    ↓
View JSON Report or Download PDF
    ↓
Use data for decision making
```

---

**Version:** 1.0  
**Last Updated:** 2026-06-30  
**Status:** ✅ Production Ready

Get started now by opening `reports.html` after login! 🚀
