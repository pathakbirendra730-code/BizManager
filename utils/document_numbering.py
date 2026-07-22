"""
utils/document_numbering.py — Update_027: Financial Year Based Document Numbering
====================================================================================
Commercial-ERP-style document numbering (Tally/Busy/Zoho/ERPNext convention):

    <Prefix>/<FinancialYear>/<ZeroPaddedSequence>
    e.g.  INV/2026-27/000001

Design (per the Update_027 spec):

  1. Only three things are ever stored as the source of truth — prefix,
     financial year, and a running sequence — in saas_document_sequences.
     The formatted string is never itself the primary record; it's always
     re-derived from those three components at read time.

  2. Independent counters per (business_id, document_type, financial_year)
     — enforced by that exact UNIQUE constraint on the table, not just by
     convention in application code.

  3. Financial year is derived from the document's own date (India FY:
     1 April – 31 March), not from "today" — so backdating a document (or
     numbering a document filed near a FY boundary) lands in the correct
     year's sequence.

  4. Sequence resets to 1 automatically for every new financial year,
     because the UNIQUE key includes financial_year — a new FY simply has
     no existing row yet, so the first document of that year starts a
     fresh counter rather than continuing the prior year's.

  5. Numbers are never reused. The sequence only ever increments — nothing
     in this module or its callers ever decrements it, including on
     cancellation. A cancelled document's number stays permanently
     "spent"; the next document (of any kind) simply gets the next number.

  8. Concurrency-safe via a single atomic UPSERT statement (INSERT ...
     ON CONFLICT ... DO UPDATE), not a read-then-write pattern — the
     database's own row-level locking is what prevents two simultaneous
     requests from ever computing the same sequence number, the same way
     Update_025 fixed the equivalent race condition for journal entry
     numbers (see utils/ledger_service.py::post_journal_entry).

Reused infrastructure, not new plumbing: `ledger_transaction()` (from
models/saas_ledger_engine.py) already provides exactly the atomic
connection/commit/rollback wrapper this needs — despite its name, there's
nothing ledger-specific about it, so it's imported here rather than
duplicated.
"""

from datetime import date, datetime

from models.saas_auth import saas_fetchone, _is_postgres
from models.saas_ledger_engine import ledger_transaction
from utils.platform_settings import get_setting


# Every document type this engine supports, and which platform setting
# holds its configurable prefix. Only sales_invoice and purchase_bill are
# currently wired into a real document-creation route (see
# modules/saas_business/billing.py and purchase.py) — the other four have
# no creation feature anywhere in the app yet (confirmed by exhaustive
# route search, same finding as the dead sales/purchase-return functions
# from Update_024/025), so calling generate_document_number() for them
# works correctly today (verified with tests) but nothing in the app does
# so yet. See CHANGELOG_Update_027.md for the full explanation.
DOCUMENT_TYPES = {
    "sales_invoice":    "prefix_sales_invoice",
    "purchase_bill":    "prefix_purchase_bill",
    "credit_note":      "prefix_credit_note",
    "debit_note":       "prefix_debit_note",
    "quotation":        "prefix_quotation",
    "delivery_challan": "prefix_delivery_challan",
}


def financial_year_for_date(d) -> str:
    """
    India's financial year runs 1 April – 31 March. Returns e.g. "2026-27"
    for any date from 2026-04-01 through 2027-03-31.

    Accepts a date/datetime object, or a "YYYY-MM-DD" (optionally with a
    time part) string — every caller in this app already has one of those
    two shapes, so this covers both without pushing parsing onto callers.
    """
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    elif isinstance(d, datetime):
        d = d.date()

    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{str(start_year + 1)[2:]}"


def generate_document_number(business_id: int, document_type: str, document_date=None) -> dict:
    """
    Atomically generate the next document number for this business +
    document type + the financial year that `document_date` falls in.

    Returns a dict — never just the formatted string — so callers can
    store the components separately, per requirement #1:
        {
          "formatted":       "INV/2026-27/000001",
          "prefix":          "INV",
          "financial_year":  "2026-27",
          "sequence":        1,
        }

    Raises ValueError for an unknown document_type — callers should treat
    that as a programming error (a typo in the document_type argument),
    not something to silently default around.
    """
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(
            f"Unknown document_type {document_type!r} — must be one of {sorted(DOCUMENT_TYPES)}"
        )

    if document_date is None:
        document_date = date.today()
    fy = financial_year_for_date(document_date)
    prefix = get_setting(DOCUMENT_TYPES[document_type]).strip() or document_type[:3].upper()

    with ledger_transaction() as (conn, c, p):
        # Single atomic statement — this is what makes it concurrency-safe.
        # Two simultaneous requests for the same (business, type, FY) both
        # try to run this; the database itself serializes them via the
        # UNIQUE constraint + row lock, so one always executes strictly
        # before the other commits. Neither can ever read a stale
        # "last sequence" and both increment from it — there's no
        # read-then-write gap for a race to happen in, unlike the bug
        # Update_025 found and fixed in journal entry numbering.
        c.execute(
            f"""INSERT INTO saas_document_sequences
                    (business_id, document_type, financial_year, last_sequence)
                VALUES ({p},{p},{p},1)
                ON CONFLICT (business_id, document_type, financial_year)
                DO UPDATE SET last_sequence = saas_document_sequences.last_sequence + 1
                """ if _is_postgres() else
            f"""INSERT INTO saas_document_sequences
                    (business_id, document_type, financial_year, last_sequence)
                VALUES ({p},{p},{p},1)
                ON CONFLICT (business_id, document_type, financial_year)
                DO UPDATE SET last_sequence = last_sequence + 1
                """,
            (business_id, document_type, fy)
        )
        # Reading it back within the SAME transaction, on the SAME
        # connection, after our own write — this is always guaranteed to
        # see our own just-written value ("read your own writes"),
        # regardless of what any other concurrent transaction is doing,
        # so no RETURNING clause is needed (kept portable to older SQLite
        # builds that predate RETURNING support, e.g. on Pydroid/Android).
        c.execute(
            f"""SELECT last_sequence FROM saas_document_sequences
                WHERE business_id={p} AND document_type={p} AND financial_year={p}""",
            (business_id, document_type, fy)
        )
        row = c.fetchone()
        sequence = row["last_sequence"]

    formatted = f"{prefix}/{fy}/{sequence:06d}"
    return {
        "formatted": formatted,
        "prefix": prefix,
        "financial_year": fy,
        "sequence": sequence,
    }


def current_sequence_position(business_id: int, document_type: str, financial_year: str = None) -> int:
    """
    Read-only: the last sequence number issued so far for this business +
    document type + FY (0 if none yet). Never increments anything — for
    display/diagnostics only (e.g. "next invoice will be #126").
    """
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(f"Unknown document_type {document_type!r}")
    if financial_year is None:
        financial_year = financial_year_for_date(date.today())

    from models.saas_auth import _is_postgres as _pg
    p = "%s" if _pg() else "?"
    row = saas_fetchone(
        f"""SELECT last_sequence FROM saas_document_sequences
            WHERE business_id={p} AND document_type={p} AND financial_year={p}""",
        (business_id, document_type, financial_year)
    )
    return row["last_sequence"] if row else 0
