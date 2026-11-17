import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from . import models, seed, service as s
from .db import Base, SessionLocal, conflict_safe_insert, engine

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_):
    # База может ещё запускаться (особенно при первом старте Docker): ждём до 30 секунд
    for attempt in range(30):
        try:
            Base.metadata.create_all(engine)
            break
        except OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)
    seed.seed()
    yield


app = FastAPI(title="CheckStock", lifespan=lifespan)


@app.exception_handler(s.DomainError)
async def domain_error(_: Request, e: s.DomainError):
    return JSONResponse(status_code=e.status, content={"error": e.code, "message": e.message, **e.extra})


def get_db():
    with SessionLocal() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


Idem = Header(None, alias="Idempotency-Key")


def idempotent(db, key, fn):
    """Повтор POST с тем же Idempotency-Key возвращает сохранённый ответ и ничего не меняет.
    Ключ вставляется в той же транзакции, что и операция: при ошибке откатывается вместе с ней."""
    if not key:
        return fn()
    insert_stmt = (
        conflict_safe_insert(db, models.IdemKey)
        .values(key=key)
        .on_conflict_do_nothing()
        .returning(models.IdemKey.key)
    )
    inserted = db.execute(insert_stmt).first()
    if inserted is None:  # параллельный дубль ждёт коммита первого и попадает сюда
        stored = db.scalar(select(models.IdemKey.response).where(models.IdemKey.key == key))
        return {**(stored or {}), "replayed": True}
    out = fn()
    db.execute(update(models.IdemKey).where(models.IdemKey.key == key).values(response=out))
    return out


class WhIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ReceiptIn(BaseModel):
    product_id: int
    warehouse_id: int
    qty: int = Field(gt=0)
    ref: str | None = None


class TransferIn(BaseModel):
    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    qty: int = Field(gt=0)


class ShipIn(BaseModel):
    product_id: int
    warehouse_id: int
    qty: int = Field(gt=0)
    ref: str | None = None


# ----- чтение -----
@app.get("/api/stock")
def stock(db=Depends(get_db)):
    return s.stock_view(db)


@app.get("/api/warehouses")
def warehouses(db=Depends(get_db)):
    return s.warehouses_view(db)


@app.get("/api/transfers")
def transfers(db=Depends(get_db)):
    q = select(models.Transfer).order_by(models.Transfer.id.desc()).limit(50)
    return [s.transfer_dict(db, t) for t in db.scalars(q)]


@app.get("/api/orders")
def orders(db=Depends(get_db)):
    q = select(models.Order).order_by(models.Order.id.desc()).limit(50)
    return [s.order_dict(db, o) for o in db.scalars(q)]


@app.get("/api/movements")
def movements(db=Depends(get_db)):
    return s.movements_view(db)


@app.get("/api/audit")
def audit(db=Depends(get_db)):
    return s.audit(db)


# ----- склады -----
@app.post("/api/warehouses")
def wh_create(b: WhIn, db=Depends(get_db)):
    return s.warehouse_create(db, b.name)


@app.patch("/api/warehouses/{wid}")
def wh_rename(wid: int, b: WhIn, db=Depends(get_db)):
    return s.warehouse_rename(db, wid, b.name)


@app.delete("/api/warehouses/{wid}")
def wh_delete(wid: int, db=Depends(get_db)):
    return s.warehouse_delete(db, wid)


# ----- операции -----
@app.post("/api/receipts")
def receipts(b: ReceiptIn, db=Depends(get_db), key: str | None = Idem):
    return idempotent(db, key, lambda: s.receipt(db, b.product_id, b.warehouse_id, b.qty, b.ref))


@app.post("/api/transfers")
def transfer_new(b: TransferIn, db=Depends(get_db), key: str | None = Idem):
    return idempotent(db, key, lambda: s.transfer_create(
        db, b.product_id, b.from_warehouse_id, b.to_warehouse_id, b.qty))


@app.post("/api/transfers/{tid}/receive")
def transfer_recv(tid: int, db=Depends(get_db)):
    return s.transfer_receive(db, tid)


@app.post("/api/fbs")
def fbs_new(b: ShipIn, db=Depends(get_db), key: str | None = Idem):
    return idempotent(db, key, lambda: s.fbs_reserve(db, b.product_id, b.warehouse_id, b.qty, b.ref))


@app.post("/api/fbs/{oid}/ship")
def fbs_ship(oid: int, db=Depends(get_db)):
    return s.fbs_ship(db, oid)


@app.post("/api/fbs/{oid}/cancel")
def fbs_cancel(oid: int, db=Depends(get_db)):
    return s.fbs_cancel(db, oid)


@app.post("/api/fbo")
def fbo_new(b: ShipIn, db=Depends(get_db), key: str | None = Idem):
    return idempotent(db, key, lambda: s.fbo_ship(db, b.product_id, b.warehouse_id, b.qty, b.ref))


@app.post("/api/fbo/{oid}/receive")
def fbo_recv(oid: int, db=Depends(get_db)):
    return s.fbo_receive(db, oid)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")