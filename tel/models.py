
from __future__ import annotations

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "sqlite:///signals.db")

engine = create_engine(DB_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(16), index=True)
    pair: Mapped[str] = mapped_column(String(32), index=True)
    entry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_snippet: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True
    )


def create_tables():
    """Initializes tables in SQLite / PostgreSQL."""
    Base.metadata.create_all(bind=engine)


def get_session():
    """Context-friendly session generator."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()