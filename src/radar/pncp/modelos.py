"""Modelos das respostas da API do PNCP.

Os nomes dos campos seguem o snake_case do projeto; os aliases em camelCase
batem com o JSON da API. Campos que a API não usa aqui ficam só no JSON bruto
guardado no banco.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from radar.config import FUSO_PNCP


class Fonte(StrEnum):
    """Endpoint de consulta de contratações. O valor é o trecho final do caminho na API."""

    PUBLICACAO = "publicacao"  # filtra pela data de publicação no PNCP
    ATUALIZACAO = "atualizacao"  # filtra pela data da última atualização


class _ModeloPNCP(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True, extra="ignore")


class Modalidade(_ModeloPNCP):
    id: int
    nome: str


class Pagina(_ModeloPNCP):
    """Uma página de resultados. Os itens ficam crus para serem validados um a um."""

    data: list[dict[str, Any]]
    total_registros: int
    total_paginas: int
    numero_pagina: int
    paginas_restantes: int


class OrgaoEntidade(_ModeloPNCP):
    cnpj: str = Field(pattern=r"^\d{14}$")
    razao_social: str
    poder_id: str | None = None
    esfera_id: str | None = None


class UnidadeOrgao(_ModeloPNCP):
    uf_sigla: str = Field(min_length=2, max_length=2)
    municipio_nome: str | None = None
    codigo_ibge: str | None = None
    codigo_unidade: str | None = None
    nome_unidade: str | None = None


ChaveContratacao = tuple[str, int, int]
"""Chave natural de uma contratação: (CNPJ do órgão, ano, sequencial)."""


class Contratacao(_ModeloPNCP):
    numero_controle_pncp: str = Field(alias="numeroControlePNCP", min_length=1)
    orgao_entidade: OrgaoEntidade
    unidade_orgao: UnidadeOrgao
    ano_compra: int
    sequencial_compra: int
    numero_compra: str | None = None
    processo: str | None = None
    modalidade_id: int
    modalidade_nome: str
    modo_disputa_id: int | None = None
    modo_disputa_nome: str | None = None
    objeto_compra: str = Field(min_length=1)
    informacao_complementar: str | None = None
    srp: bool | None = None
    valor_total_estimado: Decimal | None = None
    valor_total_homologado: Decimal | None = None
    situacao_compra_id: int
    situacao_compra_nome: str
    data_publicacao_pncp: datetime
    data_abertura_proposta: datetime | None = None
    data_encerramento_proposta: datetime | None = None
    data_inclusao: datetime | None = None
    data_atualizacao: datetime | None = None
    data_atualizacao_global: datetime | None = None
    link_sistema_origem: str | None = None

    @field_validator(
        "data_publicacao_pncp",
        "data_abertura_proposta",
        "data_encerramento_proposta",
        "data_inclusao",
        "data_atualizacao",
        "data_atualizacao_global",
    )
    @classmethod
    def _no_fuso_do_pncp(cls, valor: datetime | None) -> datetime | None:
        """A API manda datas sem fuso, no horário de Brasília."""
        if valor is not None and valor.tzinfo is None:
            return valor.replace(tzinfo=FUSO_PNCP)
        return valor

    @property
    def chave(self) -> ChaveContratacao:
        return (self.orgao_entidade.cnpj, self.ano_compra, self.sequencial_compra)

    @property
    def dia_publicacao(self) -> date:
        return self.data_publicacao_pncp.astimezone(FUSO_PNCP).date()
