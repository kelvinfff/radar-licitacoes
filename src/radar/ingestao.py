"""Coleta de contratações do PNCP: planeja as janelas pendentes e grava cada uma.

Unidade de trabalho: uma janela = (fonte, modalidade, intervalo de dias). Cada janela
é baixada por inteiro e gravada numa única transação, junto com a marcação dos dias
como concluídos. Se algo falhar no meio, nada da janela fica pela metade e a próxima
execução tenta de novo. Como a gravação é um upsert, repetir uma janela é inofensivo.
"""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any

import psycopg
from pydantic import ValidationError

from radar import repositorio
from radar.config import FUSO_PNCP, Config
from radar.pncp.cliente import ClientePNCP, ErroPNCP
from radar.pncp.modelos import ChaveContratacao, Contratacao, Fonte

log = logging.getLogger(__name__)

Relogio = Callable[[], datetime]


def agora_no_fuso_do_pncp() -> datetime:
    return datetime.now(FUSO_PNCP)


class ErroDeUso(ValueError):
    """Parâmetros inválidos para a ingestão (por exemplo, período fora do recorte)."""


class OutraExecucaoEmAndamento(RuntimeError):
    """Já existe uma ingestão rodando contra este banco."""


@dataclass(frozen=True)
class Janela:
    fonte: Fonte
    modalidade_id: int
    inicio: date
    fim: date


@dataclass
class Metricas:
    janelas: int = 0
    janelas_com_falha: int = 0
    janelas_incompletas: int = 0
    paginas: int = 0
    requisicoes: int = 0
    respostas_429: int = 0
    registros_recebidos: int = 0
    inseridas: int = 0
    atualizadas: int = 0
    inalteradas: int = 0
    fora_do_recorte: int = 0
    invalidos: int = 0
    duracao_s: float = 0.0
    interrompida: bool = False

    @property
    def status(self) -> str:
        if self.interrompida:
            return "falha"
        if self.janelas_com_falha or self.janelas_incompletas:
            return "parcial"
        return "sucesso"

    def como_dict(self) -> dict[str, Any]:
        return asdict(self) | {"status": self.status}


def planejar_janelas(
    fonte: Fonte,
    modalidades: Iterable[int],
    inicio: date,
    fim: date,
    concluidos: set[tuple[int, date]],
    janela_dias: int,
) -> list[Janela]:
    """Agrupa os dias pendentes de cada modalidade em janelas de até `janela_dias` dias.

    Dias já concluídos quebram a janela, para não baixar de novo o que já está no banco.
    A ordem é cronológica, para que os dias fiquem completos do mais antigo ao mais novo.
    """
    janelas: list[Janela] = []
    for modalidade in sorted(set(modalidades)):
        aberta: date | None = None
        dia = inicio
        while dia <= fim:
            if (modalidade, dia) in concluidos:
                if aberta is not None:
                    janelas.append(Janela(fonte, modalidade, aberta, dia - timedelta(days=1)))
                    aberta = None
            else:
                if aberta is None:
                    aberta = dia
                if (dia - aberta).days + 1 == janela_dias:
                    janelas.append(Janela(fonte, modalidade, aberta, dia))
                    aberta = None
            dia += timedelta(days=1)
        if aberta is not None:
            janelas.append(Janela(fonte, modalidade, aberta, fim))
    return sorted(janelas, key=lambda j: (j.inicio, j.modalidade_id))


def executar_ingestao(
    conn: psycopg.Connection,
    cliente: ClientePNCP,
    cfg: Config,
    *,
    de: date | None = None,
    ate: date | None = None,
    forcar: bool = False,
    relogio: Relogio = agora_no_fuso_do_pncp,
) -> Metricas:
    """Coleta o que estiver pendente (modo incremental) ou um período (modo intervalo).

    Incremental, sem `de`/`ate`: publicações desde o início do recorte até ontem, e depois
    as atualizações desde a primeira coleta. Intervalo: só publicações do período (backfill).
    O dia de hoje nunca é coletado, porque ainda não terminou.
    """
    iniciada_em = relogio()
    ontem = iniciada_em.astimezone(FUSO_PNCP).date() - timedelta(days=1)
    modo = "intervalo" if de is not None or ate is not None else "incremental"
    if modo == "intervalo":
        de = de or cfg.pncp_data_inicio
        ate = ate or ontem
        _validar_intervalo(de, ate, cfg.pncp_data_inicio, ontem)
    elif forcar:
        raise ErroDeUso("--forcar só pode ser usado com --de/--ate.")

    if not repositorio.tentar_lock_ingestao(conn):
        raise OutraExecucaoEmAndamento("outra ingestão está em andamento neste banco")
    try:
        execucao_id = repositorio.iniciar_execucao(conn, modo, iniciada_em)
        metricas = Metricas()
        coletor = _Coletor(conn, cliente, cfg, metricas, relogio)
        requisicoes_antes, respostas_429_antes = cliente.requisicoes, cliente.respostas_429
        t0 = time.monotonic()
        erro_fatal: BaseException | None = None
        try:
            modalidades = [m.id for m in cliente.modalidades()]
            if modo == "intervalo":
                assert de is not None and ate is not None
                coletor.coletar(Fonte.PUBLICACAO, modalidades, de, ate, forcar=forcar)
            else:
                coletor.coletar(Fonte.PUBLICACAO, modalidades, cfg.pncp_data_inicio, ontem)
                primeira = repositorio.primeira_coleta_de_publicacoes(conn)
                if primeira is not None and not metricas.interrompida:
                    desde = primeira.astimezone(FUSO_PNCP).date()
                    coletor.coletar(Fonte.ATUALIZACAO, modalidades, desde, ontem)
        except BaseException as erro:  # inclui Ctrl+C: registra a falha antes de sair
            erro_fatal = erro
            raise
        finally:
            metricas.requisicoes = cliente.requisicoes - requisicoes_antes
            metricas.respostas_429 = cliente.respostas_429 - respostas_429_antes
            metricas.duracao_s = round(time.monotonic() - t0, 3)
            status = "falha" if erro_fatal is not None else metricas.status
            try:
                repositorio.finalizar_execucao(
                    conn,
                    execucao_id,
                    status,
                    metricas.como_dict(),
                    relogio(),
                    repr(erro_fatal) if erro_fatal is not None else None,
                )
            except psycopg.Error:
                log.exception("não foi possível registrar o fim da execução %d", execucao_id)
        return metricas
    finally:
        try:
            repositorio.liberar_lock_ingestao(conn)
        except psycopg.Error:
            log.exception("não foi possível liberar o lock da ingestão")


def _validar_intervalo(de: date, ate: date, inicio_recorte: date, ontem: date) -> None:
    if de > ate:
        raise ErroDeUso(f"--de ({de:%d/%m/%Y}) é depois de --ate ({ate:%d/%m/%Y}).")
    if de < inicio_recorte:
        raise ErroDeUso(
            f"--de ({de:%d/%m/%Y}) é anterior ao início do recorte "
            f"({inicio_recorte:%d/%m/%Y}, PNCP_DATA_INICIO)."
        )
    if ate > ontem:
        raise ErroDeUso(
            f"--ate deve ser no máximo ontem ({ontem:%d/%m/%Y}): o dia de hoje ainda não terminou."
        )


class _Coletor:
    def __init__(
        self,
        conn: psycopg.Connection,
        cliente: ClientePNCP,
        cfg: Config,
        metricas: Metricas,
        relogio: Relogio,
    ) -> None:
        self._conn = conn
        self._cliente = cliente
        self._cfg = cfg
        self._metricas = metricas
        self._relogio = relogio
        self._falhas_seguidas = 0

    def coletar(
        self, fonte: Fonte, modalidades: list[int], inicio: date, fim: date, *, forcar: bool = False
    ) -> None:
        if inicio > fim or self._metricas.interrompida:
            return
        concluidos = (
            set() if forcar else repositorio.dias_concluidos(self._conn, fonte, inicio, fim)
        )
        janelas = planejar_janelas(
            fonte, modalidades, inicio, fim, concluidos, self._cfg.pncp_janela_dias
        )
        log.info(
            "plano fonte=%s de=%s ate=%s modalidades=%d janelas=%d",
            fonte,
            inicio,
            fim,
            len(modalidades),
            len(janelas),
        )
        for janela in janelas:
            self._metricas.janelas += 1
            try:
                self._processar(janela)
            except ErroPNCP as erro:
                self._registrar_falha(janela, erro)
            except psycopg.Error as erro:
                log.exception("erro de banco na janela %s", janela)
                self._registrar_falha(janela, erro)
            else:
                self._falhas_seguidas = 0
            if self._metricas.interrompida:
                return

    def _registrar_falha(self, janela: Janela, erro: Exception) -> None:
        self._metricas.janelas_com_falha += 1
        self._falhas_seguidas += 1
        log.error(
            "janela_falhou fonte=%s modalidade=%d de=%s ate=%s erro=%s",
            janela.fonte,
            janela.modalidade_id,
            janela.inicio,
            janela.fim,
            erro,
        )
        if self._falhas_seguidas >= self._cfg.pncp_max_falhas_seguidas:
            self._metricas.interrompida = True
            log.error(
                "ingestão interrompida depois de %d janelas seguidas com falha",
                self._falhas_seguidas,
            )

    def _processar(self, janela: Janela) -> None:
        cfg = self._cfg
        validos: dict[ChaveContratacao, tuple[Contratacao, dict[str, Any]]] = {}
        invalidos: list[tuple[dict[str, Any], str]] = []
        vistos: set[str] = set()
        total_informado = 0
        paginas = recebidos = fora_do_recorte = 0

        for pagina in self._cliente.contratacoes(
            janela.fonte,
            janela.modalidade_id,
            janela.inicio,
            janela.fim,
            uf=cfg.pncp_uf,
            tamanho_pagina=cfg.pncp_tamanho_pagina,
        ):
            if paginas == 0:
                total_informado = pagina.total_registros
            paginas += 1
            for item in pagina.data:
                recebidos += 1
                vistos.add(str(item.get("numeroControlePNCP") or f"sem-numero-{recebidos}"))
                try:
                    contratacao = Contratacao.model_validate(item)
                except ValidationError as erro:
                    invalidos.append((item, _resumir(erro)))
                    continue
                if (
                    contratacao.dia_publicacao < cfg.pncp_data_inicio
                    or contratacao.unidade_orgao.uf_sigla != cfg.pncp_uf
                ):
                    fora_do_recorte += 1
                    continue
                validos[contratacao.chave] = (contratacao, item)

        # Paginação por posição pode pular itens se a base mudar durante a leitura.
        # Se chegou menos do que a API anunciou, grava o que veio, mas não marca os dias
        # como concluídos: a próxima execução refaz a janela.
        completa = len(vistos) >= total_informado
        with self._conn.transaction():
            gravacao = repositorio.gravar_contratacoes(self._conn, validos.values())
            repositorio.guardar_invalidos(self._conn, janela.fonte, invalidos)
            if completa:
                repositorio.marcar_concluidos(
                    self._conn,
                    janela.fonte,
                    janela.modalidade_id,
                    janela.inicio,
                    janela.fim,
                    self._relogio(),
                )

        m = self._metricas
        m.paginas += paginas
        m.registros_recebidos += recebidos
        m.inseridas += gravacao.inseridas
        m.atualizadas += gravacao.atualizadas
        m.inalteradas += gravacao.inalteradas
        m.fora_do_recorte += fora_do_recorte
        m.invalidos += len(invalidos)
        if not completa:
            m.janelas_incompletas += 1
            log.warning(
                "janela_incompleta fonte=%s modalidade=%d de=%s ate=%s anunciados=%d recebidos=%d",
                janela.fonte,
                janela.modalidade_id,
                janela.inicio,
                janela.fim,
                total_informado,
                len(vistos),
            )
        if invalidos:
            log.warning(
                "registros_invalidos fonte=%s modalidade=%d quantidade=%d primeiro_erro=%r",
                janela.fonte,
                janela.modalidade_id,
                len(invalidos),
                invalidos[0][1],
            )
        log.info(
            "janela fonte=%s modalidade=%d de=%s ate=%s paginas=%d recebidos=%d "
            "inseridas=%d atualizadas=%d inalteradas=%d fora_do_recorte=%d invalidos=%d",
            janela.fonte,
            janela.modalidade_id,
            janela.inicio,
            janela.fim,
            paginas,
            recebidos,
            gravacao.inseridas,
            gravacao.atualizadas,
            gravacao.inalteradas,
            fora_do_recorte,
            len(invalidos),
        )


def _resumir(erro: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}" for e in erro.errors()[:5]
    )
