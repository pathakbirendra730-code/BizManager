"""
utils/document_numbering.py - Update_027: Configurable Document Numbering
============================================================================
Commercial-ERP-style document numbering (Tally/Busy/Zoho/ERPNext convention),
configurable per business by that business's own owner, from their Business
Settings page - with the platform-wide App Admin settings acting as the
default for any business that hasn't customized its own:

    <Prefix><Sep><Period><Sep><ZeroPaddedSequence><Suffix>
    e.g.  INV/2026-27/000001

Configurable pieces (see utils/business_settings.py for the override/
fallback mechanics, and utils/platform_settings.py for the underlying
schema - prefix/label/type/validators are defined once, there, and reused
by both the App Admin form and every business's own form):
  - Prefix            - per document type (prefix_sales_invoice, etc.)
  - Separator         - doc_numbering_separator ("/" or "-")
  - Suffix (optional) - doc_numbering_suffix, appended at the very end
  - Starting Number   - doc_numbering_start_number (default 1)
  - Digit Length      - doc_numbering_digit_length (default 6)
  - Auto Reset        - doc_numbering_auto_reset: financial_year (default),
                         monthly, or never
  - Manual Numbering  - doc_numbering_manual_enable: lets callers pass a
                         hand-typed number instead of an auto one

Every read in this module goes through utils.business_settings, which
resolves in this order: (1) this specific business's own saved override,
(2) the platform-wide App Admin default, (3) the hardcoded schema default.
Out-of-the-box, every default reproduces the exact original Update_027
format - INV/2026-27/000001 - so a business that never opens either
settings page behaves identically to before this feature existed.

Design (per the original Update_027 spec, still true):

  1. Only the primitives are ever stored as the source of truth - prefix,
     period key, and a running sequence - in saas_document_sequences.
     The formatted string is never itself the primary record; it's always
     re-derived from those components (plus the *current*, business-
     specific separator/digit-length/suffix settings) at read time. This
     means changing a business's separator or digit length later
     reformats how future numbers are *displayed* for that business going
     forward, without touching any stored data, and without affecting any
     other business.

  2. Independent counters per (business_id, document_type, period_key)
     - enforced by that exact UNIQUE constraint on the table, not just by
     convention in application code. `period_key` holds whatever this
     business's active Auto Reset mode produces (a financial year like
     "2026-27", a month like "2026-07", or the constant "ALL" for Never)
     - the column is still named financial_year for backward
     compatibility with the original schema, but it's a general period
     key now.

  3. The period is derived from the document's own date (India FY:
     1 April - 31 March for the default mode), not from "today" - so
     backdating a document (or numbering one filed near a period
     boundary) lands in the correct period's sequence.

  4. Sequence resets to the configured Starting Number automatically for
     every new period, because the UNIQUE key includes period_key - a new
     period simply has no existing row yet, so the first document of that
     period starts a fresh counter rather than continuing the prior one's.
     Changing the Starting Number setting later never rewrites a counter
     that has already started - it only takes effect the next time a
     brand new period's counter is first created.

  5. Numbers are never reused. The sequence only ever increments - nothing
     in this module or its callers ever decrements it, including on
     cancellation. A cancelled document's number stays permanently
     "spent"; the next document (of any kind) simply gets the next number.
     This is true for auto-numbered documents; manually-numbered ones
     (see Manual Numbering below) don't touch the sequence at all.

  6. Manual Numbering, when enabled, is an explicit opt-in per call
     (`manual_number=...`) - this module never invents or infers a manual
     number itself. If the setting is off, a manual_number argument is
     ignored and the normal auto sequence is used, so a route that still
     passes one (e.g. because a client is stale) can never bypass the
     owner's toggle.

  7. Every setting is resolved per-business - two businesses can run
     completely different prefixes, separators, or reset modes
     simultaneously with no cross-talk, because every lookup in this
     module is keyed by the business_id already passed into every public
     function here.

  8. Concurrency-safe via a single atomic UPSERT statement (INSERT ...
     ON CONFLICT ... DO UPDATE), not a read-then-write pattern - the
     database's own row-level locking is what prevents two simultaneous
     requests from ever computing the same sequence number, the same way
     Update_025 fixed the equivalent race condition for journal entry
     numbers (see utils/ledger_service.py::post_journal_entry).

Reused infrastructure, not new plumbing: `ledger_transaction()` (from
models/saas_ledger_engine.py) already provides exactly the atomic
connection/commit/rollback wrapper this needs - despite its name, there's
nothing ledger-specific about it, so it's imported here rather than
duplicated.
"""

from datetime import date, datetime

from models.saas_auth import saas_fetchone, _is_postgres
from models.saas_ledger_engine import ledger_transaction
from utils.business_settings import (
    get_business_setting, get_business_int_setting, get_business_bool_setting,
)


# Every document type this engine supports, and which setting key holds
# its configurable prefix. Only sales_invoice and purchase_bill are
# currently wired into a real document-creation route (see
# modules/saas_business/billing.py and purchase.py) - the other four have
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

# The Auto Reset setting's stored value, and the constant period_key used
# for "never" (a mode that has no natural period token of its own - one
# continuous sequence forever). Never a valid financial year or month
# string, so it can't collide with a real period.
RESET_NEVER_PERIOD_KEY = "ALL"


def _coerce_date(d) -> date:
    """Accepts a date/datetime object, or a "YYYY-MM-DD" (optionally with a
    time part) string - every caller in this app already has one of those
    two shapes, so this covers both without pushing parsing onto callers."""
    if d is None:
        return date.today()
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    if isinstance(d, datetime):
        return d.date()
    return d


def financial_year_for_date(d) -> str:
    """
    India's financial year runs 1 April - 31 March. Returns e.g. "2026-27"
    for any date from 2026-04-01 through 2027-03-31.
    """
    d = _coerce_date(d)
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{str(start_year + 1)[2:]}"


def _period_key_for_date(d, reset_mode: str) -> str:
    """
    The period token used both as the UNIQUE-key component (so different
    periods get independent counters) and, for financial_year/monthly, as
    the segment shown in the formatted number.
    """
    d = _coerce_date(d)
    if reset_mode == "monthly":
        return d.strftime("%Y-%m")
    if reset_mode == "never":
        return RESET_NEVER_PERIOD_KEY
    return financial_year_for_date(d)  # "financial_year" and any unknown value


def _numbering_config(business_id: int) -> dict:
    """Reads this business's current, owner-editable numbering format
    settings once (falling back to the platform default, then the schema
    default, per utils.business_settings) - every value always resolves
    to something, so this never fails on a business that hasn't
    customized anything - see utils/business_settings.py."""
    reset_mode = get_business_setting(business_id, "doc_numbering_auto_reset").strip() or "financial_year"
    if reset_mode not in ("financial_year", "monthly", "never"):
        reset_mode = "financial_year"
    return {
        "separator":    get_business_setting(business_id, "doc_numbering_separator").strip() or "/",
        "suffix":       get_business_setting(business_id, "doc_numbering_suffix").strip(),
        "start_number": get_business_int_setting(business_id, "doc_numbering_start_number"),
        "digit_length": get_business_int_setting(business_id, "doc_numbering_digit_length"),
        "reset_mode":   reset_mode,
    }


def _format_number(prefix: str, period_key: str, sequence: int, cfg: dict) -> str:
    """
    Builds the display string from its components + the current format
    settings. For "never" reset mode there's no natural period segment to
    show (one continuous sequence has nothing to distinguish by period),
    so it's omitted - INV/000001 rather than INV/ALL/000001.
    """
    sep = cfg["separator"]
    seq_str = f"{sequence:0{cfg['digit_length']}d}"
    if cfg["reset_mode"] == "never":
        formatted = f"{prefix}{sep}{seq_str}"
    else:
        formatted = f"{prefix}{sep}{period_key}{sep}{seq_str}"
    return formatted + cfg["suffix"]


def generate_document_number(business_id: int, document_type: str, document_date=None,
                              manual_number: str = None) -> dict:
    """
    Generate the document number for this business + document type + the
    period `document_date` falls in (per this business's configured Auto
    Reset mode).

    Manual Numbering: if `manual_number` is a non-empty string AND this
    business's owner has turned on "Manual Numbering" in their Business
    Settings, that exact string is used as-is and the auto sequence
    counter is left untouched (so turning manual numbering back off later
    resumes auto-numbering right where it last left off, with no gap or
    gimmick needed). If Manual Numbering is off, `manual_number` is
    ignored and the normal auto-generated number is used instead.

    Returns a dict - never just the formatted string - so callers can
    store the components separately:
        {
          "formatted":       "INV/2026-27/000001",
          "prefix":          "INV",
          "financial_year":  "2026-27",   # the period key (see module docstring)
          "sequence":        1,           # None for a manual number
          "manual":          False,
        }

    Raises ValueError for an unknown document_type - callers should treat
    that as a programming error (a typo in the document_type argument),
    not something to silently default around.
    """
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(
            f"Unknown document_type {document_type!r} - must be one of {sorted(DOCUMENT_TYPES)}"
        )

    manual_number = (manual_number or "").strip()
    if manual_number and get_business_bool_setting(business_id, "doc_numbering_manual_enable"):
        return {
            "formatted": manual_number,
            "prefix": None,
            "financial_year": None,
            "sequence": None,
            "manual": True,
        }

    if document_date is None:
        document_date = date.today()
    cfg = _numbering_config(business_id)
    period_key = _period_key_for_date(document_date, cfg["reset_mode"])
    prefix = get_business_setting(business_id, DOCUMENT_TYPES[document_type]).strip() or document_type[:3].upper()

    with ledger_transaction() as (conn, c, p):
        # Single atomic statement - this is what makes it concurrency-safe.
        # Two simultaneous requests for the same (business, type, period)
        # both try to run this; the database itself serializes them via the
        # UNIQUE constraint + row lock, so one always executes strictly
        # before the other commits. Neither can ever read a stale
        # "last sequence" and both increment from it - there's no
        # read-then-write gap for a race to happen in, unlike the bug
        # Update_025 found and fixed in journal entry numbering.
        #
        # The Starting Number setting only feeds the INSERT branch (a
        # brand-new period's first row) - the DO UPDATE branch always
        # increments the existing value by 1 regardless of the current
        # Starting Number setting, so changing it mid-period never rewrites
        # a counter that has already started.
        c.execute(
            f"""INSERT INTO saas_document_sequences
                    (business_id, document_type, financial_year, last_sequence)
                VALUES ({p},{p},{p},{p})
                ON CONFLICT (business_id, document_type, financial_year)
                DO UPDATE SET last_sequence = saas_document_sequences.last_sequence + 1
                """ if _is_postgres() else
            f"""INSERT INTO saas_document_sequences
                    (business_id, document_type, financial_year, last_sequence)
                VALUES ({p},{p},{p},{p})
                ON CONFLICT (business_id, document_type, financial_year)
                DO UPDATE SET last_sequence = last_sequence + 1
                """,
            (business_id, document_type, period_key, cfg["start_number"])
        )
        # Reading it back within the SAME transaction, on the SAME
        # connection, after our own write - this is always guaranteed to
        # see our own just-written value ("read your own writes"),
        # regardless of what any other concurrent transaction is doing,
        # so no RETURNING clause is needed (kept portable to older SQLite
        # builds that predate RETURNING support, e.g. on Pydroid/Android).
        c.execute(
            f"""SELECT last_sequence FROM saas_document_sequences
                WHERE business_id={p} AND document_type={p} AND financial_year={p}""",
            (business_id, document_type, period_key)
        )
        row = c.fetchone()
        sequence = row["last_sequence"]

    formatted = _format_number(prefix, period_key, sequence, cfg)
    return {
        "formatted": formatted,
        "prefix": prefix,
        "financial_year": period_key,
        "sequence": sequence,
        "manual": False,
    }


def current_sequence_position(business_id: int, document_type: str, period_key: str = None) -> int:
    """
    Read-only: the last sequence number issued so far for this business +
    document type + period (0 if none yet). Never increments anything -
    for display/diagnostics only (e.g. "next invoice will be #126").

    `period_key` accepts a financial year, a month ("YYYY-MM"), or the
    "never" sentinel - whatever matches how the caller wants to look it
    up. Defaults to today's period under this business's *currently
    configured* Auto Reset mode when omitted.
    """
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(f"Unknown document_type {document_type!r}")
    if period_key is None:
        reset_mode = get_business_setting(business_id, "doc_numbering_auto_reset").strip() or "financial_year"
        period_key = _period_key_for_date(date.today(), reset_mode)

    from models.saas_auth import _is_postgres as _pg
    p = "%s" if _pg() else "?"
    row = saas_fetchone(
        f"""SELECT last_sequence FROM saas_document_sequences
            WHERE business_id={p} AND document_type={p} AND financial_year={p}""",
        (business_id, document_type, period_key)
    )
    return row["last_sequence"] if row else 0


def preview_next_document_number(business_id: int, document_type: str, document_date=None):
    """
    PREVIEW ONLY - shows what the next document number will look like
    without consuming a sequence number. Used by the POS / new-purchase
    screens to display an "Invoice #" / "Purchase #" badge before saving,
    and by the Business/App Admin Settings pages for their live preview.

    Returns None when Manual Numbering is enabled for this business
    (there's nothing to preview - the number is whatever staff types on
    save), so callers should show a "(entered manually on save)"
    placeholder in that case instead of calling this at all.
    """
    if get_business_bool_setting(business_id, "doc_numbering_manual_enable"):
        return None
    if document_type not in DOCUMENT_TYPES:
        raise ValueError(f"Unknown document_type {document_type!r}")

    if document_date is None:
        document_date = date.today()
    cfg = _numbering_config(business_id)
    period_key = _period_key_for_date(document_date, cfg["reset_mode"])
    prefix = get_business_setting(business_id, DOCUMENT_TYPES[document_type]).strip() or document_type[:3].upper()
    current = current_sequence_position(business_id, document_type, period_key)
    next_seq = current + 1 if current else cfg["start_number"]
    return _format_number(prefix, period_key, next_seq, cfg)
