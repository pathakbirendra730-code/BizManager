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


def _issue(severity, category, message, fix, **actions):
    """
    Update_033: `actions` carries structured metadata the template can
    turn into real links/buttons — e.g. doc_type="invoice", doc_id=42
    lets the template render "View Document" / "View Ledger" links
    without the template itself needing to know anything about GST
    logic. Every existing call site (that doesn't pass any actions)
    keeps working exactly as before — actions is empty by default, and
    the template only renders a link when the corresponding key exists.
    """
    issue = {"severity": severity, "category": category, "message": message, "fix": fix}
    issue.update(actions)
    return issue


def _find_documents_missing_ledger_posting(biz_id: int, table: str, id_col: str, number_col: str,
                                           date_col: str, party_col: str, source_types: list,
                                           gst_expr: str, doc_type: str) -> list:
    """
    Update_033 — the document-level drill-down: instead of only
    reporting "Output GST doesn't match by ₹X", finds the SPECIFIC
    document(s) whose GST amount has no corresponding journal entry
    (matched by source_type + source_id, the same linkage
    utils/ledger_transactions.py already writes on every posting) —
    the single most common real cause of a reconciliation mismatch.
    Only flags documents with a non-zero GST amount (a zero-GST
    document, e.g. an entirely Nil Rated sale, legitimately has nothing
    to post to Output/Input GST).
    """
    p = P()
    placeholders = ",".join([p] * len(source_types))
    rows = saas_fetchall(
        f"""SELECT d.{id_col} as doc_id, d.{number_col} as doc_number, d.{date_col} as doc_date,
                   d.{party_col} as party_name, ({gst_expr}) as gst_amt
            FROM {table} d
            WHERE d.business_id={p} AND ({gst_expr}) > 0.01
              AND NOT EXISTS (
                SELECT 1 FROM saas_journal_entries je
                WHERE je.business_id = d.business_id AND je.source_id = d.{id_col}
                  AND je.source_type IN ({placeholders}) AND je.status = 'posted'
              )
            ORDER BY d.{date_col} DESC""",
        (biz_id, *source_types)
    )
    issues = []
    for r in rows:
        issues.append(_issue("error", "Missing Ledger Posting",
            f"{doc_type} {r['doc_number']} (₹{float(r['gst_amt']):,.2f} GST) has no matching ledger entry.",
            "This document's GST was never posted to the ledger — most likely a save that completed the "
            "document but failed before or during its ledger posting. Contact support for a ledger repair "
            "rather than re-saving the document (which would create a duplicate).",
            doc_type=doc_type.lower().replace(" ", "_"), doc_id=r["doc_id"],
            doc_number=r["doc_number"], doc_date=r["doc_date"], party_name=r["party_name"],
            difference=float(r["gst_amt"])))
    return issues


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
        f"SELECT id, name, sku, stock_quantity FROM saas_products WHERE business_id={P()} AND stock_quantity < 0",
        (biz_id,)
    )
    return [_issue("warning", "Inventory",
        f"'{r['name']}' ({r['sku'] or 'no SKU'}) has negative stock: {r['stock_quantity']:g}.",
        "Record any missing purchase entries for this product, or adjust stock via Products → Edit.",
        doc_type="product", doc_id=r["id"], party_name=r["name"]
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
                    f"Summary reports can classify it correctly.",
                    doc_type="hsn_search", hsn_code=code))
            elif not known_map[code]:
                issues.append(_issue("warning", "HSN",
                    f"{label} documents use HSN/SAC '{code}', which is marked inactive in the HSN master.",
                    f"Reactivate '{code}' if it's still valid, or update the affected line items to a current code.",
                    doc_type="hsn_search", hsn_code=code))
    return issues


def _check_gstin_validity(biz_id: int) -> list:
    """4. GSTIN validity — every customer/supplier GSTIN on file should
    at least be structurally well-formed (see utils/gst_validation.py;
    this doesn't verify live registration status with the department)."""
    from utils.gst_validation import validate_gstin
    issues = []
    p = P()
    customers = saas_fetchall(
        f"SELECT id, name, gstin FROM saas_customers WHERE business_id={p} AND gstin IS NOT NULL AND gstin != ''",
        (biz_id,)
    )
    for c in customers:
        err = validate_gstin(c["gstin"])
        if err:
            issues.append(_issue("warning", "GSTIN",
                f"Customer '{c['name']}': {err}",
                "Correct the GSTIN on the customer's profile.",
                doc_type="customer", doc_id=c["id"], party_name=c["name"]))
    suppliers = saas_fetchall(
        f"SELECT id, name, gstin FROM saas_suppliers WHERE business_id={p} AND gstin IS NOT NULL AND gstin != ''",
        (biz_id,)
    )
    for s in suppliers:
        err = validate_gstin(s["gstin"])
        if err:
            issues.append(_issue("warning", "GSTIN",
                f"Supplier '{s['name']}': {err}",
                "Correct the GSTIN on the supplier's profile.",
                doc_type="supplier", doc_id=s["id"], party_name=s["name"]))
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
    """
    6+7. Input GST (ITC) / Output GST — Update_033: drills down to the
    SPECIFIC invoice/purchase/credit-note/debit-note causing a mismatch
    (via _find_documents_missing_ledger_posting(), matched by the same
    source_type+source_id linkage utils/ledger_transactions.py already
    writes) rather than only reporting an aggregate difference. After
    listing every specific offender, a final aggregate check still runs
    as a safety net — if a residual mismatch remains even after
    accounting for every individually-flagged document, something
    other than "a whole document's posting is missing" is wrong (e.g. a
    partial/corrupted posting), which the per-document check alone
    can't detect but is still worth surfacing.
    """
    issues = []
    p = P()

    # ── Output GST: Invoices + Credit Notes ──
    issues += _find_documents_missing_ledger_posting(
        biz_id, "saas_invoices", "id", "invoice_number", "created_at", "customer_name",
        ["cash_sale", "credit_sale"], "cgst_amount+sgst_amount+igst_amount", "Invoice")
    issues += _find_documents_missing_ledger_posting(
        biz_id, "saas_credit_notes", "id", "credit_note_number", "created_at", "customer_name",
        ["sales_return"], "cgst_amount+sgst_amount+igst_amount", "Credit Note")

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
    output_gst_issue_count_so_far = sum(1 for i in issues if i["category"] == "Missing Ledger Posting"
                                        and i.get("doc_type") in ("invoice", "credit_note"))
    if abs(net_output_expected - net_output_actual) > 0.02 and output_gst_issue_count_so_far == 0:
        # Only surfaced when NO specific document was already flagged
        # above — otherwise this would just be restating the same
        # problem in a less actionable way.
        issues.append(_issue("error", "Output GST",
            f"Output GST per documents (₹{net_output_expected:,.2f}, net of Credit Notes) doesn't match "
            f"the Output GST ledger balance (₹{net_output_actual:,.2f}), but no single missing document "
            f"was found — the mismatch may be a partial/corrupted posting rather than a whole missing one.",
            "Contact support for a manual ledger reconciliation."))

    # ── Input GST (ITC): Purchases + Debit Notes ──
    issues += _find_documents_missing_ledger_posting(
        biz_id, "saas_purchases", "id", "purchase_number", "created_at", "supplier_name",
        ["cash_purchase", "credit_purchase"], "cgst_amount+sgst_amount+igst_amount", "Purchase")
    issues += _find_documents_missing_ledger_posting(
        biz_id, "saas_debit_notes", "id", "debit_note_number", "created_at", "supplier_name",
        ["purchase_return"], "cgst_amount+sgst_amount+igst_amount", "Debit Note")

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
    itc_issue_count_so_far = sum(1 for i in issues if i["category"] == "Missing Ledger Posting"
                                 and i.get("doc_type") in ("purchase", "debit_note"))
    if abs(net_itc_expected - net_itc_actual) > 0.02 and itc_issue_count_so_far == 0:
        issues.append(_issue("error", "Input GST (ITC)",
            f"Input GST/ITC per documents (₹{net_itc_expected:,.2f}, net of Debit Notes) doesn't match "
            f"the ITC ledger balance (₹{net_itc_actual:,.2f}), but no single missing document was found — "
            f"the mismatch may be a partial/corrupted posting rather than a whole missing one.",
            "Contact support for a manual ledger reconciliation."))

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

# Update_033: maps each issue's `category` (set by the check that raised
# it) to one of the six named sub-scores the spec calls out. Several
# issue categories can feed the same bucket — e.g. a missing ledger
# posting and an Output GST mismatch are both fundamentally "GST
# Compliance" problems, even though they're reported with different
# `category` labels for readability in the issue list itself.
SCORE_BUCKETS = {
    "GST Compliance":     ["Missing Ledger Posting", "Output GST", "Input GST (ITC)", "Inventory"],
    "Ledger Integrity":   ["Ledger"],
    "HSN Coverage":       ["HSN"],
    "GSTIN Validation":   ["GSTIN"],
    "Document Numbering": ["Numbering"],
    "Return Integrity":   ["Returns"],
}


def _score_for(issues: list) -> int:
    errors = sum(1 for i in issues if i["severity"] == "error")
    warnings = sum(1 for i in issues if i["severity"] == "warning")
    return max(0, 100 - errors * 10 - warnings * 3)


def run_health_check(biz_id: int) -> dict:
    """
    Runs every check and returns:
        {"score": int, "issues": [...], "error_count": int, "warning_count": int,
         "category_scores": {bucket_name: {"score": int, "issue_count": int}}}
    `issues` is sorted errors-first (most actionable/severe first).
    `category_scores` gives the six named sub-scores from the spec —
    each computed with the exact same formula as the overall score,
    scoped to just that bucket's own issues, so an admin can see at a
    glance which area needs attention rather than only an opaque
    overall number.
    """
    issues = []
    for check in CHECKS:
        issues.extend(check(biz_id))

    error_count = sum(1 for i in issues if i["severity"] == "error")
    warning_count = sum(1 for i in issues if i["severity"] == "warning")
    score = _score_for(issues)

    issues.sort(key=lambda i: 0 if i["severity"] == "error" else 1)

    category_scores = {}
    for bucket, categories in SCORE_BUCKETS.items():
        bucket_issues = [i for i in issues if i["category"] in categories]
        category_scores[bucket] = {"score": _score_for(bucket_issues), "issue_count": len(bucket_issues)}

    return {
        "score": score,
        "issues": issues,
        "error_count": error_count,
        "warning_count": warning_count,
        "is_clean": not issues,
        "category_scores": category_scores,
    }
