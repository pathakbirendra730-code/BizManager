# BizManager-v6 — Update_032
## GST & HSN Perfection (Phase 1)

## 1. Executive Summary

Builds the foundational commercial-grade GST engine: a complete HSN/SAC
Master, a pre-save GST Validation Engine for Sales/Purchase documents, a
Freight + Round-Off extension to the calculation engine, and a GST
Health Check that scores a business's GST-readiness before generating
returns. Also fixes a real, latent production bug found while auditing
the existing HSN lookup code (see §2).

This is explicitly Phase 1 — the brief asks for the engine to be
*designed* so GSTR-1A, full B2B/B2C/Export/Nil-Rated/Exempt/Reverse-
Charge reporting can be built "without requiring major redesign later,"
not for every one of those reports to be fully built now. §5 documents
exactly what's already in place for each, and what a Phase 2 would still
need to add.

## 2. Design Notes

**A real bug found and fixed: HSN lookups were silently broken on
Postgres.** Auditing `modules/saas_business/products.py` before writing
any new code found two HSN API routes: one correctly SaaS-aware, the
other querying `models.database.get_db()` — the legacy single-tenant
SQLite connection, which is a completely different, disconnected
database from the real one in any Postgres production deployment (see
`utils/hsn_master.py`'s module docstring for the full architecture
explanation). That route would have silently returned `{}` for every
lookup in production. Both routes now go through the same new
`utils/hsn_master.py` module.

**HSN/SAC Master is global, shared reference data — same tier as
Platform Settings.** Every business validates against the same HSN list,
matching how India's actual HSN/SAC code list works (it isn't
per-business). Managed from a new App Admin page, not per-business
settings.

**HSN validation returns warnings, not hard rejections, for anything
that isn't unambiguously wrong.** An HSN code absent from this app's
local master is common and legal — India's full CBIC list runs to
thousands of codes; this app ships a curated subset (55 pre-seeded
codes), not the complete list. Rejecting a sale because a legitimate
code isn't in a necessarily-incomplete local list would block real
business. Only a structurally malformed code (not 4/6/8 digits) and a
few unambiguous mismatches (e.g. a non-zero rate entered against an HSN
classified Exempt) are treated as worth stopping on — and even those are
warnings the Validation Engine surfaces, not codes that raise directly
(see below).

**The GST Validation Engine separates errors (block save) from warnings
(don't).** A malformed HSN code, a supply-type/state mismatch (which
would silently post the wrong tax type — CGST+SGST vs. IGST — to the
ledger), a duplicate manual document number, or an implausible date are
factually wrong and block the save. A GSTIN that fails format checks or
an HSN not in the local master are surfaced but don't block — the person
saving the document can judge whether it's actually a problem. This
mirrors exactly how `utils/hsn_master.py`'s own validation already
worked before this update; the Validation Engine extends the same
philosophy to the rest of a document.

**Duplicate-number checking is now centralized, not duplicated.**
`billing.py` and `purchase.py` each already had a hand-written inline
"is this manual document number already in use" check (from
Update_027). Both are now the same call into
`utils.gst_validation.validate_duplicate_document_number()` — one
implementation instead of two copies that could drift apart.

**Freight and Round-Off are opt-in and 100% backward compatible.**
`utils.tax_helpers.apply_freight_and_roundoff()` is a new, separate
function — it is NOT called unless a caller's payload explicitly
includes a non-zero `freight_amount` or `round_off_enabled: true`.
Every existing POS/Purchase request that doesn't send either key
produces byte-identical totals to before this update (verified — see
§4). Freight is taxed through the exact same `calculate_gst()` engine
every line item already uses (same supply_type, so it correctly becomes
CGST+SGST or IGST), folded into the running totals exactly once — never
calculated twice, matching the requirement explicitly. Round-off is a
pure arithmetic nudge on the *final* total only; it never touches
taxable value or any tax component, so it can never be mistaken for or
accidentally create GST liability.

**Known Phase 1 simplification, stated plainly:** when freight is
applied, its taxable value is currently folded into the same "Sales
Revenue" / "Purchases" ledger posting as the rest of the document
(via the existing `record_sale()`/`record_purchase()` calls, unchanged
in this update) rather than a dedicated "Freight Income" ledger account.
This is GST-correct (the right tax amount is calculated and posted to
Output/Input GST either way) but not P&L-segregated. Building a separate
freight ledger account is a clean, well-scoped Phase 2 addition — it
wasn't done here to avoid expanding this update's ledger/chart-of-
accounts footprint beyond what Phase 1's calculation-engine scope
called for. No UI form fields for freight/round-off were added to the
POS/Purchase entry screens either — the engine and storage are ready
(§3), but wiring them into the live entry forms (mirroring the careful
tax-mode-aware JS work from Update_029) is Phase 2 UI work.

**GST Health Check catches problems a person can't easily spot by
eye.** The most valuable check isn't a data-quality nitpick — it's the
Output GST / Input GST reconciliation: independently summing GST from
saas_invoices/saas_purchases (net of Credit/Debit Notes) and comparing
that to what's actually posted in the ledger's `gst_payable`/
`gst_input_credit` accounts. These are computed via two completely
separate code paths (documents vs. `utils/ledger_transactions.py`'s
postings) and should always agree exactly; any drift means a document
was saved without its matching ledger entry (or vice versa) — exactly
the kind of silent, structural problem that would corrupt a GSTR-3B
filing. Verified in testing (§4) that this check correctly catches a
deliberately-unposted invoice.

## 3. Complete List of Modified/Added Files

### HSN/SAC Master
- **`models/database.py`** — legacy `hsn_master` schema upgraded
  (additive; the copy actually read by the live SaaS app is in
  `saas_business_data.py`, see below — this one exists for local SQLite
  dev-mode consistency, see `utils/hsn_master.py`'s docstring).
- **`models/saas_business_data.py`** — `hsn_master` upgraded in both the
  SQLite and Postgres schema branches: new columns `unit`,
  `effective_date`, `is_service`, `reverse_charge`, `tax_status`,
  `itc_eligible`, `is_active`. Migration-safe (`ALTER TABLE ... ADD
  COLUMN`, guarded by existence checks on SQLite / `IF NOT EXISTS` on
  Postgres) with defaults that reproduce today's implicit behavior for
  every already-seeded code (taxable, active, ITC-eligible, goods).
- **`utils/hsn_master.py`** (new) — `search_hsn()` (autocomplete),
  `get_hsn()` (exact lookup, never filtered by is_active so old
  documents keep resolving), `validate_hsn_code_format()`,
  `validate_hsn_for_transaction()`, and admin `create_hsn()`/
  `update_hsn()`/`list_all_hsn()`.
- **`modules/saas_business/products.py`** — both `/api/hsn` routes
  rewritten to use the new module, fixing the Postgres bug described in
  §2; `add()`/`edit()` now surface non-blocking HSN validation warnings
  via flash messages when a product's HSN/GST-rate combination looks
  off.
- **`modules/app_admin/dashboard.py`** — new `hsn_master()` /
  `hsn_master_add()` / `hsn_master_edit()` routes (super-admin tier,
  same as Platform Settings).
- **`templates/app_admin/hsn_master.html`** (new) — list/search, add
  form, and inline edit form per code.
- **`templates/app_admin/base_admin.html`** — nav link added.

### GST Validation Engine
- **`utils/gst_validation.py`** (new) — `validate_gstin()`,
  `validate_state_code()`, `validate_supply_type_selection()`,
  `validate_document_date()`, `validate_fy_numbering()`,
  `validate_duplicate_document_number()`, `validate_reverse_charge()`,
  and the two aggregators `validate_sales_document()` /
  `validate_purchase_document()`.
- **`modules/saas_business/billing.py`** — `save_invoice()` now calls
  `validate_sales_document()` before generating a document number (so a
  rejected save never burns a number); hard errors return HTTP 400 with
  the specific error list; warnings ride along in the success response.
  New `reverse_charge` request field, stored on the invoice.
- **`modules/saas_business/purchase.py`** — same treatment,
  `validate_purchase_document()`.

### GST Calculation Engine — Freight & Round-Off
- **`utils/tax_helpers.py`** — new `apply_freight_and_roundoff()`.
- **`modules/saas_business/billing.py`** / **`purchase.py`** — opt-in
  wiring: reads `freight_amount`/`freight_gst_rate`/`round_off_enabled`
  from the request payload (all default to "off"); when provided,
  applies the new engine function before computing the Grand Total and
  stores `freight_amount`/`round_off` on the document.

### Schema — future-compatibility columns
- **`models/saas_business_data.py`** — `saas_invoices` / `saas_purchases`
  gain `reverse_charge` (bool), `freight_amount`, `round_off` (all
  default 0/false — no-ops for every existing row and every caller that
  doesn't pass them). `saas_invoice_items` / `saas_purchase_items` gain
  a nullable `tax_status` override column (NULL = "inherit from this
  line's HSN code" — see §5).

### GST Health Check
- **`utils/gst_health.py`** (new) — 7 independent checks (Ledger/Trial
  Balance, Inventory balance, HSN validity, GSTIN validity, Invoice/
  Purchase numbering continuity, Output/Input GST reconciliation,
  Credit/Debit Note return integrity), a 0-100 score, and a specific,
  actionable issue list with suggested fixes for each.
- **`modules/saas_business/gst.py`** — new `health_check()` route.
- **`templates/saas_business/gst/health_check.html`** (new) — score
  display + issue table (uses the Update_031 responsive table system).
- **`templates/saas_business/gst/index.html`** — Health Check card added.

## 4. Testing Report

All tests run against a real SQLite schema (`init_saas_db()` +
`init_saas_business_tables()` + `init_ledger_engine_tables()` +
`init_db()` + `seed_chart_of_accounts()`), calling the actual functions
from the new modules — not a re-implementation.

### HSN Master
| # | Check | Result |
|---|---|---|
| 1 | Migration: `hsn_master` gains all 7 new columns | ✅ Confirmed via `PRAGMA table_info` |
| 2 | All 55 pre-existing seeded HSN codes got correct defaults (taxable/active/ITC-eligible) | ✅ |
| 3 | `search_hsn("847")` autocomplete returns matches | ✅ 5 results |
| 4 | `get_hsn()` exact lookup | ✅ |
| 5 | `validate_hsn_code_format()` — 4/6/8-digit-only enforcement | ✅ |
| 6 | Matching GST rate → no issues | ✅ |
| 7 | Mismatched GST rate vs. HSN master → warning | ✅ |
| 8 | Unknown HSN code → warning (not error) | ✅ |
| 9 | `create_hsn()` | ✅ |
| 10 | Duplicate HSN code creation blocked | ✅ |
| 11 | Non-zero rate against an Exempt-classified HSN → warning | ✅ |

### GST Validation Engine
| # | Check | Result |
|---|---|---|
| 1 | GSTIN: blank optional / required / valid format / invalid state code / wrong length / garbage | ✅ All correct |
| 2 | State code: valid / invalid / blank | ✅ |
| 3 | Supply type mismatch (same state marked inter-state) detected | ✅ |
| 4 | Supply type correct selection passes | ✅ |
| 5 | No party state on file → not an error | ✅ |
| 6 | Document date: blank / valid / implausible future / pre-GST / malformed | ✅ All correct |
| 7 | FY numbering: matching date/FY passes, mismatch caught | ✅ |
| 8 | Reverse Charge with no party details → warning | ✅ |
| 9 | Full `validate_sales_document()` aggregator — clean document → no errors | ✅ |
| 10 | Full aggregator — supply type mismatch → error, blocks | ✅ |
| 11 | Duplicate manual invoice number (against a real inserted invoice) → blocked | ✅ |
| 12 | Non-duplicate manual number → passes | ✅ |

### Freight & Round-Off Engine
| # | Check | Result |
|---|---|---|
| 1 | No freight, no round-off → totals byte-identical to input (backward compat) | ✅ |
| 2 | Freight ₹500 @ 18% intra-state added → taxable +500, CGST/SGST +45/+45, total_tax +90 | ✅ |
| 3 | Round-off on a non-whole-rupee total → correct adjustment, `pre_roundoff_total` preserved for audit | ✅ |

### GST Health Check
| # | Check | Result |
|---|---|---|
| 1 | Clean-ish business (one malformed-GSTIN customer) → score 97, 1 warning, 0 errors | ✅ |
| 2 | Negative stock introduced → correctly flagged, score drops to 94 | ✅ |
| 3 | Unknown HSN code on a product + an invoice saved WITHOUT its ledger posting → HSN warning AND **Output GST reconciliation error correctly catches the missing ledger entry** (score 84) | ✅ — this is the most valuable check; verified it actually detects a real structural problem, not just a demo |

### No-regression / integration checks
| Check | Result |
|---|---|
| Full Python compile sweep (every `.py` file in the repo) | ✅ Clean |
| Full Jinja2 template parse sweep (all 85 templates) | ✅ All parse |
| Freight/round-off default (off) produces unchanged totals | ✅ Verified numerically (see above) |
| Existing inline duplicate-number checks in billing.py/purchase.py replaced by the centralized validator with identical blocking behavior | ✅ Verified against real inserted data |

## 5. Future Compatibility — what's already in place, and what Phase 2 still needs

| Future report/feature | What Phase 1 already provides | What a future phase would still add |
|---|---|---|
| **GSTR-1 B2B/B2C** | Already live since Update_030 (customer GSTIN present/absent split) | — |
| **GSTR-1 CDNR** | Already live since Update_030 | — |
| **GSTR-1A** (amendments) | `doc_prefix`/`doc_fy`/`doc_sequence` + immutable original documents give a clean basis for an "amendment references original doc X" model | A dedicated amendment document type/table, mirroring the Credit Note pattern from Update_030 |
| **GSTR-3B** | Already live since Update_030; now also has the Health Check to verify its inputs beforehand | Auto-populate from a filing-period lock/snapshot rather than live query, once return filing history is tracked |
| **HSN Summary** | Already live since Update_030 (net of returns); HSN Master now has `unit` for a proper quantity-with-unit column | Wire the new `unit` field into the HSN Summary report's display |
| **Export supplies** | `supply_type` already distinguishes intra/inter; state-code infrastructure is in place | A dedicated `export`/`sez` supply type value + zero-rated GST treatment (currently only intra/inter exist) |
| **Nil Rated / Exempt / Non-GST** | `hsn_master.tax_status` (taxable/exempt/nil_rated/non_gst) already classifies every HSN code; the new nullable `tax_status` column on `saas_invoice_items`/`saas_purchase_items` lets a line override its HSN's default classification | A GSTR-1 report section that actually groups by this classification (currently unused by any report — the column exists and is populated-by-inheritance-ready, but no report reads it yet) |
| **Reverse Charge** | `reverse_charge` flag now stored on both `saas_invoices` and `saas_purchases`, `hsn_master.reverse_charge` classifies codes, and `validate_reverse_charge()` exists in the Validation Engine | Full RCM applicability rules (Section 9(3)/9(4) notified goods/services) and the liability-shift ledger treatment (recipient self-assesses and pays GST directly) — deliberately out of scope for Phase 1, see §2 |

## 6. No-Regression Verification

- Every new/changed column on `saas_invoices`, `saas_purchases`,
  `saas_invoice_items`, `saas_purchase_items` defaults to 0/false/NULL —
  confirmed to reproduce pre-Update_032 behavior exactly for any caller
  that doesn't explicitly opt into the new fields.
- `hsn_master`'s upgrade is purely additive; every pre-existing seeded
  code's `hsn_code`/`description`/`default_gst_rate`/`category` values
  are untouched, confirmed via direct query after migration.
- The GST Validation Engine's error-level checks were designed and
  tested to only fire on states that are already objectively invalid
  under GST law (a genuinely malformed date, a genuine CGST/SGST-vs-IGST
  state mismatch, a genuine duplicate number) — no existing, previously-
  valid document construction pattern is newly rejected.
- `billing.py`/`purchase.py`'s previously-inline duplicate-number checks
  were replaced with calls to the identical logic, now centralized —
  verified the behavior (reject with "already in use") is unchanged by
  testing against a real inserted invoice number.
- Full Python compile sweep and full Jinja2 template parse sweep both
  clean across the entire repository, not just the files touched by
  this update.
