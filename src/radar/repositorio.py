"""Gravação das contratações e controle da coleta no Postgres."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from radar.pncp.modelos import Contratacao, Fonte

# Chave fixa do advisory lock que impede duas ingestões ao mesmo tempo.
CHAVE_LOCK_INGESTAO = 7_270_001

_COLUNAS = [
    "numero_controle_pncp",
    "orgao_cnpj",
    "ano_compra",
    "sequencial_compra",
    "orgao_razao_social",
    "orgao_esfera",
    "orgao_poder",
    "unidade_codigo",
    "unidade_nome",
    "uf",
    "municipio_nome",
    "municipio_codigo_ibge",
    "numero_compra",
    "processo",
    "modalidade_id",
    "modalidade_nome",
    "modo_disputa_id",
    "modo_disputa_nome",
    "objeto",
    "informacao_complementar",
    "srp",
    "valor_total_estimado",
    "valor_total_homologado",
    "situacao_id",
    "situacao_nome",
    "data_publicacao_pncp",
    "data_abertura_proposta",
    "data_encerramento_proposta",
    "data_inclusao",
    "data_atualizacao",
    "data_atualizacao_global",
    "link_sistema_origem",
    "bruto",
]

# Só reescreve a linha quando o item mudou. `old` no RETURNING (Postgres 18) diz se a
# linha já existia; quando nada muda, o WHERE descarta a linha e nada é retornado.
_SQL_UPSERT = f"""
    insert into contratacoes ({", ".join(_COLUNAS)})
    values ({", ".join(f"%({c})s" for c in _COLUNAS)})
    on conflict (orgao_cnpj, ano_compra, sequencial_compra) do update set
        {", ".join(f"{c} = excluded.{c}" for c in _COLUNAS)},
        atualizado_em = now()
    where contratacoes.bruto is distinct from excluded.bruto
    returning old.id is null as inserida
"""


@dataclass
class ResultadoGravacao:
    inseridas: int = 0
    atualizadas: int = 0
    inalteradas: int = 0


def gravar_contratacoes(
    conn: psycopg.Connection, itens: Iterable[tuple[Contratacao, dict[str, Any]]]
) -> ResultadoGravacao:
    """Upsert pela chave natural. Rodar duas vezes com os mesmos dados não muda nada."""
    linhas = [_linha(contratacao, bruto) for contratacao, bruto in itens]
    resultado = ResultadoGravacao()
    if not linhas:
        return resultado
    with conn.cursor() as cur:
        cur.executemany(_SQL_UPSERT, linhas, returning=True)
        for _ in cur.results():
            retorno = cur.fetchone()
            if retorno is None:
                resultado.inalteradas += 1
            elif retorno[0]:
                resultado.inseridas += 1
            else:
                resultado.atualizadas += 1
    return resultado


def _linha(c: Contratacao, bruto: dict[str, Any]) -> dict[str, Any]:
    return {
        "numero_controle_pncp": c.numero_controle_pncp,
        "orgao_cnpj": c.orgao_entidade.cnpj,
        "ano_compra": c.ano_compra,
        "sequencial_compra": c.sequencial_compra,
        "orgao_razao_social": c.orgao_entidade.razao_social,
        "orgao_esfera": c.orgao_entidade.esfera_id,
        "orgao_poder": c.orgao_entidade.poder_id,
        "unidade_codigo": c.unidade_orgao.codigo_unidade,
        "unidade_nome": c.unidade_orgao.nome_unidade,
        "uf": c.unidade_orgao.uf_sigla,
        "municipio_nome": c.unidade_orgao.municipio_nome,
        "municipio_codigo_ibge": c.unidade_orgao.codigo_ibge,
        "numero_compra": c.numero_compra,
        "processo": c.processo,
        "modalidade_id": c.modalidade_id,
        "modalidade_nome": c.modalidade_nome,
        "modo_disputa_id": c.modo_disputa_id,
        "modo_disputa_nome": c.modo_disputa_nome,
        "objeto": c.objeto_compra,
        "informacao_complementar": c.informacao_complementar,
        "srp": c.srp,
        "valor_total_estimado": c.valor_total_estimado,
        "valor_total_homologado": c.valor_total_homologado,
        "situacao_id": c.situacao_compra_id,
        "situacao_nome": c.situacao_compra_nome,
        "data_publicacao_pncp": c.data_publicacao_pncp,
        "data_abertura_proposta": c.data_abertura_proposta,
        "data_encerramento_proposta": c.data_encerramento_proposta,
        "data_inclusao": c.data_inclusao,
        "data_atualizacao": c.data_atualizacao,
        "data_atualizacao_global": c.data_atualizacao_global,
        "link_sistema_origem": c.link_sistema_origem,
        "bruto": Jsonb(bruto),
    }


def guardar_invalidos(
    conn: psycopg.Connection, fonte: Fonte, itens: Iterable[tuple[dict[str, Any], str]]
) -> int:
    """Guarda itens que não passaram na validação. O mesmo item não é guardado duas vezes."""
    linhas = [
        {
            "fonte": fonte.value,
            "numero": bruto.get("numeroControlePNCP"),
            "erro": erro,
            "bruto": Jsonb(bruto),
        }
        for bruto, erro in itens
    ]
    if not linhas:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into registros_invalidos (fonte, numero_controle_pncp, erro, bruto)
            values (%(fonte)s, %(numero)s, %(erro)s, %(bruto)s)
            on conflict do nothing
            """,
            linhas,
        )
    return len(linhas)


def dias_concluidos(
    conn: psycopg.Connection, fonte: Fonte, inicio: date, fim: date
) -> set[tuple[int, date]]:
    """Pares (modalidade, dia) já coletados por inteiro no período."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select modalidade_id, dia from coletas_concluidas
            where fonte = %s and dia between %s and %s
            """,
            (fonte.value, inicio, fim),
        )
        return {(modalidade, dia) for modalidade, dia in cur.fetchall()}


def marcar_concluidos(
    conn: psycopg.Connection,
    fonte: Fonte,
    modalidade_id: int,
    inicio: date,
    fim: date,
    concluido_em: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into coletas_concluidas (fonte, modalidade_id, dia, concluido_em)
            select %s, %s, d::date, %s
            from generate_series(%s::date, %s::date, interval '1 day') as d
            on conflict (fonte, modalidade_id, dia) do update
                set concluido_em = excluded.concluido_em
            """,
            (fonte.value, modalidade_id, concluido_em, inicio, fim),
        )


def primeira_coleta_de_publicacoes(conn: psycopg.Connection) -> datetime | None:
    """Momento da coleta de publicações mais antiga que ainda vale.

    O que foi coletado nesse momento já reflete todas as atualizações anteriores;
    daí em diante, as mudanças chegam pela fonte de atualizações.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select min(concluido_em) from coletas_concluidas where fonte = %s",
            (Fonte.PUBLICACAO.value,),
        )
        linha = cur.fetchone()
    return linha[0] if linha else None


def tentar_lock_ingestao(conn: psycopg.Connection) -> bool:
    """Lock de sessão: dura até `liberar_lock_ingestao` ou até a conexão fechar."""
    with conn.cursor() as cur:
        cur.execute("select pg_try_advisory_lock(%s)", (CHAVE_LOCK_INGESTAO,))
        linha = cur.fetchone()
    return bool(linha and linha[0])


def liberar_lock_ingestao(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_unlock(%s)", (CHAVE_LOCK_INGESTAO,))


def iniciar_execucao(conn: psycopg.Connection, modo: str, iniciada_em: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into execucoes_ingestao (modo, iniciada_em) values (%s, %s) returning id",
            (modo, iniciada_em),
        )
        linha = cur.fetchone()
    assert linha is not None
    return int(linha[0])


def finalizar_execucao(
    conn: psycopg.Connection,
    execucao_id: int,
    status: str,
    metricas: dict[str, Any],
    finalizada_em: datetime,
    erro: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update execucoes_ingestao
            set finalizada_em = %s, status = %s, metricas = %s, erro = %s
            where id = %s
            """,
            (finalizada_em, status, Jsonb(metricas), erro, execucao_id),
        )


@dataclass
class ResumoColeta:
    dias_completos: int
    primeiro_dia: date | None
    ultimo_dia: date | None


def resumo_coleta(conn: psycopg.Connection, fonte: Fonte, inicio: date, fim: date) -> ResumoColeta:
    """Dias do período em que todas as modalidades já vistas foram coletadas."""
    with conn.cursor() as cur:
        cur.execute(
            """
            with por_dia as (
                select dia, count(*) as modalidades
                from coletas_concluidas
                where fonte = %(fonte)s and dia between %(inicio)s and %(fim)s
                group by dia
            ),
            total as (
                select count(distinct modalidade_id) as modalidades
                from coletas_concluidas where fonte = %(fonte)s
            )
            select count(*), min(dia), max(dia)
            from por_dia, total
            where por_dia.modalidades = total.modalidades
            """,
            {"fonte": fonte.value, "inicio": inicio, "fim": fim},
        )
        linha = cur.fetchone()
    assert linha is not None
    return ResumoColeta(int(linha[0]), linha[1], linha[2])


def resumo_banco(conn: psycopg.Connection) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*),
                   min(data_publicacao_pncp at time zone 'America/Sao_Paulo')::date,
                   max(data_publicacao_pncp at time zone 'America/Sao_Paulo')::date
            from contratacoes
            """
        )
        total, primeira, ultima = cur.fetchone() or (0, None, None)
        cur.execute("select count(*) from registros_invalidos")
        invalidos = (cur.fetchone() or (0,))[0]
        cur.execute(
            """
            select id, modo, status, iniciada_em, finalizada_em, metricas
            from execucoes_ingestao order by id desc limit 1
            """
        )
        ultima_execucao = cur.fetchone()
    return {
        "contratacoes": total,
        "primeira_publicacao": primeira,
        "ultima_publicacao": ultima,
        "registros_invalidos": invalidos,
        "ultima_execucao": ultima_execucao,
    }
