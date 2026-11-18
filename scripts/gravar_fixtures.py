"""Grava respostas reais da API do PNCP em tests/fixtures/pncp/.

Os testes do coletor rodam contra essas respostas, sem rede. Rode de novo só
quando quiser atualizar os exemplos (por exemplo, se a API mudar de formato):

    uv run python scripts/gravar_fixtures.py

As consultas usam tamanhoPagina=10 (o mínimo aceito) para que os arquivos fiquem
pequenos e a paginação apareça com poucos registros.
"""

import json
import time
from pathlib import Path

import httpx

URL_BASE = "https://pncp.gov.br/api"
DESTINO = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pncp"

# (arquivo, caminho, parâmetros). 10/11/2026, PE, modalidade 4 (Concorrência - Eletrônica).
CONSULTAS: list[tuple[str, str, dict[str, str | int]]] = [
    ("modalidades.json", "/pncp/v1/modalidades", {}),
]
for fonte in ("publicacao", "atualizacao"):
    for pagina in (1, 2, 3):
        CONSULTAS.append(
            (
                f"{fonte}_mod4_20261110_p{pagina}.json",
                f"/consulta/v1/contratacoes/{fonte}",
                {
                    "dataInicial": "20261110",
                    "dataFinal": "20261110",
                    "codigoModalidadeContratacao": 4,
                    "uf": "PE",
                    "pagina": pagina,
                    "tamanhoPagina": 10,
                },
            )
        )


def main() -> None:
    DESTINO.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        base_url=URL_BASE, timeout=60, headers={"User-Agent": "radar-licitacoes/0.1"}
    ) as http:
        for arquivo, caminho, params in CONSULTAS:
            resposta = http.get(caminho, params=params)
            resposta.raise_for_status()
            if resposta.status_code == 204:
                print(f"{arquivo}: 204 (sem conteúdo), não gravado")
            else:
                corpo = json.dumps(resposta.json(), ensure_ascii=False, indent=1)
                (DESTINO / arquivo).write_text(corpo + "\n", encoding="utf-8")
                print(f"{arquivo}: gravado")
            time.sleep(1)


if __name__ == "__main__":
    main()
