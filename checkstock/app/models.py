from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

_now = dict(server_default=func.now())


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))


class Warehouse(Base):
    """ФФ. Удаление мягкое (deleted_at), чтобы остатки и история не терялись."""
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Stock(Base):
    """Остаток SKU на ФФ. Доступно = on_hand - reserved."""
    __tablename__ = "stock"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(default=0)
    reserved: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="on_hand_nonneg"),
        CheckConstraint("reserved >= 0 AND reserved <= on_hand", name="reserved_le_on_hand"),
    )


class Transfer(Base):
    """Перемещение ФФ -> ФФ: in_transit -> received."""
    __tablename__ = "transfers"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    from_wh: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    to_wh: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    qty: Mapped[int]
    status: Mapped[str] = mapped_column(String(20), default="in_transit")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_now)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("qty > 0"),)


class Order(Base):
    """FBS: reserved -> shipped | cancelled.  FBO: in_transit -> received."""
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(3))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    qty: Mapped[int]
    status: Mapped[str] = mapped_column(String(20))
    ref: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_now)
    __table_args__ = (CheckConstraint("qty > 0"),)


class Movement(Base):
    """Журнал: что, сколько, откуда и куда. Только добавление."""
    __tablename__ = "movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_now)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    op: Mapped[str] = mapped_column(String(30))
    src: Mapped[str] = mapped_column(String(200))
    dst: Mapped[str] = mapped_column(String(200))
    ref: Mapped[str | None] = mapped_column(String(100))


class IdemKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    response: Mapped[dict | None] = mapped_column(JSON)
