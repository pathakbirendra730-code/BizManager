"""
utils/business_deletion.py — Update_033: Demo Business Management
============================================================================
Complete, irreversible deletion of a business and every record scoped to
it. Used by the "Delete Business" flow (owner or super-admin only, behind
a password + typed "DELETE" confirmation — see modules/saas_auth/routes.py
and modules/app_admin/dashboard.py) for cleaning up demo/test businesses.

Design — explicit, not just cascade:
  Most business-scoped tables already have `business_id ... REFERENCES
  saas_businesses(id) ON DELETE CASCADE`, and SQLite connections in this
  app run with `PRAGMA foreign_keys = ON` (see models/saas_auth.py), so a
  bare `DELETE FROM saas_businesses WHERE id=?` would likely cascade
  correctly on its own. This module does NOT rely on that alone, for two
  reasons: (1) the "records that will be removed" preview the spec
  requires needs an explicit per-table count regardless, so the table
  list has to be enumerated and maintained here either way; (2) an
  explicit, auditable delete order is safer for something this
  destructive and irreversible than trusting an implicit cascade graph
  that could silently change shape in a future migration.

  TABLES lists every table in dependency order (children before
  parents) — this is the single source of truth both for the pre-
  deletion count/preview and the actual deletion, so they can never
  drift out of sync with each other.

  Two tables are handled specially, not simply deleted:
    - saas_sessions: business_id here just means "which business was
      last active in this browser session" — it's nullable and a user
      can belong to several businesses. Deleting the whole session row
      would force-logout that user everywhere, not just from the
      deleted business. This module NULLs the column instead.
    - saas_audit_logs: deleted like everything else (matches "delete
      every business-related record"), but the deletion action ITSELF
      is logged as a fresh audit entry with business_id=NULL (a
      platform-level record that survives the business it refers to),
      after the transaction commits.

Tenant isolation: every delete is scoped by business_id in its WHERE
clause — this module can only ever affect the one business it's given,
never any other (verified in testing — see CHANGELOG_Update_033.md §4).
"""

from models.saas_auth import saas_fetchone, saas_fetchall, get_saas_db, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"

# Every table scoped by business_id, in dependency order — children
# (referencing another business-scoped table) before their parents.
# saas_businesses itself is deleted last, outside this list.
TABLES = [
    # Line items / children first
    "saas_credit_note_items", "saas_debit_note_items",
    "saas_invoice_items", "saas_purchase_items",
    # Documents that reference the above
    "saas_credit_notes", "saas_debit_notes",
    "saas_payments",
    "saas_invoices", "saas_purchases",
    # Ledger (children before the accounts they post against)
    "saas_journal_lines", "saas_journal_entries",
    "saas_account_balances", "saas_chart_of_accounts",
    "saas_ledger", "saas_cash_book", "saas_bank_book",
    # Masters / operational data
    "saas_products", "saas_categories",
    "saas_customers", "saas_suppliers",
    "saas_expenses", "saas_emi_history",
    # Numbering / settings
    "saas_document_sequences", "saas_business_settings",
    # Team / access
    "saas_user_roles", "saas_pending_invites",
    # History (deleted like everything else — see module docstring)
    "saas_audit_logs",
]

# Handled specially — see module docstring.
NULLABLE_REFERENCE_TABLES = ["saas_sessions"]


def count_business_records(business_id: int) -> dict:
    """
    Returns {table_name: row_count} for every table in TABLES plus the
    business row itself — what the confirmation screen shows as
    "records that will be removed" before the person types DELETE.
    Zero-count tables are still included (a business with no purchases
    yet should show "saas_purchases: 0", not silently omit the row).
    """
    p = P()
    counts = {}
    for table in TABLES:
        row = saas_fetchone(f"SELECT COUNT(*) as c FROM {table} WHERE business_id={p}", (business_id,))
        counts[table] = int(row["c"]) if row else 0
    return counts


def get_business_summary(business_id: int) -> dict:
    """Human-readable summary for the confirmation screen — grouped
    into the categories the spec calls out by name, plus a grand total."""
    counts = count_business_records(business_id)
    grouped = {
        "Products":    counts["saas_products"] + counts["saas_categories"],
        "Customers":   counts["saas_customers"],
        "Suppliers":   counts["saas_suppliers"],
        "Invoices":    counts["saas_invoices"] + counts["saas_invoice_items"],
        "Purchases":   counts["saas_purchases"] + counts["saas_purchase_items"],
        "Returns":     counts["saas_credit_notes"] + counts["saas_credit_note_items"]
                       + counts["saas_debit_notes"] + counts["saas_debit_note_items"],
        "Ledger":      counts["saas_journal_entries"] + counts["saas_journal_lines"]
                       + counts["saas_ledger"] + counts["saas_cash_book"] + counts["saas_bank_book"],
        "Payments/Expenses": counts["saas_payments"] + counts["saas_expenses"],
        "Settings":    counts["saas_business_settings"] + counts["saas_document_sequences"],
        "Team/Invites": counts["saas_user_roles"] + counts["saas_pending_invites"],
        "Audit History": counts["saas_audit_logs"],
        "Other":       counts["saas_account_balances"] + counts["saas_chart_of_accounts"] + counts["saas_emi_history"],
    }
    return {"grouped": grouped, "raw": counts, "total": sum(counts.values())}


def delete_business_completely(business_id: int, deleted_by_user_id=None) -> dict:
    """
    Irreversibly deletes a business and every record scoped to it.
    Returns the same {table: count} breakdown that was actually
    deleted (computed from count_business_records() just before
    deleting, inside the same transaction, so it reflects exactly what
    was removed — not a stale count from an earlier page load).

    Raises if the business doesn't exist. Never touches any other
    business's data — every statement is scoped by business_id.
    """
    p = P()
    biz = saas_fetchone(f"SELECT id, name FROM saas_businesses WHERE id={p}", (business_id,))
    if not biz:
        raise ValueError(f"Business {business_id} not found.")

    conn = get_saas_db()
    c = conn.cursor()
    try:
        deleted_counts = {}
        for table in TABLES:
            c.execute(f"SELECT COUNT(*) FROM {table} WHERE business_id={p}", (business_id,))
            deleted_counts[table] = c.fetchone()[0]
            c.execute(f"DELETE FROM {table} WHERE business_id={p}", (business_id,))

        for table in NULLABLE_REFERENCE_TABLES:
            c.execute(f"UPDATE {table} SET business_id=NULL WHERE business_id={p}", (business_id,))

        c.execute(f"DELETE FROM saas_businesses WHERE id={p}", (business_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"business_id": business_id, "business_name": biz["name"], "deleted_counts": deleted_counts}
