import os
import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from alembic.config import Config
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Book, Principle, ReviewStatus, User
from app.schemas import PrincipleWriteBase


def _run_migrations(database_url: str) -> None:
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", settings.database_url)


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> Generator[None, None, None]:
    _run_migrations(database_url)
    yield


@pytest.fixture
def db_session(database_url: str, migrated_database: None) -> Generator[Session, None, None]:
    engine = create_engine(database_url)
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autocommit=False, autoflush=False)()

    for table in reversed(Base.metadata.sorted_tables):
        connection.execute(text(f"TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE"))

    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def seed_book(db_session: Session) -> Book:
    book = Book(
        book_id="atomic-habits",
        title="Atomic Habits",
        author="James Clear",
        core_thesis="Small changes compound into meaningful results over time.",
        review_status=ReviewStatus.human_reviewed,
    )
    db_session.add(book)
    db_session.commit()
    db_session.refresh(book)
    return book


@pytest.fixture
def seed_user(db_session: Session, seed_book: Book) -> User:
    user = User(
        user_id=uuid.uuid4(),
        email_encrypted=b"encrypted-email",
        auth_provider_id=f"auth-{uuid.uuid4()}",
        active_book_id=seed_book.book_id,
        timezone="America/Chicago",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user
