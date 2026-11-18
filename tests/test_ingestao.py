"""Ingestão de ponta a ponta: API falsa (respostas gravadas) + Postgres de verdade."""

from datetime import date, datetime
from decimal import Decimal

import httpx
import psycopg
import pytest

from radar import repositorio
from radar.config import FUSO_PNCP, Config
from radar.db import conectar
from radar.ingestao import OutraExecucaoEmAndamento, executar_ingestao
from radar.pncp.cliente import ClientePNCP
from radar.pncp.modelos import Fonte
from tests.conftest import PNCPFalso, item_gravado, paginas_gravadas, relogio_fixo

pytestmark = pytest.mark.integracao

# Hoje = 11/11 → coleta até 10/11, o dia gravado.
DEPOIS_DO_DIA_GRAVADO = relogio_fixo("2026-11-11T06:00")
TOTAL_DE_MODALIDADES = 20


def contar(conn: psycopg.Connection, sql: str, *params: object) -> int:
    linha = conn.execute(sql, params).fetchone()
    assert linha is not None
    return int(linha[0])


def test_primeira_coleta_grava_as_contratacoes_e_marca_os_dias(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.registrar_gravadas("publicacao")

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.status == "sucesso"
    assert (m.janelas, m.paginas, m.registros_recebidos) == (TOTAL_DE_MODALIDADES, 2, 12)
    assert (m.inseridas, m.atualizadas, m.inalteradas) == (12, 0, 0)
    assert contar(conn, "select count(*) from contratacoes") == 12
    assert contar(
        conn, "select count(*) from coletas_concluidas where dia = %s", date(2026, 11, 10)
    ) == (TOTAL_DE_MODALIDADES)
    # Sem coleta anterior, ainda não há atualizações a buscar.
    assert not [r for r in pncp.consultas() if "atualizacao" in r.url.path]

    linha = conn.execute(
        """
        select orgao_cnpj, ano_compra, sequencial_compra, uf, municipio_nome, modalidade_id,
               valor_total_estimado, data_publicacao_pncp, bruto
        from contratacoes where numero_controle_pncp = '24416174000106-1-000097/2026'
        """
    ).fetchone()
    assert linha is not None
    cnpj, ano, seq, uf, municipio, modalidade, valor, publicada, bruto = linha
    assert (cnpj, ano, seq, uf, municipio, modalidade) == (
        "24416174000106",
        2026,
        97,
        "PE",
        "Recife",
        4,
    )
    assert valor == Decimal("918831.27")
    assert publicada == datetime(2026, 11, 10, 6, 30, 20, tzinfo=FUSO_PNCP)
    assert bruto == item_gravado("publicacao", 0)

    execucao = conn.execute("select modo, status, metricas from execucoes_ingestao").fetchone()
    assert execucao is not None
    assert execucao[:2] == ("incremental", "sucesso")
    assert execucao[2]["inseridas"] == 12


def test_segunda_execucao_nao_refaz_o_que_ja_foi_coletado(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.registrar_gravadas("publicacao")
    executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)
    pncp.requisicoes.clear()

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.status == "sucesso"
    assert m.janelas == 0
    assert pncp.consultas() == []


def test_forcar_refaz_o_periodo_sem_duplicar(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.registrar_gravadas("publicacao")
    executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    m = executar_ingestao(
        conn,
        cliente,
        cfg,
        de=date(2026, 11, 10),
        ate=date(2026, 11, 10),
        forcar=True,
        relogio=DEPOIS_DO_DIA_GRAVADO,
    )

    assert (m.inseridas, m.atualizadas, m.inalteradas) == (0, 0, 12)
    assert contar(conn, "select count(*) from contratacoes") == 12


def test_item_alterado_na_api_e_atualizado_no_banco(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.registrar_gravadas("publicacao")
    executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    paginas = paginas_gravadas("publicacao")
    paginas[0]["data"][0] |= {"valorTotalHomologado": 900000.5, "situacaoCompraId": 4}
    pncp.registrar("publicacao", paginas)
    m = executar_ingestao(
        conn, cliente, cfg, de=date(2026, 11, 10), forcar=True, relogio=DEPOIS_DO_DIA_GRAVADO
    )

    assert (m.inseridas, m.atualizadas, m.inalteradas) == (0, 1, 11)
    homologado, situacao, mudou = conn.execute(
        """
        select valor_total_homologado, situacao_id, atualizado_em > coletado_em
        from contratacoes where numero_controle_pncp = '24416174000106-1-000097/2026'
        """
    ).fetchone() or (None, None, None)
    assert (homologado, situacao, mudou) == (Decimal("900000.5"), 4, True)


def test_atualizacoes_sao_coletadas_desde_a_primeira_coleta(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    # Simula que as publicações de 01/01 a 09/11 foram coletadas em 10/11 de manhã.
    cfg = cfg.model_copy(update={"pncp_data_inicio": date(2026, 1, 1)})
    with conn.transaction():
        for modalidade in range(1, TOTAL_DE_MODALIDADES + 1):
            repositorio.marcar_concluidos(
                conn,
                Fonte.PUBLICACAO,
                modalidade,
                date(2026, 1, 1),
                date(2026, 11, 9),
                datetime(2026, 11, 10, 6, 0, tzinfo=FUSO_PNCP),
            )
    pncp.registrar_gravadas("publicacao", "atualizacao")

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    # Publicações de 10/11 (12) + atualizações de 10/11 (29, das quais uma foi publicada
    # em 2025, fora do recorte, e 12 são as mesmas publicações, sem mudança).
    assert m.status == "sucesso"
    assert m.janelas == 2 * TOTAL_DE_MODALIDADES
    assert m.registros_recebidos == 12 + 29
    assert m.fora_do_recorte == 1
    assert (m.inseridas, m.atualizadas, m.inalteradas) == (12 + 16, 0, 12)
    assert contar(conn, "select count(*) from contratacoes") == 28
    assert (
        contar(
            conn,
            "select count(*) from coletas_concluidas where fonte = 'atualizacao' and dia = %s",
            date(2026, 11, 10),
        )
        == TOTAL_DE_MODALIDADES
    )


def test_falha_numa_janela_nao_marca_o_dia_e_e_retomada_depois(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.registrar_gravadas("publicacao")
    pncp.falha = lambda r: (
        httpx.Response(500) if r.url.params.get("codigoModalidadeContratacao") == "4" else None
    )

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.status == "parcial"
    assert m.janelas_com_falha == 1
    assert not m.interrompida
    assert contar(conn, "select count(*) from contratacoes") == 0
    assert contar(conn, "select count(*) from coletas_concluidas where modalidade_id = 4") == 0
    assert contar(conn, "select count(*) from coletas_concluidas") == TOTAL_DE_MODALIDADES - 1

    pncp.falha = None
    pncp.requisicoes.clear()
    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.status == "sucesso"
    assert m.janelas == 1
    assert m.inseridas == 12
    assert {r.url.params["codigoModalidadeContratacao"] for r in pncp.consultas()} == {"4"}


def test_interrompe_depois_de_falhas_seguidas(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    pncp.falha = lambda r: httpx.Response(503) if "/consulta/" in r.url.path else None

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.interrompida
    assert m.status == "falha"
    assert (m.janelas, m.janelas_com_falha) == (3, 3)
    assert conn.execute("select status from execucoes_ingestao").fetchone() == ("falha",)


def test_item_invalido_vai_para_a_quarentena_uma_unica_vez(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    paginas = paginas_gravadas("publicacao")
    paginas[0]["data"][0]["objetoCompra"] = None
    pncp.registrar("publicacao", paginas)

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)
    executar_ingestao(
        conn, cliente, cfg, de=date(2026, 11, 10), forcar=True, relogio=DEPOIS_DO_DIA_GRAVADO
    )

    assert m.status == "sucesso"
    assert (m.invalidos, m.inseridas) == (1, 11)
    numero, erro = conn.execute(
        "select numero_controle_pncp, erro from registros_invalidos"
    ).fetchone() or (None, None)
    assert numero == "24416174000106-1-000097/2026"
    assert "objetoCompra" in erro
    assert contar(conn, "select count(*) from registros_invalidos") == 1


def test_paginacao_que_perde_itens_nao_marca_o_dia(
    conn: psycopg.Connection, cliente: ClientePNCP, pncp: PNCPFalso, cfg: Config
):
    # A primeira página anuncia 12 registros, mas a segunda some (204).
    pncp.registrar("publicacao", paginas_gravadas("publicacao")[:1])

    m = executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert m.status == "parcial"
    assert m.janelas_incompletas == 1
    assert m.inseridas == 10
    assert contar(conn, "select count(*) from coletas_concluidas where modalidade_id = 4") == 0


def test_nao_roda_duas_ingestoes_ao_mesmo_tempo(
    conn: psycopg.Connection, cliente: ClientePNCP, cfg: Config, banco_de_testes: str
):
    with conectar(banco_de_testes) as outra:
        outra.execute("select pg_advisory_lock(%s)", (repositorio.CHAVE_LOCK_INGESTAO,))
        with pytest.raises(OutraExecucaoEmAndamento):
            executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO)

    assert contar(conn, "select count(*) from execucoes_ingestao") == 0
    # Liberado o lock, a ingestão volta a rodar.
    assert executar_ingestao(conn, cliente, cfg, relogio=DEPOIS_DO_DIA_GRAVADO).status == "sucesso"
