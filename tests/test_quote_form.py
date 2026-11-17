"""The quote form keeps a copied unit and clears the spacer when a lite is removed."""

from pathlib import Path


PAGE = (Path(__file__).resolve().parents[1] / 'templates' / 'quotes.html').read_text()


def test_copied_draft_keeps_its_unit_and_switching_converts():
    assert 'Changing unit will clear your quote' not in PAGE
    assert 'function convertQuoteUnits' in PAGE
    assert "lastUnit = spec.unit === 'mm' ? 'mm' : 'in'" in PAGE
    assert 'lastUnit = savedUnit' in PAGE


def test_clearing_a_lite_clears_its_spacer_and_sealant():
    assert 'function onLiteChange' in PAGE
    assert "if(layer==='3')clearSpacerAndSealant(prefix,'2')" in PAGE
    assert "if(layer==='2')" in PAGE
    assert "clearSpacerAndSealant(prefix,'1')" in PAGE
