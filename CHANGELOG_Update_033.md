# BizManager-v6 — Update_033
## GST & HSN Perfection (Phase 2)

## 1. Executive Summary

Builds on Update_032's foundational GST engine with: an ERP-grade GST
Health Check that identifies the *specific document* causing each
mismatch (not just an aggregate number); a National HSN/SAC Master with
Business-Type tagging and filtering; powerful multi-word/category HSN
search with ranked suggestions; complete Demo Business deletion; Freight
& Round-Off fully wired into the POS/Purchase entry screens (completing
what Update_032 left as engine-only); and a Nil-Rated/Exempt/Non-GST
breakdown in GSTR-1.

Two real bugs were found and fixed while building this, not just new
features added — see §2.

## 2. Design Notes

**Bug found: a schema migration ordering mistake in my own first draft.**
While adding `hsn_master.business_types`, I initially placed its `ALTER
TABLE` migration earlier in `_init_sqlite()`/`_init_postgres()` than
`hsn_master`'s own `CREATE TABLE` statement — which would crash on a
fresh database with "no such table: hsn_master". Caught by actually
running the migration against a fresh SQLite file (not just `py_compile`,
which can't catch this class of bug), not by code review alone. Fixed by
folding the new column into `hsn_master`'s own existing post-CREATE-TABLE
migration block instead of a separate, earlier one. See §6 for the
general lesson this reinforces about testing migrations end-to-end.

**Bug found: HSN suggestion matching was too strict to ever suggest
anything useful.** The first version of `suggest_hsn_from_description()`
reused `search_hsn()`'s "every word in the query must appear in the
description" logic — correct for an explicit, narrowing search box, but
wrong for a suggestion feature: a real product name ("Electric Table Fan
400mm White") routinely contains size/color/model words a generic HSN
description will never contain, so requiring an exact all-words match
returned nothing almost always. Caught by testing with a realistic
product name, not a hand-picked one that happened to match exactly.
Rewrote it as its own ranked, any-significant-word-matches query
(numeric-only tokens like "400mm" are skipped, and results are ordered by
how many words matched) — verified the ranking puts a 3/3-word match
above a 2/3-word match.

**GST Health Check: from "here's a number that's wrong" to "here's the
document to fix."** The single highest-value change in this update.
`_find_documents_missing_ledger_posting()` uses the exact same
`source_type` + `source_id` linkage `utils/ledger_transactions.py` already
writes on every posting to find the specific invoice/purchase/credit-note/
debit-note whose GST has no matching journal entry — the single most
common real cause of an Output GST / Input GST reconciliation mismatch.
The old aggregate-only check still runs, but now only as a residual
safety net: if a mismatch remains even after every individually-flagged
document is accounted for, that means something *other* than "a whole
document's posting is missing" is wrong (e.g. a partial/corrupted
posting) — worth surfacing, but correctly described as different from,
and not a duplicate of, the specific documents already listed.

**Category sub-scores are computed from the same issue list, not a
second pass.** `SCORE_BUCKETS` maps each issue's own `category` string
(already set by whichever check raised it) into one of the six named
buckets the spec calls out (GST Compliance, Ledger Integrity, HSN
Coverage, GSTIN Validation, Document Numbering, Return Integrity) — so
there's no risk of the overall score and the sub-scores ever being
computed from different data, since they're both just different views
over one issue list.

**"Repair Ledger Posting" and "Recalculate GST" were deliberately NOT
built as one-click actions.** The spec asks for these "where safe" —
judged that neither is safe as a fully automatic action: auto-repairing
a ledger posting risks silently creating a *second*, possibly
inconsistent entry if the original failure was partial rather than
total; auto-recalculating GST on an already-saved, possibly already-
reported document risks changing a number that's already been shown to
a customer or filed. Both are explicitly called out in the health check
UI as needing a human/support decision, with the specific document
already identified for them (§ above) rather than requiring that person
to go hunting for it. "View Document" and "View Ledger" — genuinely
safe, read-only navigation — are implemented. "Add Missing HSN" is
effectively already covered: an unknown-HSN warning already names the
exact code to add, and the National HSN/SAC Master admin page (this
update) is where it gets added.

**National HSN Master + Business Type is additive, not a fork.**
`hsn_master` stays the single global table from Update_032 — Business
Type is a new *filter* on top of it (`business_types` tag column +
`saas_businesses.hsn_business_types` selection), not a per-business copy
of the table. A business's type selection defaults to "Show All" (no
selection = unfiltered), and an explicit "Show All" override is always
one click away even after selecting types — the spec's "still allowing
Show All" requirement is structural, not a checkbox someone could
accidentally leave unchecked.

**Freight & Round-Off UI reuses the exact math Update_032 already
proved correct** — `calcFreightGst()`/round-off logic in the POS and
Purchase JS mirror `utils.tax_helpers.apply_freight_and_roundoff()`
line-for-line (freight always treated as Tax Exclusive regardless of the
document's own Tax Mode, matching the backend's existing behavior;
round-off is a pure post-tax arithmetic nudge that never touches taxable
value or any tax component). Verified numerically identical to the
Python backend's output for the same inputs (§4).

**Nil-Rated/Exempt/Non-GST reporting required zero new schema** — the
`tax_status` columns (`hsn_master.tax_status`, and the nullable override
on `saas_invoice_items`/`saas_purchase_items`) were already added in
Update_032 specifically so this could be built without a redesign; this
update is the first thing that actually reads them. Classification
resolves via `COALESCE(item override, HSN's classification, 'taxable')`.

## 3. Complete List of Modified/Added Files

### GST Health Check (§1)
- **`utils/gst_health.py`** — `_find_documents_missing_ledger_posting()`
  (new); `_issue()` extended with optional `**actions` metadata
  (`doc_type`, `doc_id`, `doc_number`, `doc_date`, `party_name`,
  `difference`); `_check_gst_reconciliation()` rewritten to drill down
  per-document first, aggregate check as residual-only fallback;
  `SCORE_BUCKETS` + `_score_for()` + `category_scores` added to
  `run_health_check()`'s return.
- **`templates/saas_business/gst/health_check.html`** — score-breakdown
  grid (6 sub-scores); issue table gains Document/Date/Party/Difference/
  Actions columns with safe "View Document"/"View Ledger" links; explicit
  note on why auto-repair actions aren't offered.

### National HSN/SAC Master + Business Type (§2, §3)
- **`models/saas_business_data.py`** — `hsn_master.business_types`
  column (folded into its existing post-CREATE-TABLE migration block,
  both SQLite and Postgres); new `saas_businesses.hsn_business_types`
  column.
- **`utils/hsn_master.py`** — `BUSINESS_TYPES` taxonomy (23 types),
  `parse_business_types()`/`format_business_types()`,
  `get_business_hsn_types()`/`set_business_hsn_types()`; `search_hsn()`
  extended with business-type filtering (untagged codes always match, so
  every pre-Update_033 seeded code keeps showing for everyone) and
  category/multi-word matching; `create_hsn()`/`update_hsn()` accept
  `business_types`.
- **`modules/app_admin/dashboard.py`** — HSN routes pass/accept
  `business_types` (checkbox list).
- **`templates/app_admin/hsn_master.html`** — business-type checkboxes
  on both the add form and each row's inline edit form.
- **`modules/saas_auth/routes.py`** — new
  `update_hsn_business_types()` route (owner-only).
- **`templates/saas_auth/business_settings.html`** — new "Business Type
  (HSN Filtering)" card.
- **`modules/saas_business/products.py`** — `/api/hsn` now defaults to
  the business's own selected type(s) (with `show_all=1` override); new
  `/api/hsn/suggest` route.

### Powerful HSN Search & Suggestions (§4, §5)
- **`utils/hsn_master.py`** — `search_hsn()` multi-word AND-matching
  against description, plus category matching (see above);
  `suggest_hsn_from_description()` (new, ranked any-word-matches
  query — see §2 for the bug this fixes over its first draft).
- **`templates/saas_business/products/add_edit.html`** — Name field
  gains an `id`; on blur (if HSN is still blank), calls the new suggest
  endpoint and shows candidates in the existing HSN dropdown UI. Never
  auto-fills, never blocks saving.

### Demo Business Management (§6)
- **`utils/business_deletion.py`** (new) — `TABLES` (27 business-scoped
  tables, explicit dependency order), `count_business_records()`,
  `get_business_summary()` (grouped for the confirmation screen),
  `delete_business_completely()` (single atomic transaction; every
  statement scoped by `business_id`).
- **`modules/saas_auth/routes.py`** — `delete_business_confirm()` /
  `delete_business_execute()` (owner-only, PIN + typed "DELETE").
- **`modules/app_admin/dashboard.py`** — App Admin equivalent
  (super-admin-only, typed exact business name + typed "DELETE", no PIN
  available in that context).
- **`templates/saas_auth/delete_business.html`**,
  **`templates/app_admin/delete_business.html`** (new) — confirmation
  screens with the record-count preview.
- **`templates/saas_auth/business_settings.html`** — "⚠️ Danger Zone"
  card.
- **`templates/app_admin/all_businesses.html`** — "Delete…" link.

### GST Reports Phase 2 (§7, partial — see §5 of this changelog for scope)
- **`modules/saas_business/gst.py`** — `gstr1()` gains a Nil-Rated/
  Exempt/Non-GST query using the `tax_status` inheritance chain.
- **`templates/saas_business/gst/gstr1.html`** — new section rendering it.

### Freight & Round-Off UI Completion (§8)
- **`templates/saas_business/billing/pos.html`** — Freight amount/GST-
  rate inputs and a Round-Off checkbox added to the bill summary;
  `calcFreightGst()` (new) + `recalc()` rewritten to fold freight and
  round-off into the live total preview; save payload gains
  `freight_amount`/`freight_gst_rate`/`round_off_enabled`.
- **`templates/saas_business/purchase/new.html`** — identical treatment.

### Mobile UI (§9)
- No dedicated new work this update — every new UI element (freight
  inputs, health check's expanded table, HSN admin checkboxes) reuses
  the existing Update_031 responsive system (`.table`/`.card-body.p-0`
  horizontal scroll + sticky header/column, `flex-wrap` grids) rather
  than introducing new layout patterns, so it inherits that system's
  mobile behavior automatically. See §5 for what a dedicated mobile
  pass would still need to verify live.

## 4. Database Changes / Migration Notes

All changes are additive `ALTER TABLE ... ADD COLUMN`, guarded by an
existence check on SQLite (`PRAGMA table_info`) / `IF NOT EXISTS` on
Postgres — safe to run against an existing production database with
data already in it, no data loss, no locking beyond what `ADD COLUMN`
itself requires.

| Table | New column | Default | Purpose |
|---|---|---|---|
| `hsn_master` | `business_types` | `''` | Comma-separated Business Type tags (empty = general-purpose, matches every filter) |
| `saas_businesses` | `hsn_business_types` | `''` | This business's own selected Business Type(s) |

**Migration ordering requirement**: `hsn_master.business_types` must be
migrated *after* `hsn_master`'s own `CREATE TABLE IF NOT EXISTS`
statement runs (true in this update's actual code — see §2's bug
writeup for why this is called out explicitly). No action needed by a
deployer; this is a note for any future contributor adding another
`hsn_master` column.

**No destructive changes.** No column was renamed, retyped, or dropped.
No existing row's values are altered by any migration in this update.

## 5. Testing Report

All tests run against a real SQLite schema (`init_saas_db()` +
`init_saas_business_tables()` + `init_ledger_engine_tables()` +
`seed_chart_of_accounts()`), calling the actual functions — not a
re-implementation. All were run to completion with `assert` statements,
not just "did it crash."

### Demo Business Deletion
| # | Check | Result |
|---|---|---|
| 1 | Two businesses created, each with products/customers/suppliers | ✅ |
| 2 | `get_business_summary()` returns a non-zero grouped total | ✅ |
| 3 | `delete_business_completely()` on business A | ✅ |
| 4 | Business A's row no longer exists | ✅ |
| 5 | Business A has zero records left across all 27 tables | ✅ |
| 6 | **Business B's record counts are byte-identical before/after** (tenant isolation) | ✅ |
| 7 | Business B's row and name are untouched | ✅ |

### National HSN Master / Business Type / Search / Suggestions
| # | Check | Result |
|---|---|---|
| 1 | `format_business_types()`/`parse_business_types()` round-trip, drop unknown slugs | ✅ |
| 2 | Multi-word search "electric fan" finds "Electric table fan" | ✅ |
| 3 | Category search finds tagged services | ✅ |
| 4 | Business-type-filtered search includes matching-tag codes, excludes non-matching | ✅ |
| 5 | **Untagged/pre-existing seeded codes still visible under every filter** (backward compat) | ✅ |
| 6 | Suggestion for a realistic product name ("Electric Table Fan 400mm") returns the right code — this is the case that failed with the first (too-strict) implementation, see §2 | ✅ |
| 7 | `get_business_hsn_types()`/`set_business_hsn_types()` round-trip | ✅ |
| 8 | **Ranking**: a 3/3-word match ("Electric Table Fan") ranks above a 2/3-word match ("Electric ceiling fan") | ✅ |

### GST Health Check — Document-Level Drill-Down
| # | Check | Result |
|---|---|---|
| 1 | An invoice saved with GST but no matching ledger entry (simulated failure) | Set up |
| 2 | Health check correctly identifies it as a "Missing Ledger Posting" issue | ✅ |
| 3 | Issue carries the exact invoice number, doc_type, doc_id, party name, and difference amount | ✅ (`INV/2026-27/000042`, `invoice`, correct id, `Alice Customer`, `₹180.00`) |
| 4 | The generic aggregate "Output GST" mismatch issue does NOT also fire redundantly once the specific document is found | ✅ |
| 5 | Category sub-scores computed correctly (GST Compliance dropped to 90, all others stayed 100) | ✅ |

### Nil-Rated / Exempt / Non-GST (GSTR-1)
| # | Check | Result |
|---|---|---|
| 1 | One invoice with a Nil-Rated line (Wheat, HSN tagged nil_rated) and a Taxable line (Laptop) | Set up |
| 2 | Query correctly splits them: Nil-Rated taxable_amount = ₹500.00, Taxable = ₹680.00 | ✅ |

### Freight & Round-Off (engine, from Update_032 — re-verified numerically against the new JS)
| # | Check | Result |
|---|---|---|
| 1 | JS `calcFreightGst()` + `recalc()` math, run in Node with the same inputs as Update_032's Python test (taxable=1000, cgst=90, sgst=90, freight=500 @ 18%) | Produces `{taxable:1500, cgst:135, sgst:135, totalTax:270, preRoundoff:1770}` — **identical to the Python backend's output** |

### No-regression / integration checks
| Check | Result |
|---|---|
| Full Python compile sweep (every `.py` file in the repo) | ✅ Clean |
| Full Jinja2 template parse sweep (all 87 templates) | ✅ All parse |
| Migration ordering bug (§2) — caught by actually running migrations against a fresh DB, then fixed and re-verified | ✅ Fixed, re-tested clean |
| Suggestion matching bug (§2) — caught by testing a realistic input, then fixed and re-verified with a ranking test | ✅ Fixed, re-tested clean |

### Not exercised in this pass (recommend before production)
- The actual HTTP routes for every new page (Health Check, HSN Master
  admin, Delete Business flows, POS/Purchase with freight) — verified
  by code inspection, Jinja2 parse-check, and direct function-level
  testing, but not driven through a real browser/HTTP request.
- A live Android Chrome / tablet visual check (same limitation noted in
  Update_031 — this environment has no way to drive a real device).
- The "Repair Ledger Posting" support workflow this update points to
  (intentionally not automated — see §2) has no tooling built for
  *support staff* to actually perform the repair; only the diagnostic
  side (finding and describing the problem) is complete.

## 6. Deployment Notes

1. **Back up the database before deploying**, as with any schema
   migration — this update's migrations are additive/non-destructive,
   but this is a standing recommendation for every update, not specific
   to this one.
2. **No environment variables or configuration changes required.**
3. **No new third-party dependencies.**
4. **First request after deploy** runs the schema migrations
   automatically (same pattern as every previous update) — no manual
   migration step needed for either SQLite or Postgres.
5. **HSN Master business-type tagging is opt-in** — every existing
   (and newly seeded) HSN code starts untagged (`business_types = ''`),
   which matches every business's filter by design (§2), so there is
   no "please tag your HSN codes before X breaks" step required at
   deploy time. Tagging is a purely additive, whenever-convenient App
   Admin task.
6. **Recommend running the GST Health Check once per business shortly
   after deploy** — not because this update is expected to introduce
   any inconsistency (it doesn't touch existing posting logic), but
   because it's the first time this diagnostic has existed at all, so
   it's a good moment to establish a baseline and catch anything that
   may have been silently wrong from before this update existed.
7. **Demo Business deletion is irreversible** — confirm this is
   communicated to anyone with owner/super-admin access before this
   ships, since the confirmation UI is the only safeguard (no
   "recently deleted" recovery/undo exists).

## 7. No-Regression Verification

- No existing route, permission, or template block was removed or
  renamed anywhere in this update.
- `calculate_gst()`, `apply_freight_and_roundoff()`, and every
  Update_032 validation function are unchanged — this update only adds
  new callers (the POS/Purchase UI wiring) and new consumers (the
  Health Check, GSTR-1's new section), never modifies their internals.
- Every new database column defaults to a value that reproduces prior
  behavior exactly for any row/business that doesn't explicitly use the
  new feature (empty business_types = shown everywhere; NULL tax_status
  override = inherit, defaulting further to 'taxable' if nothing else
  classifies it).
- `search_hsn()`'s signature change (new optional `business_types`
  parameter, defaulting to `None`) is backward compatible — every
  existing call site that doesn't pass it behaves exactly as before.
- Full compile/parse sweep clean across the entire repository, not just
  files touched by this update.
