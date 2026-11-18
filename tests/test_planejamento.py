from datetime import date, timedelta

import pytest

from radar.config import Config
from radar.ingestao import ErroDeUso, Janela, executar_ingestao, planejar_janelas
from radar.pncp.modelos import Fonte
from tests.conftest import relogio_fixo

P = Fonte.PUBLICACAO


def d(dia: int) -> date:
    return date(2026, 11, dia)


def test_divide_os_dias_em_janelas_do_tamanho_maximo():
    janelas = planejar_janelas(P, [6], d(1), d(10), set(), janela_dias=7)

    assert janelas == [Janela(P, 6, d(1), d(7)), Janela(P, 6, d(8), d(10))]


def test_dias_concluidos_quebram_a_janela():
    concluidos = {(6, d(3)), (6, d(4))}

    janelas = planejar_janelas(P, [6], d(1), d(6), concluidos, janela_dias=7)

    assert janelas == [Janela(P, 6, d(1), d(2)), Janela(P, 6, d(5), d(6))]


def test_cada_modalidade_tem_seu_proprio_checkpoint_e_a_ordem_e_cronologica():
    concluidos = {(4, d(1))}

    janelas = planejar_janelas(P, [6, 4], d(1), d(2), concluidos, janela_dias=1)

    assert janelas == [
        Janela(P, 6, d(1), d(1)),
        Janela(P, 4, d(2), d(2)),
        Janela(P, 6, d(2), d(2)),
    ]


def test_nada_pendente_ou_periodo_vazio_nao_gera_janelas():
    todos = {(6, d(1) + timedelta(days=i)) for i in range(5)}

    assert planejar_janelas(P, [6], d(1), d(5), todos, janela_dias=7) == []
    assert planejar_janelas(P, [6], d(5), d(4), set(), janela_dias=7) == []


class NaoUsar:
    """Conexão que falha se alguém tentar usá-la: a validação vem antes do banco."""

    def __getattr__(self, nome):
        raise AssertionError(f"a conexão não deveria ser usada ({nome})")


@pytest.mark.parametrize(
    ("de", "ate", "mensagem"),
    [
        (d(12), d(11), "é depois de"),
        (d(9), d(10), "anterior ao início do recorte"),
        (d(10), d(17), "no máximo ontem"),
    ],
)
def test_intervalo_invalido_e_recusado_antes_de_tocar_no_banco(cfg: Config, de, ate, mensagem):
    with pytest.raises(ErroDeUso, match=mensagem):
        executar_ingestao(
            NaoUsar(),  # type: ignore[arg-type]
            NaoUsar(),  # type: ignore[arg-type]
            cfg,
            de=de,
            ate=ate,
            relogio=relogio_fixo("2026-11-17T09:00"),
        )


def test_forcar_sem_intervalo_e_recusado(cfg: Config):
    with pytest.raises(ErroDeUso, match="--forcar"):
        executar_ingestao(
            NaoUsar(),  # type: ignore[arg-type]
            NaoUsar(),  # type: ignore[arg-type]
            cfg,
            forcar=True,
            relogio=relogio_fixo("2026-11-17T09:00"),
        )
