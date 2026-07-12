import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

# DATABASE_PATH lets a deployment point this at a persistent disk (e.g.
# Render/Railway mount a volume at something like /data) instead of the
# repo-relative file used for local dev. Falls back to the existing local
# behavior when unset.
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./books.db")
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
    }
)

# Enable WAL mode so SQLite can handle concurrent reads/writes
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL;"))

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()
