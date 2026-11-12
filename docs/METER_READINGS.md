# Submeter readings — how it works

Electricity submeter billing, automated as far as it safely can be while keeping
you in control of every rupee.

---

## What changed vs. your old routine

**Before:** you drove to the shop, photographed the meter, looked up the last
reading, worked out the units, checked the rate, and wrote the bill by hand.

**Now:** the tenant photographs their own meter and types the number. You get a
queue of submissions; on each one you see their photo, the previous reading and
their number, you type what *you* read off the photo, and press Approve. The
bill is calculated and raised for you.

You never stop being the authority — you just stop doing the driving, the
arithmetic and the paperwork.

---

## The rule everything is built around

> **The bill is calculated from YOUR verified reading. Never from the tenant's.**

The tenant's number and photo are evidence to help you decide. The system will
not raise a bill until an admin has looked at the photo and typed a reading.
If your number differs from theirs, it says so plainly and asks for a one-line
reason — which is stored permanently against that reading.

---

## The flow

```
TENANT                                    YOU (ADMIN)
  |                                          |
  photographs the meter                      |
  types the number                           |
  taps "Send to office"                      |
  |                                          |
  +---> waiting for review  ---------------> sidebar badge: "1"
                                             |
                                             opens the reading
                                             sees the photo, zooms in
                                             sees previous: 12,450
                                             sees tenant said: 12,732
                                             |
                                             types what THEY read: 12,730
                                             |
                                             system shows live:
                                               ⚠ differs from tenant by -2
                                               280 units x Rs 9.50 = Rs 2,660
                                             |
                                             types reason, presses Approve
                                             |
  <--- sees "confirmed" + the bill <-------- ONE bill created, Rs 2,660
```

Rejecting instead of approving records your reason, raises no bill, and lets the
tenant send a fresh photo.

---

## Where things are in the app

**Admin → Finance → Meter Readings**, three tabs:

| Tab | What it's for |
|---|---|
| **Review readings** | Your queue. Pending first, oldest at the top. The sidebar badge shows how many are waiting. |
| **Meters** | Register a submeter against a shop; set the reading on the day it was installed. |
| **Unit price** | The rate per unit, with history. |

**Admin → Insights → Settings** — rename the app, change the words it uses,
photo size limits, warning thresholds, payment window, and whether approving
should raise a bill at all.

**Tenant portal** — an "Electricity meter" card appears on their home page
showing their last confirmed reading and one button to send a new one. Tenants
with no submeter never see it.

---

## Things worth knowing

**The unit price has history.** To change the rate you *add a new one* with the
date it starts from — you never edit the old one. A bill raised in June at
Rs 9.00 stays Rs 9.00 forever, even after you put the rate up to Rs 9.50 in July.
Set this up before approving your first reading, or there's no price to bill at.

**The first bill only charges from installation.** When you register a meter you
enter the reading on its face that day. The first bill counts up from there, not
from zero. Once that meter has an approved reading, this field locks — changing
it would silently rewrite the basis of a bill you already issued.

**The previous reading always comes from the last *approved* reading.** Pending
and rejected submissions are ignored. This is why a bill can never be built on a
number nobody checked.

**A reading below the previous one is refused outright** — no bill, with an
explanation. That usually means the meter was replaced or the photo was misread.
If the meter really was replaced, reject the reading and register a new meter.

**Warnings never block you.** Zero consumption, unusually high consumption, and
a big jump against this meter's own average are all flagged for your attention.
You can still approve — you're the one who can see the photo.

**Approving twice can't bill twice.** The second click is refused, and the
database itself enforces one bill per approved reading. If anything fails
mid-approval (say the rate is missing), the whole thing rolls back — you'll
never find a reading marked approved with no bill behind it.

**One open submission per meter.** A tenant can't queue up five readings for the
same meter while you're reviewing the first.

**The photo is kept.** It's the evidence behind the bill. Original file, never
overwritten, retrievable later for any dispute.

---

## Photos: where they live and who can see them

Photos are stored **on your own server's disk**, not in a cloud bucket — nothing
to pay for, no AWS account, no keys to manage.

They're deliberately **not** in a public folder and have no public URL. The only
way to view one is through an endpoint that checks who's asking:

- an admin can view any photo
- a tenant can view only their own
- anyone else gets a 404 — the response won't even confirm the reading exists

The folder is set in **Settings → Meter readings → Photo storage folder**
(default `uploads/meter-photos`, organised as `shop-4/meter-7/2026/08/…`).
Two things to do on your side: **back that folder up** along with your database,
and if you're on Docker, **mount it as a volume** so photos survive a container
rebuild:

```
docker run -v /srv/tms-photos:/app/uploads/meter-photos ...
```

Uploads are checked by their actual file signature, not just the extension — a
program renamed to `.jpg` is rejected. Size limit is configurable (default 10 MB).

---

## Files

**New backend**

| File | Why |
|---|---|
| `meter_service.py` | All the rules that decide money — consumption, tariff lookup, comparison, anomalies, approval. Separate from `app.py` so it can be read and tested on its own. |
| `photo_storage.py` | Saving/reading photos with validation. Swapping to S3 later means changing only this file. |
| `settings_service.py` | Runtime configuration with typed defaults, so settings can be added later without a migration. |

**Modified**

| File | Change |
|---|---|
| `create_tables.py` | Added `meters`, `meter_tariffs`, `meter_readings`, `app_settings`. Seeds a starting rate of Rs 8.00/unit. Existing tables untouched. |
| `app.py` | Added the meter/tariff/reading/settings endpoints at the end. Nothing existing was altered beyond the import line. |
| `db_config.py` | `DATABASE_URL` can now be overridden by env var (used by the tests). Default behaviour unchanged. |
| `requirements.txt` | Added `python-multipart` (photo uploads), `pytest`, `httpx`. |
| `Dockerfile` | Copies the three new modules. |

**Frontend**

| File | Change |
|---|---|
| `ADMIN/js/meters.js` | New — review queue, verification screen, meters, tariffs. |
| `ADMIN/js/settings.js` | New — settings form, generated from the API schema. |
| `USER/js/tenant-meters.js` | New — the tenant's send-a-reading card and history. |
| `ADMIN/index.html`, `js/nav.js`, `js/init.js` | Two new nav items, routes, sidebar badge. |
| `USER/index.html`, `js/ui-helpers.js`, `js/tenant-dashboard.js` | Modal shell + mounting the meter card. |
| `ADMIN/style.css`, `USER/style.css` | Styles for the new screens, using the existing design tokens. |

---

## Database

**`meters`** — a submeter on a shop. `(shop_id, meter_number)` unique.

**`meter_tariffs`** — unit price, fixed charge, tax %, effective from a date.
Indexed on `(meter_type, effective_from)` for the "rate live on this date" lookup.

**`meter_readings`** — the whole history of one submission on one row:
what the tenant sent, the photo, what you verified, what was approved, the units,
the rate applied, and the resulting bill.

- `bill_id` is **UNIQUE** — the database-level guarantee of one bill per reading
- indexed on `(status, reading_date)` and `(meter_id, status)` for the queue and
  the previous-reading lookup

**`app_settings`** — key/value config. A row only exists once you've changed
something from its default, so new settings in a future version work on your
existing database with no migration.

---

## API

All endpoints require a bearer token. Admin ones return **403** for tenants.

### Tenant

| Method | URL | Notes |
|---|---|---|
| `GET` | `/api/tenant/meters` | Their meters + what the next reading must exceed |
| `POST` | `/api/tenant/meter-readings` | `multipart/form-data`: `meter_id`, `customer_reading`, optional `customer_note`, `photo`. One request, no separate upload step. |
| `GET` | `/api/tenant/meter-readings` | Their submissions (`?status=` optional) |
| `GET` | `/api/tenant/meter-readings/{id}` | 404 if it isn't theirs |

### Admin

| Method | URL | Notes |
|---|---|---|
| `GET` | `/api/meter-readings` | Queue. `?status=pending`, `complex_id`, `shop_id`, `user_id` |
| `GET` | `/api/meter-readings/{id}` | Everything for the review screen |
| `POST` | `/api/meter-readings/{id}/preview` | Dry run — what would happen at this reading. Changes nothing. |
| `PATCH` | `/api/meter-readings/{id}/verify` | Save your reading without approving |
| `POST` | `/api/meter-readings/{id}/approve` | `admin_verified_reading`, optional `override_reason`. Transactional. |
| `POST` | `/api/meter-readings/{id}/reject` | `reason` required |
| `GET` | `/api/meter-readings/{id}/photo` | Owner or admin only |
| `GET/POST/PUT/DELETE` | `/api/meters`, `/api/meters/{id}` | Meter management |
| `GET/POST/DELETE` | `/api/meter-tariffs` | Rate history |
| `GET/PUT/POST` | `/api/settings`, `/api/settings/reset` | Configuration |
| `GET` | `/api/settings/public` | App name/tagline only — the one unauthenticated endpoint |

**Error codes:** `400` bad reading, bad photo, missing rate, missing override
reason · `403` wrong role or not your meter · `404` not found / not yours ·
`409` already approved, already rejected, or a submission already pending ·
`500` disk failure (nothing is saved).

---

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

81 tests, no database server needed — they run on a temporary SQLite file and a
temporary photo folder, so your real data is never touched.

- `tests/test_meter_readings.py` (61) — the workflow: consumption maths, the
  negative-reading block, historical tariffs, admin-overrides-tenant, double
  approval producing exactly one bill, rollback when the rate is missing,
  photo validation, and the ownership/IDOR checks
- `tests/test_existing_regressions.py` (20) — proof the old app still works:
  login, roles, CRUD, payment reconciliation, rent generation idempotency,
  every report the admin UI calls, and that meter bills behave like ordinary
  bills in the existing screens

---

## Setting it up

1. `pip install -r requirements.txt`
2. `python create_tables.py` — adds the four new tables (it's self-healing and
   safe to re-run; your existing tables aren't touched)
3. Sign in as admin → **Meter Readings → Unit price** → set your real rate
4. **Meter Readings → Meters** → add each submeter with its current face reading
5. Tell those tenants they'll now see an "Electricity meter" card on their page

Optional: **Settings** to rename the app, change wording, or adjust limits.
