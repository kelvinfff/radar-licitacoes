import psycopg
import pytest
from alembic import command

from radar import cli
from radar.config import carregar_config
from tests.conftest import config_alembic


def test_forcar_sem_periodo_e_erro_de_uso(capsys):
    with pytest.raises(SystemExit) as saida:
        cli.main(["ingest", "--forcar"])

    assert saida.value.code == 2
    assert "--forcar exige" in capsys.readouterr().err


def test_data_em_formato_errado_e_erro_de_uso(capsys):
    with pytest.raises(SystemExit) as saida:
        cli.main(["ingest", "--de", "10/11/2026"])

    assert saida.value.code == 2
    assert "AAAA-MM-DD" in capsys.readouterr().err


@pytest.mark.integracao
def test_status_com_banco_vazio(conn: psycopg.Connection, banco_de_testes, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", banco_de_testes)
    carregar_config.cache_clear()
    try:
        assert cli.main(["status"]) == 0
    finally:
        carregar_config.cache_clear()

    saida = capsys.readouterr().out
    assert "Contratações no banco: 0" in saida
    assert "Última execução: nenhuma" in saida


@pytest.mark.integracao
def test_migracoes_descem_e_sobem(banco_de_testes):
    alembic = config_alembic(banco_de_testes)
    try:
        command.downgrade(alembic, "base")
        with psycopg.connect(banco_de_testes) as conn:
            assert conn.execute("select to_regclass('contratacoes')").fetchone() == (None,)
    finally:
        command.upgrade(alembic, "head")
    with psycopg.connect(banco_de_testes) as conn:
        assert conn.execute("select to_regclass('contratacoes')").fetchone() == ("contratacoes",)
