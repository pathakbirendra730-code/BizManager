"""
utils/gst_health.py — Update_032: GST Health Check
============================================================================
Runs a battery of consistency checks across everything a GST return
(GSTR-1/GSTR-3B) depends on, before generating one — catching data
problems while they're still cheap to fix, rather than after filing.

Design: every check returns zero or more `Issue` dicts
    {"severity": "error"|"warning", "category": str, "message": str, "fix": str}
`run_health_check()` collects all of them, computes a 0-100 score, and
returns the whole report — modules/saas_business/gst.py's health_check()
route only renders it, it doesn't compute anything.

Score formula: start at 100, each "error" issue costs 10 points, each
"warning" costs 3, floored at 0 — deliberately simple and legible (an
admin can mentally reconstruct the score from the issue list, rather
than trying to reverse-engineer an opaque weighted formula).
"""

from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"


def _issue(severity, category, message, fix):
    return {"severity": severity, "category": category, "message": message, "fix": fix}


def _check_trial_balance(biz_id: int) -> list:
    """1. Ledger balance — total debits must equal total credits across
    every journal line for this business. Any mismatch here means the
    double-entry ledger itself is broken, which invalidates every GST
    figure derived from it."""
    row = saas_fetchone(
        f"""SELECT COALESCE(SUM(debit),0) as d, COALESCE(SUM(credit),0) as c
            FROM saas_journal_lines WHERE business_id={P()}""",
        (biz_id,)
    )
    debit, credit = float(row["d"]), float(row["c"])
    if abs(debit - credit) > 0.01:
        return [_issue("error", "Ledger",
            f"Trial Balance doesn't balance: total debits ₹{debit:,.2f} vs. total credits ₹{credit:,.2f} "
            f"(difference ₹{abs(debit-credit):,.2f}).",
            "This should never happen through normal use of the app — every posting route uses an atomic, "
            "balanced double-entry helper. If you see this, stop filing and contact support before proceeding.")]
    return []


def _check_inventory_balance(biz_id: int) -> list:
    """2. Inventory balance — no product should ever have negative
    stock; it means more was sold/returned-out than was ever
    purchased/returned-in, which usually points to a data entry
    problem (e.g. selling before recording the purchase)."""
    rows = saas_fetchall(
        f"SELECT name, sku, stock_quantity FROM saas_products WHERE business_id={P()} AND stock_quantity < 0",
        (biz_id,)
    )
    return [_issue("warning", "Inventory",
        f"'{r['name']}' ({r['sku'] or 'no SKU'}) has negative stock: {r['stock_quantity']:g}.",
        "Record any missing purchase entries for this product, or adjust stock via Products → Edit."
    ) for r in rows]


def _check_hsn_validity(biz_id: int) -> list:
    """3. HSN validity — every HSN/SAC code actually used on a Sales/
    Purchase line should exist in the HSN master and be active."""
    issues = []
    p = P()
    for label, items_table, join_table, join_col in [
        ("Sales", "saas_invoice_items", "saas_invoices", "invoice_id"),
        ("Purchase", "saas_purchase_items", "saas_purchases", "purchase_id"),
    ]:
        rows = saas_fetchall(
            f"""SELECT DISTINCT ii.hsn_code
                FROM {items_table} ii
                WHERE ii.business_id={p} AND ii.hsn_code IS NOT NULL AND ii.hsn_code != ''""",
            (biz_id,)
        )
        used_codes = {r["hsn_code"] for r in rows}
        if not used_codes:
            continue
        placeholders = ",".join([p] * len(used_codes))
        known = saas_fetchall(
            f"SELECT hsn_code, is_active FROM hsn_master WHERE hsn_code IN ({placeholders})",
            tuple(used_codes)
        )
        known_map = {r["hsn_code"]: r["is_active"] for r in known}
        for code in sorted(used_codes):
            if code not in known_map:
                issues.append(_issue("warning", "HSN",
                    f"{label} documents use HSN/SAC '{code}', which isn't in the HSN master.",
                    f"Add '{code}' to the HSN Master (App Admin → HSN/SAC Master) so future GSTR-1/HSN "
                    f"Summary reports can classify it correctly."))
            elif not known_map[code]:
                issues.append(_issue("warning", "HSN",
                    f"{label} documents use HSN/SAC '{code}', which is marked inactive in the HSN master.",
                    f"Reactivate '{code}' if it's still valid, or update the affected line items to a current code."))
    return issues


def _check_gstin_validity(biz_id: int) -> list:
    """4. GSTIN validity — every customer/supplier GSTIN on file should
    at least be structurally well-formed (see utils/gst_validation.py;
    this doesn't verify live registration status with the department)."""
    from utils.gst_validation import validate_gstin
    issues = []
    p = P()
    customers = saas_fetchall(
        f"SELECT name, gstin FROM saas_customers WHERE business_id={p} AND gstin IS NOT NULL AND gstin != ''",
        (biz_id,)
    )
    for c in customers:
        err = validate_gstin(c["gstin"])
        if err:
            issues.append(_issue("warning", "GSTIN",
                f"Customer '{c['name']}': {err}",
                "Correct the GSTIN on the customer's profile."))
    suppliers = saas_fetchall(
        f"SELECT name, gstin FROM saas_suppliers WHERE business_id={p} AND gstin IS NOT NULL AND gstin != ''",
        (biz_id,)
    )
    for s in suppliers:
        err = validate_gstin(s["gstin"])
        if err:
            issues.append(_issue("warning", "GSTIN",
                f"Supplier '{s['name']}': {err}",
                "Correct the GSTIN on the supplier's profile."))
    return issues


def _check_invoice_numbering(biz_id: int) -> list:
    """5. Invoice numbering — checks each (document type, financial
    year) sequence for gaps. A gap isn't necessarily wrong (a failed
    save after the number was already allocated deliberately never
    reuses it — see utils/document_numbering.py), but it's worth
    surfacing so it can be explained rather than silently missed."""
    issues = []
    p = P()
    for label, table, doc_label in [("Sales Invoice", "saas_invoices", "invoice"),
                                     ("Purchase Bill", "saas_purchases", "purchase")]:
        rows = saas_fetchall(
            f"""SELECT doc_fy, MIN(doc_sequence) as lo, MAX(doc_sequence) as hi, COUNT(*) as cnt
                FROM {table} WHERE business_id={p} AND doc_sequence IS NOT NULL
                GROUP BY doc_fy""",
            (biz_id,)
        )
        for r in rows:
            expected = int(r["hi"]) - int(r["lo"]) + 1
            if expected != int(r["cnt"]):
                gap = expected - int(r["cnt"])
                issues.append(_issue("warning", "Numbering",
                    f"{label} numbering for FY {r['doc_fy']} has {gap} missing number(s) between "
                    f"#{int(r['lo'])} and #{int(r['hi'])} ({r['cnt']} of {expected} expected {doc_label}s present).",
                    "Usually harmless (a save that failed after a number was already allocated never reuses "
                    "it) — confirm there's no missing document you need to record, then no action is needed."))
    return issues


def _check_gst_reconciliation(biz_id: int) -> list:
    """6+7. Input GST (ITC) / Output GST — cross-checks the GST stored
    on Sales/Purchase documents (net of Credit/Debit Notes) against
    what's actually posted to the Output GST / Input GST ledger
    accounts. These are computed independently (documents vs. ledger
    postings via utils/ledger_transactions.py) and should always agree
    exactly — any drift means a document was saved without its matching
    ledger entry, or vice versa."""
    issues = []
    p = P()

    output_gst_docs = saas_fetchone(
        f"SELECT COALESCE(SUM(cgst_amount+sgst_amount+igst_amount),0) as t FROM saas_invoices WHERE business_id={p}",
        (biz_id,)
    )
    cdnr_gst = saas_fetchone(
        f"SELECT COALESCE(SUM(cgst_amount+sgst_amount+igst_amount),0) as t FROM saas_credit_notes WHERE business_id={p}",
        (biz_id,)
    )
    net_output_expected = round(float(output_gst_docs["t"]) - float(cdnr_gst["t"]), 2)
    output_gst_ledger = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.credit - jl.debit),0) as t FROM saas_journal_lines jl
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND coa.account_subtype='gst_payable'""",
        (biz_id,)
    )
    net_output_actual = round(float(output_gst_ledger["t"]), 2)
    if abs(net_output_expected - net_output_actual) > 0.02:
        issues.append(_issue("error", "Output GST",
            f"Output GST per documents (₹{net_output_expected:,.2f}, net of Credit Notes) doesn't match "
            f"the Output GST ledger balance (₹{net_output_actual:,.2f}).",
            "Check for an invoice or credit note saved without its matching ledger posting."))

    itc_docs = saas_fetchone(
        f"SELECT COALESCE(SUM(cgst_amount+sgst_amount+igst_amount),0) as t FROM saas_purchases WHERE business_id={p} AND status != 'cancelled'",
        (biz_id,)
    )
    dnr_gst = saas_fetchone(
        f"SELECT COALESCE(SUM(cgst_amount+sgst_amount+igst_amount),0) as t FROM saas_debit_notes WHERE business_id={p}",
        (biz_id,)
    )
    net_itc_expected = round(float(itc_docs["t"]) - float(dnr_gst["t"]), 2)
    itc_ledger = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.debit - jl.credit),0) as t FROM saas_journal_lines jl
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND coa.account_subtype='gst_input_credit'""",
        (biz_id,)
    )
    net_itc_actual = round(float(itc_ledger["t"]), 2)
    if abs(net_itc_expected - net_itc_actual) > 0.02:
        issues.append(_issue("error", "Input GST (ITC)",
            f"Input GST/ITC per documents (₹{net_itc_expected:,.2f}, net of Debit Notes) doesn't match "
            f"the ITC ledger balance (₹{net_itc_actual:,.2f}).",
            "Check for a purchase or debit note saved without its matching ledger posting."))

    return issues


def _check_returns_integrity(biz_id: int) -> list:
    """8. Credit/Debit Notes & Returns adjustment — re-verifies (as a
    safety net; this is already enforced at write time by returns.py)
    that no line item's returned_quantity exceeds its original
    quantity."""
    issues = []
    p = P()

    over_returned_inv = saas_fetchall(
        f"""SELECT ii.product_name, ii.quantity, ii.returned_quantity, i.invoice_number
            FROM saas_invoice_items ii JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND ii.returned_quantity > ii.quantity""",
        (biz_id,)
    )
    for r in over_returned_inv:
        issues.append(_issue("error", "Returns",
            f"Invoice {r['invoice_number']}: '{r['product_name']}' shows returned_quantity "
            f"({r['returned_quantity']:g}) greater than the sold quantity ({r['quantity']:g}).",
            "This should be structurally impossible through the Returns feature — contact support."))

    over_returned_pur = saas_fetchall(
        f"""SELECT pi.product_name, pi.quantity, pi.returned_quantity, pu.purchase_number
            FROM saas_purchase_items pi JOIN saas_purchases pu ON pu.id = pi.purchase_id
            WHERE pi.business_id={p} AND pi.returned_quantity > pi.quantity""",
        (biz_id,)
    )
    for r in over_returned_pur:
        issues.append(_issue("error", "Returns",
            f"Purchase {r['purchase_number']}: '{r['product_name']}' shows returned_quantity "
            f"({r['returned_quantity']:g}) greater than the purchased quantity ({r['quantity']:g}).",
            "This should be structurally impossible through the Returns feature — contact support."))

    return issues


CHECKS = [
    _check_trial_balance,
    _check_inventory_balance,
    _check_hsn_validity,
    _check_gstin_validity,
    _check_invoice_numbering,
    _check_gst_reconciliation,
    _check_returns_integrity,
]


def run_health_check(biz_id: int) -> dict:
    """
    Runs every check and returns:
        {"score": int, "issues": [...], "error_count": int, "warning_count": int}
    `issues` is sorted errors-first (most actionable/severe first).
    """
    issues = []
    for check in CHECKS:
        issues.extend(check(biz_id))

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    score = max(0, 100 - error_count * 10 - warning_count * 3)

    issues.sort(key=lambda i: 0 if i["severity"] == "error" else 1)

    return {
        "score": score,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "is_clean": not issues,
    }
