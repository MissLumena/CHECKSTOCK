"""Вся складская логика. Вызывающий код отвечает за commit/rollback транзакции."""
from datetime import datetime, timezone

from sqlalchemy import func, select

from .db import conflict_safe_insert
from .models import Movement, Order, Product, Stock, Transfer, Warehouse


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# ---------- helpers ----------
def _log(db, pid, qty, op, src, dst, ref=None):
    db.add(Movement(product_id=pid, qty=qty, op=op, src=src, dst=dst, ref=ref))


def _product(db, pid) -> Product:
    p = db.get(Product, pid)
    if not p:
        raise DomainError("product_not_found", "Товар не найден", 404)
    return p


def _wh(db, wid, active=False) -> Warehouse:
    w = db.get(Warehouse, wid)
    if not w:
        raise DomainError("warehouse_not_found", "Склад не найден", 404)
    if active and w.deleted_at:
        raise DomainError("warehouse_deleted", f"Склад «{w.name}» удалён: сюда нельзя принимать/резервировать товар")
    return w


def _lock_stock(db, pid, wid, create=False) -> Stock | None:
    q = select(Stock).where(Stock.product_id == pid, Stock.warehouse_id == wid).with_for_update()
    row = db.execute(q).scalar_one_or_none()
    if row is None and create:
        insert_stmt = (
            conflict_safe_insert(db, Stock)
            .values(product_id=pid, warehouse_id=wid)
            .on_conflict_do_nothing()
        )
        db.execute(insert_stmt)
        row = db.execute(q).scalar_one()
    return row


def _require_available(db, pid, wh: Warehouse, qty) -> Stock:
    """Блокирует строку остатка и проверяет доступное. При нехватке различает
    «нет на этом ФФ, но есть на других» и «нет вообще»."""
    row = _lock_stock(db, pid, wh.id)
    here = (row.on_hand - row.reserved) if row else 0
    if here >= qty:
        return row
    rows = db.execute(
        select(Warehouse, Stock).join(Stock, Stock.warehouse_id == Warehouse.id)
        .where(Stock.product_id == pid, Warehouse.id != wh.id)
    ).all()
    others = [
        {"warehouse_id": w.id, "name": w.name, "deleted": w.deleted_at is not None,
         "available": s.on_hand - s.reserved}
        for w, s in rows if s.on_hand - s.reserved > 0
    ]
    total = here + sum(o["available"] for o in others)
    if total >= qty:
        raise DomainError(
            "insufficient_at_warehouse",
            f"На «{wh.name}» доступно {here}, нужно {qty}. На других складах есть ещё {total - here}.",
            available_here=here, missing=qty - here, others=others)
    raise DomainError("insufficient_total", f"Недостаточно товара: нужно {qty}, всего доступно {total}.",
                      available_total=total)


# ---------- serializers ----------
def transfer_dict(db, t: Transfer):
    return {"id": t.id, "sku": _product(db, t.product_id).sku, "product_id": t.product_id, "qty": t.qty,
            "from": _wh(db, t.from_wh).name, "to": _wh(db, t.to_wh).name, "status": t.status}


def order_dict(db, o: Order):
    return {"id": o.id, "kind": o.kind, "sku": _product(db, o.product_id).sku, "product_id": o.product_id,
            "warehouse": _wh(db, o.warehouse_id).name, "warehouse_id": o.warehouse_id,
            "qty": o.qty, "status": o.status, "ref": o.ref}


# ---------- склады ----------
def _check_name(db, name, exclude_id=None):
    name = name.strip()
    if not name:
        raise DomainError("bad_name", "Название не может быть пустым", 422)
    q = select(Warehouse.name).where(Warehouse.deleted_at.is_(None))
    if exclude_id is not None:
        q = q.where(Warehouse.id != exclude_id)
    duplicate = any(
        existing_name.casefold() == name.casefold()
        for existing_name in db.scalars(q)
    )
    if duplicate:
        raise DomainError("duplicate_name", f"ФФ «{name}» уже есть")
    return name


def warehouse_create(db, name):
    w = Warehouse(name=_check_name(db, name))
    db.add(w)
    db.flush()
    return {"id": w.id, "name": w.name}


def warehouse_rename(db, wid, name):
    w = _wh(db, wid, active=True)
    old, w.name = w.name, _check_name(db, name, wid)
    return {"id": w.id, "name": w.name, "old": old}


def warehouse_delete(db, wid):
    """Мягкое удаление. Остатки остаются на складе и видны, история не трогается."""
    w = _wh(db, wid, active=True)
    incoming = db.scalar(select(func.count(Transfer.id)).where(Transfer.to_wh == wid, Transfer.status == "in_transit"))
    if incoming:
        raise DomainError("has_incoming_transfers", f"На «{w.name}» едет {incoming} перемещ. — сначала примите их")
    w.deleted_at = datetime.now(timezone.utc)
    left = db.execute(select(func.coalesce(func.sum(Stock.on_hand), 0), func.coalesce(func.sum(Stock.reserved), 0))
                      .where(Stock.warehouse_id == wid)).one()
    return {"id": w.id, "deleted": True, "on_hand_left": int(left[0]), "reserved_left": int(left[1])}


# ---------- приход ----------
def receipt(db, pid, wid, qty, ref=None):
    p, w = _product(db, pid), _wh(db, wid, active=True)
    row = _lock_stock(db, pid, wid, create=True)
    row.on_hand += qty
    _log(db, pid, qty, "receipt", "Поставщик", w.name, ref)
    return {"sku": p.sku, "warehouse": w.name, "on_hand": row.on_hand}


# ---------- перемещения ФФ -> ФФ ----------
def transfer_create(db, pid, from_id, to_id, qty):
    if from_id == to_id:
        raise DomainError("same_warehouse", "Склады отправления и назначения совпадают", 422)
    _product(db, pid)
    src, dst = _wh(db, from_id), _wh(db, to_id, active=True)
    row = _require_available(db, pid, src, qty)
    row.on_hand -= qty
    t = Transfer(product_id=pid, from_wh=from_id, to_wh=to_id, qty=qty)
    db.add(t)
    db.flush()
    _log(db, pid, qty, "transfer_send", src.name, f"В пути → {dst.name}", f"T{t.id}")
    return {"transfer": transfer_dict(db, t), "changed": True}


def transfer_receive(db, tid):
    t = db.execute(select(Transfer).where(Transfer.id == tid).with_for_update()).scalar_one_or_none()
    if not t:
        raise DomainError("not_found", "Перемещение не найдено", 404)
    if t.status == "received":  # повторное подтверждение: ничего не меняем
        return {"transfer": transfer_dict(db, t), "changed": False}
    row = _lock_stock(db, t.product_id, t.to_wh, create=True)
    row.on_hand += t.qty
    t.status, t.received_at = "received", datetime.now(timezone.utc)
    dst = _wh(db, t.to_wh).name
    _log(db, t.product_id, t.qty, "transfer_receive", f"В пути → {dst}", dst, f"T{t.id}")
    return {"transfer": transfer_dict(db, t), "changed": True}


# ---------- FBS ----------
def fbs_reserve(db, pid, wid, qty, ref=None):
    _product(db, pid)
    w = _wh(db, wid, active=True)
    row = _require_available(db, pid, w, qty)
    row.reserved += qty
    o = Order(kind="FBS", product_id=pid, warehouse_id=wid, qty=qty, status="reserved", ref=ref)
    db.add(o)
    db.flush()
    _log(db, pid, qty, "fbs_reserve", f"{w.name} (доступно)", f"{w.name} (резерв FBS)", f"FBS{o.id}")
    return {"order": order_dict(db, o), "changed": True}


def _order(db, oid, kind) -> Order:
    o = db.execute(select(Order).where(Order.id == oid).with_for_update()).scalar_one_or_none()
    if not o or o.kind != kind:
        raise DomainError("not_found", "Заказ не найден", 404)
    return o


def fbs_ship(db, oid):
    o = _order(db, oid, "FBS")
    if o.status == "shipped":
        return {"order": order_dict(db, o), "changed": False}
    if o.status != "reserved":
        raise DomainError("bad_state", f"Нельзя отправить заказ в статусе «{o.status}»")
    row = _lock_stock(db, o.product_id, o.warehouse_id)
    row.reserved -= o.qty
    row.on_hand -= o.qty
    o.status = "shipped"
    w = _wh(db, o.warehouse_id)
    _log(db, o.product_id, o.qty, "fbs_ship", f"{w.name} (резерв FBS)", "В пути → клиент (FBS)", f"FBS{o.id}")
    return {"order": order_dict(db, o), "changed": True}


def fbs_cancel(db, oid):
    o = _order(db, oid, "FBS")
    if o.status == "cancelled":
        return {"order": order_dict(db, o), "changed": False}
    if o.status != "reserved":
        raise DomainError("bad_state", f"Нельзя отменить заказ в статусе «{o.status}»")
    row = _lock_stock(db, o.product_id, o.warehouse_id)
    row.reserved -= o.qty
    o.status = "cancelled"
    w = _wh(db, o.warehouse_id)
    _log(db, o.product_id, o.qty, "fbs_cancel", f"{w.name} (резерв FBS)", f"{w.name} (доступно)", f"FBS{o.id}")
    return {"order": order_dict(db, o), "changed": True}


# ---------- FBO ----------
def fbo_ship(db, pid, wid, qty, ref=None):
    _product(db, pid)
    w = _wh(db, wid)  # со снятого ФФ вывозить можно
    row = _require_available(db, pid, w, qty)
    row.on_hand -= qty
    o = Order(kind="FBO", product_id=pid, warehouse_id=wid, qty=qty, status="in_transit", ref=ref)
    db.add(o)
    db.flush()
    _log(db, pid, qty, "fbo_ship", w.name, "В пути → склад маркетплейса (FBO)", f"FBO{o.id}")
    return {"order": order_dict(db, o), "changed": True}


def fbo_receive(db, oid):
    o = _order(db, oid, "FBO")
    if o.status == "received":
        return {"order": order_dict(db, o), "changed": False}
    o.status = "received"
    _log(db, o.product_id, o.qty, "fbo_receive", "В пути → склад маркетплейса (FBO)",
         "Склад маркетплейса (FBO)", f"FBO{o.id}")
    return {"order": order_dict(db, o), "changed": True}


# ---------- чтение ----------
def stock_view(db):
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    whs = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    st = {(x.product_id, x.warehouse_id): x for x in db.scalars(select(Stock))}
    tr = dict(db.execute(select(Transfer.product_id, func.sum(Transfer.qty))
                         .where(Transfer.status == "in_transit").group_by(Transfer.product_id)).all())
    od = {(p, k, s): q for p, k, s, q in db.execute(
        select(Order.product_id, Order.kind, Order.status, func.sum(Order.qty))
        .group_by(Order.product_id, Order.kind, Order.status)).all()}
    out = []
    for p in products:
        rows = []
        for w in whs:
            x = st.get((p.id, w.id))
            oh, rs = (x.on_hand, x.reserved) if x else (0, 0)
            if w.deleted_at and not (oh or rs):
                continue
            rows.append({"warehouse_id": w.id, "name": w.name, "deleted": w.deleted_at is not None,
                         "on_hand": oh, "reserved": rs, "available": oh - rs})
        out.append({
            "id": p.id, "sku": p.sku, "name": p.name, "warehouses": rows,
            "totals": {k: sum(r[k] for r in rows) for k in ("on_hand", "reserved", "available")},
            "in_transit": {"between_ff": int(tr.get(p.id, 0)),
                           "to_marketplace_fbo": int(od.get((p.id, "FBO", "in_transit"), 0)),
                           "to_client_fbs": int(od.get((p.id, "FBS", "shipped"), 0))},
            "at_marketplace": int(od.get((p.id, "FBO", "received"), 0)),
        })
    return {"products": out}


def warehouses_view(db):
    sv = stock_view(db)["products"]
    res = []
    for w in db.scalars(select(Warehouse).order_by(Warehouse.id)).all():
        items = [{"sku": p["sku"], "name": p["name"], "on_hand": r["on_hand"], "reserved": r["reserved"]}
                 for p in sv for r in p["warehouses"] if r["warehouse_id"] == w.id and (r["on_hand"] or r["reserved"])]
        if w.deleted_at and not items:
            continue
        res.append({"id": w.id, "name": w.name, "deleted": w.deleted_at is not None, "items": items})
    return res


def audit(db):
    """Закон сохранения: всё, что пришло по приходам = на складах + в пути + у клиента/на МП."""
    received = dict(db.execute(select(Movement.product_id, func.sum(Movement.qty))
                               .where(Movement.op == "receipt").group_by(Movement.product_id)).all())
    rows, ok = [], True
    for p in stock_view(db)["products"]:
        accounted = p["totals"]["on_hand"] + sum(p["in_transit"].values()) + p["at_marketplace"]
        exp = int(received.get(p["id"], 0))
        rows.append({"sku": p["sku"], "received_total": exp, "accounted": accounted, "ok": exp == accounted})
        ok &= exp == accounted
    return {"ok": ok, "products": rows}


def movements_view(db, limit=300):
    q = (select(Movement, Product.sku).join(Product, Product.id == Movement.product_id)
         .order_by(Movement.id.desc()).limit(limit))
    return [{"id": m.id, "ts": m.ts.isoformat(), "sku": sku, "qty": m.qty, "op": m.op, "from": m.src,
             "to": m.dst, "ref": m.ref} for m, sku in db.execute(q).all()]
