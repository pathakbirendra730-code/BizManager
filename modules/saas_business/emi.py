"""
modules/saas_business/emi.py — EMI / Loan Calculator (Finance tool)
========================================================================
Tenant-scoped EMI calculator ported from a standalone client-side tool
(the actual EMI/amortization math is unchanged — it's pure interest-rate
arithmetic, not business logic). What this module adds on top of the
original is server-side persistence for "saved calculations", instead of
the original's browser localStorage.

Privacy note (deliberate, per product decision): saved calculations are
scoped by BOTH business_id AND user_id. Every query below filters on
both — one team member's saved EMI calculations are never visible to
another team member, including the business owner. This is the one
place in the app that intentionally does NOT follow the usual
"owner/accountant sees all business data" model, because EMI history is
closer to a personal scratchpad (a manager sanity-checking a customer's
EMI on the spot) than a business record like an invoice or expense.

Permissions: reuses view_finance (accountant and above) — same gate as
the rest of the Finance section in the sidebar.
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf
from utils.saas_middleware import permission_required, get_tenant_id

saas_emi_bp = Blueprint("saas_emi", __name__, url_prefix="/biz/finance/emi")

P = lambda: "%s" if _is_postgres() else "?"

MAX_HISTORY = 50  # keep the saved-calculations list from growing unbounded


# ════════════════════════════════ CALCULATOR + HISTORY ═════════════════════════

@saas_emi_bp.route("/")
@saas_business_required
@permission_required("view_finance")
def index():
    biz_id  = get_tenant_id()
    user_id = session.get("saas_user_id")
    p = P()

    history = saas_fetchall(
        f"""SELECT * FROM saas_emi_history
            WHERE business_id={p} AND user_id={p}
            ORDER BY is_favorite DESC, created_at DESC
            LIMIT {MAX_HISTORY}""",
        (biz_id, user_id)
    )
    return render_template("saas_business/finance/emi.html", history=history)


@saas_emi_bp.route("/save", methods=["POST"])
@saas_business_required
@permission_required("view_finance")
def save():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_emi.index"))

    biz_id  = get_tenant_id()
    user_id = session.get("saas_user_id")
    p = P()

    label = request.form.get("label", "").strip()[:150]

    try:
        principal = float(request.form.get("principal", 0) or 0)
        rate      = float(request.form.get("annual_rate", 0) or 0)
        months    = int(float(request.form.get("tenure_months", 0) or 0))
        emi       = float(request.form.get("emi_amount", 0) or 0)
        interest  = float(request.form.get("total_interest", 0) or 0)
        total     = float(request.form.get("total_payment", 0) or 0)
    except (TypeError, ValueError):
        flash("Couldn't read that calculation — please recalculate and try again.", "danger")
        return redirect(url_for("saas_emi.index"))

    if principal <= 0 or rate < 0 or months <= 0:
        flash("Enter a valid principal, rate, and tenure before saving.", "danger")
        return redirect(url_for("saas_emi.index"))

    saas_execute(
        f"""INSERT INTO saas_emi_history
            (business_id, user_id, label, principal, annual_rate, tenure_months,
             emi_amount, total_interest, total_payment)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (biz_id, user_id, label, principal, rate, months, emi, interest, total)
    )
    flash("Calculation saved.", "success")
    return redirect(url_for("saas_emi.index"))


@saas_emi_bp.route("/<int:entry_id>/favorite", methods=["POST"])
@saas_business_required
@permission_required("view_finance")
def toggle_favorite(entry_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_emi.index"))

    biz_id  = get_tenant_id()
    user_id = session.get("saas_user_id")
    p = P()

    row = saas_fetchone(
        f"SELECT is_favorite FROM saas_emi_history WHERE id={p} AND business_id={p} AND user_id={p}",
        (entry_id, biz_id, user_id)
    )
    if not row:
        flash("Calculation not found.", "danger")
        return redirect(url_for("saas_emi.index"))

    new_val = 0 if row["is_favorite"] else 1
    saas_execute(
        f"UPDATE saas_emi_history SET is_favorite={p} WHERE id={p} AND business_id={p} AND user_id={p}",
        (new_val, entry_id, biz_id, user_id)
    )
    return redirect(url_for("saas_emi.index"))


@saas_emi_bp.route("/<int:entry_id>/delete", methods=["POST"])
@saas_business_required
@permission_required("view_finance")
def delete(entry_id):
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_emi.index"))

    biz_id  = get_tenant_id()
    user_id = session.get("saas_user_id")
    p = P()

    # WHERE includes user_id — even if someone guesses another user's row
    # id, they can only ever delete their own rows.
    saas_execute(
        f"DELETE FROM saas_emi_history WHERE id={p} AND business_id={p} AND user_id={p}",
        (entry_id, biz_id, user_id)
    )
    flash("Calculation deleted.", "success")
    return redirect(url_for("saas_emi.index"))
