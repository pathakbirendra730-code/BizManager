"""
utils/tax_helpers.py — Shared GST & date utilities
====================================================
Pure, tenant-agnostic helpers used across the SaaS-native business
modules. Split out from the old utils/helpers.py, which mixed these
in with legacy single-tenant shop/auth decorators that no longer exist.
"""

from datetime import datetime


def calculate_gst(unit_price: float, quantity: float,
                  gst_rate: float, supply_type: str = "intra",
                  item_discount: float = 0, is_inclusive: bool = False) -> dict:
    """
    Core GST calculation engine — supports both pricing conventions
    (Update_029):

      is_inclusive=False (default, "Tax Exclusive" — the original,
      unchanged behavior): `unit_price` is the taxable rate; GST is
      calculated on top of it. Grand Total = Taxable + GST. This branch
      is byte-for-byte the original pre-Update_029 code path — CGST and
      SGST are each computed independently from the taxable value (not
      derived by splitting a combined total_tax in half), because that
      independent-rounding order is what every existing invoice/purchase
      in the database was already computed with. Splitting a
      pre-combined total_tax instead diverges by a paisa in a large
      fraction of real amounts (verified: ~48% of cent-value/GST-rate
      combinations tested), so this exact order is preserved deliberately
      to avoid changing historical/regression behavior, not by oversight.

      is_inclusive=True ("Tax Inclusive"): `unit_price` is the GST-
      inclusive rate the customer/supplier actually pays. The taxable
      value is DERIVED by division, and total_tax is computed as the
      REMAINDER (gross − taxable) — never by independently calculating
      gst_rate% and adding it to the entered figure, which is exactly
      what would silently double-count GST on an already-inclusive rate.
      This guarantees Grand Total == the entered amount exactly, always.

    supply_type: 'intra'  → CGST + SGST  (each = gst_rate / 2)
                 'inter'  → IGST         (= gst_rate)

    Returns dict with all GST components, plus:
      - `subtotal` — always unit_price * quantity, the raw entered
        amount, identical in both modes (interpretation differs, the
        number doesn't).
      - `taxable_per_unit` — the per-unit taxable rate net of
        item_discount, for costing purposes (inventory valuation must
        only ever use taxable value, never a GST-inclusive rate — see
        modules/saas_business/purchase.py).
      - `is_inclusive` — echoes the mode this result was computed under,
        so callers/records can self-describe without re-deriving it.
    """
    subtotal      = round(unit_price * quantity, 2)
    disc_amount   = round(subtotal * item_discount / 100, 2) if item_discount else 0

    if is_inclusive:
        # ── Tax Inclusive: derive, never add ────────────────────────────
        # `gross` is the GST-inclusive amount left after any item-level
        # discount — this is what the customer/supplier actually pays for
        # this line, and total (below) always equals it exactly.
        gross     = round(subtotal - disc_amount, 2)
        taxable   = round(gross / (1 + gst_rate / 100), 2) if gst_rate else gross
        total_tax = round(gross - taxable, 2)  # remainder, not an independent calc — see docstring

        if supply_type == "inter":
            igst_rate  = gst_rate
            igst_amt   = total_tax
            cgst_rate  = cgst_amt = sgst_rate = sgst_amt = 0.0
        else:
            cgst_rate  = sgst_rate = round(gst_rate / 2, 2)
            cgst_amt   = round(total_tax / 2, 2)
            sgst_amt   = round(total_tax - cgst_amt, 2)  # remainder — guarantees cgst+sgst == total_tax exactly
            igst_rate  = igst_amt = 0.0

        total = round(taxable + total_tax, 2)  # == gross, always — GST is never added a second time here
    else:
        # ── Tax Exclusive: ORIGINAL, unmodified code path ───────────────
        taxable       = round(subtotal - disc_amount, 2)

        if supply_type == "inter":
            igst_rate  = gst_rate
            igst_amt   = round(taxable * igst_rate / 100, 2)
            cgst_rate  = cgst_amt = sgst_rate = sgst_amt = 0.0
        else:
            cgst_rate  = sgst_rate = round(gst_rate / 2, 2)
            cgst_amt   = round(taxable * cgst_rate / 100, 2)
            sgst_amt   = round(taxable * sgst_rate / 100, 2)
            igst_rate  = igst_amt = 0.0

        total_tax = round(cgst_amt + sgst_amt + igst_amt, 2)
        total     = round(taxable + total_tax, 2)

    return {
        "subtotal":         subtotal,
        "disc_amount":      disc_amount,
        "taxable":          taxable,
        "taxable_per_unit": round(taxable / quantity, 2) if quantity else 0.0,
        "gst_rate":         gst_rate,
        "cgst_rate":        cgst_rate,
        "sgst_rate":        sgst_rate,
        "igst_rate":        igst_rate,
        "cgst_amount":      cgst_amt,
        "sgst_amount":      sgst_amt,
        "igst_amount":      igst_amt,
        "total_tax":        total_tax,
        "total":            total,
        "is_inclusive":     is_inclusive,
    }


def determine_supply_type(business_state: str, customer_state: str) -> str:
    """
    Return 'intra' if both states match, else 'inter'.
    Empty customer state defaults to intra (B2C local).
    """
    if not customer_state or business_state == customer_state:
        return "intra"
    return "inter"


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
