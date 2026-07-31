"""
utils/hsn_master.py — Update_032/033: Complete HSN/SAC Master
============================================================================
Global (NOT tenant-scoped) reference data — every business shares the
same National HSN/SAC code list, the same way real GST law does. Every
business can filter/search it (and Update_033 lets a business narrow
that search to its own Business Type(s)), but only App Admin can ever
add, edit, or deactivate a code — see modules/app_admin/dashboard.py's
hsn_master() routes. This module is the single source of truth for
reading, searching, validating, and suggesting HSN/SAC codes across the
whole app (Products, Billing, Purchase entry, and the GST Validation
Engine in utils/gst_validation.py).

Consolidation note: before Update_032, HSN lookups were split across two
inconsistent code paths in modules/saas_business/products.py — one
correctly SaaS-aware, the other querying `models.database.get_db()`, the
legacy, Postgres-disconnected single-tenant connection (meaning that
second route silently returned nothing in a real Postgres production
deployment). Both are now routed through this module instead.

Fields (matching the "complete HSN/SAC master" requirement):
  hsn_code, description, default_gst_rate, category, unit,
  effective_date, is_service (goods vs. service), reverse_charge,
  tax_status (taxable/exempt/nil_rated/non_gst), itc_eligible, is_active,
  business_types (Update_033 — see BUSINESS_TYPES below).
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

# Update_033: the business-type taxonomy used both for a business's own
# hsn_business_types selection (saas_businesses) and for tagging which
# types an HSN/SAC code is commonly relevant to (hsn_master.business_types)
# — both stored as a comma-separated list of these slugs. A business
# selecting several types (e.g. a "Mobile Shop" that's also a "Distributor")
# sees HSN codes tagged with ANY of its selected types; an HSN code with
# an empty business_types list is untagged/general-purpose and always
# shows for everyone (see search_hsn()'s business_types filter) — this is
# what keeps the 55+ codes seeded before this update working unchanged
# for every business, with no migration needed to "fill in" their type.
BUSINESS_TYPES = [
    ("grocery", "Grocery"), ("electronics", "Electronics"), ("hardware", "Hardware"),
    ("medical_store", "Medical Store"), ("pharmacy", "Pharmacy"), ("garments", "Garments"),
    ("furniture", "Furniture"), ("automobile", "Automobile"), ("mobile_shop", "Mobile Shop"),
    ("restaurant", "Restaurant"), ("hotel", "Hotel"), ("bakery", "Bakery"),
    ("stationery", "Stationery"), ("agriculture", "Agriculture"), ("manufacturing", "Manufacturing"),
    ("distributor", "Distributor"), ("wholesaler", "Wholesaler"), ("retailer", "Retailer"),
    ("service_provider", "Service Provider"), ("construction", "Construction"),
    ("education", "Education"), ("healthcare", "Healthcare"), ("others", "Others"),
]
BUSINESS_TYPE_SLUGS = {slug for slug, _ in BUSINESS_TYPES}
BUSINESS_TYPE_LABELS = dict(BUSINESS_TYPES)


def _true():
    return "TRUE" if _is_postgres() else "1"


def parse_business_types(raw: str) -> list:
    """Comma-separated column value -> clean list of slugs, dropping
    anything not in BUSINESS_TYPE_SLUGS (defensive against stale/typo'd
    data rather than ever raising on read)."""
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip() in BUSINESS_TYPE_SLUGS]


def format_business_types(slugs: list) -> str:
    """List of slugs -> the comma-separated column value, deduplicated
    and restricted to known slugs."""
    clean = [s for s in dict.fromkeys(slugs) if s in BUSINESS_TYPE_SLUGS]
    return ",".join(clean)


def search_hsn(query: str = "", limit: int = 15, include_inactive: bool = False,
               business_types: list = None) -> list:
    """
    Powerful search — matches HSN code (full or partial), description
    (including multi-word phrases like "electric fan" — every word in
    the query must appear somewhere in the description), and category.
    Used by the Product form and the Sales/Purchase entry screens'
    autocomplete.

    `business_types`, if given (a business's own hsn_business_types, or
    an explicit filter), narrows results to codes tagged with ANY of
    those types, OR untagged/general-purpose codes (empty
    business_types on the HSN row) — so a business never loses access to
    a general code just because it hasn't been categorized yet. Pass
    None or [] for the unfiltered "Show All" behavior.

    Active codes only by default: a code an admin has since deactivated
    shouldn't be *offered* for a brand-new line, even though any
    document that already references it keeps displaying/working
    correctly (see get_hsn() below, which never filters by is_active).
    """
    p = P()
    active_clause = "" if include_inactive else f"AND is_active = {_true()}"

    bt_clause = ""
    bt_params = []
    if business_types:
        # Match if the HSN's business_types column contains ANY of the
        # requested types, OR is empty (general-purpose/untagged).
        # LIKE '%,slug,%' against a ",<list>," padded value would be
        # cleaner, but the column is stored as a plain "a,b,c" string
        # (no leading/trailing comma) to keep create_hsn/update_hsn
        # simple — so each slug is matched as a whole comma-delimited
        # token via three LIKE variants (only-item / first-item /
        # last-or-middle-item) instead.
        ors = []
        for slug in business_types:
            ors.append(f"(business_types = {p} OR business_types LIKE {p} OR business_types LIKE {p} OR business_types LIKE {p})")
            bt_params.extend([slug, f"{slug},%", f"%,{slug}", f"%,{slug},%"])
        bt_clause = f"AND (business_types = '' OR business_types IS NULL OR {' OR '.join(ors)})"

    if query:
        query = query.strip()
        words = query.split()
        like_code = f"%{query}%"
        # Every word in a multi-word query must appear in the description
        # (order-independent — "electric fan" matches "Electric ceiling fan").
        desc_word_clauses = " AND ".join([f"description LIKE {p}"] * len(words))
        desc_params = [f"%{w}%" for w in words]
        rows = saas_fetchall(
            f"""SELECT * FROM hsn_master
                WHERE (hsn_code LIKE {p} OR ({desc_word_clauses}) OR category LIKE {p})
                {active_clause} {bt_clause}
                ORDER BY (CASE WHEN hsn_code LIKE {p} THEN 0 ELSE 1 END), hsn_code
                LIMIT {p}""",
            (like_code, *desc_params, like_code, *bt_params, like_code, limit)
        )
    else:
        rows = saas_fetchall(
            f"SELECT * FROM hsn_master WHERE 1=1 {active_clause} {bt_clause} ORDER BY hsn_code LIMIT {p}",
            (*bt_params, limit)
        )
    return [dict(r) for r in rows]


def suggest_hsn_from_description(product_name: str, business_types: list = None, limit: int = 5) -> list:
    """
    Update_033 — "Suggest HSN automatically from description": matches
    a product's NAME (the closest thing to a "description" a product
    has at creation time) against the HSN master, to offer candidate
    codes while the person is still typing the product name — before
    they've touched the HSN field at all. Pure suggestion, never
    auto-fills or blocks — the product form still lets the person pick
    a different code or leave it blank (see modules/saas_business/
    products.py's add()/edit(), which only ever WARNS about an HSN/rate
    mismatch, never rejects it).

    Deliberately NOT the same matching as search_hsn()'s explicit
    search box: search_hsn() requires every word in the query to appear
    in the description (correct for a deliberate, narrowing search like
    "electric fan"). A product NAME routinely has extra words a generic
    HSN description will never contain — sizes, models, colors, brand
    ("Electric Table Fan 400mm White") — so requiring an exact
    all-words match would almost always return nothing. This instead
    matches on ANY significant word (3+ letters, skipping pure-number
    tokens like "400mm"'s digits) and ranks by how many words matched,
    most-relevant first.
    """
    name = (product_name or "").strip()
    if len(name) < 3:
        return []

    words = [w for w in name.split() if len(w) >= 3 and not w.isdigit()]
    if not words:
        return []

    p = P()
    active_clause = f"AND is_active = {_true()}"
    bt_clause = ""
    bt_params = []
    if business_types:
        ors = []
        for slug in business_types:
            ors.append(f"(business_types = {p} OR business_types LIKE {p} OR business_types LIKE {p} OR business_types LIKE {p})")
            bt_params.extend([slug, f"{slug},%", f"%,{slug}", f"%,{slug},%"])
        bt_clause = f"AND (business_types = '' OR business_types IS NULL OR {' OR '.join(ors)})"

    # One CASE-WHEN-matched-then-1-else-0 term per word, summed, so
    # results with more matching words rank first — a plain OR would
    # give no way to prefer "Electric Table Fan" (3/3 words matched)
    # over a code that only happens to match "Table" (1/3).
    score_terms = " + ".join([f"(CASE WHEN description LIKE {p} THEN 1 ELSE 0 END)" for _ in words])
    or_clause = " OR ".join([f"description LIKE {p}" for _ in words])
    like_params = [f"%{w}%" for w in words]

    rows = saas_fetchall(
        f"""SELECT *, ({score_terms}) as match_score
            FROM hsn_master
            WHERE ({or_clause}) {active_clause} {bt_clause}
            ORDER BY match_score DESC, hsn_code
            LIMIT {p}""",
        (*like_params, *like_params, *bt_params, limit)
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

def get_business_hsn_types(business_id: int) -> list:
    """This business's own selected HSN business-type tag(s) — set from
    Business Settings (see modules/saas_auth/routes.py). Empty list
    means the business hasn't picked any yet, which search_hsn()
    treats the same as "Show All" (no filtering applied)."""
    row = saas_fetchone(f"SELECT hsn_business_types FROM saas_businesses WHERE id={P()}", (business_id,))
    return parse_business_types(row["hsn_business_types"]) if row else []


def set_business_hsn_types(business_id: int, business_types: list) -> None:
    saas_execute(
        f"UPDATE saas_businesses SET hsn_business_types={P()} WHERE id={P()}",
        (format_business_types(business_types), business_id),
        returning=None
    )


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
             is_service, reverse_charge, tax_status, itc_eligible, is_active, business_types)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (code, description, gst_rate, (data.get("category") or "").strip(),
         (data.get("unit") or "").strip(), (data.get("effective_date") or "").strip(),
         bool(data.get("is_service")), bool(data.get("reverse_charge")),
         tax_status, bool(data.get("itc_eligible", True)), bool(data.get("is_active", True)),
         format_business_types(data.get("business_types") or []))
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
                tax_status={p}, itc_eligible={p}, is_active={p}, business_types={p}
            WHERE id={p}""",
        (data.get("description", "").strip(), gst_rate, (data.get("category") or "").strip(),
         (data.get("unit") or "").strip(), (data.get("effective_date") or "").strip(),
         bool(data.get("is_service")), bool(data.get("reverse_charge")),
         tax_status, bool(data.get("itc_eligible", True)), bool(data.get("is_active", True)),
         format_business_types(data.get("business_types") or []),
         hsn_id),
        returning=None
    )
