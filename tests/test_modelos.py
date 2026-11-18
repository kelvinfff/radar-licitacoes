from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from radar.config import FUSO_PNCP
from radar.pncp.modelos import Contratacao, Pagina
from tests.conftest import FIXTURES, item_gravado, paginas_gravadas


def test_todas_as_respostas_gravadas_sao_validas():
    for fonte in ("publicacao", "atualizacao"):
        for pagina in paginas_gravadas(fonte):
            for item in pagina["data"]:
                Contratacao.model_validate(item)


def test_campos_de_um_item_real():
    c = Contratacao.model_validate(item_gravado("publicacao", 0))

    assert c.numero_controle_pncp == "24416174000106-1-000097/2026"
    assert c.chave == ("24416174000106", 2026, 97)
    assert c.orgao_entidade.razao_social == "UNIVERSIDADE FEDERAL RURAL DE PERNAMBUCO"
    assert c.unidade_orgao.uf_sigla == "PE"
    assert c.unidade_orgao.municipio_nome == "Recife"
    assert c.modalidade_id == 4
    assert c.situacao_compra_id == 1
    assert c.valor_total_estimado == Decimal("918831.27")
    assert c.valor_total_homologado is None


def test_datas_sem_fuso_sao_do_horario_de_brasilia():
    c = Contratacao.model_validate(item_gravado("publicacao", 0))

    assert c.data_publicacao_pncp == datetime(2026, 11, 10, 6, 30, 20, tzinfo=FUSO_PNCP)
    assert c.data_publicacao_pncp.utcoffset() is not None
    assert c.dia_publicacao == date(2026, 11, 10)


def test_publicacao_perto_da_meia_noite_conta_no_dia_de_brasilia():
    item = item_gravado() | {"dataPublicacaoPncp": "2026-11-10T23:30:00"}

    assert Contratacao.model_validate(item).dia_publicacao == date(2026, 11, 10)


def test_valores_monetarios_sao_decimais_exatos():
    item = item_gravado() | {"valorTotalEstimado": 0.1, "valorTotalHomologado": 123456789.99}
    c = Contratacao.model_validate(item)

    assert c.valor_total_estimado == Decimal("0.1")
    assert c.valor_total_homologado == Decimal("123456789.99")


def test_situacao_como_texto_tambem_e_aceita():
    # O Swagger declara situacaoCompraId como string; na prática vem número.
    c = Contratacao.model_validate(item_gravado() | {"situacaoCompraId": "3"})

    assert c.situacao_compra_id == 3


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("objetoCompra", None),
        ("objetoCompra", ""),
        ("numeroControlePNCP", None),
        ("dataPublicacaoPncp", "ontem"),
        ("orgaoEntidade", {"cnpj": "123", "razaoSocial": "X"}),
    ],
)
def test_item_sem_dados_essenciais_e_rejeitado(campo, valor):
    with pytest.raises(ValidationError):
        Contratacao.model_validate(item_gravado() | {campo: valor})


def test_pagina_gravada():
    pagina = Pagina.model_validate_json(
        (FIXTURES / "publicacao_mod4_20261110_p1.json").read_bytes()
    )

    assert pagina.total_registros == 12
    assert pagina.total_paginas == 2
    assert pagina.paginas_restantes == 1
    assert len(pagina.data) == 10
