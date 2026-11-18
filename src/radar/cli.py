"""Linha de comando: `radar ingest` e `radar status`."""

import argparse
import logging
import os
from datetime import date, timedelta

from radar import repositorio
from radar.config import FUSO_PNCP, Config, carregar_config
from radar.db import conectar
from radar.ingestao import (
    ErroDeUso,
    OutraExecucaoEmAndamento,
    agora_no_fuso_do_pncp,
    executar_ingestao,
)
from radar.pncp.cliente import ClientePNCP, ErroPNCP
from radar.pncp.modelos import Fonte

log = logging.getLogger("radar")

SUCESSO, FALHA, ERRO_DE_USO = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="Radar de Licitações")
    comandos = parser.add_subparsers(dest="comando", required=True)

    ingest = comandos.add_parser(
        "ingest",
        help="coleta contratações do PNCP",
        description=(
            "Sem --de/--ate, coleta tudo o que estiver pendente desde PNCP_DATA_INICIO até "
            "ontem, mais as atualizações. Com --de/--ate, coleta as publicações do período."
        ),
    )
    ingest.add_argument("--de", type=_data, metavar="AAAA-MM-DD", help="primeiro dia")
    ingest.add_argument("--ate", type=_data, metavar="AAAA-MM-DD", help="último dia (até ontem)")
    ingest.add_argument(
        "--forcar", action="store_true", help="coleta de novo dias já concluídos (exige --de/--ate)"
    )
    comandos.add_parser("status", help="mostra o que já foi coletado")

    args = parser.parse_args(argv)
    if args.comando == "ingest" and args.forcar and args.de is None and args.ate is None:
        parser.error("--forcar exige --de e/ou --ate")

    _configurar_logs()
    cfg = carregar_config()
    if args.comando == "ingest":
        return _ingest(cfg, args.de, args.ate, args.forcar)
    return _status(cfg)


def _ingest(cfg: Config, de: date | None, ate: date | None, forcar: bool) -> int:
    cliente = ClientePNCP(
        cfg.pncp_url_base,
        timeout_s=cfg.pncp_timeout_s,
        max_tentativas=cfg.pncp_max_tentativas,
        intervalo_minimo_s=cfg.pncp_intervalo_minimo_s,
    )
    with conectar(cfg.database_url) as conn, cliente:
        try:
            metricas = executar_ingestao(conn, cliente, cfg, de=de, ate=ate, forcar=forcar)
        except ErroDeUso as erro:
            log.error("%s", erro)
            return ERRO_DE_USO
        except OutraExecucaoEmAndamento as erro:
            log.warning("nada a fazer: %s", erro)
            return SUCESSO
        except ErroPNCP as erro:
            log.error("ingestão abortada: %s", erro)
            return FALHA
    log.info(
        "fim status=%s janelas=%d falhas=%d incompletas=%d requisicoes=%d respostas_429=%d "
        "inseridas=%d atualizadas=%d inalteradas=%d invalidos=%d duracao_s=%.1f",
        metricas.status,
        metricas.janelas,
        metricas.janelas_com_falha,
        metricas.janelas_incompletas,
        metricas.requisicoes,
        metricas.respostas_429,
        metricas.inseridas,
        metricas.atualizadas,
        metricas.inalteradas,
        metricas.invalidos,
        metricas.duracao_s,
    )
    return SUCESSO if metricas.status == "sucesso" else FALHA


def _status(cfg: Config) -> int:
    ontem = agora_no_fuso_do_pncp().date() - timedelta(days=1)
    with conectar(cfg.database_url) as conn:
        banco = repositorio.resumo_banco(conn)
        publicacoes = repositorio.resumo_coleta(conn, Fonte.PUBLICACAO, cfg.pncp_data_inicio, ontem)
        primeira = repositorio.primeira_coleta_de_publicacoes(conn)
        inicio_atualizacoes = primeira.astimezone(FUSO_PNCP).date() if primeira else None
        atualizacoes = (
            repositorio.resumo_coleta(conn, Fonte.ATUALIZACAO, inicio_atualizacoes, ontem)
            if inicio_atualizacoes
            else None
        )

    print(f"Recorte: UF {cfg.pncp_uf}, publicadas a partir de {cfg.pncp_data_inicio:%d/%m/%Y}")
    periodo = ""
    if banco["primeira_publicacao"]:
        periodo = (
            f" (publicadas de {banco['primeira_publicacao']:%d/%m/%Y}"
            f" a {banco['ultima_publicacao']:%d/%m/%Y})"
        )
    print(f"Contratações no banco: {_milhar(banco['contratacoes'])}{periodo}")

    dias_esperados = (ontem - cfg.pncp_data_inicio).days + 1
    print(
        f"Publicações: {publicacoes.dias_completos} de {dias_esperados} dias completos "
        f"até ontem ({ontem:%d/%m/%Y})"
    )
    if atualizacoes is not None and inicio_atualizacoes is not None:
        esperados = (ontem - inicio_atualizacoes).days + 1
        if esperados > 0:
            print(
                f"Atualizações: {atualizacoes.dias_completos} de {esperados} dias completos "
                f"desde {inicio_atualizacoes:%d/%m/%Y}"
            )
        else:
            print(
                f"Atualizações: a partir de {inicio_atualizacoes:%d/%m/%Y} "
                "(cada dia é coletado depois que termina)"
            )
    else:
        print("Atualizações: começam a ser coletadas depois da primeira coleta de publicações")

    execucao = banco["ultima_execucao"]
    if execucao:
        id_, modo, status, iniciada, _finalizada, metricas = execucao
        print(
            f"Última execução: #{id_} {modo}, {status}, iniciada em "
            f"{iniciada.astimezone(FUSO_PNCP):%d/%m/%Y %H:%M}: "
            f"{metricas.get('inseridas', 0)} novas, {metricas.get('atualizadas', 0)} atualizadas, "
            f"{metricas.get('janelas_com_falha', 0)} janelas com falha"
        )
    else:
        print("Última execução: nenhuma")
    print(f"Registros inválidos guardados: {banco['registros_invalidos']}")
    return SUCESSO


def _data(texto: str) -> date:
    try:
        return date.fromisoformat(texto)
    except ValueError as erro:
        raise argparse.ArgumentTypeError(f"data inválida {texto!r}, use AAAA-MM-DD") from erro


def _milhar(numero: int) -> str:
    return f"{numero:,}".replace(",", ".")


def _configurar_logs() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
