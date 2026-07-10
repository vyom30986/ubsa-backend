"""Database setup — SQLite by default, swap DATABASE_URL to Postgres for prod."""
import os
from sqlmodel import create_engine, SQLModel, Session

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./bookstore.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})

def init_db():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as s:
        yield s
