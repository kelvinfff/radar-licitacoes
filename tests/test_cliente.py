from datetime import date
from typing import Any

import httpx
import pytest

from radar.pncp.cliente import ClientePNCP, ErroPNCP
from radar.pncp.modelos import Fonte
from tests.conftest import URL_BASE, PNCPFalso


class Tempo:
    """Relógio falso: dormir só avança o relógio e anota quanto tempo foi pedido."""

    def __init__(self) -> None:
        self.agora = 0.0
        self.esperas: list[float] = []

    def relogio(self) -> float:
        return self.agora

    def dormir(self, segundos: float) -> None:
        self.esperas.append(segundos)
        self.agora += segundos


def criar_cliente(handler, tempo: Tempo | None = None, **opcoes) -> ClientePNCP:
    tempo = tempo or Tempo()
    padrao: dict[str, Any] = {
        "max_tentativas": 4,
        "intervalo_minimo_s": 0.0,
        "aleatorio": lambda: 0.0,  # sem jitter: esperas exatas
    }
    return ClientePNCP(
        URL_BASE,
        transport=httpx.MockTransport(handler),
        dormir=tempo.dormir,
        relogio=tempo.relogio,
        **(padrao | opcoes),
    )


def consultar(cliente: ClientePNCP):
    return list(
        cliente.contratacoes(
            Fonte.PUBLICACAO,
            4,
            date(2026, 11, 10),
            date(2026, 11, 10),
            uf="PE",
            tamanho_pagina=10,
        )
    )


def respostas_em_sequencia(*respostas):
    """Handler que devolve as respostas na ordem; exceções são levantadas."""
    fila = list(respostas)
    chamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chamadas.append(request)
        resposta = fila.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    handler.chamadas = chamadas  # type: ignore[attr-defined]
    return handler


def test_percorre_as_paginas_e_para_na_ultima(pncp: PNCPFalso):
    pncp.registrar_gravadas("publicacao")
    with criar_cliente(pncp) as cliente:
        paginas = consultar(cliente)

    assert [len(p.data) for p in paginas] == [10, 2]
    # Para pela contagem de páginas restantes, sem pedir uma terceira página.
    assert [r.url.params["pagina"] for r in pncp.requisicoes] == ["1", "2"]
    params = pncp.requisicoes[0].url.params
    assert params["dataInicial"] == "20261110"
    assert params["dataFinal"] == "20261110"
    assert params["codigoModalidadeContratacao"] == "4"
    assert params["uf"] == "PE"
    assert params["tamanhoPagina"] == "10"
    assert pncp.requisicoes[0].url.path == "/api/consulta/v1/contratacoes/publicacao"
    assert pncp.requisicoes[0].headers["User-Agent"].startswith("radar-licitacoes/")


def test_sem_conteudo_nao_gera_paginas(pncp: PNCPFalso):
    with criar_cliente(pncp) as cliente:
        assert consultar(cliente) == []
    assert len(pncp.requisicoes) == 1


def test_tenta_de_novo_em_erro_transitorio_com_backoff():
    handler = respostas_em_sequencia(httpx.Response(503), httpx.Response(502), httpx.Response(204))
    tempo = Tempo()
    with criar_cliente(handler, tempo) as cliente:
        assert consultar(cliente) == []

    assert len(handler.chamadas) == 3
    assert tempo.esperas == [1.0, 2.0]


def test_respeita_o_retry_after_do_servidor():
    handler = respostas_em_sequencia(
        httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(204)
    )
    tempo = Tempo()
    with criar_cliente(handler, tempo) as cliente:
        consultar(cliente)

    assert tempo.esperas == [7.0]


def test_limite_de_requisicoes_sem_retry_after_espera_a_janela_e_desacelera():
    # O PNCP responde 429 com uma página HTML e sem Retry-After.
    handler = respostas_em_sequencia(
        httpx.Response(429, text="<html>Limite de requisições excedido</html>"),
        httpx.Response(429),
        httpx.Response(204),
    )
    tempo = Tempo()
    with criar_cliente(handler, tempo, intervalo_minimo_s=5.0) as cliente:
        consultar(cliente)

        assert tempo.esperas == [30.0, 60.0]
        assert cliente.respostas_429 == 2
        # 5 s → 10 s → 20 s com os dois 429; o sucesso seguinte já devolve 5%.
        assert cliente.intervalo_atual_s == pytest.approx(19.0)


def test_ritmo_volta_ao_minimo_depois_de_varios_sucessos():
    handler = respostas_em_sequencia(httpx.Response(429), *[httpx.Response(204)] * 20)
    with criar_cliente(handler, intervalo_minimo_s=5.0) as cliente:
        for _ in range(16):
            consultar(cliente)

        assert cliente.intervalo_atual_s == 5.0


def test_erro_de_rede_tambem_tenta_de_novo():
    handler = respostas_em_sequencia(httpx.ConnectTimeout("lento demais"), httpx.Response(204))
    with criar_cliente(handler) as cliente:
        assert consultar(cliente) == []
    assert len(handler.chamadas) == 2


def test_desiste_depois_do_numero_maximo_de_tentativas():
    handler = respostas_em_sequencia(*[httpx.Response(500)] * 3)
    with (
        criar_cliente(handler, max_tentativas=3) as cliente,
        pytest.raises(ErroPNCP, match="3 tentativas"),
    ):
        consultar(cliente)
    assert len(handler.chamadas) == 3


def test_erro_de_parametro_nao_tenta_de_novo():
    corpo = {"message": "Tamanho de página inválido", "status": "400"}
    handler = respostas_em_sequencia(httpx.Response(400, json=corpo))
    with (
        criar_cliente(handler) as cliente,
        pytest.raises(ErroPNCP, match="Tamanho de página inválido"),
    ):
        consultar(cliente)
    assert len(handler.chamadas) == 1


def test_resposta_que_nao_e_json_vira_erro():
    handler = respostas_em_sequencia(httpx.Response(200, text="<html>manutenção</html>"))
    with criar_cliente(handler) as cliente, pytest.raises(ErroPNCP, match="formato inesperado"):
        consultar(cliente)


def test_mantem_intervalo_minimo_entre_requisicoes(pncp: PNCPFalso):
    pncp.registrar_gravadas("publicacao")
    tempo = Tempo()
    with criar_cliente(pncp, tempo, intervalo_minimo_s=0.5) as cliente:
        consultar(cliente)

    assert tempo.esperas == [0.5]


def test_lista_de_modalidades(pncp: PNCPFalso):
    with criar_cliente(pncp) as cliente:
        modalidades = cliente.modalidades()

    assert len(modalidades) == 20
    assert modalidades[5].id == 6
    assert modalidades[5].nome == "Pregão - Eletrônico"
    assert cliente.requisicoes == 1
