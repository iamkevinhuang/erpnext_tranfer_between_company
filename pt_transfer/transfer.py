import frappe
from frappe.model.naming import make_autoname

OUT = "Transfer Out Between Company"
IN = "Transfer In Between Company"

# nama field custom di Stock Entry
F_GROUP = "custom_transfer_group_id"
F_COMPANY = "custom_company_fromto"
F_SO = "custom_reference_sales_order"
F_PAIR = "custom_opposite_stock_entry_number"


def _syncing():
    return frappe.flags.get("in_pair_sync")


def _run_sync(fn):
    frappe.flags.in_pair_sync = True
    try:
        return fn()
    finally:
        frappe.flags.in_pair_sync = False


def _receiving_warehouse(src_warehouse, src_company, target_company):
    """AVROS - TERA  ->  AVROS - SDI (kode sama, singkatan company tujuan)."""
    src_abbr = frappe.db.get_value("Company", src_company, "abbr")
    suffix = f" - {src_abbr}"
    if not src_warehouse.endswith(suffix):
        frappe.throw(f"Warehouse {src_warehouse} tidak berformat 'KODE{suffix}'")
    code = src_warehouse[: -len(suffix)]

    target_abbr = frappe.db.get_value("Company", target_company, "abbr")
    target = f"{code} - {target_abbr}"
    row = frappe.db.get_value("Warehouse", target, ["company", "is_group"], as_dict=True)
    if not row:
        frappe.throw(f"Warehouse tujuan {target} tidak ditemukan")
    if row.company != target_company or row.is_group:
        frappe.throw(f"Warehouse {target} bukan warehouse milik {target_company}")
    return target


def validate(doc, method=None):
    if doc.stock_entry_type == OUT:
        if not doc.get(F_COMPANY):
            frappe.throw("Isi Company From/To (company tujuan)")
        if doc.get(F_COMPANY) == doc.company:
            frappe.throw("Company tujuan tidak boleh sama dengan company asal")
        if doc.amended_from and doc.is_new():
            # hasil amend: buang pasangan lama, In baru akan dibuat
            doc.set(F_PAIR, None)
            doc.set(F_GROUP, None)
        if not doc.get(F_GROUP):
            doc.set(F_GROUP, make_autoname("TRF-.YYYY.-.####"))
        for r in doc.items:
            if not r.s_warehouse:
                frappe.throw(f"Baris {r.idx}: isi gudang asal (Source Warehouse)")
    elif doc.stock_entry_type == IN and not _syncing() and not doc.is_new():
        frappe.throw(f"Ubah melalui dokumen Transfer Out: {doc.get(F_PAIR)}")


def _copy_items(out, inn):
    inn.set("items", [])
    for r in out.items:
        inn.append("items", {
            "item_code": r.item_code,
            "qty": r.qty,
            "uom": r.uom,
            "conversion_factor": r.conversion_factor,
            "t_warehouse": _receiving_warehouse(
                r.s_warehouse, out.company, out.get(F_COMPANY)
            ),
            "basic_rate": r.basic_rate or r.valuation_rate,
            "allow_zero_valuation_rate": r.allow_zero_valuation_rate,
        })


def on_update(doc, method=None):
    if doc.stock_entry_type != OUT or _syncing() or doc.docstatus != 0:
        return

    def work():
        pair = doc.get(F_PAIR)
        if pair and frappe.db.exists("Stock Entry", pair):
            inn = frappe.get_doc("Stock Entry", pair)
        else:
            inn = frappe.new_doc("Stock Entry")
            inn.stock_entry_type = IN
            inn.purpose = "Material Receipt"
            inn.company = doc.get(F_COMPANY)
            inn.set(F_GROUP, doc.get(F_GROUP))
            inn.set(F_COMPANY, doc.company)
            inn.set(F_SO, doc.get(F_SO))
            inn.set(F_PAIR, doc.name)
        inn.posting_date = doc.posting_date
        inn.posting_time = doc.posting_time
        _copy_items(doc, inn)
        inn.flags.ignore_permissions = True
        inn.save()
        if doc.get(F_PAIR) != inn.name:
            doc.db_set(F_PAIR, inn.name, update_modified=False)

    _run_sync(work)


def before_submit(doc, method=None):
    if doc.stock_entry_type == IN and not _syncing():
        frappe.throw(f"Submit melalui dokumen Transfer Out: {doc.get(F_PAIR)}")
    if doc.stock_entry_type == OUT:
        def work():
            inn = frappe.get_doc("Stock Entry", doc.get(F_PAIR))
            inn.flags.ignore_permissions = True
            _copy_items(doc, inn)   # segarkan qty dan rate terakhir
            inn.save()
            inn.submit()
        _run_sync(work)


def on_cancel(doc, method=None):
    if _syncing() or not doc.get(F_PAIR):
        return
    if doc.stock_entry_type == IN:
        frappe.throw(f"Batalkan melalui dokumen Transfer Out: {doc.get(F_PAIR)}")
    if doc.stock_entry_type == OUT:
        def work():
            inn = frappe.get_doc("Stock Entry", doc.get(F_PAIR))
            inn.flags.ignore_permissions = True
            if inn.docstatus == 1:
                inn.cancel()
        _run_sync(work)


def on_trash(doc, method=None):
    if _syncing():
        return
    pair = doc.get(F_PAIR)
    # tidak ada pasangan (atau pasangan sudah hilang): boleh dihapus
    if not pair or not frappe.db.exists("Stock Entry", pair):
        return
    if doc.stock_entry_type == IN:
        frappe.throw("Hapus melalui dokumen Transfer Out")
    if doc.stock_entry_type == OUT:
        if frappe.db.get_value("Stock Entry", pair, "docstatus") == 1:
            frappe.throw(f"Pasangan {pair} sudah submit. Batalkan dulu sebelum menghapus.")

        def work():
            frappe.delete_doc("Stock Entry", pair,
                              ignore_permissions=True, force=True)
        _run_sync(work)
