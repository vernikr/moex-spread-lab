"""Integration regression for the pre-P7.2 evaluation star-import surface."""

from moex_spread_lab.proof import evaluation, proof_kernel


def test_evaluation_wildcard_and_hook_identity():
    namespace = {}
    exec("from moex_spread_lab.proof.evaluation import *", namespace)
    assert set(namespace) - {"__builtins__"} == {
        "DEFAULT_INITIAL_CAPITAL_RUB",
        "W0Error",
        "account_checks",
        "annotations",
        "automatic",
        "date",
        "datetime",
        "lqdt_overlay_values",
        "math",
        "time",
    }
    for name in ("annualized_returns", "excess_curve", "missing_exposure"):
        assert getattr(evaluation, name) is getattr(evaluation, "_" + name)
        assert getattr(proof_kernel, "_" + name) is getattr(evaluation, "_" + name)
