"""Contratações do PNCP e controle da coleta.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        create table contratacoes (
            id                          bigint generated always as identity primary key,
            numero_controle_pncp        text not null unique,
            orgao_cnpj                  text not null check (orgao_cnpj ~ '^[0-9]{14}$'),
            ano_compra                  integer not null,
            sequencial_compra           integer not null,
            orgao_razao_social          text not null,
            orgao_esfera                text,
            orgao_poder                 text,
            unidade_codigo              text,
            unidade_nome                text,
            uf                          char(2) not null,
            municipio_nome              text,
            municipio_codigo_ibge       text,
            numero_compra               text,
            processo                    text,
            modalidade_id               integer not null,
            modalidade_nome             text not null,
            modo_disputa_id             integer,
            modo_disputa_nome           text,
            objeto                      text not null,
            informacao_complementar     text,
            srp                         boolean,
            valor_total_estimado        numeric,
            valor_total_homologado      numeric,
            situacao_id                 integer not null,
            situacao_nome               text not null,
            data_publicacao_pncp        timestamptz not null,
            data_abertura_proposta      timestamptz,
            data_encerramento_proposta  timestamptz,
            data_inclusao               timestamptz,
            data_atualizacao            timestamptz,
            data_atualizacao_global     timestamptz,
            link_sistema_origem         text,
            -- Item exatamente como veio da API, para reprocessar sem chamar a API de novo.
            bruto                       jsonb not null,
            coletado_em                 timestamptz not null default now(),
            atualizado_em               timestamptz not null default now(),
            unique (orgao_cnpj, ano_compra, sequencial_compra)
        )
    """)
    op.execute("create index on contratacoes (data_publicacao_pncp)")
    op.execute("create index on contratacoes (modalidade_id, data_publicacao_pncp)")
    op.execute("create index on contratacoes (municipio_codigo_ibge)")

    # Um registro por (fonte, modalidade, dia) já coletado por inteiro. É o checkpoint:
    # o que não está aqui é coletado (de novo) na próxima execução.
    op.execute("""
        create table coletas_concluidas (
            fonte          text not null check (fonte in ('publicacao', 'atualizacao')),
            modalidade_id  integer not null,
            dia            date not null,
            concluido_em   timestamptz not null,
            primary key (fonte, modalidade_id, dia)
        )
    """)

    # Itens que a API mandou mas não passaram na validação. Ficam guardados para
    # reprocessar depois de ajustar o modelo, em vez de sumir em silêncio.
    op.execute("""
        create table registros_invalidos (
            id                    bigint generated always as identity primary key,
            fonte                 text not null,
            numero_controle_pncp  text,
            erro                  text not null,
            bruto                 jsonb not null,
            recebido_em           timestamptz not null default now()
        )
    """)
    op.execute("create unique index on registros_invalidos (fonte, md5(bruto::text))")

    op.execute("""
        create table execucoes_ingestao (
            id             bigint generated always as identity primary key,
            iniciada_em    timestamptz not null,
            finalizada_em  timestamptz,
            modo           text not null check (modo in ('incremental', 'intervalo')),
            status         text not null default 'em_andamento'
                           check (status in ('em_andamento', 'sucesso', 'parcial', 'falha')),
            metricas       jsonb not null default '{}',
            erro           text
        )
    """)


def downgrade() -> None:
    op.execute("drop table execucoes_ingestao")
    op.execute("drop table registros_invalidos")
    op.execute("drop table coletas_concluidas")
    op.execute("drop table contratacoes")
