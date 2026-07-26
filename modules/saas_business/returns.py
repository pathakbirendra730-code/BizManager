"""
modules/saas_business/returns.py — Update_030: Credit/Debit Notes & Returns
============================================================================
Full commercial return workflow (Tally/Busy/Zoho-style):

  Sales Return    → Credit Note  (CN/FY/000001)  — utils.ledger_transactions.record_sales_return()
  Purchase Return → Debit Note   (DN/FY/000001)  — utils.ledger_transactions.record_purchase_return()

Both ledger primitives already existed (added by an earlier update) but
were unreachable dead code — no route in the app called them. This module
is what actually wires them up.

Design:

  1. A return is always made AGAINST a specific original invoice/purchase,
     never as a free-standing document. Every returned line references
     the exact original line item (`invoice_item_id` / `purchase_item_id`)
     it reverses.

  2. The GST/taxable split on a return is a PROPORTIONAL SLICE of the
     original line's own already-computed, already-stored breakdown —
     never independently recalculated via calculate_gst(). If 3 of 10
     units are returned, the return reverses exactly 30% of that line's
     stored taxable_amount/cgst_amount/sgst_amount/igst_amount. This is
     what makes returns correct under BOTH Tax Exclusive and Tax
     Inclusive automatically, with no tax-mode-aware logic needed here
     at all: whatever mode was used to compute the original figure is
     already baked into it, so slicing it proportionally carries that
     correctness forward without re-deriving anything.

  3. returned_quantity on the original line item is the single source of
     truth for how much of that line has already been returned (across
     any number of partial returns) — checked to prevent over-returning,
     and read by the P&L's COGS calculation (accounts.py) to net returned
     units out of "quantity sold" so COGS reverses in lockstep with the
     revenue reversal. See models/saas_business_data.py's migration
     comment for the full rationale.

  4. Stock movement is immediate and unconditional: a sales return always
     increases stock (goods physically came back), a purchase return
     always decreases it (goods physically left) — regardless of
     refund_method. product.cost_price is never touched by a return; a
     return adjusts quantity, not valuation basis.

  5. Ledger posting reuses record_sales_return()/record_purchase_return()
     verbatim — this module's only job is computing the correct
     taxable/cgst/sgst/igst figures to hand them and keeping the
     documents/stock/returned_quantity bookkeeping in sync in the same
     database transaction.

Permissions: create_credit_note / create_debit_note → manager and above
(same tier as create_invoice/manage_purchase — a return, like the
original sale/purchase, is a manager-level action; delete_invoice-style
"undo a return" is intentionally not offered — see §7 in
CHANGELOG_Update_030.md for why).
"""

from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from models.saas_auth import saas_fetchone, saas_fetchall, saas_execute, _is_postgres
from utils.saas_helpers import saas_business_required, validate_csrf, audit_log
from utils.saas_middleware import permission_required, get_tenant_id, assert_tenant_access
from utils.document_numbering import generate_document_number
from utils.ledger_transactions import record_sales_return, record_purchase_return

saas_returns_bp = Blueprint("saas_returns", __name__, url_prefix="/biz/returns")

P = lambda: "%s" if _is_postgres() else "?"


# ═══════════════════════════ SALES RETURN / CREDIT NOTE ═══════════════════════

@saas_returns_bp.route("/sales")
@saas_business_required
@permission_required("view_invoice")
def sales_returns_list():
    biz_id = get_tenant_id()
    p = P()
    notes = saas_fetchall(
        f"""SELECT * FROM saas_credit_notes WHERE business_id={p}
            ORDER BY created_at DESC""",
        (biz_id,)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/sales_returns_list.html", biz=biz, notes=notes)


@saas_returns_bp.route("/sales/<int:invoice_id>/new")
@saas_business_required
@permission_required("create_credit_note")
def sales_return_new(invoice_id):
    biz_id = get_tenant_id()
    p = P()

    invoice = saas_fetchone(
        f"SELECT * FROM saas_invoices WHERE id={p} AND business_id={p} AND status != 'cancelled'",
        (invoice_id, biz_id)
    )
    if not invoice:
        flash("Invoice not found, or it has been cancelled.", "danger")
        return redirect(url_for("saas_billing.history"))
    assert_tenant_access(invoice["business_id"])

    raw_items = saas_fetchall(
        f"SELECT * FROM saas_invoice_items WHERE invoice_id={p} AND business_id={p} ORDER BY id",
        (invoice_id, biz_id)
    )
    items = []
    for it in raw_items:
        remaining = round(float(it["quantity"]) - float(it["returned_quantity"] or 0), 4)
        if remaining <= 0.0001:
            continue  # fully returned already — nothing left to offer
        row = dict(it)
        row["remaining_qty"] = remaining
        items.append(row)

    if not items:
        flash("Every item on this invoice has already been fully returned.", "warning")
        return redirect(url_for("saas_billing.view_invoice", inv_id=invoice_id))

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/sales_return_new.html",
                           biz=biz, invoice=invoice, items=items)


@saas_returns_bp.route("/sales/<int:invoice_id>/save", methods=["POST"])
@saas_business_required
@permission_required("create_credit_note")
def sales_return_save(invoice_id):
    biz_id = get_tenant_id()
    p = P()

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data received"}), 400
    if not validate_csrf(data.get("csrf_token")):
        return jsonify({"success": False, "message": "Security error. Please refresh and try again."}), 403

    invoice = saas_fetchone(
        f"SELECT * FROM saas_invoices WHERE id={p} AND business_id={p} AND status != 'cancelled'",
        (invoice_id, biz_id)
    )
    if not invoice:
        return jsonify({"success": False, "message": "Invoice not found or cancelled."}), 404
    assert_tenant_access(invoice["business_id"])

    return_lines = data.get("items", [])
    reason        = (data.get("reason") or "").strip()
    refund_method = data.get("refund_method", "credit")
    user_id       = session.get("saas_user_id")

    if refund_method == "credit" and not invoice["customer_id"]:
        return jsonify({"success": False,
            "message": "This invoice has no customer on file — choose a cash/bank refund instead."}), 400

    try:
        result = _create_credit_note(biz_id, invoice, return_lines, reason, refund_method, user_id)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    audit_log("credit_note_created", business_id=biz_id,
              entity_type="credit_note", entity_id=str(result["credit_note_id"]),
              detail=f"number={result['formatted']} against_invoice={invoice['invoice_number']}")

    return jsonify({"success": True, "credit_note_id": result["credit_note_id"],
                    "credit_note_number": result["formatted"], "total": result["total"]})


def _create_credit_note(biz_id, invoice, return_lines, reason, refund_method, user_id):
    """
    Core Sales Return logic — shared by the JSON save route. Raises
    ValueError (safe to show directly to the user) for any validation
    failure. Every write here happens after all validation passes, so a
    rejected return never partially applies.
    """
    p = P()
    if not return_lines:
        raise ValueError("No items selected to return.")

    line_calcs = []
    taxable_sum = cgst_sum = sgst_sum = igst_sum = 0.0

    for req in return_lines:
        try:
            qty_to_return = float(req.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if qty_to_return <= 0:
            continue

        orig = saas_fetchone(
            f"SELECT * FROM saas_invoice_items WHERE id={p} AND invoice_id={p} AND business_id={p}",
            (req.get("invoice_item_id"), invoice["id"], biz_id)
        )
        if not orig:
            raise ValueError("One of the selected items no longer matches this invoice.")

        orig_qty = float(orig["quantity"])
        already_returned = float(orig["returned_quantity"] or 0)
        remaining = round(orig_qty - already_returned, 4)
        if qty_to_return > remaining + 1e-6:
            raise ValueError(
                f"Cannot return {qty_to_return:g} of '{orig['product_name']}' — "
                f"only {remaining:g} remaining (already returned: {already_returned:g})."
            )

        # Proportional slice of the ORIGINAL line's own stored breakdown —
        # see module docstring §2 for why this is correct under both tax
        # modes with no re-derivation needed.
        fraction = qty_to_return / orig_qty if orig_qty else 0
        item_taxable = round(float(orig["taxable_amount"]) * fraction, 2)
        item_cgst    = round(float(orig["cgst_amount"])    * fraction, 2)
        item_sgst    = round(float(orig["sgst_amount"])    * fraction, 2)
        item_igst    = round(float(orig["igst_amount"])    * fraction, 2)
        item_total   = round(item_taxable + item_cgst + item_sgst + item_igst, 2)

        taxable_sum += item_taxable; cgst_sum += item_cgst
        sgst_sum    += item_sgst;    igst_sum += item_igst

        line_calcs.append({
            "invoice_item_id": orig["id"], "product_id": orig["product_id"],
            "product_name": orig["product_name"], "hsn_code": orig["hsn_code"],
            "quantity": qty_to_return, "unit_price": orig["unit_price"],
            "taxable_amount": item_taxable, "gst_rate": orig["gst_rate"],
            "cgst_rate": orig["cgst_rate"], "sgst_rate": orig["sgst_rate"], "igst_rate": orig["igst_rate"],
            "cgst_amount": item_cgst, "sgst_amount": item_sgst, "igst_amount": item_igst,
            "total_price": item_total,
        })

    if not line_calcs:
        raise ValueError("No valid return quantity entered.")

    taxable_sum = round(taxable_sum, 2); cgst_sum = round(cgst_sum, 2)
    sgst_sum    = round(sgst_sum, 2);    igst_sum = round(igst_sum, 2)
    total_tax   = round(cgst_sum + sgst_sum + igst_sum, 2)
    grand_total = round(taxable_sum + total_tax, 2)

    doc = generate_document_number(biz_id, "credit_note", date.today().isoformat())

    cn_id = saas_execute(
        f"""INSERT INTO saas_credit_notes
            (business_id, credit_note_number, doc_prefix, doc_fy, doc_sequence,
             invoice_id, invoice_number, customer_id, customer_name, customer_gstin,
             customer_state, supply_type, reason, taxable_amount, cgst_amount,
             sgst_amount, igst_amount, total_tax, total, refund_method, tax_mode, created_by)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (biz_id, doc["formatted"], doc["prefix"], doc["financial_year"], doc["sequence"],
         invoice["id"], invoice["invoice_number"], invoice["customer_id"], invoice["customer_name"],
         invoice["customer_gstin"], invoice["customer_state"], invoice["supply_type"], reason,
         taxable_sum, cgst_sum, sgst_sum, igst_sum, total_tax, grand_total, refund_method,
         invoice["tax_mode"], user_id)
    )

    for line in line_calcs:
        saas_execute(
            f"""INSERT INTO saas_credit_note_items
                (credit_note_id, business_id, invoice_item_id, product_id, product_name,
                 hsn_code, quantity, unit_price, taxable_amount, gst_rate,
                 cgst_rate, sgst_rate, igst_rate, cgst_amount, sgst_amount, igst_amount, total_price)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (cn_id, biz_id, line["invoice_item_id"], line["product_id"], line["product_name"],
             line["hsn_code"], line["quantity"], line["unit_price"], line["taxable_amount"],
             line["gst_rate"], line["cgst_rate"], line["sgst_rate"], line["igst_rate"],
             line["cgst_amount"], line["sgst_amount"], line["igst_amount"], line["total_price"])
        )
        # returned_quantity: the source of truth preventing over-return
        # across multiple partial returns, and read by the P&L's COGS
        # calculation (accounts.py) to net returned units out of "sold".
        saas_execute(
            f"""UPDATE saas_invoice_items SET returned_quantity = returned_quantity + {p}
                WHERE id={p} AND business_id={p}""",
            (line["quantity"], line["invoice_item_id"], biz_id)
        )
        # Stock always increases on a sales return — goods physically
        # came back. cost_price is deliberately untouched (a return
        # adjusts quantity, not valuation basis).
        if line["product_id"]:
            saas_execute(
                f"""UPDATE saas_products SET stock_quantity = stock_quantity + {p}, updated_at={p}
                    WHERE id={p} AND business_id={p}""",
                (line["quantity"], datetime.utcnow().isoformat(), line["product_id"], biz_id)
            )

    # Reverse Sales, Reverse Output GST, Reverse Customer Balance — all in
    # one atomic double-entry posting (see utils/ledger_transactions.py).
    record_sales_return(
        biz_id, taxable_sum, customer_id=invoice["customer_id"], customer_name=invoice["customer_name"],
        cgst=cgst_sum, sgst=sgst_sum, igst=igst_sum, refund_method=refund_method,
        source_id=cn_id, narration=f"Sales return against {invoice['invoice_number']} ({doc['formatted']})",
        created_by=user_id
    )

    return {"credit_note_id": cn_id, "formatted": doc["formatted"], "total": grand_total}


@saas_returns_bp.route("/sales/note/<int:cn_id>")
@saas_business_required
@permission_required("view_invoice")
def view_credit_note(cn_id):
    biz_id = get_tenant_id()
    p = P()
    note = saas_fetchone(f"SELECT * FROM saas_credit_notes WHERE id={p} AND business_id={p}", (cn_id, biz_id))
    if not note:
        flash("Credit note not found.", "danger")
        return redirect(url_for("saas_returns.sales_returns_list"))
    assert_tenant_access(note["business_id"])

    items = saas_fetchall(
        f"SELECT * FROM saas_credit_note_items WHERE credit_note_id={p} AND business_id={p}",
        (cn_id, biz_id)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/credit_note_view.html",
                           note=note, items=items, biz=biz)


# ═══════════════════════════ PURCHASE RETURN / DEBIT NOTE ═════════════════════

@saas_returns_bp.route("/purchase")
@saas_business_required
@permission_required("view_purchase")
def purchase_returns_list():
    biz_id = get_tenant_id()
    p = P()
    notes = saas_fetchall(
        f"""SELECT * FROM saas_debit_notes WHERE business_id={p}
            ORDER BY created_at DESC""",
        (biz_id,)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/purchase_returns_list.html", biz=biz, notes=notes)


@saas_returns_bp.route("/purchase/<int:purchase_id>/new")
@saas_business_required
@permission_required("create_debit_note")
def purchase_return_new(purchase_id):
    biz_id = get_tenant_id()
    p = P()

    purchase = saas_fetchone(
        f"SELECT * FROM saas_purchases WHERE id={p} AND business_id={p} AND status != 'cancelled'",
        (purchase_id, biz_id)
    )
    if not purchase:
        flash("Purchase not found, or it has been cancelled.", "danger")
        return redirect(url_for("saas_purchase.history"))
    assert_tenant_access(purchase["business_id"])

    raw_items = saas_fetchall(
        f"SELECT * FROM saas_purchase_items WHERE purchase_id={p} AND business_id={p} ORDER BY id",
        (purchase_id, biz_id)
    )
    items = []
    for it in raw_items:
        remaining = round(float(it["quantity"]) - float(it["returned_quantity"] or 0), 4)
        if remaining <= 0.0001:
            continue
        row = dict(it)
        row["remaining_qty"] = remaining
        items.append(row)

    if not items:
        flash("Every item on this purchase has already been fully returned.", "warning")
        return redirect(url_for("saas_purchase.view", pid=purchase_id))

    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/purchase_return_new.html",
                           biz=biz, purchase=purchase, items=items)


@saas_returns_bp.route("/purchase/<int:purchase_id>/save", methods=["POST"])
@saas_business_required
@permission_required("create_debit_note")
def purchase_return_save(purchase_id):
    biz_id = get_tenant_id()
    p = P()

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "No data received"}), 400
    if not validate_csrf(data.get("csrf_token")):
        return jsonify({"success": False, "message": "Security error. Please refresh and try again."}), 403

    purchase = saas_fetchone(
        f"SELECT * FROM saas_purchases WHERE id={p} AND business_id={p} AND status != 'cancelled'",
        (purchase_id, biz_id)
    )
    if not purchase:
        return jsonify({"success": False, "message": "Purchase not found or cancelled."}), 404
    assert_tenant_access(purchase["business_id"])

    return_lines = data.get("items", [])
    reason        = (data.get("reason") or "").strip()
    refund_method = data.get("refund_method", "credit")
    user_id       = session.get("saas_user_id")

    if refund_method == "credit" and not purchase["supplier_id"]:
        return jsonify({"success": False,
            "message": "This purchase has no supplier on file — choose a cash/bank refund instead."}), 400

    try:
        result = _create_debit_note(biz_id, purchase, return_lines, reason, refund_method, user_id)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400

    audit_log("debit_note_created", business_id=biz_id,
              entity_type="debit_note", entity_id=str(result["debit_note_id"]),
              detail=f"number={result['formatted']} against_purchase={purchase['purchase_number']}")

    return jsonify({"success": True, "debit_note_id": result["debit_note_id"],
                    "debit_note_number": result["formatted"], "total": result["total"]})


def _create_debit_note(biz_id, purchase, return_lines, reason, refund_method, user_id):
    """Core Purchase Return logic — mirrors _create_credit_note() above,
    with stock DECREASING (goods physically leave) instead of increasing."""
    p = P()
    if not return_lines:
        raise ValueError("No items selected to return.")

    line_calcs = []
    taxable_sum = cgst_sum = sgst_sum = igst_sum = 0.0

    for req in return_lines:
        try:
            qty_to_return = float(req.get("quantity", 0))
        except (TypeError, ValueError):
            continue
        if qty_to_return <= 0:
            continue

        orig = saas_fetchone(
            f"SELECT * FROM saas_purchase_items WHERE id={p} AND purchase_id={p} AND business_id={p}",
            (req.get("purchase_item_id"), purchase["id"], biz_id)
        )
        if not orig:
            raise ValueError("One of the selected items no longer matches this purchase.")

        orig_qty = float(orig["quantity"])
        already_returned = float(orig["returned_quantity"] or 0)
        remaining = round(orig_qty - already_returned, 4)
        if qty_to_return > remaining + 1e-6:
            raise ValueError(
                f"Cannot return {qty_to_return:g} of '{orig['product_name']}' — "
                f"only {remaining:g} remaining (already returned: {already_returned:g})."
            )

        stock_row = None
        if orig["product_id"]:
            stock_row = saas_fetchone(
                f"SELECT stock_quantity FROM saas_products WHERE id={p} AND business_id={p}",
                (orig["product_id"], biz_id)
            )
        if stock_row is not None and float(stock_row["stock_quantity"]) < qty_to_return:
            raise ValueError(
                f"Cannot return {qty_to_return:g} of '{orig['product_name']}' — "
                f"only {float(stock_row['stock_quantity']):g} currently in stock."
            )

        fraction = qty_to_return / orig_qty if orig_qty else 0
        item_taxable = round(float(orig["taxable_amount"]) * fraction, 2)
        item_cgst    = round(float(orig["cgst_amount"])    * fraction, 2)
        item_sgst    = round(float(orig["sgst_amount"])    * fraction, 2)
        item_igst    = round(float(orig["igst_amount"])    * fraction, 2)
        item_total   = round(item_taxable + item_cgst + item_sgst + item_igst, 2)

        taxable_sum += item_taxable; cgst_sum += item_cgst
        sgst_sum    += item_sgst;    igst_sum += item_igst

        line_calcs.append({
            "purchase_item_id": orig["id"], "product_id": orig["product_id"],
            "product_name": orig["product_name"], "hsn_code": orig["hsn_code"],
            "quantity": qty_to_return, "unit_price": orig["unit_price"],
            "taxable_amount": item_taxable, "gst_rate": orig["gst_rate"],
            "cgst_rate": orig["cgst_rate"], "sgst_rate": orig["sgst_rate"], "igst_rate": orig["igst_rate"],
            "cgst_amount": item_cgst, "sgst_amount": item_sgst, "igst_amount": item_igst,
            "total_price": item_total,
        })

    if not line_calcs:
        raise ValueError("No valid return quantity entered.")

    taxable_sum = round(taxable_sum, 2); cgst_sum = round(cgst_sum, 2)
    sgst_sum    = round(sgst_sum, 2);    igst_sum = round(igst_sum, 2)
    total_tax   = round(cgst_sum + sgst_sum + igst_sum, 2)
    grand_total = round(taxable_sum + total_tax, 2)

    doc = generate_document_number(biz_id, "debit_note", date.today().isoformat())

    dn_id = saas_execute(
        f"""INSERT INTO saas_debit_notes
            (business_id, debit_note_number, doc_prefix, doc_fy, doc_sequence,
             purchase_id, purchase_number, supplier_id, supplier_name, supplier_gstin,
             supply_type, reason, taxable_amount, cgst_amount, sgst_amount, igst_amount,
             total_tax, total, refund_method, tax_mode, created_by)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
        (biz_id, doc["formatted"], doc["prefix"], doc["financial_year"], doc["sequence"],
         purchase["id"], purchase["purchase_number"], purchase["supplier_id"], purchase["supplier_name"],
         purchase["supplier_gstin"], purchase["supply_type"], reason,
         taxable_sum, cgst_sum, sgst_sum, igst_sum, total_tax, grand_total, refund_method,
         purchase["tax_mode"], user_id)
    )

    for line in line_calcs:
        saas_execute(
            f"""INSERT INTO saas_debit_note_items
                (debit_note_id, business_id, purchase_item_id, product_id, product_name,
                 hsn_code, quantity, unit_price, taxable_amount, gst_rate,
                 cgst_rate, sgst_rate, igst_rate, cgst_amount, sgst_amount, igst_amount, total_price)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})""",
            (dn_id, biz_id, line["purchase_item_id"], line["product_id"], line["product_name"],
             line["hsn_code"], line["quantity"], line["unit_price"], line["taxable_amount"],
             line["gst_rate"], line["cgst_rate"], line["sgst_rate"], line["igst_rate"],
             line["cgst_amount"], line["sgst_amount"], line["igst_amount"], line["total_price"])
        )
        saas_execute(
            f"""UPDATE saas_purchase_items SET returned_quantity = returned_quantity + {p}
                WHERE id={p} AND business_id={p}""",
            (line["quantity"], line["purchase_item_id"], biz_id)
        )
        # Stock always DECREASES on a purchase return — goods physically
        # leave. Already validated above that enough stock exists.
        if line["product_id"]:
            saas_execute(
                f"""UPDATE saas_products SET stock_quantity = stock_quantity - {p}, updated_at={p}
                    WHERE id={p} AND business_id={p}""",
                (line["quantity"], datetime.utcnow().isoformat(), line["product_id"], biz_id)
            )

    # Reverse Purchase, Reverse Input GST (ITC), Reduce Supplier Balance —
    # all in one atomic double-entry posting.
    record_purchase_return(
        biz_id, taxable_sum, supplier_id=purchase["supplier_id"], supplier_name=purchase["supplier_name"],
        cgst=cgst_sum, sgst=sgst_sum, igst=igst_sum, refund_method=refund_method,
        source_id=dn_id, narration=f"Purchase return against {purchase['purchase_number']} ({doc['formatted']})",
        created_by=user_id
    )

    return {"debit_note_id": dn_id, "formatted": doc["formatted"], "total": grand_total}


@saas_returns_bp.route("/purchase/note/<int:dn_id>")
@saas_business_required
@permission_required("view_purchase")
def view_debit_note(dn_id):
    biz_id = get_tenant_id()
    p = P()
    note = saas_fetchone(f"SELECT * FROM saas_debit_notes WHERE id={p} AND business_id={p}", (dn_id, biz_id))
    if not note:
        flash("Debit note not found.", "danger")
        return redirect(url_for("saas_returns.purchase_returns_list"))
    assert_tenant_access(note["business_id"])

    items = saas_fetchall(
        f"SELECT * FROM saas_debit_note_items WHERE debit_note_id={p} AND business_id={p}",
        (dn_id, biz_id)
    )
    biz = saas_fetchone(f"SELECT * FROM saas_businesses WHERE id={p}", (biz_id,))
    return render_template("saas_business/returns/debit_note_view.html",
                           note=note, items=items, biz=biz)
