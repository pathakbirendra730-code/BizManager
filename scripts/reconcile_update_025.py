"""
scripts/reconcile_update_025.py — Update_025 one-time financial reconciliation
================================================================================
NOT a Flask route, NOT wired into the running app, NOT a new feature — this is
a standalone maintenance script, run once (or whenever a spot-check is wanted)
from the command line, on the same database the app itself uses.

What it does, per business (in this order — backfill first, then reconcile,
so a single --apply run is enough to fully settle a business's numbers):

  1. BACKFILL MISSING OPENING BALANCES — finds suppliers with a nonzero
     opening_balance and no corresponding 'opening_balance' journal entry
     (i.e. created before the Update_024 fix that started posting these),
     and posts the missing entry via the existing record_opening_balance()
     helper — the exact same function newly-created suppliers already use.

  2. RECONCILE SUPPLIER BALANCES — recomputes each supplier's true balance by
     summing every posted journal line against their accounts_payable
     sub-ledger account (the same formula the ledger engine itself uses:
     balance = total_credit - total_debit for a liability/credit-normal
     account — see utils/ledger_service.py::_update_account_balance).
     Compares that to saas_suppliers.balance (the UI-facing cache) AND to
     saas_account_balances (the ledger's own cache table), and reports any
     drift. This recomputation covers Update_025 tasks #1 and #2 — any
     historical advance payment that was silently clamped to zero by the
     bug fixed in Update_024 shows up here as drift between the
     from-scratch ledger sum and the (previously wrong) cached balance,
     because the ledger itself was never clamped, only the UI cache was.

Usage:
    python scripts/reconcile_update_025.py                  # dry run, ALL businesses
    python scripts/reconcile_update_025.py --business-id 7  # dry run, one business
    python scripts/reconcile_update_025.py --apply           # apply fixes, ALL businesses
    python scripts/reconcile_update_025.py --apply --business-id 7

Dry run (the default) only reads and reports — it makes no writes. --apply
is required to actually correct any drift found. Safe to re-run any number
of times: a business with nothing to fix reports "no drift found" and
"nothing to backfill" and touches no rows.
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(business_id=None, apply=False):
    # Imported here (not at module level) so this script can be dropped into
    # any checkout and still resolve the app's own config/env exactly like
    # app.py does, without needing a running Flask server.
    from app import create_app
    from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
    from utils.chart_of_accounts import get_or_create_party_account
    from utils.ledger_transactions import record_opening_balance

    app = create_app()
    with app.app_context():
        p = lambda: "%s" if _is_postgres() else "?"

        if business_id:
            businesses = saas_fetchall(f"SELECT id, name FROM saas_businesses WHERE id={p()}", (business_id,))
        else:
            businesses = saas_fetchall("SELECT id, name FROM saas_businesses ORDER BY id")

        if not businesses:
            print("No matching business found.")
            return

        grand_drift_count = 0
        grand_backfill_count = 0

        for biz in businesses:
            bid = biz["id"]
            P = p()
            print(f"\n{'='*70}\nBusiness #{bid} — {biz['name']}\n{'='*70}")

            # ── Part 1: backfill missing opening-balance journal entries ──
            # (must run BEFORE the balance reconciliation below — otherwise
            # a supplier's ledger total is still incomplete when we compare
            # it to saas_suppliers.balance, and we'd "correct" the cache to
            # a value that's about to change again the moment we post the
            # missing entry, requiring a second run to settle. Doing this
            # first means one --apply pass is enough.)
            missing_ob = saas_fetchall(
                f"""SELECT s.id, s.name, s.opening_balance FROM saas_suppliers s
                    WHERE s.business_id={P} AND s.opening_balance != 0
                      AND NOT EXISTS (
                        SELECT 1 FROM saas_journal_lines jl
                        JOIN saas_journal_entries je ON je.id = jl.entry_id
                        WHERE jl.business_id={P} AND je.source_type='opening_balance'
                          AND jl.party_type='supplier' AND jl.party_id=s.id
                      )""",
                (bid, bid)
            )
            if not missing_ob:
                print("  Opening balances: nothing to backfill.")
            else:
                print(f"  Opening balances: {len(missing_ob)} supplier(s) missing their ledger entry.")
                for s in missing_ob:
                    grand_backfill_count += 1
                    print(f"    Supplier #{s['id']} {s['name']!r} — opening_balance={s['opening_balance']}")
                    if apply:
                        record_opening_balance(
                            bid, "accounts_payable", float(s["opening_balance"]),
                            party_id=s["id"], party_name=s["name"], party_type="supplier",
                            narration=f"Opening balance (Update_025 backfill) — {s['name']}",
                        )
                        print(f"      -> posted opening_balance entry")
                if not apply:
                    print("    (re-run with --apply to post these)")

            # ── Part 2: reconcile every supplier's balance against the ledger ──
            suppliers = saas_fetchall(
                f"SELECT id, name, balance FROM saas_suppliers WHERE business_id={P}", (bid,)
            )
            drift_found = 0
            for s in suppliers:
                acct = get_or_create_party_account(bid, "supplier", s["id"], s["name"])
                totals = saas_fetchone(
                    f"""SELECT COALESCE(SUM(debit),0) as d, COALESCE(SUM(credit),0) as c
                        FROM saas_journal_lines WHERE business_id={P} AND account_id={P}""",
                    (bid, acct["id"])
                )
                true_balance = round(float(totals["c"]) - float(totals["d"]), 2)  # credit-normal
                cached_balance = round(float(s["balance"] or 0), 2)

                cache_row = saas_fetchone(
                    f"SELECT balance FROM saas_account_balances WHERE business_id={P} AND account_id={P}",
                    (bid, acct["id"])
                )
                ledger_cache_balance = round(float(cache_row["balance"]), 2) if cache_row else 0.0

                if abs(true_balance - cached_balance) > 0.01 or abs(true_balance - ledger_cache_balance) > 0.01:
                    drift_found += 1
                    grand_drift_count += 1
                    print(f"  DRIFT — Supplier #{s['id']} {s['name']!r}:")
                    print(f"          saas_suppliers.balance = {cached_balance}")
                    print(f"          saas_account_balances  = {ledger_cache_balance}")
                    print(f"          true (from journal lines) = {true_balance}")
                    if apply:
                        saas_execute(
                            f"UPDATE saas_suppliers SET balance={P} WHERE id={P} AND business_id={P}",
                            (true_balance, s["id"], bid)
                        )
                        if cache_row:
                            saas_execute(
                                f"""UPDATE saas_account_balances SET balance={P}, total_debit={P}, total_credit={P}
                                    WHERE business_id={P} AND account_id={P}""",
                                (true_balance, totals["d"], totals["c"], bid, acct["id"])
                            )
                        else:
                            saas_execute(
                                f"""INSERT INTO saas_account_balances
                                    (business_id, account_id, total_debit, total_credit, balance)
                                    VALUES ({P},{P},{P},{P},{P})""",
                                (bid, acct["id"], totals["d"], totals["c"], true_balance)
                            )
                        print(f"          -> corrected to {true_balance}")

            if drift_found == 0:
                print(f"  Suppliers: no drift found across {len(suppliers)} supplier(s).")
            else:
                verb = "corrected" if apply else "found (re-run with --apply to correct)"
                print(f"  Suppliers: {drift_found} of {len(suppliers)} had drift — {verb}.")

        print(f"\n{'='*70}")
        if apply:
            print(f"DONE. Corrected {grand_drift_count} supplier balance(s), "
                  f"backfilled {grand_backfill_count} opening balance(s).")
        else:
            print(f"DRY RUN COMPLETE. {grand_drift_count} drifted balance(s), "
                  f"{grand_backfill_count} missing opening balance(s) — no changes made. "
                  f"Re-run with --apply to fix.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update_025 supplier balance reconciliation")
    parser.add_argument("--business-id", type=int, default=None, help="Limit to one business")
    parser.add_argument("--apply", action="store_true", help="Actually write corrections (default: dry run)")
    args = parser.parse_args()
    run(business_id=args.business_id, apply=args.apply)
