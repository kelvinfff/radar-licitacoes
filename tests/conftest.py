import copy
import json
import os
from collections.abc import Callable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config as ConfigAlembic

from radar.config import FUSO_PNCP, Config
from radar.db import conectar
from radar.pncp.cliente import ClientePNCP

RAIZ = Path(__file__).resolve().parent.parent
FIXTURES = RAIZ / "tests" / "fixtures" / "pncp"
URL_BASE = "https://pncp.gov.br/api"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://radar:radar@127.0.0.1:5433/radar_test"
)

# As respostas gravadas são de 10/11/2026, PE, modalidade 4 (Concorrência - Eletrônica).
DIA_GRAVADO = "20261110"
MODALIDADE_GRAVADA = 4


def paginas_gravadas(fonte: str) -> list[dict[str, Any]]:
    """Páginas gravadas de uma fonte, em ordem. Cópias: o teste pode alterá-las."""
    arquivos = sorted(FIXTURES.glob(f"{fonte}_mod4_{DIA_GRAVADO}_p*.json"))
    return [json.loads(arquivo.read_text(encoding="utf-8")) for arquivo in arquivos]


def item_gravado(fonte: str = "publicacao", indice: int = 0) -> dict[str, Any]:
    itens = [item for pagina in paginas_gravadas(fonte) for item in pagina["data"]]
    return copy.deepcopy(itens[indice])


class PNCPFalso:
    """Imita a API do PNCP com as respostas gravadas. O que não foi registrado devolve 204."""

    def __init__(self) -> None:
        self.rotas: dict[tuple[str, int, str, str, int], dict[str, Any]] = {}
        self.requisicoes: list[httpx.Request] = []
        self.falha: Callable[[httpx.Request], httpx.Response | None] | None = None

    def registrar(
        self,
        fonte: str,
        paginas: list[dict[str, Any]],
        *,
        modalidade: int = MODALIDADE_GRAVADA,
        inicio: str = DIA_GRAVADO,
        fim: str = DIA_GRAVADO,
    ) -> None:
        for numero, pagina in enumerate(paginas, start=1):
            self.rotas[(fonte, modalidade, inicio, fim, numero)] = pagina

    def registrar_gravadas(self, *fontes: str) -> None:
        for fonte in fontes:
            self.registrar(fonte, paginas_gravadas(fonte))

    def consultas(self) -> list[httpx.Request]:
        return [r for r in self.requisicoes if "/consulta/" in r.url.path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requisicoes.append(request)
        if self.falha is not None and (resposta := self.falha(request)) is not None:
            return resposta
        if request.url.path == "/api/pncp/v1/modalidades":
            return httpx.Response(200, content=(FIXTURES / "modalidades.json").read_bytes())
        p = request.url.params
        chave = (
            request.url.path.rsplit("/", 1)[-1],
            int(p["codigoModalidadeContratacao"]),
            p["dataInicial"],
            p["dataFinal"],
            int(p["pagina"]),
        )
        if chave in self.rotas:
            return httpx.Response(200, json=self.rotas[chave])
        return httpx.Response(204)


@pytest.fixture
def pncp() -> PNCPFalso:
    return PNCPFalso()


@pytest.fixture
def cliente(pncp: PNCPFalso) -> Iterator[ClientePNCP]:
    with ClientePNCP(
        URL_BASE,
        transport=httpx.MockTransport(pncp),
        max_tentativas=2,
        intervalo_minimo_s=0,
        dormir=lambda _segundos: None,
    ) as c:
        yield c


@pytest.fixture
def cfg() -> Config:
    return Config(
        _env_file=None,
        database_url=TEST_DATABASE_URL,
        pncp_url_base=URL_BASE,
        pncp_uf="PE",
        pncp_data_inicio=date(2026, 11, 10),
        pncp_janela_dias=7,
        pncp_tamanho_pagina=10,
        pncp_max_tentativas=2,
        pncp_intervalo_minimo_s=0,
        pncp_max_falhas_seguidas=3,
    )


def relogio_fixo(texto: str) -> Callable[[], datetime]:
    momento = datetime.fromisoformat(texto).replace(tzinfo=FUSO_PNCP)
    return lambda: momento


def config_alembic(url: str) -> ConfigAlembic:
    alembic = ConfigAlembic(str(RAIZ / "alembic.ini"))
    alembic.attributes["database_url"] = url
    alembic.attributes["configurar_logs"] = False
    return alembic


@pytest.fixture(scope="session")
def banco_de_testes() -> str:
    try:
        psycopg.connect(TEST_DATABASE_URL, connect_timeout=3).close()
    except psycopg.OperationalError as erro:
        pytest.fail(
            f"Postgres de testes indisponível em {TEST_DATABASE_URL} ({erro}). "
            "Suba com `docker compose up -d db` ou rode só os testes de unidade com "
            "`uv run pytest -m 'not integracao'`.",
            pytrace=False,
        )
    alembic = config_alembic(TEST_DATABASE_URL)
    command.downgrade(alembic, "base")
    command.upgrade(alembic, "head")
    return TEST_DATABASE_URL


@pytest.fixture
def conn(banco_de_testes: str) -> Iterator[psycopg.Connection]:
    with conectar(banco_de_testes) as conexao:
        conexao.execute(
            "truncate contratacoes, coletas_concluidas, registros_invalidos, execucoes_ingestao "
            "restart identity"
        )
        yield conexao
