"""
modules/saas_business/accounts.py — SaaS-Native Accounting
================================================================
Tenant-scoped unified ledger, cash book, bank book, and profit & loss
reporting for the SaaS multi-tenant system. Mirrors legacy
modules/accounts.py, but every query is scoped by business_id and
reads/writes saas_ledger / saas_cash_book / saas_bank_book /
saas_invoices / saas_purchases / saas_expenses.

Fixes applied vs the legacy module (not just a straight port):
  • CSRF protection added to all 3 manual-entry forms (journal entry,
    cash entry, bank entry) — the legacy versions had none at all,
    the same gap caught and fixed for Customers in an earlier phase.
  • Receivables calculation corrected: legacy only summed invoices
    with status='unpaid' via a dead-weight nested subquery that never
    actually subtracted any paid amount. This version sums
    due_amount directly across both 'unpaid' AND 'partial' invoices,
    which is the actually-correct receivable figure.
  • Revenue recognition for the dashboard/index KPIs now includes
    'partial' status alongside 'paid', consistent with how Finance
    and Reports already compute revenue elsewhere in the SaaS system
    (accrual-style, not cash-received-only).
  • Profit & Loss report intentionally keeps the legacy's simpler
    "Sales − Purchases − Expenses" cash-flow-style framing (distinct
    from Finance's COGS-based margin analysis) since both are valid,
    differently-purposed reports and this matches the original intent.

Permissions: view_finance / manage_finance → accountant and above
(reusing the same permission keys as modules/saas_business/finance.py,
since Accounts and Finance are both money-management features at the
same access level).
"""

import io
import csv
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, Response
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id
from utils.tax_helpers import today_str
from utils.money import to_decimal

saas_accounts_bp = Blueprint("saas_accounts", __name__, url_prefix="/biz/accounts")

P = lambda: "%s" if _is_postgres() else "?"


def _month_filter(col: str) -> str:
    return f"TO_CHAR({col}, 'YYYY-MM')" if _is_postgres() else f"strftime('%Y-%m', {col})"


def _year_filter(col: str) -> str:
    return f"TO_CHAR({col}, 'YYYY')" if _is_postgres() else f"strftime('%Y', {col})"


# ════════════════════════════════ DASHBOARD ════════════════════════════════════

@saas_accounts_bp.route("/")
@saas_business_required
@permission_required("view_finance")
def index():
    biz_id = get_tenant_id()
    p = P()
    month  = datetime.now().strftime("%Y-%m")
    mf_inv = _month_filter("created_at")
    mf_exp = _month_filter("expense_date")

    from utils.chart_of_accounts import get_account_by_subtype
    mf_je = _month_filter("je.entry_date")
    try:
        cash_acct_id = get_account_by_subtype(biz_id, "cash")["id"]
        cash_row = saas_fetchone(
            f"""SELECT COALESCE(SUM(jl.debit),0) as din, COALESCE(SUM(jl.credit),0) as dout
                FROM saas_journal_lines jl JOIN saas_journal_entries je ON je.id = jl.entry_id
                WHERE jl.account_id={p} AND jl.business_id={p} AND je.status='posted'
                  AND {mf_je}={p}""",
            (cash_acct_id, biz_id, month)
        )
        cash_in, cash_out = cash_row["din"] or 0, cash_row["dout"] or 0
    except LookupError:
        cash_in = cash_out = 0

    # Receivables: net due across unpaid + partial invoices (corrected vs legacy)
    receivable = saas_fetchone(
        f"""SELECT COALESCE(SUM(due_amount),0) as t FROM saas_invoices
            WHERE business_id={p} AND status IN ('unpaid','partial')""",
        (biz_id,)
    )["t"]

    # Update_024 fix: was SUM(saas_purchases.due_amount) — same drift bug as
    # the main dashboard's payables card (see modules/saas_business/
    # dashboard.py for the full explanation). saas_suppliers.balance is kept
    # in sync on every code path and is now the single source both pages use.
    payable = saas_fetchone(
        f"""SELECT COALESCE(SUM(balance), 0) as t FROM saas_suppliers
            WHERE business_id={p} AND is_active=TRUE""",
        (biz_id,)
    )["t"]

    sales = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as t FROM saas_invoices
            WHERE business_id={p} AND {mf_inv}={p} AND status IN ('paid','partial')""",
        (biz_id, month)
    )["t"]
    purchases = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as t FROM saas_purchases
            WHERE business_id={p} AND {mf_inv}={p} AND status!='cancelled'""",
        (biz_id, month)
    )["t"]
    expenses = saas_fetchone(
        f"SELECT COALESCE(SUM(amount),0) as t FROM saas_expenses WHERE business_id={p} AND {mf_exp}={p}",
        (biz_id, month)
    )["t"]

    recent_ledger = saas_fetchall(
        f"""SELECT je.entry_date as txn_date, coa.name as party_name, je.source_type as txn_type,
                   je.entry_number as ref_number, jl.debit, jl.credit,
                   COALESCE(jl.description, je.narration) as narration
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND je.status='posted' AND coa.party_type != ''
            ORDER BY je.created_at DESC LIMIT 10""",
        (biz_id,)
    )

    net_profit = round(sales - purchases - expenses, 2)
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/accounts/index.html",
        biz=biz, month=month,
        cash_in=round(cash_in, 2), cash_out=round(cash_out, 2),
        cash_balance=round(cash_in - cash_out, 2),
        receivable=round(receivable, 2), payable=round(payable, 2),
        sales=round(sales, 2), purchases=round(purchases, 2),
        expenses=round(expenses, 2), net_profit=net_profit,
        recent_ledger=recent_ledger)


# ════════════════════════════════ LEDGER ═══════════════════════════════════════
#
# Reads/writes saas_journal_lines / saas_journal_entries / saas_chart_of_accounts
# — the real double-entry engine — instead of the old single-entry saas_ledger
# table. billing.py, purchase.py, and suppliers.py stopped writing to saas_ledger
# once they were rewired onto the ledger engine, which silently orphaned this
# page (it kept reading the old table and showed nothing real). This is the
# read-side fix for that same rewiring.

CONTRA_SUBTYPES = [
    ("sales_revenue",     "Sales Revenue"),
    ("cogs",              "Purchases / COGS"),
    ("discount_given",    "Discount Given"),
    ("other_income",      "Other Income"),
    ("other_expense",     "Other Expense"),
    ("operating_expense", "Operating Expense"),
    ("cash",              "Cash"),
    ("bank",               "Bank Account"),
]


@saas_accounts_bp.route("/ledger")
@saas_business_required
@permission_required("view_finance")
def ledger():
    biz_id     = get_tenant_id()
    party_type = request.args.get("type", "")
    party_id   = request.args.get("party", "")
    date_from  = request.args.get("from", "")
    date_to    = request.args.get("to", "")
    q_str      = request.args.get("q", "")
    p = P()

    sql = f"""
        SELECT je.entry_date as txn_date, je.entry_number as ref_number,
               je.source_type as txn_type, COALESCE(jl.description, je.narration) as narration,
               jl.debit, jl.credit, coa.party_type as party_type, coa.party_id as party_id,
               coa.name as party_name
        FROM saas_journal_lines jl
        JOIN saas_journal_entries je ON je.id = jl.entry_id
        JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
        WHERE jl.business_id={p} AND je.status='posted' AND coa.party_type != ''
    """
    args = [biz_id]
    if party_type:
        sql += f" AND coa.party_type={p}"; args.append(party_type)
    if party_id:
        sql += f" AND coa.party_id={p}"; args.append(int(party_id))
    if date_from:
        sql += f" AND je.entry_date >= {p}"; args.append(date_from)
    if date_to:
        sql += f" AND je.entry_date <= {p}"; args.append(date_to)
    if q_str:
        sql += f" AND (coa.name LIKE {p} OR je.narration LIKE {p})"
        args += [f"%{q_str}%", f"%{q_str}%"]
    sql += " ORDER BY je.entry_date DESC, je.id DESC"

    entries = saas_fetchall(sql, tuple(args))

    total_debit  = sum(e["debit"]  or 0 for e in entries)
    total_credit = sum(e["credit"] or 0 for e in entries)
    summary = {"total_debit": total_debit, "total_credit": total_credit}

    customers = saas_fetchall(
        f"SELECT id, name FROM saas_customers WHERE business_id={p} ORDER BY name", (biz_id,)
    )
    suppliers = saas_fetchall(
        f"SELECT id, name FROM saas_suppliers WHERE business_id={p} AND is_active=TRUE ORDER BY name", (biz_id,)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/accounts/ledger.html",
                           entries=entries, biz=biz, summary=summary,
                           customers=customers, suppliers=suppliers,
                           contra_subtypes=CONTRA_SUBTYPES,
                           party_type=party_type, party_id=party_id,
                           date_from=date_from, date_to=date_to, q=q_str)


@saas_accounts_bp.route("/ledger/add", methods=["POST"])
@saas_business_required
@permission_required("manage_finance")
def add_ledger_entry():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_accounts.ledger"))

    biz_id         = get_tenant_id()
    party_type     = request.form.get("party_type", "customer")
    party_id_raw   = request.form.get("party_id", "")
    direction      = request.form.get("direction", "debit")     # which side the party is on
    contra_subtype = request.form.get("contra_subtype", "other_income")
    narration      = request.form.get("narration", "").strip()
    txn_date       = request.form.get("txn_date") or today_str()
    amount = to_decimal(request.form.get("amount", 0))

    if amount <= 0:
        flash("Enter a valid amount.", "danger")
        return redirect(url_for("saas_accounts.ledger"))
    if not narration:
        flash("Narration is required for a manual entry.", "danger")
        return redirect(url_for("saas_accounts.ledger"))
    if party_type not in ("customer", "supplier") or not party_id_raw:
        flash("Select a customer or supplier for this entry.", "danger")
        return redirect(url_for("saas_accounts.ledger"))

    from utils.chart_of_accounts import get_or_create_party_account, get_account_by_subtype
    from utils.ledger_service import post_journal_entry, InvalidLineError

    party_id = int(party_id_raw)
    p = P()
    party_row = saas_fetchone(
        f"SELECT name FROM saas_{'customers' if party_type=='customer' else 'suppliers'} "
        f"WHERE id={p} AND business_id={p}", (party_id, biz_id)
    )
    if not party_row:
        flash("Selected party not found.", "danger")
        return redirect(url_for("saas_accounts.ledger"))

    user_id = session.get("saas_user_id")
    try:
        party_acct  = get_or_create_party_account(biz_id, party_type, party_id, party_row["name"])
        contra_acct = get_account_by_subtype(biz_id, contra_subtype)
        amount = round(amount, 2)
        if direction == "debit":
            lines = [
                {"account_id": party_acct["id"],  "debit": amount, "credit": 0, "description": narration},
                {"account_id": contra_acct["id"], "debit": 0, "credit": amount, "description": narration},
            ]
        else:
            lines = [
                {"account_id": contra_acct["id"], "debit": amount, "credit": 0, "description": narration},
                {"account_id": party_acct["id"],  "debit": 0, "credit": amount, "description": narration},
            ]
        entry = post_journal_entry(
            biz_id, lines, source_type="manual_journal",
            narration=narration, entry_date=txn_date, created_by=user_id
        )
    except (InvalidLineError, LookupError) as e:
        flash(f"Could not post entry: {e}", "danger")
        return redirect(url_for("saas_accounts.ledger"))

    audit_log("ledger_manual_entry", user_id=user_id, business_id=biz_id,
              entity_type="journal_entry", entity_id=str(entry["entry_id"]),
              detail=f"party={party_type}:{party_id} amount={amount} direction={direction}")
    flash("Journal entry added.", "success")
    return redirect(url_for("saas_accounts.ledger"))


@saas_accounts_bp.route("/ledger/export")
@saas_business_required
@permission_required("view_finance")
def export_ledger():
    biz_id = get_tenant_id()
    p = P()
    rows = saas_fetchall(
        f"""SELECT je.entry_date as txn_date, coa.party_type as party_type, coa.name as party_name,
                   je.source_type as txn_type, je.entry_number as ref_number,
                   jl.debit, jl.credit, COALESCE(jl.description, je.narration) as narration
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND je.status='posted' AND coa.party_type != ''
            ORDER BY je.entry_date, je.id""",
        (biz_id,)
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Party Type", "Party", "Txn Type", "Ref#", "Debit", "Credit", "Narration"])
    for r in rows:
        w.writerow([r["txn_date"], r["party_type"], r["party_name"], r["txn_type"],
                    r["ref_number"], r["debit"], r["credit"], r["narration"]])
    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ledger.csv"})


# ════════════════════════════════ CASH BOOK ════════════════════════════════════
#
# Same fix as Ledger above: reads/writes the real Cash account in the ledger
# engine rather than the orphaned saas_cash_book table that billing/purchase/
# suppliers stopped writing to once they moved onto the double-entry engine.

def _book_lines(biz_id, subtype, date_from, date_to):
    """Journal lines touching the given account subtype (cash/bank), joined
    with entry info, for a date range. Excludes reversed entries."""
    from utils.chart_of_accounts import get_account_by_subtype
    p = P()
    try:
        acct = get_account_by_subtype(biz_id, subtype)
    except LookupError:
        return [], None
    rows = saas_fetchall(
        f"""SELECT je.entry_date as txn_date, je.entry_number as ref_number,
                   je.source_type as category, COALESCE(jl.description, je.narration) as description,
                   jl.debit, jl.credit
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            WHERE jl.account_id={p} AND jl.business_id={p} AND je.status='posted'
              AND je.entry_date BETWEEN {p} AND {p}
            ORDER BY je.entry_date, je.id""",
        (acct["id"], biz_id, date_from, date_to)
    )
    opening_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.debit),0) as d, COALESCE(SUM(jl.credit),0) as c
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            WHERE jl.account_id={p} AND jl.business_id={p} AND je.status='posted'
              AND je.entry_date < {p}""",
        (acct["id"], biz_id, date_from)
    )
    opening = round((opening_row["d"] or 0) - (opening_row["c"] or 0), 2)
    return rows, opening


@saas_accounts_bp.route("/cashbook")
@saas_business_required
@permission_required("view_finance")
def cashbook():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", datetime.now().strftime("%Y-%m-01"))
    date_to   = request.args.get("to", today_str())

    rows, opening = _book_lines(biz_id, "cash", date_from, date_to)
    entries = [{
        "txn_date":    r["txn_date"],
        "txn_type":    "receipt" if (r["debit"] or 0) > 0 else "payment",
        "category":    (r["category"] or "").replace("_", " ").title(),
        "description": r["description"],
        "amount":      r["debit"] if (r["debit"] or 0) > 0 else r["credit"],
    } for r in rows]

    cash_in  = sum(e["amount"] for e in entries if e["txn_type"] == "receipt")
    cash_out = sum(e["amount"] for e in entries if e["txn_type"] == "payment")
    summary  = {"cash_in": cash_in, "cash_out": cash_out}

    p = P()
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/accounts/cashbook.html",
                           entries=entries, biz=biz, summary=summary,
                           opening_balance=opening or 0,
                           date_from=date_from, date_to=date_to)


@saas_accounts_bp.route("/cashbook/add", methods=["POST"])
@saas_business_required
@permission_required("manage_finance")
def add_cash_entry():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_accounts.cashbook"))

    biz_id   = get_tenant_id()
    txn_type = request.form.get("txn_type", "receipt")
    category = request.form.get("category", "other_income").strip() or "other_income"
    desc     = request.form.get("description", "").strip()
    amount = to_decimal(request.form.get("amount", 0))
    txn_date = request.form.get("txn_date") or today_str()

    if amount <= 0:
        flash("Enter a valid amount.", "danger")
        return redirect(url_for("saas_accounts.cashbook"))
    if not desc:
        flash("Description is required for a manual entry.", "danger")
        return redirect(url_for("saas_accounts.cashbook"))

    from utils.ledger_transactions import record_adjustment
    from utils.ledger_service import InvalidLineError
    from utils.chart_of_accounts import seed_chart_of_accounts
    seed_chart_of_accounts(biz_id)  # idempotent — adds only whatever's missing (e.g. Update_026's Other Expenses)
    user_id = session.get("saas_user_id")
    try:
        if txn_type == "receipt":
            record_adjustment(biz_id, amount, debit_subtype="cash", credit_subtype=category,
                              narration=desc, entry_date=txn_date, created_by=user_id)
        else:
            record_adjustment(biz_id, amount, debit_subtype=category, credit_subtype="cash",
                              narration=desc, entry_date=txn_date, created_by=user_id)
    except (InvalidLineError, LookupError) as e:
        flash(f"Could not post entry: {e}", "danger")
        return redirect(url_for("saas_accounts.cashbook"))

    flash("Cash entry recorded.", "success")
    return redirect(url_for("saas_accounts.cashbook"))


@saas_accounts_bp.route("/cashbook/export")
@saas_business_required
@permission_required("view_finance")
def export_cashbook():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", "2000-01-01")
    date_to   = request.args.get("to", today_str())

    rows, _ = _book_lines(biz_id, "cash", date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Category", "Description", "Amount"])
    for r in rows:
        txn_type = "receipt" if (r["debit"] or 0) > 0 else "payment"
        amount = r["debit"] if (r["debit"] or 0) > 0 else r["credit"]
        w.writerow([r["txn_date"], txn_type, r["category"], r["description"], amount])
    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cashbook.csv"})


# ════════════════════════════════ BANK BOOK ════════════════════════════════════

@saas_accounts_bp.route("/bankbook")
@saas_business_required
@permission_required("view_finance")
def bankbook():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", datetime.now().strftime("%Y-%m-01"))
    date_to   = request.args.get("to", today_str())
    p = P()

    rows, opening = _book_lines(biz_id, "bank", date_from, date_to)
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    entries = [{
        "txn_date":     r["txn_date"],
        "account_name": (biz["name"] if biz else "Bank Account"),
        "txn_type":     "credit" if (r["debit"] or 0) > 0 else "debit",
        "description":  r["description"],
        "ref_number":   r["ref_number"],
        "amount":       r["debit"] if (r["debit"] or 0) > 0 else r["credit"],
    } for r in rows]

    credits = sum(e["amount"] for e in entries if e["txn_type"] == "credit")
    debits  = sum(e["amount"] for e in entries if e["txn_type"] == "debit")
    summary = {"credits": credits, "debits": debits}

    return render_template("saas_business/accounts/bankbook.html",
                           entries=entries, biz=biz, summary=summary,
                           opening_balance=opening or 0,
                           date_from=date_from, date_to=date_to)


@saas_accounts_bp.route("/bankbook/add", methods=["POST"])
@saas_business_required
@permission_required("manage_finance")
def add_bank_entry():
    if not validate_csrf(request.form.get("csrf_token")):
        flash("Security error. Please try again.", "danger")
        return redirect(url_for("saas_accounts.bankbook"))

    biz_id   = get_tenant_id()
    txn_type = request.form.get("txn_type", "credit")
    category = request.form.get("category", "other_income").strip() or "other_income"
    desc     = request.form.get("description", "").strip()
    ref_no   = request.form.get("ref_number", "").strip()
    if ref_no:
        desc = f"{desc} (Ref: {ref_no})" if desc else f"Ref: {ref_no}"
    amount = to_decimal(request.form.get("amount", 0))
    txn_date = request.form.get("txn_date") or today_str()

    if amount <= 0:
        flash("Enter a valid amount.", "danger")
        return redirect(url_for("saas_accounts.bankbook"))
    if not desc:
        flash("Description is required for a manual entry.", "danger")
        return redirect(url_for("saas_accounts.bankbook"))

    from utils.ledger_transactions import record_adjustment
    from utils.ledger_service import InvalidLineError
    from utils.chart_of_accounts import seed_chart_of_accounts
    seed_chart_of_accounts(biz_id)  # idempotent — adds only whatever's missing (e.g. Update_026's Other Expenses)
    user_id = session.get("saas_user_id")
    try:
        if txn_type == "credit":
            record_adjustment(biz_id, amount, debit_subtype="bank", credit_subtype=category,
                              narration=desc, entry_date=txn_date, created_by=user_id)
        else:
            record_adjustment(biz_id, amount, debit_subtype=category, credit_subtype="bank",
                              narration=desc, entry_date=txn_date, created_by=user_id)
    except (InvalidLineError, LookupError) as e:
        flash(f"Could not post entry: {e}", "danger")
        return redirect(url_for("saas_accounts.bankbook"))

    flash("Bank transaction recorded.", "success")
    return redirect(url_for("saas_accounts.bankbook"))


@saas_accounts_bp.route("/bankbook/export")
@saas_business_required
@permission_required("view_finance")
def export_bankbook():
    biz_id    = get_tenant_id()
    date_from = request.args.get("from", "2000-01-01")
    date_to   = request.args.get("to", today_str())

    rows, _ = _book_lines(biz_id, "bank", date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Type", "Description", "Ref#", "Amount"])
    for r in rows:
        txn_type = "credit" if (r["debit"] or 0) > 0 else "debit"
        amount = r["debit"] if (r["debit"] or 0) > 0 else r["credit"]
        w.writerow([r["txn_date"], txn_type, r["description"], r["ref_number"], amount])
    return Response(buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bankbook.csv"})


# ════════════════════════════════ PROFIT & LOSS ════════════════════════════════

@saas_accounts_bp.route("/profit-loss")
@saas_business_required
@permission_required("view_finance")
def profit_loss():
    biz_id    = get_tenant_id()
    month     = request.args.get("month", datetime.now().strftime("%Y-%m"))
    year_mode = request.args.get("mode", "monthly")
    p = P()

    if year_mode == "yearly":
        date_filter = _year_filter("created_at") + f" = '{month[:4]}'"
        exp_filter  = _year_filter("expense_date") + f" = '{month[:4]}'"
        inv_filter  = _year_filter("i.created_at") + f" = '{month[:4]}'"
        je_filter   = _year_filter("je.entry_date") + f" = '{month[:4]}'"
    else:
        date_filter = _month_filter("created_at") + f" = '{month}'"
        exp_filter  = _month_filter("expense_date") + f" = '{month}'"
        inv_filter  = _month_filter("i.created_at") + f" = '{month}'"
        je_filter   = _month_filter("je.entry_date") + f" = '{month}'"

    sales = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as total, COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(total_tax),0) as tax, COUNT(*) as cnt
            FROM saas_invoices WHERE business_id={p} AND {date_filter}
              AND status IN ('paid','partial')""",
        (biz_id,)
    )
    purchases = saas_fetchone(
        f"""SELECT COALESCE(SUM(total),0) as total, COALESCE(SUM(taxable_amount),0) as taxable,
                   COALESCE(SUM(total_tax),0) as tax, COUNT(*) as cnt
            FROM saas_purchases WHERE business_id={p} AND {date_filter} AND status!='cancelled'""",
        (biz_id,)
    )
    exp_total = saas_fetchone(
        f"SELECT COALESCE(SUM(amount),0) as total FROM saas_expenses WHERE business_id={p} AND {exp_filter}",
        (biz_id,)
    )
    exp_by_cat = saas_fetchall(
        f"""SELECT category, COALESCE(SUM(amount),0) as total, COUNT(*) as cnt
            FROM saas_expenses WHERE business_id={p} AND {exp_filter}
            GROUP BY category ORDER BY total DESC""",
        (biz_id,)
    )

    # ── Update_026: COGS — same join/filter shape as finance.py's dashboard ──
    # (quantity actually sold this period × each product's cost_price), so
    # this page and the Finance Dashboard can never compute a different
    # number for the same period. See CHANGELOG_Update_026.md §2 for the
    # verification that proves this.
    cogs_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(ii.quantity * pr.cost_price), 0) as cogs
            FROM saas_invoice_items ii
            JOIN saas_invoices i ON i.id = ii.invoice_id
            JOIN saas_products pr ON pr.id = ii.product_id
            WHERE ii.business_id={p} AND {inv_filter}
              AND i.status IN ('paid','partial')""",
        (biz_id,)
    )
    cogs = round(float(cogs_row["cogs"] or 0), 2)

    # ── Sales Returns ──
    # returns_expense is a real, dedicated account (seeded for every
    # business) but no route in the app currently posts to it —
    # record_sales_return() exists in the ledger engine and is fully
    # correct, but is unreachable (confirmed dead code, Update_024/025).
    # This always reports 0 today; queried for real so it's correct the
    # moment that feature exists, rather than a fixed placeholder.
    sales_returns_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.debit - jl.credit),0) as t
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND coa.account_subtype='returns_expense'
              AND je.status='posted' AND {je_filter}""",
        (biz_id,)
    )
    sales_returns = round(max(0.0, float(sales_returns_row["t"] or 0)), 2)

    # ── Purchase Returns ──
    # Genuinely no data source at all, even in principle: record_purchase_
    # return() (also dead code — no route calls it) posts its reversal
    # straight into the same "cogs" account regular purchases use, by
    # design (see its docstring), so even if it were wired up there'd be
    # no way to separate returns from regular purchases in that account
    # without a schema change. Hardcoded 0 rather than a query that would
    # look real but can never mean anything — see CHANGELOG §2 for detail.
    purchase_returns = 0.0

    # ── Other Income / Other Expenses ──
    # Real, postable accounts (Accounts → Cash Book/Bank Book "Add Entry"
    # forms already support "Other Income"; "Other Expense" is new in this
    # update — see utils/chart_of_accounts.py and CONTRA_SUBTYPES below).
    other_income_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.credit - jl.debit),0) as t
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND coa.account_subtype='other_income'
              AND je.status='posted' AND {je_filter}""",
        (biz_id,)
    )
    other_income = round(max(0.0, float(other_income_row["t"] or 0)), 2)

    other_expense_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(jl.debit - jl.credit),0) as t
            FROM saas_journal_lines jl
            JOIN saas_journal_entries je ON je.id = jl.entry_id
            JOIN saas_chart_of_accounts coa ON coa.id = jl.account_id
            WHERE jl.business_id={p} AND coa.account_subtype='other_expense'
              AND je.status='posted' AND {je_filter}""",
        (biz_id,)
    )
    other_expense = round(max(0.0, float(other_expense_row["t"] or 0)), 2)

    # ── Closing / Opening Stock ──
    # Closing Stock reuses the exact valuation method already used by the
    # Inventory report (reports.py): current stock_quantity × cost_price
    # per product, summed. This is a CURRENT valuation, not a historical
    # snapshot as of the selected period's end — the app has no daily
    # stock ledger to reconstruct a true historical figure. For the
    # current period this is accurate; for a past period it's an
    # approximation. Labeled as such in the template.
    closing_stock_row = saas_fetchone(
        f"""SELECT COALESCE(SUM(stock_quantity * cost_price),0) as t
            FROM saas_products WHERE business_id={p} AND is_active=TRUE""",
        (biz_id,)
    )
    closing_stock = round(float(closing_stock_row["t"] or 0), 2)

    # Opening Stock is derived from the standard accounting identity
    # (COGS = Opening Stock + Purchases − Closing Stock), rearranged, using
    # the real COGS and Purchases figures above and the current closing
    # stock valuation. This is a legitimate accounting derivation, not a
    # fabricated number — but it inherits the same "current valuation, not
    # historical" caveat as Closing Stock above. Floored at 0 (a negative
    # opening stock isn't meaningful).
    opening_stock = round(max(0.0, cogs - float(purchases["total"]) + closing_stock), 2)

    net_sales      = round(float(sales["total"]) - sales_returns, 2)
    gross_profit   = round(net_sales - cogs, 2)
    net_profit     = round(gross_profit - float(exp_total["total"]) + other_income - other_expense, 2)
    gross_margin   = round((gross_profit / net_sales * 100), 2) if net_sales else 0.0
    net_margin     = round((net_profit  / net_sales * 100), 2) if net_sales else 0.0

    mf = _month_filter("created_at")
    trend = saas_fetchall(
        f"""SELECT {mf} as mon, COALESCE(SUM(total),0) as sales
            FROM saas_invoices WHERE business_id={p} AND status IN ('paid','partial')
            GROUP BY mon ORDER BY mon DESC LIMIT 12""",
        (biz_id,)
    )
    purchase_trend = saas_fetchall(
        f"""SELECT {mf} as mon, COALESCE(SUM(total),0) as purchases
            FROM saas_purchases WHERE business_id={p} AND status!='cancelled'
            GROUP BY mon ORDER BY mon DESC LIMIT 12""",
        (biz_id,)
    )

    trend_map  = {r["mon"]: r["sales"] for r in trend}
    pur_map    = {r["mon"]: r["purchases"] for r in purchase_trend}
    all_months = sorted(set(list(trend_map.keys()) + list(pur_map.keys())), reverse=True)[:12]
    chart_data = [{"month": m, "sales": trend_map.get(m, 0), "purchases": pur_map.get(m, 0)}
                  for m in reversed(all_months)]

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))

    return render_template("saas_business/accounts/profit_loss.html",
        biz=biz, month=month, year_mode=year_mode,
        sales=sales, purchases=purchases,
        exp_total=round(float(exp_total["total"]), 2), exp_by_cat=exp_by_cat,
        cogs=cogs, net_sales=net_sales,
        sales_returns=sales_returns, purchase_returns=purchase_returns,
        other_income=other_income, other_expense=other_expense,
        opening_stock=opening_stock, closing_stock=closing_stock,
        gross_profit=gross_profit, net_profit=net_profit,
        gross_margin=gross_margin, net_margin=net_margin,
        chart_data=chart_data)
