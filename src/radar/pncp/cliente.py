"""Cliente HTTP da API pública do PNCP, com ritmo controlado e novas tentativas.

Limite medido em 17/11/2026: uma rajada de ~12 requisições recebe HTTP 429 por cerca
de 25 s, sem cabeçalho Retry-After; uma requisição a cada 5 s não recebeu nenhum 429.
"""

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import date
from types import TracebackType
from typing import Self

import httpx
from pydantic import TypeAdapter, ValidationError

from radar.pncp.modelos import Fonte, Modalidade, Pagina

log = logging.getLogger(__name__)

USER_AGENT = "radar-licitacoes/0.1"
STATUS_TRANSITORIOS = frozenset({429, 500, 502, 503, 504})
ESPERA_MAXIMA_S = 120.0
# Sem Retry-After, a primeira espera depois de um 429 cobre a janela do limite (~25 s).
ESPERA_LIMITE_S = 30.0
# A cada 429 o intervalo entre requisições dobra até este teto; a cada sucesso ele
# volta 5% em direção ao mínimo configurado.
INTERVALO_MAXIMO_S = 30.0

_LISTA_MODALIDADES = TypeAdapter(list[Modalidade])


class ErroPNCP(Exception):
    """A API recusou a requisição, respondeu algo inesperado ou falhou em todas as tentativas."""


class ClientePNCP:
    def __init__(
        self,
        url_base: str,
        *,
        timeout_s: float = 60.0,
        max_tentativas: int = 5,
        intervalo_minimo_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Callable[[], float] = time.monotonic,
        aleatorio: Callable[[], float] = random.random,
    ) -> None:
        self._http = httpx.Client(
            base_url=url_base,
            timeout=timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )
        self._max_tentativas = max_tentativas
        self._intervalo_minimo_s = intervalo_minimo_s
        self._dormir = dormir
        self._relogio = relogio
        self._aleatorio = aleatorio
        self._ultima_requisicao: float | None = None
        self.intervalo_atual_s = intervalo_minimo_s
        self.requisicoes = 0
        self.respostas_429 = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def modalidades(self) -> list[Modalidade]:
        resposta = self._get("/pncp/v1/modalidades", {})
        if resposta is None:
            raise ErroPNCP("a API não retornou nenhuma modalidade")
        try:
            return _LISTA_MODALIDADES.validate_json(resposta.content)
        except ValidationError as erro:
            raise ErroPNCP(f"lista de modalidades em formato inesperado: {erro}") from erro

    def contratacoes(
        self,
        fonte: Fonte,
        modalidade_id: int,
        inicio: date,
        fim: date,
        *,
        uf: str,
        tamanho_pagina: int,
    ) -> Iterator[Pagina]:
        """Percorre todas as páginas de uma consulta, da primeira à última."""
        caminho = f"/consulta/v1/contratacoes/{fonte.value}"
        params: dict[str, str | int] = {
            "dataInicial": inicio.strftime("%Y%m%d"),
            "dataFinal": fim.strftime("%Y%m%d"),
            "codigoModalidadeContratacao": modalidade_id,
            "uf": uf,
            "tamanhoPagina": tamanho_pagina,
        }
        numero = 1
        while True:
            resposta = self._get(caminho, {**params, "pagina": numero})
            if resposta is None:  # 204: consulta vazia ou página além da última
                return
            try:
                pagina = Pagina.model_validate_json(resposta.content)
            except ValidationError as erro:
                raise ErroPNCP(f"página em formato inesperado em {caminho}: {erro}") from erro
            yield pagina
            if pagina.paginas_restantes <= 0 or not pagina.data:
                return
            numero += 1

    def _get(self, caminho: str, params: dict[str, str | int]) -> httpx.Response | None:
        """GET com novas tentativas. Retorna None para 204 (sem conteúdo)."""
        for tentativa in range(1, self._max_tentativas + 1):
            self._respeitar_intervalo()
            self.requisicoes += 1
            status: int | None = None
            retry_after: str | None = None
            try:
                resposta = self._http.get(caminho, params=params)
            except httpx.TransportError as erro:  # timeout, conexão recusada, etc.
                motivo = f"{type(erro).__name__}: {erro}"
            else:
                status = resposta.status_code
                if status == 204 or resposta.is_success:
                    self._recuperar_ritmo()
                    return None if status == 204 else resposta
                if status not in STATUS_TRANSITORIOS:
                    raise ErroPNCP(f"HTTP {status} em {caminho}: {_mensagem_de_erro(resposta)}")
                motivo = f"HTTP {status}"
                retry_after = resposta.headers.get("Retry-After")
                if status == 429:
                    self.respostas_429 += 1
                    self._reduzir_ritmo()

            if tentativa == self._max_tentativas:
                raise ErroPNCP(f"{caminho} falhou em {tentativa} tentativas (última: {motivo})")
            espera = self._espera(tentativa, status, retry_after)
            log.warning(
                "pncp_nova_tentativa caminho=%s tentativa=%d motivo=%r espera_s=%.1f "
                "intervalo_s=%.1f",
                caminho,
                tentativa,
                motivo,
                espera,
                self.intervalo_atual_s,
            )
            self._dormir(espera)
        raise AssertionError("inalcançável")

    def _espera(self, tentativa: int, status: int | None, retry_after: str | None) -> float:
        """Retry-After do servidor, se houver; senão, backoff exponencial com jitter.

        Erros de rede e 5xx: 1, 2, 4, 8 s... Limite de requisições (429): 30, 60, 120 s.
        O jitter só aumenta a espera, para nunca voltar antes do limite liberar.
        """
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), ESPERA_MAXIMA_S)
            except ValueError:
                pass  # Retry-After em formato de data: cai no backoff normal
        primeira = ESPERA_LIMITE_S if status == 429 else 1.0
        base = min(primeira * 2.0 ** (tentativa - 1), ESPERA_MAXIMA_S)
        return base * (1.0 + 0.25 * self._aleatorio())

    def _reduzir_ritmo(self) -> None:
        self.intervalo_atual_s = min(max(self.intervalo_atual_s * 2, 1.0), INTERVALO_MAXIMO_S)

    def _recuperar_ritmo(self) -> None:
        self.intervalo_atual_s = max(self._intervalo_minimo_s, self.intervalo_atual_s * 0.95)

    def _respeitar_intervalo(self) -> None:
        if self._ultima_requisicao is not None:
            falta = self.intervalo_atual_s - (self._relogio() - self._ultima_requisicao)
            if falta > 0:
                self._dormir(falta)
        self._ultima_requisicao = self._relogio()


def _mensagem_de_erro(resposta: httpx.Response) -> str:
    try:
        corpo = resposta.json()
    except ValueError:
        return resposta.text[:200]
    if isinstance(corpo, dict) and "message" in corpo:
        return str(corpo["message"])
    return str(corpo)[:200]
