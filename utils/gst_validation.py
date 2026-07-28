"""
utils/gst_validation.py — Update_032: GST Validation Engine
============================================================================
Pre-save validation for Sales Invoices and Purchase Bills — called by
modules/saas_business/billing.py::save_invoice() and
modules/saas_business/purchase.py::save() immediately before a document
is written, so an invalid document is rejected with a clear, specific
message instead of being silently saved wrong.

Design — errors block, warnings don't:
  Every check function returns "" for "no problem" or a human-readable
  message for "there's a problem" — never raises, never returns a code.
  The two top-level aggregators (validate_sales_document() /
  validate_purchase_document()) sort every individual check into one of
  two buckets:
    - `errors`   — the document is factually wrong (bad date, mismatched
                    CGST/SGST vs IGST selection, duplicate manual invoice
                    number, malformed HSN format) and saving it would
                    corrupt GST filing data. The caller (billing.py/
                    purchase.py) rejects the save and shows these.
    - `warnings` — the document might still be legitimate (an HSN code
                    that isn't in this app's local master — see
                    utils/hsn_master.py's docstring for why that's
                    common and legal; a GSTIN that fails checksum
                    format but the person may have typo'd digits they
                    can fix themselves) — shown to the user, but the
                    save proceeds. Being too strict here would block
                    real transactions over data-quality issues that
                    aren't actually wrong.
  This mirrors exactly how utils/hsn_master.py::validate_hsn_for_
  transaction() already works — this module is the same philosophy,
  extended to the rest of a document (GSTIN, state, dates, numbering).
"""

import re
from datetime import datetime, date, timedelta

from models.saas_auth import saas_fetchone, _is_postgres
from utils.hsn_master import validate_hsn_for_transaction

P = lambda: "%s" if _is_postgres() else "?"

# Standard GSTIN pattern: 2-digit state code + 10-char PAN (5 letters,
# 4 digits, 1 letter) + 1 entity-count digit/letter + literal 'Z' +
# 1 checksum character.
GSTIN_PATTERN = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$')


def _valid_state_codes() -> set:
    from config import ActiveConfig
    return {code for code, _ in ActiveConfig.INDIAN_STATES}


# ═══════════════════════════════ INDIVIDUAL CHECKS ═════════════════════════

def validate_gstin(gstin: str, required: bool = False) -> str:
    """Format + state-code cross-check. Does NOT verify the GSTIN is
    actually registered/active with the department (that needs a live
    GSTN API call — out of scope for this local, offline validation
    pass) — this only catches "this can't possibly be a valid GSTIN"."""
    gstin = (gstin or "").strip().upper()
    if not gstin:
        return "GSTIN is required." if required else ""
    if len(gstin) != 15:
        return f"GSTIN '{gstin}' must be exactly 15 characters (got {len(gstin)})."
    if not GSTIN_PATTERN.match(gstin):
        return (f"'{gstin}' doesn't match the standard GSTIN format "
                f"(2-digit state code + 10-character PAN + entity code + 'Z' + checksum).")
    if gstin[:2] not in _valid_state_codes():
        return f"GSTIN '{gstin}' starts with state code {gstin[:2]}, which isn't a recognized Indian state/UT code."
    return ""


def validate_state_code(state_code: str, required: bool = True) -> str:
    state_code = (state_code or "").strip()
    if not state_code:
        return "State is required." if required else ""
    if state_code not in _valid_state_codes():
        return f"'{state_code}' isn't a recognized Indian state/UT code."
    return ""


def validate_supply_type_selection(business_state: str, party_state: str, supply_type: str) -> str:
    """
    Cross-checks the chosen supply_type ('intra'/'inter') against the
    actual state codes on file — this is exactly what decides CGST+SGST
    vs. IGST, so a mismatch here would silently post the wrong tax type
    to the ledger and the wrong GSTR-1 section (B2B intra vs. inter).
    No party state on file (e.g. a walk-in cash customer) means there's
    nothing to cross-check against — not itself an error.
    """
    if not party_state:
        return ""
    expected = "intra" if business_state == party_state else "inter"
    if supply_type != expected:
        right = "CGST+SGST" if expected == "intra" else "IGST"
        wrong = "IGST" if expected == "intra" else "CGST+SGST"
        return (f"Supply type mismatch: business state ({business_state}) vs. party state "
                f"({party_state}) should be {right}, but {wrong} was selected.")
    return ""


def validate_document_date(date_str: str, allow_future_days: int = 1) -> str:
    """
    Rejects a blank/malformed date and a date implausibly far in the
    future (almost always a typo'd year) or before GST's own
    implementation date. `allow_future_days` gives a small grace window
    for timezone edge cases rather than rejecting "today" evaluated in
    a different timezone than the server's.
    """
    if not date_str:
        return "Document date is required."
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return f"'{date_str}' isn't a valid date (expected YYYY-MM-DD)."
    if d > date.today() + timedelta(days=allow_future_days):
        return f"Document date {date_str} is in the future — check for a typo."
    if d < date(2017, 7, 1):
        return f"Document date {date_str} is before GST's implementation (1 July 2017) — check for a typo."
    return ""


def validate_fy_numbering(document_date: str, doc_fy: str) -> str:
    """
    Sanity-checks that the financial year a document was NUMBERED under
    (doc_fy, stamped by utils/document_numbering.py at creation —  e.g.
    "2026-27") actually matches the financial year its OWN document_date
    falls in. These can only diverge if a caller passes an inconsistent
    date/doc_fy pair directly (not possible through the normal billing.py/
    purchase.py flow, where both come from the same generate_document_
    number() call) — this exists as a defensive backstop for the GST
    Health Check (see gst.py::health_check()) to flag if it ever happens,
    e.g. from a future direct-API integration.
    """
    if not doc_fy or not document_date:
        return ""
    from utils.document_numbering import financial_year_for_date
    try:
        actual_fy = financial_year_for_date(document_date)
    except (ValueError, TypeError):
        return ""
    if actual_fy != doc_fy:
        return f"Document date {document_date} falls in FY {actual_fy}, but it was numbered under FY {doc_fy}."
    return ""


def validate_duplicate_document_number(business_id: int, document_number: str,
                                        table: str, column: str, exclude_id=None) -> str:
    """
    Checks for a duplicate document number within one business. Under
    normal auto-numbering (Update_027) this can never actually fire —
    the database's own UNIQUE(business_id, document_type, financial_year)
    constraint on saas_document_sequences already makes collisions
    structurally impossible. This check exists specifically for Manual
    Numbering mode (Update_027 §"Manual Numbering"), where staff type
    their own number and nothing else guards against a repeat — see
    billing.py/purchase.py's save routes, which already had this exact
    check written inline; it's centralized here so both call the same
    logic instead of two near-duplicate copies drifting apart.
    """
    if not document_number:
        return ""
    p = P()
    id_col = "id"
    exclude_clause = f" AND {id_col} != {p}" if exclude_id else ""
    params = (business_id, document_number) + ((exclude_id,) if exclude_id else ())
    row = saas_fetchone(
        f"SELECT {id_col} FROM {table} WHERE business_id={p} AND {column}={p}{exclude_clause}",
        params
    )
    if row:
        return f"Document number '{document_number}' is already in use."
    return ""


def validate_reverse_charge(reverse_charge: bool, party_gstin: str, party_state: str) -> str:
    """
    Lightweight structural sanity check only — this is NOT a full
    Reverse Charge Mechanism (RCM) applicability engine (deciding
    *whether* RCM should apply to a given good/service/party
    combination requires India's full Section 9(3)/9(4) notified-goods-
    and-services list, which is out of scope for this Phase 1 pass —
    see CHANGELOG_Update_032.md §5 "Future Compatibility"). This only
    catches the one case worth flagging today: RCM marked on a document
    with no party details at all, which is unusual enough to be worth a
    warning (RCM invoices/bills almost always have an identified
    counterparty on record).
    """
    if reverse_charge and not party_gstin and not party_state:
        return "Reverse Charge is marked, but no party GSTIN/state is on file for this document — verify this is intentional."
    return ""


# ═══════════════════════════════ AGGREGATORS ═══════════════════════════════

def validate_sales_document(business_id: int, *, business_state: str,
                            customer_gstin: str = "", customer_state: str = "",
                            supply_type: str = "intra", document_date: str = "",
                            items: list = None, reverse_charge: bool = False,
                            manual_invoice_number: str = None) -> dict:
    """
    Runs every applicable check for a Sales Invoice about to be saved.
    Returns {"errors": [...], "warnings": [...]} — see module docstring
    for how billing.py::save_invoice() should treat each list.
    `items` is the same item-dict list billing.py already builds
    (each with at least "hsn_code" and "gst_rate").
    """
    errors, warnings = [], []

    e = validate_document_date(document_date)
    if e: errors.append(e)

    e = validate_state_code(business_state)
    if e: errors.append(f"Business state: {e}")

    if customer_gstin:
        w = validate_gstin(customer_gstin)
        if w: warnings.append(f"Customer GSTIN: {w}")

    e = validate_supply_type_selection(business_state, customer_state, supply_type)
    if e: errors.append(e)

    if manual_invoice_number:
        e = validate_duplicate_document_number(
            business_id, manual_invoice_number, "saas_invoices", "invoice_number")
        if e: errors.append(e)

    w = validate_reverse_charge(reverse_charge, customer_gstin, customer_state)
    if w: warnings.append(w)

    for idx, item in enumerate(items or [], start=1):
        for issue in validate_hsn_for_transaction(item.get("hsn_code", ""), item.get("gst_rate")):
            warnings.append(f"Item {idx} ({item.get('product_name', '?')}): {issue}")

    return {"errors": errors, "warnings": warnings}


def validate_purchase_document(business_id: int, *, business_state: str,
                               supplier_gstin: str = "", supplier_state: str = "",
                               supply_type: str = "intra", document_date: str = "",
                               items: list = None, reverse_charge: bool = False,
                               manual_purchase_number: str = None) -> dict:
    """Purchase-side equivalent of validate_sales_document() — same
    structure, supplier instead of customer."""
    errors, warnings = [], []

    e = validate_document_date(document_date)
    if e: errors.append(e)

    e = validate_state_code(business_state)
    if e: errors.append(f"Business state: {e}")

    if supplier_gstin:
        w = validate_gstin(supplier_gstin)
        if w: warnings.append(f"Supplier GSTIN: {w}")

    e = validate_supply_type_selection(business_state, supplier_state, supply_type)
    if e: errors.append(e)

    if manual_purchase_number:
        e = validate_duplicate_document_number(
            business_id, manual_purchase_number, "saas_purchases", "purchase_number")
        if e: errors.append(e)

    w = validate_reverse_charge(reverse_charge, supplier_gstin, supplier_state)
    if w: warnings.append(w)

    for idx, item in enumerate(items or [], start=1):
        for issue in validate_hsn_for_transaction(item.get("hsn_code", ""), item.get("gst_rate")):
            warnings.append(f"Item {idx} ({item.get('product_name', '?')}): {issue}")

    return {"errors": errors, "warnings": warnings}
