"""
utils/hsn_master.py — Update_032: Complete HSN/SAC Master
============================================================================
Global (NOT tenant-scoped) reference data — every business shares the
same HSN/SAC code list, the same way real GST law does. This module is
the single source of truth for reading, searching, and validating HSN/
SAC codes across the whole app (Products, Billing, Purchase entry, and
the GST Validation Engine in utils/gst_validation.py).

Consolidation note: before this update, HSN lookups were split across
two inconsistent code paths in modules/saas_business/products.py — one
correctly SaaS-aware (`get_hsn_master()` in models/saas_business_data.py),
the other querying `models.database.get_db()`, the legacy, Postgres-
disconnected single-tenant connection (meaning that second route silently
returned nothing in a real Postgres production deployment). Both are now
routed through this module instead — see products.py's two `/api/hsn*`
routes.

Fields (matching the "complete HSN/SAC master" requirement):
  hsn_code, description, default_gst_rate, category, unit,
  effective_date, is_service (goods vs. service), reverse_charge,
  tax_status (taxable/exempt/nil_rated/non_gst), itc_eligible, is_active.
"""

from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres

P = lambda: "%s" if _is_postgres() else "?"

TAX_STATUSES = ["taxable", "exempt", "nil_rated", "non_gst"]
TAX_STATUS_LABELS = {
    "taxable":   "Taxable",
    "exempt":    "Exempt",
    "nil_rated": "Nil Rated",
    "non_gst":   "Non-GST (out of scope of GST)",
}


def _true():
    return "TRUE" if _is_postgres() else "1"


def search_hsn(query: str = "", limit: int = 15, include_inactive: bool = False) -> list:
    """
    Autocomplete search by code or description — used by the Product
    form and the Sales/Purchase entry screens. Active codes only by
    default: a code an admin has since deactivated shouldn't be
    *offered* for a brand-new line, even though any document that
    already references it keeps displaying/working correctly (see
    get_hsn() below, which never filters by is_active).
    """
    p = P()
    active_clause = "" if include_inactive else f"AND is_active = {_true()}"
    if query:
        like = f"%{query.strip()}%"
        rows = saas_fetchall(
            f"""SELECT * FROM hsn_master
                WHERE (hsn_code LIKE {p} OR description LIKE {p}) {active_clause}
                ORDER BY hsn_code LIMIT {p}""",
            (like, like, limit)
        )
    else:
        rows = saas_fetchall(
            f"SELECT * FROM hsn_master WHERE 1=1 {active_clause} ORDER BY hsn_code LIMIT {p}",
            (limit,)
        )
    return [dict(r) for r in rows]


def get_hsn(hsn_code: str):
    """
    Exact lookup — returns None if not found. Deliberately never filters
    by is_active: an existing invoice/purchase that references a
    since-deactivated code must still be able to look it up (for
    display, for the GST Health Check, for reprinting a past document)
    even though search_hsn() stops offering it for new entries.
    """
    code = (hsn_code or "").strip()
    if not code:
        return None
    row = saas_fetchone(f"SELECT * FROM hsn_master WHERE hsn_code={P()}", (code,))
    return dict(row) if row else None


def validate_hsn_code_format(hsn_code: str) -> str:
    """
    Structural check only (length/digits) — returns an error string, or
    "" if the format is fine. HSN codes are 4, 6, or 8 digits under
    India's GST law (SAC — services — codes are 6 digits, conventionally
    written with a "99" prefix); this only checks the *shape*, not
    whether the specific code exists in this app's local master (a
    perfectly legal HSN code can be absent from a curated local list —
    see validate_hsn_for_transaction() below for that distinction).
    """
    code = (hsn_code or "").strip()
    if not code:
        return "HSN/SAC code is required."
    if not code.isdigit():
        return f"'{code}' should contain digits only."
    if len(code) not in (4, 6, 8):
        return f"'{code}' should be 4, 6, or 8 digits (got {len(code)})."
    return ""


def validate_hsn_for_transaction(hsn_code: str, gst_rate=None) -> list:
    """
    Returns a list of human-readable issue strings for a specific
    Sales/Purchase line — empty list means no issues found. Called by
    the GST Validation Engine (utils/gst_validation.py) before a
    document is saved.

    Deliberately returns WARNINGS the caller can choose how to treat,
    not hard exceptions raised from inside this function: an HSN code
    absent from this app's local master is common and legal (India's
    full CBIC list runs to thousands of codes; this app ships a curated
    subset, not the complete list), so "not found locally" is a
    warning, not a rejection. Only a structurally malformed code (not
    4/6/8 digits) is treated as a hard problem by the caller.
    """
    issues = []
    fmt_error = validate_hsn_code_format(hsn_code)
    if fmt_error:
        issues.append(fmt_error)
        return issues  # no point checking further against the master

    info = get_hsn(hsn_code)
    if info is None:
        issues.append(f"HSN/SAC '{hsn_code}' isn't in the HSN master yet — rate/classification can't be cross-checked.")
        return issues

    if not info["is_active"]:
        issues.append(f"HSN {hsn_code} is marked inactive in the HSN master.")

    if info["tax_status"] != "taxable":
        if gst_rate not in (None, "") and float(gst_rate or 0) != 0:
            issues.append(
                f"HSN {hsn_code} is classified '{TAX_STATUS_LABELS.get(info['tax_status'], info['tax_status'])}' "
                f"but a non-zero GST rate ({gst_rate}%) was entered."
            )
    elif gst_rate not in (None, ""):
        try:
            if abs(float(gst_rate) - float(info["default_gst_rate"])) > 0.01:
                issues.append(
                    f"HSN {hsn_code}'s master rate is {info['default_gst_rate']}%, "
                    f"but {gst_rate}% was entered on this line — confirm this is intentional."
                )
        except (TypeError, ValueError):
            pass

    return issues


# ═══════════════════════════════ ADMIN CRUD ════════════════════════════════
# HSN master is global/shared reference data, same tier as
# utils/platform_settings.py — managed from App Admin, not per-business.

def list_all_hsn(search: str = "", limit: int = 500) -> list:
    """Full listing for the Admin HSN Master page — includes inactive
    codes (an admin needs to see and be able to reactivate those)."""
    return search_hsn(search, limit=limit, include_inactive=True)


def create_hsn(data: dict, ) -> int:
    """Create a new HSN/SAC master entry. Raises ValueError on a
    duplicate code or malformed input — the caller (the Admin route)
    shows this directly to the user."""
    code = (data.get("hsn_code") or "").strip()
    fmt_error = validate_hsn_code_format(code)
    if fmt_error:
        raise ValueError(fmt_error)
    if get_hsn(code):
        raise ValueError(f"HSN/SAC code {code} already exists.")

    description = (data.get("description") or "").strip()
    if not description:
        raise ValueError("Description is required.")

    tax_status = data.get("tax_status", "taxable")
    if tax_status not in TAX_STATUSES:
        raise ValueError(f"Invalid tax status: {tax_status}")

    try:
        gst_rate = float(data.get("default_gst_rate", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("GST rate must be a number.")

    p = P()
    return saas_execute(
        f"""INSERT INTO hsn_master
            (hsn_code, description, default_gst_rate, category, unit, effective_date,
             is_service, reverse_charge, tax_status, itc_eligible, is_active)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (code, description, gst_rate, (data.get("category") or "").strip(),
         (data.get("unit") or "").strip(), (data.get("effective_date") or "").strip(),
         bool(data.get("is_service")), bool(data.get("reverse_charge")),
         tax_status, bool(data.get("itc_eligible", True)), bool(data.get("is_active", True)))
    )


def update_hsn(hsn_id: int, data: dict) -> None:
    """Update an existing HSN/SAC master entry. Never changes hsn_code
    itself (the code is the stable identifier every invoice/purchase
    item and product references) — only its descriptive/classification
    fields, and deliberately doesn't retroactively touch anything
    already saved on a past document (see the "stamp it, don't
    recompute it" principle used throughout this app's GST engine)."""
    tax_status = data.get("tax_status", "taxable")
    if tax_status not in TAX_STATUSES:
        raise ValueError(f"Invalid tax status: {tax_status}")
    try:
        gst_rate = float(data.get("default_gst_rate", 0) or 0)
    except (TypeError, ValueError):
        raise ValueError("GST rate must be a number.")

    p = P()
    saas_execute(
        f"""UPDATE hsn_master SET
                description={p}, default_gst_rate={p}, category={p}, unit={p},
                effective_date={p}, is_service={p}, reverse_charge={p},
                tax_status={p}, itc_eligible={p}, is_active={p}
            WHERE id={p}""",
        (data.get("description", "").strip(), gst_rate, (data.get("category") or "").strip(),
         (data.get("unit") or "").strip(), (data.get("effective_date") or "").strip(),
         bool(data.get("is_service")), bool(data.get("reverse_charge")),
         tax_status, bool(data.get("itc_eligible", True)), bool(data.get("is_active", True)),
         hsn_id),
        returning=None
    )
