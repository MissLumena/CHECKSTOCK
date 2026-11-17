import os

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.sql.dml import Insert

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock"
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def conflict_safe_insert(db: Session, model: type[Base]) -> Insert:
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(model)
    elif dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"Unsupported database dialect: {dialect}")
