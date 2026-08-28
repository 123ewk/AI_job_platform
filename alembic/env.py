from logging.config import fileConfig

from alembic import context

# 让 Alembic 与适配层共用同一引擎来源（db/base.get_engine → AI_PLATFORM_DB / 默认路径）。
from db.base import DB_BACKEND, get_engine  # noqa: E402

if DB_BACKEND != "sqlite":
    raise NotImplementedError(f"DB_BACKEND={DB_BACKEND} 本期未实施，仅支持 sqlite")

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标元数据：db/models.py 的 Base.metadata（11 张表，含 Agent 4 新表）。
from db.models import Base  # noqa: E402

target_metadata = Base.metadata


def _db_url():
    # alembic.ini 未配 sqlalchemy.url，始终由 get_engine 推算（同 App 运行时）。
    return get_engine().url


def run_migrations_offline() -> None:
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 直接用 db.base 的引擎（含 WAL/FK pragma），使 Alembic 与运行库完全一致。
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
