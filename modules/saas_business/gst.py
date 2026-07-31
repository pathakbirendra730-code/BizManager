"""
modules/saas_business/gst.py — SaaS-Native GST Reports
============================================================
Tenant-scoped GST compliance reporting for the SaaS multi-tenant
system. Mirrors legacy modules/gst.py's reporting routes, but every
query is scoped by business_id and reads from saas_invoices /
saas_invoice_items.

Deliberately NOT ported:
  • HSN master CRUD (hsn_list, hsn_add, hsn_delete) — hsn_master is
    global reference data shared across the whole platform, not
    tenant-scoped business data. Read-only HSN lookup for product
    forms already exists via modules/saas_business/products.py's
    api_hsn / api_hsn_code routes (built in an earlier phase).
    Adding/editing the shared HSN master is an app-admin concern,
    not a per-business one.

Permissions: view_gst / manage_gst → accountant and above.
"""

import io
import csv
from datetime import datetime
from flask import Blueprint, render_template, request, Response
from models.saas_auth import saas_fetchone, saas_fetchall, _is_postgres
from utils.saas_helpers import saas_business_required
from utils.saas_middleware import permission_required, get_tenant_id

saas_gst_bp = Blueprint("saas_gst", __name__, url_prefix="/biz/gst")

P = lambda: "%s" if _is_postgres() else "?"


def _month_filter_clause(col: str) -> str:
    """Returns the correct SQL fragment for 'YYYY-MM' extraction per DB backend."""
    if _is_postgres():
        return f"TO_CHAR({col}, 'YYYY-MM')"
    return f"strftime('%Y-%m', {col})"


@saas_gst_bp.route("/")
@saas_business_required
@permission_required("view_gst")
def index():
    biz_id = get_tenant_id()
    p = P()
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/gst/index.html", biz=biz)


# ════════════════════════════════ MONTHLY SUMMARY ══════════════════════════════

@saas_gst_bp.route("/monthly")
@saas_business_required
@permission_required("view_gst")
def monthly_summary():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    totals = saas_fetchone(
        f"""SELECT COUNT(*) as invoice_count,
                   COALESCE(SUM(subtotal),0) as subtotal,
                   COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst,
                   COALESCE(SUM(total_tax),0) as total_tax,
                   COALESCE(SUM(total),0) as grand_total
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')""",
        (biz_id, month)
    )

    mf_items = _month_filter_clause("i.created_at")
    slabs = saas_fetchall(
        f"""SELECT ii.gst_rate,
                   COUNT(DISTINCT i.id) as inv_count,
                   COALESCE(SUM(ii.taxable_amount),0) as taxable,
                   COALESCE(SUM(ii.cgst_amount),0) as cgst,
                   COALESCE(SUM(ii.sgst_amount),0) as sgst,
                   COALESCE(SUM(ii.igst_amount),0) as igst
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            WHERE ii.business_id={p} AND {mf_items}={p} AND i.status IN ('paid','partial')
            GROUP BY ii.gst_rate ORDER BY ii.gst_rate""",
        (biz_id, month)
    )

    supply_split = saas_fetchall(
        f"""SELECT supply_type, COUNT(*) as cnt, COALESCE(SUM(total),0) as total
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')
            GROUP BY supply_type""",
        (biz_id, month)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/monthly.html",
                           biz=biz, month=month, totals=totals,
                           slabs=slabs, supply_split=supply_split)


# ════════════════════════════════ GSTR-1 ═══════════════════════════════════════

@saas_gst_bp.route("/gstr1")
@saas_business_required
@permission_required("view_gst")
def gstr1():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    b2b = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin,
                   customer_state, supply_type,
                   taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total,
                   DATE(created_at) as inv_date
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p}
              AND status IN ('paid','partial') AND customer_gstin != ''
            ORDER BY created_at""",
        (biz_id, month)
    )

    b2c = saas_fetchall(
        f"""SELECT invoice_number, customer_name,
                   taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total,
                   DATE(created_at) as inv_date
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p}
              AND status IN ('paid','partial') AND (customer_gstin = '' OR customer_gstin IS NULL)
            ORDER BY created_at""",
        (biz_id, month)
    )

    # ── Update_030: CDNR — Credit/Debit Notes Registered ──────────────────────
    # A real GSTR-1 has a dedicated section for credit/debit notes issued
    # in the period, reported against the period they were ISSUED in —
    # not retroactively folded into the original invoice's month, even if
    # the original sale happened earlier. This matches actual GST filing
    # practice (a return in month 2 for a sale in month 1 is reported as
    # a CDNR entry in month 2's GSTR-1, not a restatement of month 1's).
    cdnr = saas_fetchall(
        f"""SELECT credit_note_number, invoice_number, customer_name, customer_gstin,
                   supply_type, taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total, reason,
                   DATE(created_at) as note_date
            FROM saas_credit_notes
            WHERE business_id={p} AND {mf}={p}
            ORDER BY created_at""",
        (biz_id, month)
    )
    cdnr_total = round(sum(float(r["taxable_amount"]) for r in cdnr), 2)

    # ── Update_033: Nil Rated / Exempt / Non-GST supplies ─────────────────────
    # GSTR-1 Table 8 requires these reported separately from regular
    # taxable B2B/B2C supplies. Classification comes from
    # COALESCE(item's own tax_status override, its HSN's tax_status,
    # 'taxable') — the item-level `tax_status` column (Update_032) lets
    # a specific line override its HSN's default classification; when
    # it's NULL (the common case), the line simply inherits whatever
    # its HSN code is classified as in the National Master. A line with
    # no HSN code at all, or an HSN not in the master, defaults to
    # 'taxable' (matches its actual GST treatment on the invoice, which
    # already charged GST on it) rather than being silently dropped.
    nil_exempt_rows = saas_fetchall(
        f"""SELECT COALESCE(ii.tax_status, hm.tax_status, 'taxable') as tax_status,
                   SUM(ii.taxable_amount) as taxable_amount, COUNT(DISTINCT i.id) as doc_count
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            LEFT JOIN hsn_master hm ON hm.hsn_code = ii.hsn_code
            WHERE ii.business_id={p} AND {_month_filter_clause('i.created_at')}={p}
              AND i.status IN ('paid','partial')
            GROUP BY COALESCE(ii.tax_status, hm.tax_status, 'taxable')
            HAVING COALESCE(ii.tax_status, hm.tax_status, 'taxable') != 'taxable'""",
        (biz_id, month)
    )
    nil_exempt_total = round(sum(float(r["taxable_amount"] or 0) for r in nil_exempt_rows), 2)

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/gstr1.html",
                           biz=biz, month=month, b2b=b2b, b2c=b2c,
                           cdnr=cdnr, cdnr_total=cdnr_total,
                           nil_exempt_rows=nil_exempt_rows, nil_exempt_total=nil_exempt_total)


@saas_gst_bp.route("/gstr3b")
@saas_business_required
@permission_required("view_gst")
def gstr3b():
    """
    Update_030: GSTR-3B summary — outward tax liability (net of Credit
    Notes issued this period) and ITC available (net of Debit Notes
    issued this period), giving Net Tax Payable. Reads only already-
    computed, already-stored columns from saas_invoices/saas_purchases/
    saas_credit_notes/saas_debit_notes — no independent recalculation,
    so this can never disagree with what GSTR-1/HSN Summary or the
    ledger's Output GST / ITC accounts show for the same period.
    """
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    outward = saas_fetchone(
        f"""SELECT COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst
            FROM saas_invoices WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')""",
        (biz_id, month)
    )
    cdnr = saas_fetchone(
        f"""SELECT COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst
            FROM saas_credit_notes WHERE business_id={p} AND {mf}={p}""",
        (biz_id, month)
    )
    itc = saas_fetchone(
        f"""SELECT COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst
            FROM saas_purchases WHERE business_id={p} AND {mf}={p} AND status != 'cancelled'""",
        (biz_id, month)
    )
    dnr = saas_fetchone(
        f"""SELECT COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(cgst_amount),0) as cgst,
                   COALESCE(SUM(sgst_amount),0) as sgst,
                   COALESCE(SUM(igst_amount),0) as igst
            FROM saas_debit_notes WHERE business_id={p} AND {mf}={p}""",
        (biz_id, month)
    )

    net_outward = {
        "taxable": round(float(outward["taxable"]) - float(cdnr["taxable"]), 2),
        "cgst":    round(float(outward["cgst"])    - float(cdnr["cgst"]), 2),
        "sgst":    round(float(outward["sgst"])    - float(cdnr["sgst"]), 2),
        "igst":    round(float(outward["igst"])    - float(cdnr["igst"]), 2),
    }
    net_itc = {
        "taxable": round(float(itc["taxable"]) - float(dnr["taxable"]), 2),
        "cgst":    round(float(itc["cgst"])    - float(dnr["cgst"]), 2),
        "sgst":    round(float(itc["sgst"])    - float(dnr["sgst"]), 2),
        "igst":    round(float(itc["igst"])    - float(dnr["igst"]), 2),
    }
    net_payable = {
        "cgst": round(max(0.0, net_outward["cgst"] - net_itc["cgst"]), 2),
        "sgst": round(max(0.0, net_outward["sgst"] - net_itc["sgst"]), 2),
        "igst": round(max(0.0, net_outward["igst"] - net_itc["igst"]), 2),
    }
    net_payable["total"] = round(net_payable["cgst"] + net_payable["sgst"] + net_payable["igst"], 2)

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/gst/gstr3b.html",
                           biz=biz, month=month, outward=outward, cdnr=cdnr, itc=itc, dnr=dnr,
                           net_outward=net_outward, net_itc=net_itc, net_payable=net_payable)


# ════════════════════════════════ GST HEALTH CHECK ═════════════════════════════

@saas_gst_bp.route("/health-check")
@saas_business_required
@permission_required("view_gst")
def health_check():
    """
    Update_032: runs utils.gst_health.run_health_check() — Ledger,
    Inventory, HSN, GSTIN, numbering, Input/Output GST reconciliation,
    and Returns integrity — and shows a 0-100 score with a specific,
    actionable issue list. Meant to be run before generating a GSTR-1/
    GSTR-3B for filing.
    """
    biz_id = get_tenant_id()
    p = P()
    from utils.gst_health import run_health_check
    report = run_health_check(biz_id)
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/gst/health_check.html", biz=biz, report=report)


# ════════════════════════════════ HSN-WISE SUMMARY ═════════════════════════════

@saas_gst_bp.route("/hsn-summary")
@saas_business_required
@permission_required("view_gst")
def hsn_summary():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf_items = _month_filter_clause("i.created_at")
    mf_notes = _month_filter_clause("cn.created_at")

    # Update_030: nets Sales Return quantities/amounts out of the HSN-wise
    # totals via UNION ALL with credit_note_items (negated) — a returned
    # unit must not keep inflating the HSN Summary just because the
    # original invoice line itself is never edited. Netted within the
    # SAME reporting month a credit note was actually issued in (not
    # retroactively folded into the original sale's month), matching how
    # GSTR-1's CDNR section works — see gstr1() above for the same
    # principle.
    rows = saas_fetchall(
        f"""SELECT hsn_code, MAX(description) as description, gst_rate,
                   SUM(qty) as total_qty, SUM(taxable) as taxable,
                   SUM(cgst) as cgst, SUM(sgst) as sgst, SUM(igst) as igst,
                   SUM(cgst)+SUM(sgst)+SUM(igst) as total_tax
            FROM (
                SELECT ii.hsn_code as hsn_code, ii.product_name as description,
                       ii.gst_rate as gst_rate, ii.quantity as qty,
                       ii.taxable_amount as taxable, ii.cgst_amount as cgst,
                       ii.sgst_amount as sgst, ii.igst_amount as igst
                FROM saas_invoice_items ii
                JOIN saas_invoices i ON i.id = ii.invoice_id
                WHERE ii.business_id={p} AND {mf_items}={p} AND i.status IN ('paid','partial')
                  AND ii.hsn_code != ''
                UNION ALL
                SELECT cni.hsn_code, cni.product_name, cni.gst_rate,
                       -cni.quantity, -cni.taxable_amount, -cni.cgst_amount,
                       -cni.sgst_amount, -cni.igst_amount
                FROM saas_credit_note_items cni
                JOIN saas_credit_notes cn ON cn.id = cni.credit_note_id
                WHERE cni.business_id={p} AND {mf_notes}={p} AND cni.hsn_code != ''
            ) combined
            GROUP BY hsn_code, gst_rate
            ORDER BY taxable DESC""",
        (biz_id, month, biz_id, month)
    )

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/gst/hsn_summary.html",
                           biz=biz, month=month, rows=rows)


# ════════════════════════════════ CSV EXPORTS ══════════════════════════════════

@saas_gst_bp.route("/export/monthly")
@saas_business_required
@permission_required("view_gst")
def export_monthly():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf = _month_filter_clause("created_at")

    rows = saas_fetchall(
        f"""SELECT invoice_number, customer_name, customer_gstin,
                   DATE(created_at) as d, supply_type,
                   subtotal, taxable_amount, cgst_amount, sgst_amount,
                   igst_amount, total_tax, total, payment_method
            FROM saas_invoices
            WHERE business_id={p} AND {mf}={p} AND status IN ('paid','partial')
            ORDER BY created_at""",
        (biz_id, month)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Invoice No", "Customer", "GSTIN", "Date", "Supply Type",
                "Subtotal", "Taxable", "CGST", "SGST", "IGST", "Total Tax",
                "Grand Total", "Payment"])
    for r in rows:
        w.writerow([r["invoice_number"], r["customer_name"], r["customer_gstin"],
                    r["d"], r["supply_type"], r["subtotal"], r["taxable_amount"],
                    r["cgst_amount"], r["sgst_amount"], r["igst_amount"],
                    r["total_tax"], r["total"], r["payment_method"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=GST_{month}.csv"})


@saas_gst_bp.route("/export/hsn")
@saas_business_required
@permission_required("view_gst")
def export_hsn():
    biz_id = get_tenant_id()
    month  = request.args.get("month", datetime.now().strftime("%Y-%m"))
    p = P()
    mf_items = _month_filter_clause("i.created_at")
    mf_notes = _month_filter_clause("cn.created_at")

    # Update_030: same returns-netting UNION as hsn_summary() above, kept
    # in lockstep so the CSV export always matches the on-screen report.
    rows = saas_fetchall(
        f"""SELECT hsn_code, MAX(description) as description, gst_rate,
                   SUM(qty) as qty, SUM(taxable) as taxable,
                   SUM(cgst) as cgst, SUM(sgst) as sgst, SUM(igst) as igst
            FROM (
                SELECT ii.hsn_code as hsn_code, ii.product_name as description,
                       ii.gst_rate as gst_rate, ii.quantity as qty,
                       ii.taxable_amount as taxable, ii.cgst_amount as cgst,
                       ii.sgst_amount as sgst, ii.igst_amount as igst
                FROM saas_invoice_items ii
                JOIN saas_invoices i ON i.id = ii.invoice_id
                WHERE ii.business_id={p} AND {mf_items}={p} AND i.status IN ('paid','partial')
                UNION ALL
                SELECT cni.hsn_code, cni.product_name, cni.gst_rate,
                       -cni.quantity, -cni.taxable_amount, -cni.cgst_amount,
                       -cni.sgst_amount, -cni.igst_amount
                FROM saas_credit_note_items cni
                JOIN saas_credit_notes cn ON cn.id = cni.credit_note_id
                WHERE cni.business_id={p} AND {mf_notes}={p}
            ) combined
            GROUP BY hsn_code, gst_rate ORDER BY taxable DESC""",
        (biz_id, month, biz_id, month)
    )

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["HSN Code", "Description", "Total Qty", "GST%", "Taxable", "CGST", "SGST", "IGST"])
    for r in rows:
        w.writerow([r["hsn_code"], r["description"], r["qty"], r["gst_rate"],
                    r["taxable"], r["cgst"], r["sgst"], r["igst"]])

    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=HSN_Summary_{month}.csv"})
