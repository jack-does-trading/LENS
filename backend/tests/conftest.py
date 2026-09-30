import os
import uuid
from types import SimpleNamespace
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
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
    # Passed as an -x argument, not set_main_option: alembic/env.py resolves
    # the URL itself and only an -x override outranks its settings-derived
    # default. A set_main_option() here is silently discarded.
    alembic_cfg.cmd_opts = SimpleNamespace(x=[f"db_url={database_url}"])
    command.upgrade(alembic_cfg, "head")


# Every db_session TRUNCATEs each table in the schema. That is fine against a
# throwaway container and catastrophic against a shared one, so the suite
# refuses any host that isn't plainly local unless the operator says otherwise
# with LENS_ALLOW_REMOTE_TEST_DB=1. Falling back to settings.database_url
# (i.e. whatever .env points at, which in this repo is the live Supabase
# instance) is what made a bare `pytest` able to reach production at all --
# the guard below is what stops it, since the fallback itself is load-bearing
# for anyone whose local DSN genuinely lives in .env.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db", ""})


def _assert_local(url: str) -> str:
    if os.environ.get("LENS_ALLOW_REMOTE_TEST_DB") == "1":
        return url
    host = make_url(url).host or ""
    if host not in _LOCAL_HOSTS:
        raise pytest.UsageError(
            f"Refusing to run the test suite against non-local database host {host!r}.\n"
            "Every test TRUNCATEs every table, which would destroy real data and "
            "hold ACCESS EXCLUSIVE locks on a live instance.\n"
            "Start the local container (docker compose up -d) and set "
            "TEST_DATABASE_URL=postgresql://lens:lens@localhost:5432/lens, or set "
            "LENS_ALLOW_REMOTE_TEST_DB=1 if you are certain the target is disposable."
        )
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    return _assert_local(os.environ.get("TEST_DATABASE_URL", settings.database_url))


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


def pytest_collection_modifyitems(items) -> None:
    """Refuse a test that claims to be both cassette-replayed and live.

    `eval` means "no secrets, no network, runs on every PR"; `eval_live` means
    "hits a paid provider". A test carrying both is selected by the PR gate,
    where it either bills on every push or silently skips for want of a key --
    and a gate that sometimes skips is not a gate. This is a real mistake that
    happened once: the live judge check inherited a module-level `eval` mark and
    was quietly making sixteen Groq calls inside the offline suite.
    """
    for item in items:
        markers = {m.name for m in item.iter_markers()}
        if {"eval", "eval_live"} <= markers:
            raise pytest.UsageError(
                f"{item.nodeid} is marked both `eval` and `eval_live`. Move it to its "
                "own module -- a module-level pytestmark cannot be removed per test."
            )
