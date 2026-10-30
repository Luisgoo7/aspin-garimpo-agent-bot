"""Nota ASPIN 2.0: preferência legal, esfera do devedor e notas mais espalhadas."""
from datetime import datetime, timezone

import pytest

from app import worker

from test_reliability import oficio_opp


def opp_with(**classification):
    base = oficio_opp()
    extra = classification.pop("_opp", {})
    return dict(oficio_opp(classification=dict(base["classification"], **classification)), **extra)


def test_weights_sum_to_ten():
    assert sum(weight for _, _, weight in worker.NOTA_CRITERIA) == 10
    assert len({key for key, _, _ in worker.NOTA_CRITERIA}) == len(worker.NOTA_CRITERIA)


@pytest.mark.parametrize("debtor,alias,sphere", [
    ("União Federal", "api_publica_tjsp", "federal"),
    ("INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS", None, "federal"),
    ("Universidade Federal do Pará", None, "federal"),
    ("Fazenda Pública do Estado de São Paulo", "api_publica_tjsp", "estadual"),
    ("Distrito Federal", None, "estadual"),
    ("Município de São Paulo", None, "estadual"),           # capital
    ("Prefeitura Municipal de Belém", None, "estadual"),    # capital
    ("MUNICIPIO DE VITORIA DA CONQUISTA", None, "municipal"),  # não é Vitória (ES)
    ("Prefeitura Municipal de Campinas", "api_publica_tjsp", "municipal"),
    ("", "api_publica_trf3", "federal"),                    # TRF = devedor federal
    ("Construtora Exemplo Ltda", "api_publica_tjsp", None),
    (None, None, None),
])
def test_debtor_sphere(debtor, alias, sphere):
    assert worker.debtor_sphere(debtor, alias) == sphere


def test_debtor_quality_changes_the_score():
    federal = opp_with(_opp={"debtor_entity": "União Federal"})
    estadual = opp_with(_opp={"debtor_entity": "Estado de São Paulo", "alias": "api_publica_tjsp"})
    municipal = opp_with(_opp={"debtor_entity": "Município de Campinas", "alias": "api_publica_tjsp"})
    notas = [worker.nota_aspin(o)[0] for o in (federal, estadual, municipal)]
    assert notas[0] - notas[1] == 1 and notas[1] - notas[2] == 1
    items = {key: (label, ok, weight) for key, label, ok, weight in worker.nota_aspin(estadual)[1]}
    assert items["devedor"] == ("Devedor estadual, DF ou capital", True, 1)
    assert worker.nota_aspin(municipal)[1][4][2] is False


@pytest.mark.parametrize("preference,expected", [
    ("Idosos a partir de 60 anos", True),
    ("Portador de doença grave", True),
    ("Pessoa com deficiência", True),
    ("Superpreferência", True),
    ("não informada", False),
    ("Sem preferência", False),
    ("", False),
    (None, False),
])
def test_legal_preference_is_its_own_criterion(preference, expected):
    assert worker.has_legal_preference(opp_with(legal_preference=preference)) is expected


def test_superpreference_in_the_publication_text_counts():
    opp = opp_with(legal_preference=None, _opp={"text_excerpt": "Pagamento de SUPERPREFERÊNCIA por doença grave"})
    assert worker.has_legal_preference(opp)


def test_alimentar_counts_one_point():
    alimentar = worker.nota_aspin(opp_with(credit_nature="alimentar"))[0]
    comum = worker.nota_aspin(opp_with(credit_nature="comum"))[0]
    assert alimentar - comum == 1


def test_scores_are_spread_out():
    """A versão 1 dava 6 ou 7 a quase tudo; agora o pior e o melhor caso típicos ficam longe."""
    weak = opp_with(credit_nature="comum", legal_preference=None, third_party_interest=True,
                    _opp={"debtor_entity": "Município de Campinas", "alias": "api_publica_tjsp", "amount": 160000.0})
    medium = opp_with(credit_nature="comum", legal_preference=None,
                      _opp={"debtor_entity": "Estado de São Paulo", "alias": "api_publica_tjsp"})
    strong = opp_with(published_at=datetime.now(timezone.utc).isoformat(),
                      _opp={"amount": 1_200_000.0, "datajud_signal": {"published_at": None}})
    notas = [worker.nota_aspin(o)[0] for o in (weak, medium, strong)]
    assert notas == [1, 4, 10]
    assert worker.nota_aspin(oficio_opp())[0] == 7  # exemplo padrão: federal, alimentar, idoso


def test_return_format_is_unchanged():
    nota, items = worker.nota_aspin(oficio_opp())
    assert isinstance(nota, int) and 0 <= nota <= 10
    assert all(len(item) == 4 and isinstance(item[2], bool) for item in items)
    assert nota == sum(weight for _, _, ok, weight in items if ok)
