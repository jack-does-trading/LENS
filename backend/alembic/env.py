from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base
from app.models import (  # noqa: F401 — register models with metadata
    Analysis,
    Book,
    DailyLog,
    Principle,
    SiteVisit,
    StreakProgress,
    Suggestion,
    User,
)

config = context.config
# alembic.ini's own sqlalchemy.url is always ignored (it is a stale local
# placeholder); the real source is DATABASE_URL via settings, exactly as in
# production. `-x db_url=...` is the one explicit override, and it is what
# tests/conftest.py uses -- previously conftest's set_main_option() call was
# overwritten here unconditionally, so `pytest` migrated whatever .env pointed
# at (the live Supabase instance in this repo) regardless of TEST_DATABASE_URL.
_explicit_url = context.get_x_argument(as_dictionary=True).get("db_url")
config.set_main_option("sqlalchemy.url", _explicit_url or settings.database_url)

if config.config_file_name is not None:
    # disable_existing_loggers=False, against fileConfig's default of True.
    #
    # The default disables every logger that already exists when this runs --
    # including all of app.*. That is invisible when migrations run as their own
    # process (`alembic upgrade head` on Render), and load-bearing when they run
    # in-process, which is exactly what tests/conftest.py does for every test
    # session. The effect was that any test asserting on an application log line
    # saw nothing at all, and passed or failed depending on whether migrations
    # had run first. A log assertion that silently stops asserting is worse than
    # no log assertion, and this pipeline's whole Phase 0 argument is that the
    # log line at the moment of fallback is the thing that catches an outage.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
