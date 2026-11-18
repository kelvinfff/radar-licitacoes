"""Conexão com o Postgres."""

import psycopg


def conectar(database_url: str) -> psycopg.Connection:
    """Conexão em autocommit: cada gravação usa um bloco `conn.transaction()` explícito.

    Assim nenhuma transação fica aberta enquanto o coletor espera a rede.
    """
    return psycopg.connect(database_url, autocommit=True, application_name="radar")


def url_sqlalchemy(database_url: str) -> str:
    """Converte a URL do libpq para o formato do SQLAlchemy com o driver psycopg 3."""
    for prefixo in ("postgresql://", "postgres://"):
        if database_url.startswith(prefixo):
            return "postgresql+psycopg://" + database_url.removeprefix(prefixo)
    return database_url
