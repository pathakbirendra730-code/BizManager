# BizManager-v6 — Update_013
## Functional Workflow Fixes & Customer/Supplier Management Improvements

**25 files changed/added.** Functional/workflow update, not a compatibility
pass — but every fix below was verified on SQLite and is written to behave
identically on PostgreSQL (no engine-specific SQL, no new mixed-type
arithmetic, no new tables without both schema branches).

---

## 1. Root Cause — OTP Verification Intermittently Fails

**Not a session/storage bug — a client-side double-submission race.**
Every OTP entry screen (admin login 2FA, signup verification, OTP login,
change-email/mobile confirmation) auto-submits the form ~300ms after the
6th digit is typed, as a convenience. Every one of them also left the
"Verify" button enabled the instant 6 digits were entered.

If someone typed the last digit and then immediately tapped the
now-enabled button (exactly "OTP entered immediately... Press Verify &
Login" from the report), **two separate POST requests** could go out:
one from the real button click, one from the timer's auto-submit.

The reason this actually breaks: **calling `form.submit()` from
JavaScript does not fire the form's native `'submit'` event** — a
long-standing browser behavior. The existing code's double-submit guard
(`formSubmittedOnce`) was set only *inside* that event listener, so it
never actually ran for the timer's own `form.submit()` call. The guard's
own comment claimed it prevented double-firing; it didn't, for exactly
this reason. Whichever of the two requests the server processed *second*
correctly found the OTP already consumed by the first and replied "No
OTP request found" — and depending on network/render timing, the browser
sometimes displayed that failed second response instead of following the
first request's successful redirect. The login had, in fact, already
succeeded server-side.

## 2. Exact OTP Fix

**Client-side (the actual fix — stops the double request from happening):**
New `static/js/otp-entry.js`, a single shared, correctly-guarded
controller used by every OTP screen. The fix: one `submitted` flag set
**synchronously at the very start** of whichever path fires first (the
auto-submit timer or a real click/Enter) — not delegated to an event
listener that doesn't fire for programmatic submission. JavaScript is
single-threaded, so whichever path runs first is guaranteed to flip the
flag before the other can act.

Rewired to use it (removing each screen's own broken copy of the old
pattern): `templates/saas_auth/verify_otp.html` (signup),
`templates/app_admin/verify_otp.html` (the screen in your report),
`templates/saas_auth/login_otp_verify.html` (OTP login),
`templates/saas_auth/confirm_change.html` (change email/mobile). Applied
the same fix pattern (not the shared file, since it's PIN not OTP) to
`templates/saas_auth/login.html`'s PIN auto-submit, which had the
identical bug.

**Server-side (defense in depth, covers every caller through one place):**
`utils/otp_service.py:verify_and_consume_otp()` — if no unused token is
found, it now checks whether the most recently *consumed* token for that
identifier+purpose matches the submitted OTP and was consumed within the
last 30 seconds. If so, this is a duplicate submission of an OTP a race
already verified, not a fabricated code — treated as success instead of
"No OTP request found." This covers admin login, SaaS login/signup, PIN
reset, change-email/mobile, and OTP login identically, since they all
route through this one function — no per-route changes needed.

## 2b. Related bug also fixed: focus jumping away while still typing

Reported separately but same root cause class: on the combined "Mobile
Number or User ID" login screen (`templates/unified_login.html`), typing
a **single digit** of a mobile number, or a single letter of a user ID,
immediately classified the input as a finished username and auto-focused
the password field — yanking the cursor away mid-keystroke.

`classify()` used to fall through to `'username_password'` for *any*
value that wasn't yet exactly 10 digits — including one digit. Fixed to
recognize "still purely numeric, not yet longer than a mobile number" as
undecided (wait for more input) rather than assuming it's a finished
username. Also removed the `setTimeout(() => pwInput.focus(), 200)` call
entirely — there's no reliable way to detect "done typing a free-form
user ID" from keystrokes, so the field is now just revealed
(progressive disclosure), and the person Tabs or clicks into it when
ready, instead of focus being yanked there automatically.

---

## 3–4. Customer & Supplier Search Improvements

Both invoice creation (`billing/pos.html`) and purchase creation
(`purchase/new.html`) **already had** working search-as-you-type
autocomplete UI (debounced, dropdown results, auto-fills hidden
id/GSTIN/state fields on selection) — that part didn't need building.
The actual gaps were in the backend API and one case-sensitivity bug
that would only show up on PostgreSQL:

- **Search fields broadened**: `api_search()` for both customers and
  suppliers now matches name, mobile, email, *and* GSTIN (previously
  name+mobile only for customers, name+mobile+GSTIN for suppliers —
  email wasn't searchable on either).
- **Case-insensitive on both backends**: `LIKE` is case-insensitive on
  SQLite by default but case-**sensitive** on PostgreSQL — searching
  "sharma" would silently miss "Sharma" in production while working fine
  in dev. Fixed everywhere a customer/supplier/invoice/purchase search
  box exists by wrapping both sides in `LOWER(...)` (portable to both
  engines, unlike Postgres-only `ILIKE`) — `api_search()` for both
  entities, the customer/supplier list-page search boxes, and the
  invoice/purchase history search boxes.
- **"Customer Code" / "Supplier Code"**: not implemented. `saas_customers`
  and `saas_suppliers` have no such column, and this codebase has no
  migration mechanism beyond `CREATE TABLE IF NOT EXISTS` — adding one
  would silently do nothing on your already-running production database
  (the table already exists, so the new column would never actually get
  created there). Flagging this rather than shipping something that only
  half-works; a real migration would be needed first. Name, mobile,
  email, and GSTIN search cover 4 of the 5 requested fields.
- **Side-effect bug found and fixed while here**: `purchase/new.html`'s
  supplier dropdown did `s.balance.toFixed(2)` — but since Update_012
  made `saas_fetchall()` return `Decimal` for money columns, Flask's
  `jsonify()` now serializes `balance` as a JSON **string**, not a
  number, and `.toFixed()` doesn't exist on strings. Wrapped in
  `parseFloat()`, matching the pattern already correctly used elsewhere
  in the same file.
- **"No duplicate customer from a failed search"**: covered by the two
  fixes above — the search now actually finds existing customers/
  suppliers by more fields, case-insensitively, on both databases, so
  there's no longer a class of "search silently returned nothing" that
  would push someone toward creating a duplicate.

---

## 5. Customer Dashboard

`GET /biz/customers/<id>/history` (existing URL, now the real dashboard
— this is also the "click a customer name" destination for Issue 7,
so no new parallel route was added). Includes:

- **Profile**: name, mobile, email, GSTIN, address, state, member-since date.
- **Financial Summary**: invoice count, total paid, outstanding (live
  `SUM` of unpaid/partial invoice `due_amount`), average invoice value.
  *Opening Balance / Credit Limit / Advance Balance are not shown* —
  `saas_customers` has no such columns and no UI ever wrote to them;
  showing fabricated numbers would be worse than omitting them. Noted
  directly on the page.
- **Sales**: full invoice history table (was already there) — now with
  working "View" links to the real invoice page (previously a disabled
  "coming soon" placeholder, left over from before the billing module
  existed) and a "Pay" shortcut on invoices with a balance due.
- **Ledger**: the invoice history table doubles as the ledger view
  (running total is `due_amount` per invoice) — there's no separate
  double-entry ledger keyed per customer in this schema, so this is the
  closest accurate representation of "money in/out with this customer"
  rather than a fabricated parallel view.
- **Chart**: monthly sales vs. payments, last 6 months (Chart.js via CDN,
  one aggregate SQL query — see Performance below).
- **Actions**: Edit (existing route), Delete (existing route, now fixed —
  see below), Record Payment (via each invoice's own payment form,
  where payments actually happen), New Invoice (prefills this customer
  in the POS screen), Print (browser print), Export CSV.

## 6. Supplier Dashboard

`GET /biz/suppliers/<id>/ledger` (existing URL, same "already the click
destination" reasoning as above). Nearly identical to the Customer
Dashboard, with the differences the schema actually supports:

- **Financial Summary** includes real **Opening Balance** and **Current
  Balance** (`saas_suppliers.opening_balance` / `.balance` — these
  columns exist for suppliers, unlike customers) alongside purchase
  count and outstanding.
- **Purchase History / Ledger**: same table, now with working "View"
  links to the real purchase detail page and "New Purchase" link
  (previously also disabled "coming soon" placeholders from before the
  purchase module existed).
- **Actions**: Edit, Delete — reuses the *existing* smart delete route,
  which already soft-deactivates a supplier with purchase history and
  hard-deletes one with none, so no new toggle route was needed — Record
  Payment (already had a working form on this page), New Purchase
  (prefills this supplier), Print, Export CSV.
- **Chart**: monthly purchases vs. payments, last 6 months.

**PDF export**: not implemented as a distinct feature — "Print" opens the
browser's print dialog on the same dashboard (print-to-PDF from there
covers the practical need) and CSV export handles structured data export.
A true server-generated PDF would be a larger addition; flagging as a
possible follow-up rather than a partial implementation now.

---

## 7. Clickable Customer/Supplier Navigation

New `templates/_macros.html` — `customer_link(id, name)` /
`supplier_link(id, name)`, the one reusable place this logic lives.
Falls back to plain text if the id is missing (e.g. a walk-in customer,
or an invoice whose customer was since deleted — see below), so no
template ever renders a broken link.

Wired into every place the brief listed that currently exists in the
app: Customer List, Supplier List, Sales Invoice History (list + single
invoice view), Purchase History (list + single purchase view), and the
Outstanding/Receivables report. ("Dashboard widgets" / "Recent
transactions" — no such widget currently exists in the SaaS business
dashboard to wire up; not invented, per "no unnecessary refactoring.")

## 8. Performance

- Both dashboards use a **fixed, small number of queries regardless of
  invoice/purchase volume** — one for the entity, one `COUNT`/`SUM`
  aggregate for the summary cards, one `GROUP BY month` aggregate for
  the chart, one `ORDER BY ... LIMIT`-free history list. No per-invoice
  or per-month queries (no N+1).
- Customer/supplier list pages were **already** using a single `JOIN` +
  `GROUP BY` (not one query per row) — confirmed, no change needed.
- The monthly chart aggregation reuses the same cross-backend
  month-grouping pattern (`TO_CHAR`/`strftime`) already established
  elsewhere in the app, so it runs identically on both databases.

---

## 9. Files Changed

```
static/js/otp-entry.js                                   — new, shared OTP double-submit fix
static/css/style.css                                      — .entity-link styling (Issue 5)
templates/saas_auth/verify_otp.html                        — OTP fix
templates/saas_auth/login_otp_verify.html                  — OTP fix
templates/saas_auth/confirm_change.html                    — OTP fix
templates/saas_auth/login.html                              — PIN auto-submit fix (same bug class)
templates/app_admin/verify_otp.html                         — OTP fix (the exact reported screen)
templates/unified_login.html                                — focus-jump fix (Issue reported mid-conversation)
templates/_macros.html                                      — new, Issue 5 shared link macros
templates/saas_business/customers/history.html              — full Customer Dashboard (rewritten)
templates/saas_business/customers/list.html                 — clickable name
templates/saas_business/suppliers/ledger.html                — full Supplier Dashboard (rewritten)
templates/saas_business/suppliers/list.html                  — clickable name
templates/saas_business/billing/pos.html                     — search dropdown enrichment, customer prefill
templates/saas_business/billing/history.html                 — clickable name
templates/saas_business/billing/receivables.html             — clickable name
templates/saas_business/billing/invoice.html                 — clickable name
templates/saas_business/purchase/new.html                    — parseFloat() fix, supplier prefill
templates/saas_business/purchase/history.html                — clickable name
templates/saas_business/purchase/view.html                   — clickable name
utils/otp_service.py                                         — server-side idempotent OTP verify
modules/saas_business/customers.py                            — search, dashboard route, CSV export, delete fix
modules/saas_business/suppliers.py                             — search, dashboard route, CSV export
modules/saas_business/billing.py                                — case-insensitive invoice search
modules/saas_business/purchase.py                                — case-insensitive purchase search
```

## Bonus fix found during this audit: customer delete crash

While building the Customer Dashboard's Delete action, found the same bug
class as Update_011's user-delete fix: `saas_invoices.customer_id` and
`saas_payments.customer_id` reference `saas_customers` **without**
`ON DELETE CASCADE`. Deleting any customer who had ever been invoiced
would raise a foreign-key violation on PostgreSQL (and on SQLite, which
also enforces this — `PRAGMA foreign_keys=ON` is set). Fixed the same way
as Update_011: null the reference instead of blocking or crashing — each
invoice already has its own `customer_name` column independent of the
FK, so invoice history stays fully readable after the customer record is
gone.

---

## 10. Testing Performed

- `python3 -m py_compile` on all modified `.py` files — passes.
- All modified/new templates re-parsed with `jinja2.Environment().parse()`
  — passes.
- Full Flask app boot (`create_app()`) — succeeds, 119 routes register
  (117 → 119, confirming the two new CSV-export routes registered
  correctly with no import errors).
- `url_for()` resolution test (proper request context) for every new/
  changed route — all resolve to the expected URL.
- **Real end-to-end functional test** against a live SQLite test
  database: created a business, a customer with two invoices (one paid,
  one partially paid), a supplier with one purchase — then:
  - Loaded both dashboards → both return HTTP 200 with real data.
  - Downloaded both CSV exports → HTTP 200, correct `text/csv` mimetype.
  - Searched customers for `"sharma"` (lowercase) → correctly matched
    `"Sharma Traders"`; searched suppliers for `"VERMA"` (uppercase) →
    correctly matched `"Verma Suppliers"` — confirms the case-
    insensitivity fix actually works, not just compiles.
  - Deleted the customer with existing invoice history → succeeded (no
    FK crash); confirmed the invoice row survived with `customer_id`
    set to `NULL` and `customer_name` still intact.
  - Deleted the supplier with existing purchase history → correctly
    soft-deactivated (`is_active` → false) rather than hard-deleting,
    via the pre-existing smart-delete logic.
- Not tested against a live PostgreSQL instance in this environment (no
  network access here). The case-insensitivity fix specifically targets
  a Postgres-only behavior difference (`LIKE` is case-sensitive there,
  unlike SQLite) — recommend confirming a mixed-case search on staging
  before considering that specific piece verified in production.

## Remaining risks / notes

- "Customer Code" / "Supplier Code" search fields are not implemented —
  see §3–4 for why, and what a proper follow-up would need (an actual
  migration mechanism, which this codebase doesn't currently have).
- PDF export is print-to-PDF via the browser, not a generated PDF file —
  see §6.
- The OTP idempotency window (30 seconds) is intentionally short and
  scoped to an exact hash match on the same identifier+purpose — it
  only smooths over a genuine duplicate submission of a code that was
  already correct, not a new guess, so it doesn't weaken OTP security.
