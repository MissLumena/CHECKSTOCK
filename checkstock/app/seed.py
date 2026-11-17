import sys

from sqlalchemy import func, select

from . import models, service
from .db import Base, SessionLocal, engine

# (артикул, товар, Север, Юг)
SEED = [("SKU-101", "Органайзер", 10, 6), ("SKU-102", "Контейнер", 5, 0), ("SKU-103", "Корзина", 0, 4)]


def seed():
    with SessionLocal() as db:
        if db.scalar(select(func.count(models.Product.id))):
            return
        north, south = models.Warehouse(name="Север"), models.Warehouse(name="Юг")
        db.add_all([north, south])
        prods = [models.Product(sku=s, name=n) for s, n, *_ in SEED]
        db.add_all(prods)
        db.flush()
        for p, (_, _, n, s) in zip(prods, SEED):
            for wh, qty in ((north, n), (south, s)):
                if qty:
                    service.receipt(db, p.id, wh.id, qty, "тестовый приход")
        db.commit()


def reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    seed()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        Base.metadata.create_all(engine)
        seed()
