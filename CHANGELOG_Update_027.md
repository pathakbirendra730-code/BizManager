# BizManager-v6 — Update_027
## Configurable Voucher/Document Numbering + Live Settings Preview

## 1. Executive Summary

Extends the FY-based document numbering engine (`utils/document_numbering.py`)
from a fixed `PREFIX/FY/000001` format into a fully admin-configurable one,
driven entirely from App Admin → Settings → Document Numbering:

- **Prefix** — per document type (already existed; unchanged)
- **Suffix (optional)** — free text appended at the very end
- **Separator** — `/` or `-`
- **Starting Number** — default 1, applies to the next *new* period only
- **Digit Length** — default 6, zero-pads the running number
- **Auto Reset** — Financial Year (default), Monthly, or Never
- **Manual Numbering** — Enable/Disable; when on, staff can type their own
  invoice/purchase number instead of using the auto sequence

The Settings page now also shows a **live preview** of the next number for
all six document types, updating as any field changes — before Save is
clicked.

Every new setting defaults to exactly what Update_027 already produced, so
an existing install that never opens the new controls keeps generating
`INV/2026-27/000001`, `PB/2026-27/000001`, `CN/2026-27/000001`,
`DN/2026-27/000001`, `QT/2026-27/000001`, `DC/2026-27/000001` — byte for
byte identical to before this change. No existing `saas_invoices` /
`saas_purchases` row, and no row already in `saas_document_sequences`, is
touched by this update.

## 2. Design Notes

**Formatting is re-derived at generation time, never stored as a template.**
`saas_document_sequences` still only stores prefix-independent primitives
(business, document type, period key, running sequence) — the separator,
digit length, and suffix are read from settings and applied when a number
is generated or previewed, not baked into the stored row. This means the
*next* document reflects a settings change immediately, while every
already-issued number (stored verbatim as `invoice_number` /
`purchase_number` on the document itself) is completely unaffected.

**The `financial_year` column is now a general period key.** Rather than
add a schema migration, the existing `TEXT` column that used to hold only
`"2026-27"`-shaped values now holds whatever the active Auto Reset mode
produces: a financial year for the default mode, `"YYYY-MM"` for Monthly,
or the constant `"ALL"` for Never (one continuous sequence, so there's
nothing to key by). The `UNIQUE(business_id, document_type, financial_year)`
constraint does exactly the same job it always did — give each period its
own independent counter — just with a broader definition of "period".

**Starting Number only affects brand-new counters.** It feeds the `INSERT`
branch of the existing atomic UPSERT (`ON CONFLICT ... DO UPDATE SET
last_sequence = last_sequence + 1`) — a counter that already has a row
always just increments by 1, regardless of the current Starting Number
setting. Changing it mid-year (or mid-month) never renumbers anything; it
only takes effect the next time a genuinely new period begins.

**Manual Numbering never guesses.** `generate_document_number()` takes an
explicit `manual_number` argument. It's only honored when both (a) a
non-empty value was passed AND (b) the admin toggle is currently on — a
stale client that still sends a manual number after the toggle is switched
back off is silently ignored, and the auto sequence (which never stopped
running in the background) is used instead. This means flipping the
toggle off always resumes auto-numbering with no gap and no re-entry
needed.

**Duplicate-number guard for manual entries.** Since manually-typed
numbers bypass the sequence table entirely, `billing.py`/`purchase.py`
now check for an existing row with the same `invoice_number` /
`purchase_number` before saving, and reject the save with a clear error
if it's already in use — the one place manual numbering could otherwise
create a silent collision.

## 3. Complete List of Changes

1. **`utils/platform_settings.py`** — six new settings added to the
   existing "Document Numbering" group, ahead of the six prefix fields:
   `doc_numbering_separator` (select, `/` or `-`), `doc_numbering_suffix`
   (text, optional, max 10 chars), `doc_numbering_start_number` (number,
   1–999999999), `doc_numbering_digit_length` (number, 1–12),
   `doc_numbering_auto_reset` (select: `financial_year` / `monthly` /
   `never`, with friendly `option_labels` for the select UI), and
   `doc_numbering_manual_enable` (bool). All default to the values that
   reproduce the original fixed format exactly.

2. **`utils/document_numbering.py`** — rewritten to read the new settings:
   - `_numbering_config()` — reads separator/suffix/start-number/
     digit-length/reset-mode once per call.
   - `_period_key_for_date()` — computes the period key per the active
     Auto Reset mode (FY / month / the `"ALL"` sentinel for Never).
   - `_format_number()` — the single place the display string is
     assembled, used by both real generation and preview so they can
     never drift out of sync with each other.
   - `generate_document_number()` — now accepts `manual_number=`; the
     Starting Number setting seeds new counters; the digit length /
     separator / suffix settings are applied to every formatted result.
   - `current_sequence_position()` — generalized from a `financial_year`
     parameter to a `period_key` one (defaults to today's period under
     whatever reset mode is currently active).
   - New `preview_next_document_number()` — the one function the POS and
     New Purchase screens call for their "Invoice #" / "Purchase #"
     badge; returns `None` when Manual Numbering is on (nothing to
     preview — the number is whatever staff types on save).

3. **`modules/saas_business/billing.py`** — `_generate_invoice_number()`
   now delegates to `preview_next_document_number()` instead of
   hand-building the FY/prefix string; `pos()` passes a `manual_numbering`
   flag to the template; `save_invoice()` reads an optional
   `manual_doc_number` from the save payload, checks it isn't already in
   use, and passes it through to `generate_document_number()`.

4. **`modules/saas_business/purchase.py`** — identical treatment:
   `_generate_purchase_number()`, the `new()` route, and `save()`
   (with the same duplicate-number guard against `saas_purchases`).

5. **`templates/saas_business/billing/pos.html`** /
   **`templates/saas_business/purchase/new.html`** — the invoice/purchase
   number badge becomes an editable text input when Manual Numbering is
   on (otherwise unchanged, read-only preview badge as before); the save
   payload now includes `manual_doc_number`, with a client-side guard
   requiring it when manual mode is active.

6. **`templates/app_admin/settings.html`**:
   - Fixed a pre-existing display bug where *every* settings group
     (including "Document Numbering") showed the caption "Only used when
     Email Provider or SMS Provider above is set to {group}" — that text
     is now per-group, with a correct description for Document Numbering.
   - Added `option_labels` support to the `<select>` renderer so Auto
     Reset shows "Financial Year" / "Monthly" / "Never" instead of the
     raw `financial_year` / `monthly` / `never` values.
   - Added a **live preview card** under the Document Numbering group:
     a small JS function (`updateNumberingPreview()`) reads the current
     (unsaved) values of all Document Numbering fields plus all six
     prefix fields, and renders what the *next* number for each of the
     six document types would look like, recomputing on every keystroke/
     change — entirely client-side, no round trip, so admins can see the
     effect of a change before saving it. Shows a note instead when
     Manual Numbering is toggled on, since there's nothing to preview.

7. **`modules/app_admin/dashboard.py`** — `platform_settings()` now also
   passes `current_fy` / `current_month` to the template (computed via
   the existing `financial_year_for_date()`), so the JS preview can show
   realistic period tokens without duplicating India-FY date math in
   JavaScript.

## 4. No-Regression Verification

- Every new setting's default reproduces the original literal format
  (`/` separator, empty suffix, start 1, 6 digits, Financial Year reset,
  Manual Numbering off) — confirmed by re-reading `_format_number()`
  against the six example numbers in the spec.
- `saas_document_sequences` schema is untouched (no migration): the
  `financial_year` column simply now holds a broader set of valid values
  under non-default settings; every row written before this update keeps
  its original meaning and keeps being read correctly.
- Nothing renumbers or reformats an already-issued `invoice_number` /
  `purchase_number` — those are stored as plain strings on the document
  row at creation time and are never regenerated on read.
- `generate_document_number()`'s public signature is backward compatible
  — `manual_number` is a new optional keyword argument, so every existing
  call site continues to work unchanged until explicitly updated to pass
  one.
