import os

os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL", "sqlite:///./checkstock_test.sqlite3")

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.main import app


@pytest.fixture()
def c():
    seed.reset()
    with TestClient(app) as client:
        yield client


def ids(c):
    p = {x["sku"]: x["id"] for x in c.get("/api/stock").json()["products"]}
    w = {x["name"]: x["id"] for x in c.get("/api/warehouses").json()}
    return p, w


def product(c, sku):
    return next(x for x in c.get("/api/stock").json()["products"] if x["sku"] == sku)


def row(c, sku, wh):
    return next(r for r in product(c, sku)["warehouses"] if r["name"] == wh)


def audit_ok(c):
    assert c.get("/api/audit").json()["ok"]


def test_seed(c):
    assert (row(c, "SKU-101", "Север")["on_hand"], row(c, "SKU-101", "Юг")["on_hand"]) == (10, 6)
    assert row(c, "SKU-103", "Север")["on_hand"] == 0
    audit_ok(c)


def test_fbs_reserve_ship_and_repeat(c):
    p, w = ids(c)
    r = c.post("/api/fbs", json={"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 4})
    oid = r.json()["order"]["id"]
    x = row(c, "SKU-101", "Север")
    assert (x["on_hand"], x["reserved"], x["available"]) == (10, 4, 6)  # резерв не списывает физику
    assert c.post(f"/api/fbs/{oid}/ship").json()["changed"] is True
    x = row(c, "SKU-101", "Север")
    assert (x["on_hand"], x["reserved"]) == (6, 0)
    assert product(c, "SKU-101")["in_transit"]["to_client_fbs"] == 4
    assert c.post(f"/api/fbs/{oid}/ship").json()["changed"] is False  # повтор
    assert row(c, "SKU-101", "Север")["on_hand"] == 6
    audit_ok(c)


def test_fbs_cancel_releases_reserve(c):
    p, w = ids(c)
    oid = c.post("/api/fbs", json={"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 4}).json()["order"]["id"]
    assert c.post(f"/api/fbs/{oid}/cancel").json()["changed"] is True
    assert c.post(f"/api/fbs/{oid}/cancel").json()["changed"] is False
    assert row(c, "SKU-101", "Север")["available"] == 10
    assert c.post(f"/api/fbs/{oid}/ship").status_code == 409  # отменённый не отправить
    audit_ok(c)


def test_transfer_and_repeat_receive(c):
    p, w = ids(c)
    t = c.post("/api/transfers", json={"product_id": p["SKU-101"], "from_warehouse_id": w["Север"],
                                       "to_warehouse_id": w["Юг"], "qty": 3}).json()["transfer"]
    assert row(c, "SKU-101", "Север")["on_hand"] == 7
    assert product(c, "SKU-101")["in_transit"]["between_ff"] == 3
    audit_ok(c)
    assert c.post(f"/api/transfers/{t['id']}/receive").json()["changed"] is True
    assert c.post(f"/api/transfers/{t['id']}/receive").json()["changed"] is False
    assert row(c, "SKU-101", "Юг")["on_hand"] == 9
    audit_ok(c)


def test_shortage_at_warehouse_but_enough_total(c):
    p, w = ids(c)
    r = c.post("/api/fbs", json={"product_id": p["SKU-103"], "warehouse_id": w["Север"], "qty": 2})
    assert r.status_code == 409 and r.json()["error"] == "insufficient_at_warehouse"
    assert r.json()["others"][0]["name"] == "Юг" and r.json()["missing"] == 2
    assert row(c, "SKU-103", "Север")["reserved"] == 0  # ничего не изменилось
    # решение: перевезти недостающее и повторить
    t = c.post("/api/transfers", json={"product_id": p["SKU-103"], "from_warehouse_id": w["Юг"],
                                       "to_warehouse_id": w["Север"], "qty": 2}).json()["transfer"]
    c.post(f"/api/transfers/{t['id']}/receive")
    assert c.post("/api/fbs", json={"product_id": p["SKU-103"], "warehouse_id": w["Север"], "qty": 2}).status_code == 200
    audit_ok(c)


def test_shortage_total(c):
    p, w = ids(c)
    r = c.post("/api/fbo", json={"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 99})
    assert r.status_code == 409 and r.json()["error"] == "insufficient_total"


def test_reserved_stock_is_not_available_for_transfer(c):
    p, w = ids(c)
    c.post("/api/fbs", json={"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 8})
    r = c.post("/api/transfers", json={"product_id": p["SKU-101"], "from_warehouse_id": w["Север"],
                                       "to_warehouse_id": w["Юг"], "qty": 3})
    assert r.status_code == 409  # доступно только 2


def test_delete_warehouse_with_stock(c):
    p, w = ids(c)
    assert c.delete(f"/api/warehouses/{w['Юг']}").json()["on_hand_left"] == 10  # 6+0+4
    south = next(x for x in c.get("/api/warehouses").json() if x["name"] == "Юг")
    assert south["deleted"] and len(south["items"]) == 2  # остатки видны: "где числится"
    assert row(c, "SKU-101", "Юг")["on_hand"] == 6
    n_hist = len(c.get("/api/movements").json())
    assert n_hist == 4  # 4 тестовых прихода никуда не делись
    assert c.post("/api/receipts", json={"product_id": p["SKU-101"], "warehouse_id": w["Юг"], "qty": 1}).status_code == 409
    assert c.post("/api/fbs", json={"product_id": p["SKU-101"], "warehouse_id": w["Юг"], "qty": 1}).status_code == 409
    # вывезти остаток с удалённого ФФ можно
    t = c.post("/api/transfers", json={"product_id": p["SKU-101"], "from_warehouse_id": w["Юг"],
                                       "to_warehouse_id": w["Север"], "qty": 6}).json()["transfer"]
    c.post(f"/api/transfers/{t['id']}/receive")
    assert row(c, "SKU-101", "Север")["on_hand"] == 16
    audit_ok(c)


def test_cannot_delete_with_incoming_transfer(c):
    p, w = ids(c)
    c.post("/api/transfers", json={"product_id": p["SKU-101"], "from_warehouse_id": w["Север"],
                                   "to_warehouse_id": w["Юг"], "qty": 1})
    assert c.delete(f"/api/warehouses/{w['Юг']}").status_code == 409


def test_rename_and_create(c):
    p, w = ids(c)
    assert c.patch(f"/api/warehouses/{w['Юг']}", json={"name": "Юго-Запад"}).status_code == 200
    assert c.post("/api/warehouses", json={"name": " север "}).status_code == 409  # дубль
    assert c.post("/api/warehouses", json={"name": "Восток"}).status_code == 200


def test_idempotency_key(c):
    p, w = ids(c)
    body = {"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 4}
    r1 = c.post("/api/fbs", json=body, headers={"Idempotency-Key": "k1"})
    r2 = c.post("/api/fbs", json=body, headers={"Idempotency-Key": "k1"})
    assert r2.json()["replayed"] and r1.json()["order"]["id"] == r2.json()["order"]["id"]
    assert row(c, "SKU-101", "Север")["reserved"] == 4


def test_fbo_flow(c):
    p, w = ids(c)
    o = c.post("/api/fbo", json={"product_id": p["SKU-101"], "warehouse_id": w["Север"], "qty": 5}).json()["order"]
    assert row(c, "SKU-101", "Север")["on_hand"] == 5
    assert product(c, "SKU-101")["in_transit"]["to_marketplace_fbo"] == 5
    audit_ok(c)
    assert c.post(f"/api/fbo/{o['id']}/receive").json()["changed"] is True
    assert c.post(f"/api/fbo/{o['id']}/receive").json()["changed"] is False
    pr = product(c, "SKU-101")
    assert pr["in_transit"]["to_marketplace_fbo"] == 0 and pr["at_marketplace"] == 5
    audit_ok(c)
