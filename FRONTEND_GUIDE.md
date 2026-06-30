# Reports Frontend Integration Guide

## Overview
A dedicated reports page (`reports.html`) has been created with a clean, professional UI for viewing and downloading pending bills and payment reports with filters.

## Features

### 1. **Pending Bills Report Tab**
- View all pending and partially paid bills
- Filter by:
  - Date range (start date, end date)
  - Complex
  - User/Tenant
- Summary cards showing:
  - Total number of pending bills
  - Total pending amount
- Professional table with:
  - Bill ID, Tenant name, Mobile, Shop, Complex
  - Bill type, Amount, Paid, Pending
  - Status badge, Bill date
- **Download as PDF** with all applied filters

### 2. **Payments Report Tab**
- View all payments made
- Filter by:
  - Date range (start date, end date)
  - Complex
  - User/Tenant
  - Payment method (Cash, Cheque, Online Transfer, Credit Card)
- Summary cards showing:
  - Total number of payments
  - Total amount collected
- Professional table with:
  - Payment ID, Tenant, Shop, Complex
  - Bill type, Amount, Payment method
  - Payment date, Remarks
- **Download as PDF** with all applied filters

## File Structure

```
/shopmanagement
├── app.py                          # Backend API (already updated)
├── requirements.txt                # Dependencies (already updated)
├── reports.html                    # NEW: Standalone reports page
├── tenant-management-system.html   # Existing main app
├── REPORT_FEATURES.md             # API documentation
└── IMPLEMENTATION_SUMMARY.md      # Technical details
```

## How to Use

### 1. Direct Access (Standalone)
Open `reports.html` directly in your browser after logging in:
```
http://localhost:8000/reports.html
```

Or navigate from a link:
```html
<a href="reports.html">View Reports</a>
```

### 2. Integration with Main App
If you want to integrate the reports into the existing `tenant-management-system.html`, you can:

**Option A: Embed as Modal/View**
Add a Reports view to the main app's view switcher:

```javascript
// In tenant-management-system.html, add to views object:
case 'reports': 
  window.location.href = 'reports.html'; 
  break;
```

**Option B: Add Navigation Link**
Add a link in the sidebar:
```html
<a href="reports.html" class="nav-item">
  📊 Reports
</a>
```

### 3. Login Flow
The reports page automatically checks for a valid JWT token in localStorage:
- If token exists → loads page with dropdowns
- If token missing → redirects to login page

## Features & UI Components

### Filter System
- **Date Pickers:** ISO date format (YYYY-MM-DD)
- **Dropdowns:** Auto-populated from API
  - Complexes list
  - Users/Tenants list
  - Payment methods (predefined)
- **Action Buttons:**
  - Load Report → Fetches data with filters
  - Download PDF → Generates and downloads PDF
  - Reset Filters → Clears all filter values

### Data Display
- **Summary Cards:** Quick metrics at a glance
  - Total count (bills/payments)
  - Total amount (pending/collected)
- **Tables:** Responsive data grid
  - Sortable headers
  - Hover effects
  - Color-coded status badges
  - Currency formatting (₹)
  - Truncated on mobile

### Status Badges
- **Paid:** Green badge
- **Pending:** Orange/rust badge
- **Partial:** Yellow badge

## API Integration

### Pending Bills Endpoint
```
GET /api/reports/pending-bills
Query Params:
  - start_date (ISO datetime) - optional
  - end_date (ISO datetime) - optional
  - complex_id (integer) - optional
  - user_id (integer) - optional
```

### Download Pending Bills PDF
```
GET /api/reports/pending-bills/download
Query Params: (same as above)
Response: PDF file
```

### Payments Endpoint
```
GET /api/reports/payments
Query Params:
  - start_date (ISO datetime) - optional
  - end_date (ISO datetime) - optional
  - complex_id (integer) - optional
  - user_id (integer) - optional
  - payment_method (string) - optional
```

### Download Payments PDF
```
GET /api/reports/payments/download
Query Params: (same as above)
Response: PDF file
```

## Styling & Theming

The page uses a custom color scheme matching the main app:
```css
--green: #2F6F4F          (Primary button)
--green-deep: #1F4D38     (Table header)
--rust: #C0612B           (Pending status)
--success: #1F8A6B        (Paid/Collected)
--paper: #F7F4EC          (Background)
--paper-raised: #FFFFFF   (Cards)
```

## Responsive Design

- **Desktop (>768px):** 2-4 column grid filters
- **Tablet (768px):** 2 column filters
- **Mobile (<768px):** Single column filters, full-width tables

## Browser Compatibility

- Chrome 90+
- Firefox 88+
- Safari 14+
- Edge 90+

Requires:
- ES6+ support (async/await, fetch API)
- LocalStorage API
- CSS Grid & Flexbox

## Customization

### Adding New Report Types

1. Add a new tab button:
```html
<button class="tab-btn" data-tab="new-report">New Report</button>
```

2. Add tab content:
```html
<div id="new-report" class="tab-content">
  <!-- New report UI -->
</div>
```

3. Add handler functions:
```javascript
async function loadNewReport() { /* ... */ }
async function downloadNewReportPDF() { /* ... */ }
function clearNewReportFilters() { /* ... */ }
```

### Customizing Filters

To add a new filter field:

1. Add HTML input:
```html
<div class="filter-group">
  <label for="pb-new-filter">New Filter</label>
  <input type="text" id="pb-new-filter">
</div>
```

2. Update `getFilterParams()` function:
```javascript
const newFilter = document.getElementById(`${prefix}-new-filter`).value;
if (newFilter) params.append('new_filter', newFilter);
```

### Styling Customization

Edit CSS variables in `<style>`:
```css
:root {
  --green: #your-color;
  --font-body: 'Your Font';
  /* etc */
}
```

## Troubleshooting

### Reports Show "No Data"
- Verify date filters are correct
- Check if there are actually bills/payments in the database
- Try resetting filters

### PDF Download Fails
- Check backend is running: `python app.py`
- Verify JWT token is valid
- Check browser console for errors

### Dropdowns Empty
- Verify you're logged in with admin account
- Check if there are complexes and users in the database
- Check network tab in developer tools

### Filter Values Not Applying
- Ensure date format is YYYY-MM-DD
- Verify complex_id and user_id are valid
- Check browser console for JavaScript errors

## Security Notes

- JWT token stored in localStorage (consider using secure cookies for production)
- All API calls require valid authentication
- Admin role required for all reports
- URL parameters are sanitized before sending to API

## Performance Tips

- Date ranges: Use shorter ranges for faster queries (e.g., monthly instead of yearly)
- Complex filter: Pre-select a specific complex to reduce results
- User filter: Filter by single tenant for focused reports
- Clear filters before changing report type to reset data

## Future Enhancements

1. **Export Formats:** CSV, Excel
2. **Advanced Filtering:** Bill type filter for payments
3. **Charts/Graphs:** Visual data representation
4. **Scheduled Reports:** Automated report delivery
5. **Report Templates:** Save custom filter combinations
6. **Multi-select:** Multiple complexes/users in one report
7. **Print Friendly:** Browser print optimization

## Support & Documentation

- **API Docs:** See `REPORT_FEATURES.md`
- **Implementation:** See `IMPLEMENTATION_SUMMARY.md`
- **Main App:** `tenant-management-system.html`

---

**Version:** 1.0  
**Created:** 2026-06-30  
**Status:** ✅ Ready for Use
