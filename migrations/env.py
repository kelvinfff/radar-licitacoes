"""Ambiente do Alembic. As migrações são SQL explícito, sem modelos do ORM."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from radar.config import carregar_config
from radar.db import url_sqlalchemy

config = context.config
# Os testes desligam isso para o Alembic não reconfigurar os logs do pytest.
if config.config_file_name is not None and config.attributes.get("configurar_logs", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Os testes passam a URL do banco de testes por aqui; no uso normal vale DATABASE_URL.
url = config.attributes.get("database_url") or carregar_config().database_url


def run_migrations_online() -> None:
    engine = create_engine(url_sqlalchemy(url), poolclass=pool.NullPool)
    with engine.connect() as conexao:
        context.configure(connection=conexao, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Modo offline não é suportado: rode com um banco acessível.")
run_migrations_online()
