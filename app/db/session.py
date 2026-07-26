from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import DATABASE_URL, DB_SCHEMA

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
