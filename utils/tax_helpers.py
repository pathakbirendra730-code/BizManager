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


def apply_freight_and_roundoff(taxable_tot: float, cgst_tot: float, sgst_tot: float, igst_tot: float,
                                freight_amount: float = 0, freight_gst_rate: float = 18,
                                supply_type: str = "intra", round_off_enabled: bool = False) -> dict:
    """
    Update_032: adds Freight/Other Charges and an optional Round-Off
    adjustment on top of already-computed item totals — the last step
    before a document's Grand Total, for both Sales and Purchase.

    Freight: taxed exactly like any other line (through the same
    calculate_gst() engine, same supply_type, so it correctly becomes
    CGST+SGST or IGST depending on intra/inter-state) at
    `freight_gst_rate`, then folded into the running taxable/cgst/sgst/
    igst totals ONCE — never calculated twice, and never bypassing the
    engine with ad hoc arithmetic.

    Round-Off: a pure, GST-free arithmetic nudge to the nearest whole
    rupee on the FINAL total only (standard Indian retail/GST invoicing
    practice) — it never touches taxable value or any tax component, so
    it can never be mistaken for (or accidentally create) additional GST
    liability. Disabled by default (`round_off_enabled=False`) so every
    existing caller that doesn't explicitly opt in gets `round_off=0` and
    an unchanged total — this function is purely additive to the
    existing calculation flow, never a required step.

    Returns every component needed both to display an itemized
    breakdown (Freight Taxable/CGST/SGST/IGST as their own line) and to
    reverse it exactly later — see returns.py's proportional-slice
    design, which this is built to be compatible with (a future Credit/
    Debit Note against a document with freight can slice
    `freight_taxable` the same way it already slices item taxable
    values).
    """
    freight_amount = round(float(freight_amount or 0), 2)
    freight_taxable = freight_cgst = freight_sgst = freight_igst = 0.0
    if freight_amount:
        fg = calculate_gst(freight_amount, 1, freight_gst_rate, supply_type)
        freight_taxable = fg["taxable"]
        freight_cgst, freight_sgst, freight_igst = fg["cgst_amount"], fg["sgst_amount"], fg["igst_amount"]

    new_taxable   = round(taxable_tot + freight_taxable, 2)
    new_cgst      = round(cgst_tot + freight_cgst, 2)
    new_sgst      = round(sgst_tot + freight_sgst, 2)
    new_igst      = round(igst_tot + freight_igst, 2)
    new_total_tax = round(new_cgst + new_sgst + new_igst, 2)
    pre_roundoff_total = round(new_taxable + new_total_tax, 2)

    round_off   = 0.0
    final_total = pre_roundoff_total
    if round_off_enabled:
        final_total = round(pre_roundoff_total)
        round_off   = round(final_total - pre_roundoff_total, 2)

    return {
        "taxable":            new_taxable,
        "cgst_amount":        new_cgst,
        "sgst_amount":        new_sgst,
        "igst_amount":        new_igst,
        "total_tax":          new_total_tax,
        "freight_amount":     freight_amount,
        "freight_taxable":    freight_taxable,
        "freight_cgst":       freight_cgst,
        "freight_sgst":       freight_sgst,
        "freight_igst":       freight_igst,
        "pre_roundoff_total": pre_roundoff_total,
        "round_off":          round_off,
        "total":              final_total,
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
