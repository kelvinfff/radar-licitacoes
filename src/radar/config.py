"""Configuração lida de variáveis de ambiente (ou do arquivo .env)."""

from datetime import date
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# As datas do PNCP vêm sem fuso e estão no horário de Brasília.
FUSO_PNCP = ZoneInfo("America/Sao_Paulo")


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://radar:radar@127.0.0.1:5433/radar"

    pncp_url_base: str = "https://pncp.gov.br/api"
    pncp_uf: str = Field(default="PE", min_length=2, max_length=2)
    # Início do recorte: nada publicado antes desta data é coletado.
    pncp_data_inicio: date = date(2026, 1, 1)
    # Tamanho máximo, em dias, de cada consulta paginada. Janelas menores
    # refazem menos trabalho quando algo falha no meio.
    pncp_janela_dias: int = Field(default=7, ge=1, le=31)
    pncp_tamanho_pagina: int = Field(default=50, ge=10, le=50)
    pncp_timeout_s: float = Field(default=60.0, gt=0)
    pncp_max_tentativas: int = Field(default=5, ge=1)
    # Intervalo mínimo entre requisições. A API pública limita rajadas (ver pncp/cliente.py):
    # 1 requisição a cada 5 s é o ritmo que foi medido sem nenhum HTTP 429.
    pncp_intervalo_minimo_s: float = Field(default=5.0, ge=0)
    # Depois de tantas janelas seguidas com falha, a execução é interrompida
    # (a API provavelmente está fora do ar e insistir só gasta tempo).
    pncp_max_falhas_seguidas: int = Field(default=3, ge=1)


@lru_cache
def carregar_config() -> Config:
    return Config()
