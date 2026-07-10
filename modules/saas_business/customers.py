"""
modules/saas_business/customers.py — SaaS-Native Customer Management
========================================================================
Tenant-scoped customer CRUD for the SaaS multi-tenant system.
Mirrors the legacy modules/customers.py feature set, but every query
is scoped by business_id (from the SaaS session) instead of shop_id,
and reads/writes saas_customers / saas_invoices instead of the legacy
customers / invoices tables.

Permissions (via utils.saas_middleware):
  view_customers    → staff and above (everyone can view)
  manage_customers  → manager and above (add/edit/delete)
"""

import io
import csv
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from models.saas_auth import fmt_dt
from config import ActiveConfig

saas_customers_bp = Blueprint("saas_customers", __name__, url_prefix="/biz/customers")

P = lambda: "%s" if _is_postgres() else "?"


# ════════════════════════════════ LIST ════════════════════════════════════════

@saas_customers_bp.route("/")
@saas_business_required
@permission_required("view_customers")
def index():
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    sql = f"""SELECT c.*,
                     COUNT(i.id) as inv_cnt,
                     COALESCE(SUM(CASE WHEN i.status='paid' THEN i.total ELSE 0 END), 0) as total_spent
              FROM saas_customers c
              LEFT JOIN saas_invoices i ON i.customer_id = c.id AND i.business_id = {p}
              WHERE c.business_id = {p}"""
    args = [biz_id, biz_id]

    if q:
        sql += (f" AND (LOWER(c.name) LIKE {p} OR LOWER(c.phone) LIKE {p} "
                f"OR LOWER(c.email) LIKE {p} OR LOWER(c.gstin) LIKE {p})")
        args += [f"%{q.lower()}%"] * 4

    sql += " GROUP BY c.id ORDER BY c.name"

    customers = saas_fetchall(sql, tuple(args))

    return render_template("saas_business/customers/list.html",
                           customers=customers, q=q)


# ════════════════════════════════ ADD ═════════════════════════════════════════

@saas_customers_bp.route("/add", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_customers")
def add():
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_customers.add"))

        d = _form()
        if not d["name"]:
            flash("Customer name is required.", "danger")
            return render_template("saas_business/customers/add_edit.html",
                                   customer=d, action="Add", states=states)

        cust_id = saas_execute(
            f"""INSERT INTO saas_customers
                (business_id, name, phone, email, address, state_code, gstin)
                VALUES ({p},{p},{p},{p},{p},{p},{p})""",
            (biz_id, d["name"], d["phone"], d["email"],
             d["address"], d["state_code"], d["gstin"])
        )
        audit_log("customer_created", business_id=biz_id,
                  entity_type="customer", entity_id=str(cust_id),
                  detail=f"name={d['name']}")
        flash(f"Customer '{d['name']}' added.", "success")
        return redirect(url_for("saas_customers.index"))

    return render_template("saas_business/customers/add_edit.html",
                           customer={}, action="Add", states=states)


# ════════════════════════════════ EDIT ════════════════════════════════════════

@saas_customers_bp.route("/edit/<int:cid>", methods=["GET", "POST"])
@saas_business_required
@permission_required("manage_customers")
def edit(cid):
    biz_id = get_tenant_id()
    states = ActiveConfig.INDIAN_STATES
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    assert_tenant_access(customer["business_id"])

    if request.method == "POST":
        if not validate_csrf(request.form.get("csrf_token")):
            flash("Security error. Please try again.", "danger")
            return redirect(url_for("saas_customers.edit", cid=cid))

        d = _form()
        if not d["name"]:
            flash("Customer name is required.", "danger")
            return render_template("saas_business/customers/add_edit.html",
                                   customer=customer, action="Edit", states=states)

        saas_execute(
            f"""UPDATE saas_customers SET
                name={p}, phone={p}, email={p}, address={p}, state_code={p}, gstin={p}
                WHERE id={p} AND business_id={p}""",
            (d["name"], d["phone"], d["email"], d["address"],
             d["state_code"], d["gstin"], cid, biz_id)
        )
        audit_log("customer_updated", business_id=biz_id,
                  entity_type="customer", entity_id=str(cid))
        flash("Customer updated.", "success")
        return redirect(url_for("saas_customers.index"))

    return render_template("saas_business/customers/add_edit.html",
                           customer=customer, action="Edit", states=states)


# ════════════════════════════════ DELETE ══════════════════════════════════════

@saas_customers_bp.route("/delete/<int:cid>", methods=["POST"])
@saas_business_required
@permission_required("manage_customers")
def delete(cid):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_customers.index"))

    biz_id = get_tenant_id()
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    try:
        # saas_invoices.customer_id / saas_payments.customer_id reference
        # this row WITHOUT ON DELETE CASCADE — deleting a customer who has
        # any invoice or payment history would otherwise crash with a
        # foreign-key violation. Null the reference instead: each invoice
        # already has its own denormalized customer_name column, so
        # invoice history stays fully readable even after the customer
        # record itself is gone — it just becomes unlinked.
        saas_execute(f"UPDATE saas_invoices SET customer_id=NULL WHERE customer_id={p}", (cid,))
        saas_execute(f"UPDATE saas_payments SET customer_id=NULL WHERE customer_id={p}", (cid,))
        saas_execute(
            f"DELETE FROM saas_customers WHERE id={p} AND business_id={p}",
            (cid, biz_id)
        )
    except Exception as e:
        audit_log("customer_delete_failed", business_id=biz_id,
                  entity_type="customer", entity_id=str(cid), detail=str(e))
        flash("Could not delete this customer due to a database error. "
              "Please try again or contact support if this persists.", "danger")
        return redirect(url_for("saas_customers.history", cid=cid))

    audit_log("customer_deleted", business_id=biz_id,
              entity_type="customer", entity_id=str(cid),
              detail=f"name={customer['name']}")
    flash("Customer deleted.", "success")
    return redirect(url_for("saas_customers.index"))


# ════════════════════════════════ HISTORY ═════════════════════════════════════

@saas_customers_bp.route("/<int:cid>/history")
@saas_business_required
@permission_required("view_customers")
def history(cid):
    biz_id = get_tenant_id()
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    assert_tenant_access(customer["business_id"])

    invoices = saas_fetchall(
        f"""SELECT * FROM saas_invoices
            WHERE customer_id={p} AND business_id={p}
            ORDER BY created_at DESC""",
        (cid, biz_id)
    )

    stats_row = saas_fetchone(
        f"""SELECT COUNT(*) as cnt,
                   COALESCE(SUM(CASE WHEN status='paid' THEN total ELSE 0 END), 0) as total,
                   COALESCE(AVG(CASE WHEN status='paid' THEN total END), 0) as avg,
                   COALESCE(SUM(due_amount), 0) as outstanding,
                   COALESCE(SUM(paid_amount), 0) as total_paid
            FROM saas_invoices
            WHERE customer_id={p} AND business_id={p} AND status!='cancelled'""",
        (cid, biz_id)
    )

    # Monthly sales/payments for the chart — one aggregate query (not one
    # per month, and not one per invoice) so this stays fast regardless
    # of invoice volume. Same cross-backend month-grouping pattern
    # already used elsewhere in the app (TO_CHAR on Postgres, strftime
    # on SQLite).
    month_expr = "TO_CHAR(created_at, 'YYYY-MM')" if _is_postgres() else "strftime('%Y-%m', created_at)"
    monthly = saas_fetchall(
        f"""SELECT {month_expr} as month,
                   COALESCE(SUM(total), 0) as sales,
                   COALESCE(SUM(paid_amount), 0) as payments
            FROM saas_invoices
            WHERE customer_id={p} AND business_id={p} AND status!='cancelled'
            GROUP BY {month_expr}
            ORDER BY month DESC LIMIT 6""",
        (cid, biz_id)
    )
    monthly = list(reversed(monthly))  # chronological order for the chart

    return render_template("saas_business/customers/history.html",
                           customer=customer,
                           invoices=invoices,
                           monthly=monthly,
                           stats=stats_row or {"cnt": 0, "total": 0, "avg": 0,
                                                "outstanding": 0, "total_paid": 0})


@saas_customers_bp.route("/<int:cid>/export")
@saas_business_required
@permission_required("view_customers")
def export_csv(cid):
    """CSV export of a customer's full invoice history."""
    biz_id = get_tenant_id()
    p = P()

    customer = saas_fetchone(
        f"SELECT * FROM saas_customers WHERE id={p} AND business_id={p}",
        (cid, biz_id)
    )
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("saas_customers.index"))

    invoices = saas_fetchall(
        f"""SELECT invoice_number, created_at, total, total_tax, paid_amount,
                   due_amount, payment_method, status
            FROM saas_invoices
            WHERE customer_id={p} AND business_id={p}
            ORDER BY created_at DESC""",
        (cid, biz_id)
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Invoice #", "Date", "Total", "Tax", "Paid", "Due", "Payment Method", "Status"])
    for inv in invoices:
        writer.writerow([
            inv["invoice_number"], fmt_dt(inv["created_at"], 16),
            inv["total"], inv["total_tax"], inv["paid_amount"],
            inv["due_amount"], inv["payment_method"], inv["status"]
        ])

    filename = f"{customer['name'].replace(' ', '_')}_statement.csv"
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ════════════════════════════════ API SEARCH ══════════════════════════════════

@saas_customers_bp.route("/api/search")
@saas_business_required
@permission_required("view_customers")
def api_search():
    """
    Search customers by name, mobile, email, or GSTIN — partial match,
    case-insensitive on both SQLite and PostgreSQL.

    LIKE is case-insensitive by default on SQLite (for ASCII) but
    case-SENSITIVE on PostgreSQL — searching "sharma" would silently
    miss a customer named "Sharma" in production while working fine in
    development. Wrapping both sides in LOWER() makes the match
    identical on both engines instead of relying on ILIKE (Postgres-only,
    not portable to SQLite).
    """
    biz_id = get_tenant_id()
    q = request.args.get("q", "").strip()
    p = P()

    if not q:
        return jsonify([])

    like = f"%{q.lower()}%"
    rows = saas_fetchall(
        f"""SELECT id, name, phone, email, state_code, gstin
            FROM saas_customers
            WHERE business_id={p} AND (
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
    return {
        "name":       f("name", "").strip(),
        "phone":      f("phone", "").strip(),
        "email":      f("email", "").strip(),
        "address":    f("address", "").strip(),
        "state_code": f("state_code", "").strip(),
        "gstin":      f("gstin", "").strip(),
    }
