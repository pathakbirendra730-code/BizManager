"""
modules/saas_business/suppliers.py — SaaS-Native Supplier Management
========================================================================
Tenant-scoped supplier CRUD + ledger/payment tracking for the SaaS
multi-tenant system. Mirrors legacy modules/supplier.py, but every
query is scoped by business_id and reads/writes saas_suppliers /
saas_purchases / saas_ledger / saas_cash_book.

Permissions (via utils.saas_middleware):
  view_supplier    → manager and above (staff never sees supplier data)
  manage_supplier  → manager and above
"""

import io
import csv
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, Response
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from models.saas_auth import fmt_dt
from config import ActiveConfig

saas_suppliers_bp = Blueprint("saas_suppliers", __name__, url_prefix="/biz/suppliers")

P = lambda: "%s" if _is_postgres() else "?"


# ════════════════════════════════ LIST ════════════════════════════════════════

@saas_suppliers_bp.route("/")
@saas_business_required
@permission_required("view_supplier")
def index():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "active")
    p = P()

    sql = f"""SELECT s.*,
                     COUNT(DISTINCT pu.id) as purchase_count,
                     COALESCE(SUM(CASE WHEN pu.status!='cancelled' THEN pu.total ELSE 0 END), 0) as total_purchased,
                     COALESCE(SUM(CASE WHEN pu.status!='cancelled' THEN pu.due_amount ELSE 0 END), 0) as total_due
              FROM saas_suppliers s
              LEFT JOIN saas_purchases pu ON pu.supplier_id = s.id AND pu.business_id = {p}
              WHERE s.business_id = {p}"""
    args = [biz_id, biz_id]

    if show == "active":
        sql += " AND s.is_active=TRUE"
    if q:
        sql += f" AND (LOWER(s.name) LIKE {p} OR LOWER(s.phone) LIKE {p} OR LOWER(s.email) LIKE {p} OR LOWER(s.gstin) LIKE {p})"
        args += [f"%{q.lower()}%"] * 4
    sql += " GROUP BY s.id ORDER BY s.name"

    suppliers = saas_fetchall(sql, tuple(args))

    summary = saas_fetchone(
        f"""SELECT COUNT(*) as total, COALESCE(SUM(balance), 0) as total_payable
            FROM saas_suppliers WHERE business_id={p} AND is_active=TRUE""",
        (biz_id,)
    )

    return render_template("saas_business/suppliers/list.html",
                           suppliers=suppliers,
                           summary=summary or {"total": 0, "total_payable": 0},
                           q=q, show=show)


# ════════════════════════════════ ADD ═════════════════════════════════════════

@saas_suppliers_bp.route("/add", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_supplier")
def add():
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_suppliers.add"))

        d = _form()
        if not d["name"]:
            flash("Supplier name is required.", "danger")
            return render_template("saas_business/suppliers/add_edit.html",
                                   supplier=d, action="Add", states=states)

        sup_id = saas_execute(
            f"""INSERT INTO saas_suppliers
                (business_id, name, phone, email, address, gstin, state_code,
                 opening_balance, balance)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, d["name"], d["phone"], d["email"], d["address"],
             d["gstin"], d["state_code"], d["opening_balance"], d["opening_balance"])
        )
        audit_log("supplier_created", business_id=biz_id,
                  entity_type="supplier", entity_id=str(sup_id), detail=f"name={d['name']}")
        flash(f"Supplier '{d['name']}' added.", "success")
        return redirect(url_for("saas_suppliers.index"))

    return render_template("saas_business/suppliers/add_edit.html",
                           supplier={}, action="Add", states=states)


# ════════════════════════════════ EDIT ════════════════════════════════════════

@saas_suppliers_bp.route("/edit/<int:sid>", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_supplier")
def edit(sid):
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    supplier = saas_fetchone(
        f"SELECT * FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
    )
    if not supplier:
        flash("Supplier not found.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    assert_tenant_access(supplier["business_id"])

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_suppliers.edit", sid=sid))

        d = _form()
        if not d["name"]:
            flash("Supplier name is required.", "danger")
            return render_template("saas_business/suppliers/add_edit.html",
                                   supplier=supplier, action="Edit", states=states)

        saas_execute(
            f"""UPDATE saas_suppliers SET
                name={p}, phone={p}, email={p}, address={p}, gstin={p}, state_code={p}
                WHERE id={p} AND business_id={p}""",
            (d["name"], d["phone"], d["email"], d["address"],
             d["gstin"], d["state_code"], sid, biz_id)
        )
        audit_log("supplier_updated", business_id=biz_id,
                  entity_type="supplier", entity_id=str(sid))
        flash("Supplier updated.", "success")
        return redirect(url_for("saas_suppliers.index"))

    return render_template("saas_business/suppliers/add_edit.html",
                           supplier=supplier, action="Edit", states=states)


# ════════════════════════════════ DELETE / DEACTIVATE ═════════════════════════

@saas_suppliers_bp.route("/delete/<int:sid>", methods=["POST"])
@saas_business_required
@permission_required("manage_supplier")
def delete(sid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    biz_id = get_tenant_id()
    p = P()

    supplier = saas_fetchone(
        f"SELECT * FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
    )
    if not supplier:
        flash("Supplier not found.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    has_purchases = saas_fetchone(
        f"SELECT COUNT(*) as c FROM saas_purchases WHERE supplier_id={p} AND business_id={p}",
        (sid, biz_id)
    )["c"]

    if has_purchases:
        saas_execute(
            f"UPDATE saas_suppliers SET is_active=FALSE WHERE id={p} AND business_id={p}",
            (sid, biz_id)
        )
        audit_log("supplier_deactivated", business_id=biz_id,
                  entity_type="supplier", entity_id=str(sid), detail=f"name={supplier['name']}")
        flash("Supplier deactivated (has purchase history).", "warning")
    else:
        saas_execute(
            f"DELETE FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
        )
        audit_log("supplier_deleted", business_id=biz_id,
                  entity_type="supplier", entity_id=str(sid), detail=f"name={supplier['name']}")
        flash("Supplier deleted.", "success")

    return redirect(url_for("saas_suppliers.index"))


# ════════════════════════════════ LEDGER ══════════════════════════════════════

@saas_suppliers_bp.route("/<int:sid>/ledger")
@saas_business_required
@permission_required("view_supplier")
def ledger(sid):
    biz_id = get_tenant_id()
    p = P()

    supplier = saas_fetchone(
        f"SELECT * FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
    )
    if not supplier:
        flash("Supplier not found.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    assert_tenant_access(supplier["business_id"])

    purchases = saas_fetchall(
        f"""SELECT id, purchase_number, bill_number, bill_date, total,
                   paid_amount, due_amount, status, payment_method, created_at
            FROM saas_purchases
            WHERE supplier_id={p} AND business_id={p}
            ORDER BY created_at DESC""",
        (sid, biz_id)
    )

    stats = saas_fetchone(
        f"""SELECT COUNT(*) as cnt,
                   COALESCE(SUM(total), 0) as total_purchased,
                   COALESCE(SUM(paid_amount), 0) as total_paid,
                   COALESCE(SUM(due_amount), 0) as total_due
            FROM saas_purchases
            WHERE supplier_id={p} AND business_id={p} AND status!='cancelled'""",
        (sid, biz_id)
    )

    # One aggregate query for the monthly chart — not one query per month
    # or per purchase, so this stays responsive at any purchase volume.
    month_expr = "TO_CHAR(created_at, 'YYYY-MM')" if _is_postgres() else "strftime('%Y-%m', created_at)"
    monthly = saas_fetchall(
        f"""SELECT {month_expr} as month,
                   COALESCE(SUM(total), 0) as purchases,
                   COALESCE(SUM(paid_amount), 0) as payments
            FROM saas_purchases
            WHERE supplier_id={p} AND business_id={p} AND status!='cancelled'
            GROUP BY {month_expr}
            ORDER BY month DESC LIMIT 6""",
        (sid, biz_id)
    )
    monthly = list(reversed(monthly))

    return render_template("saas_business/suppliers/ledger.html",
                           supplier=supplier,
                           purchases=purchases,
                           monthly=monthly,
                           stats=stats or {"cnt": 0, "total_purchased": 0,
                                            "total_paid": 0, "total_due": 0})


@saas_suppliers_bp.route("/<int:sid>/export")
@saas_business_required
@permission_required("view_supplier")
def export_csv(sid):
    """CSV export of a supplier's full purchase history."""
    biz_id = get_tenant_id()
    p = P()

    supplier = saas_fetchone(
        f"SELECT * FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
    )
    if not supplier:
        flash("Supplier not found.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    purchases = saas_fetchall(
        f"""SELECT purchase_number, created_at, total, paid_amount, due_amount,
                   payment_method, status
            FROM saas_purchases
            WHERE supplier_id={p} AND business_id={p}
            ORDER BY created_at DESC""",
        (sid, biz_id)
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Purchase #", "Date", "Total", "Paid", "Due", "Payment Method", "Status"])
    for pur in purchases:
        writer.writerow([
            pur["purchase_number"], fmt_dt(pur["created_at"], 16),
            pur["total"], pur["paid_amount"], pur["due_amount"],
            pur["payment_method"], pur["status"]
        ])

    filename = f"{supplier['name'].replace(' ', '_')}_statement.csv"
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ════════════════════════════════ RECORD PAYMENT ══════════════════════════════

@saas_suppliers_bp.route("/<int:sid>/pay", methods=["POST"])
@saas_business_required
@permission_required("manage_supplier")
def record_payment(sid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_suppliers.ledger", sid=sid))

    biz_id = get_tenant_id()
    p = P()

    supplier = saas_fetchone(
        f"SELECT * FROM saas_suppliers WHERE id={p} AND business_id={p}", (sid, biz_id)
    )
    if not supplier:
        flash("Supplier not found.", "danger")
        return redirect(url_for("saas_suppliers.index"))

    try:
        amount = float(request.form.get("amount", 0) or 0)
    except ValueError:
        amount = 0

    method      = request.form.get("payment_method", "Cash")
    purchase_id = request.form.get("purchase_id", "")
    notes       = request.form.get("notes", "")

    if amount <= 0:
        flash("Enter a valid payment amount.", "danger")
        return redirect(url_for("saas_suppliers.ledger", sid=sid))

    # Update specific purchase if given — tenant-scoped subquery prevents
    # this UPDATE from ever touching another business's purchase row.
    if purchase_id:
        saas_execute(
            f"""UPDATE saas_purchases SET
                paid_amount = CASE WHEN (paid_amount + {p}) > total THEN total ELSE paid_amount + {p} END,
                due_amount  = CASE WHEN (due_amount - {p}) < 0 THEN 0 ELSE due_amount - {p} END,
                status = CASE WHEN (due_amount - {p}) <= 0 THEN 'received' ELSE status END
                WHERE id={p} AND business_id={p}""",
            (amount, amount, amount, amount, amount, purchase_id, biz_id)
        )

    # Update supplier running balance (never below zero)
    saas_execute(
        f"""UPDATE saas_suppliers SET
            balance = CASE WHEN (balance - {p}) < 0 THEN 0 ELSE balance - {p} END
            WHERE id={p} AND business_id={p}""",
        (amount, amount, sid, biz_id)
    )

    today = datetime.utcnow().date().isoformat()

    # Post through the double-entry accounting engine — reduces Cash/Bank
    # and reduces the supplier's payable balance via a proper journal
    # entry, so it shows up correctly in the ledger, cash/bank book, and
    # trial balance, with reversal support if it ever needs correcting.
    from utils.ledger_transactions import record_payment_to_supplier
    from utils.ledger_service import InvalidLineError

    try:
        record_payment_to_supplier(
            biz_id, amount,
            supplier_id=sid, supplier_name=supplier["name"],
            payment_method=method.lower(), source_id=int(purchase_id) if purchase_id else None,
            narration=f"Payment to {supplier['name']}: {notes}" if notes else f"Payment to {supplier['name']}",
            entry_date=today, created_by=session.get("saas_user_id")
        )
    except (InvalidLineError, LookupError) as e:
        # The payment amount / supplier balance were already updated above —
        # deliberately not rolled back, since partial cash-collection state
        # is closer to the truth than silently losing the payment record.
        # Surface the accounting-engine failure clearly instead.
        flash(f"Payment recorded, but could not post to the ledger: {e}", "warning")
        return redirect(url_for("saas_suppliers.ledger", sid=sid))

    audit_log("supplier_payment_recorded", business_id=biz_id,
              entity_type="supplier", entity_id=str(sid),
              detail=f"amount={amount} method={method}")

    flash(f"₹{amount:,.2f} payment recorded.", "success")
    return redirect(url_for("saas_suppliers.ledger", sid=sid))


# ════════════════════════════════ API SEARCH ══════════════════════════════════

@saas_suppliers_bp.route("/api/search")
@saas_business_required
@permission_required("view_supplier")
def api_search():
    """Search suppliers by name, mobile, email, or GSTIN — partial match,
    case-insensitive on both SQLite and PostgreSQL. See the matching
    docstring in customers.py:api_search() for why LOWER() is used
    instead of LIKE/ILIKE directly."""
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    if not q:
        return jsonify([])

    like = f"%{q.lower()}%"
    rows = saas_fetchall(
        f"""SELECT id, name, phone, email, gstin, state_code, balance
            FROM saas_suppliers
            WHERE business_id={p} AND is_active=TRUE
              AND (
                LOWER(name)  LIKE {p} OR
                LOWER(phone) LIKE {p} OR
                LOWER(email) LIKE {p} OR
                LOWER(gstin) LIKE {p}
              )
            ORDER BY name LIMIT 10""",
        (biz_id, like, like, like, like)
    )
    return jsonify(rows)


# ════════════════════════════════ HELPERS ═════════════════════════════════════

def _form():
    f = request.form.get
    try:
        opening_balance = float(f("opening_balance", 0) or 0)
    except ValueError:
        opening_balance = 0.0

    return {
        "name":            f("name", "").strip(),
        "phone":           f("phone", "").strip(),
        "email":           f("email", "").strip(),
        "address":         f("address", "").strip(),
        "gstin":           f("gstin", "").strip(),
        "state_code":      f("state_code", "").strip(),
        "opening_balance": opening_balance,
    }
