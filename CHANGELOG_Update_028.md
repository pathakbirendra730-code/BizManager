# BizManager-v6 — Update_028
## Document Numbering: Per-Business Owner Control

## 1. Executive Summary

Follow-up to Update_027 (Configurable Document Numbering). That update made
prefix/separator/suffix/digit-length/auto-reset/manual-numbering
admin-editable, but only at the platform level — a single App Admin
Settings page controlled the format for every business on BizManager.

This update lets **each business's own owner** configure their own
Document Numbering from their existing Business Settings page
(`/saas/business-settings`) — no App Admin / super-admin access needed.
The platform-level settings from Update_027 don't go away; they become
the **default** every business inherits until it saves its own override,
and stay in effect for the whole formatting engine (prefixes, separator,
suffix, starting number, digit length, auto reset, manual numbering).

## 2. Design Notes

**Override, not replace.** A new per-business key/value table,
`saas_business_settings`, stores only the keys a business owner has
explicitly changed. `utils/business_settings.py::get_business_setting()`
checks that table first; if the business has never saved a given key, it
falls straight through to `utils.platform_settings.get_setting()` — the
exact platform-wide value from Update_027 (itself falling back to the
hardcoded schema default). Net effect: a business that never opens its
new numbering section behaves identically to before this update, and the
App Admin page remains meaningful as the platform default rather than
becoming a dead setting.

**One schema, two forms.** `BUSINESS_SETTINGS_SCHEMA` in
`utils/business_settings.py` is not a redefinition — it's literally the
"Document Numbering" group already declared in
`utils/platform_settings.py`'s `SETTINGS_SCHEMA` (`[s for s in
SETTINGS_SCHEMA if s.get("group") == "Document Numbering"]`). The App
Admin form and every business's own form render from the same labels,
types, options, and validators, so they can never drift out of sync, and
adding a future numbering setting is still "add one schema entry", not
"add it twice."

**Full isolation between businesses.** Every function in
`utils/document_numbering.py` already took `business_id` as a parameter
(needed for the sequence counters), so switching its settings reads from
`utils.platform_settings` to `utils.business_settings` was a matter of
threading that same `business_id` through to the settings lookups too —
no new parameter needed anywhere. Verified end-to-end: two businesses
with different prefixes/separators/manual-numbering-toggles generate
completely independent, correctly-formatted numbers with no cross-talk,
and changing one business's format never touches another's sequence
counter or its next number.

**Owner-only, consistent with the existing permission model.**
`utils/saas_middleware.py`'s `PERMISSIONS` dict already had
`"business_settings": "owner"` — a permission key that existed but had no
route enforcing it yet. The two new routes
(`/saas/business-settings/numbering` and
`/saas/business-settings/numbering/reset`) are the first to actually gate
on it: only the business owner (not manager/accountant/staff) can view or
change their business's numbering configuration, since it affects every
invoice/bill the whole team issues.

**"Reset to platform defaults" is a real, explicit action**, not just
"set every field back to its current default value" (which would freeze
in today's platform default even if the platform default changes later).
It deletes the business's override rows entirely, so the business goes
back to *live-following* whatever the platform default currently is — the
same behavior as a business that never customized anything.

## 3. Complete List of Changes

1. **New table: `saas_business_settings`** (`models/saas_business_data.py`,
   both SQLite and Postgres schemas) — generic per-business key/value
   store: `(business_id, key)` unique, `value`, `updated_by`,
   `updated_at`. Not Document-Numbering-specific by design, in case a
   future business-level setting needs the same override/fallback
   pattern.

2. **New `utils/business_settings.py`** — `BUSINESS_SETTINGS_SCHEMA`
   (reused from platform schema, see Design Notes), `get_business_setting()`
   / `get_business_bool_setting()` / `get_business_int_setting()` (business
   override → platform default → schema default), `set_business_setting()`
   (validates against the same schema validators as the platform version),
   `reset_business_setting()` / `reset_all_business_numbering_settings()`,
   and `all_business_settings()` (for rendering the form, includes an
   `is_override` flag per field so the UI can show which fields are
   customized vs. inherited).

3. **`utils/document_numbering.py`** — every settings read
   (`_numbering_config()`, the per-document-type prefix lookup, the Manual
   Numbering check in `generate_document_number()` and
   `preview_next_document_number()`, the Auto Reset mode lookup in
   `current_sequence_position()`) now goes through
   `utils.business_settings` instead of `utils.platform_settings`,
   resolved for the specific `business_id` already passed into every one
   of these functions. Docstring updated to describe the override/fallback
   chain.

4. **`modules/saas_business/billing.py` / `purchase.py`** — the
   `manual_numbering` flag read in `pos()` / `new()` now calls
   `utils.business_settings.get_business_bool_setting(biz_id, ...)`
   instead of the platform-level `get_bool_setting(...)`.

5. **`modules/saas_auth/routes.py`**:
   - `business_settings()` (GET path) now also gathers
     `all_business_settings(biz_id)`, an `is_owner` flag, and
     `current_fy` / `current_month` (for the live preview), and passes
     them to the existing Business Settings template.
   - New `POST /saas/business-settings/numbering` —
     `document_numbering_settings()` — owner-only, validates and saves
     each Document Numbering field via `set_business_setting()`, same
     tamper-guarding pattern as the App Admin settings route (unknown
     `select` values are silently ignored rather than saved).
   - New `POST /saas/business-settings/numbering/reset` —
     `reset_document_numbering_settings()` — owner-only, clears every
     override for this business via
     `reset_all_business_numbering_settings()`.

6. **`templates/saas_auth/business_settings.html`** — new "🔢 Document
   Numbering" card, visible only to owners (`is_owner`), with:
   - The six general format fields (Separator, Suffix, Starting Number,
     Digit Length, Auto Reset, Manual Numbering) and the six per-type
     prefixes, each labeled "CUSTOM" when this business has its own
     override vs. inheriting the platform default.
   - The same live client-side preview pattern introduced in Update_027's
     App Admin page (`updateBizNumberingPreview()`), showing the next
     number for all six document types as any field changes, before
     saving.
   - A "Reset to platform defaults" action (with a confirm dialog) that
     posts to the new reset route.

7. **`utils/platform_settings.py` / `templates/app_admin/settings.html`**
   — updated comments and the Document Numbering group description to
   state plainly that these values are now the *platform-wide default*,
   overridable per business, rather than the only numbering configuration
   in the app.

## 4. No-Regression Verification

- End-to-end test (real SQLite schema, two businesses): both businesses
  start out generating the identical default format
  (`INV/2026-27/000001`); overriding one business's prefix and separator
  changes only that business's output and leaves the other business's
  format and sequence counter completely untouched.
- Manual Numbering is confirmed per-business: turning it on for one
  business and passing a `manual_number` is honored only for that
  business; the same call shape for a business with the setting off is
  silently ignored and falls through to the normal auto sequence (no
  gap, no skipped number).
- `reset_all_business_numbering_settings()` confirmed to fully revert a
  business's fields back to the live platform default.
- Every new route (`.../numbering`, `.../numbering/reset`) is additive to
  the existing `saas_auth` blueprint — no existing route, permission, or
  template block was removed or renamed. `business_settings()`'s existing
  GET/POST business-profile behavior (name/GSTIN/address/etc.) is
  unchanged; the new context variables are additive kwargs to the same
  `render_template()` call.
- Businesses created before this update have no rows in the new
  `saas_business_settings` table, so `get_business_setting()` falls
  through to the platform default for every key — identical behavior to
  Update_027 for any business that doesn't visit the new section.
