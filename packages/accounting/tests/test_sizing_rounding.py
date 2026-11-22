"""A sized replay must not retain fixed-template rounding disclosures."""

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import ResearchSettings
from test_position_sizing import unit_proof


def test_sizing_discards_fixed_template_rounding_disclosures():
    proof, marks, contracts = unit_proof()
    for component in ("gross", "costs", "net"):
        proof["summary"][f"displayed_trade_{component}_rounding_delta_rub"] = 42
    result = size_proof(proof, marks, ResearchSettings(), contracts)
    assert not any("rounding_delta" in key for key in result["summary"])
