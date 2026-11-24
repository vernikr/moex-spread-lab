"""Proof-search regressions: landscape, research runs and loopback boundary.

P7.2 leaf #131 keeps every test case ID in this module while the assertion-rich
bodies live in uniquely named helper modules below the code-health size ceiling.
No test, assertion, subtest or fixture behaviour is changed.
"""

import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.proof.service import ProofService
from proof_search_fixtures_p72_packages_proof_tests_test_proof_search import CALENDAR as CALENDAR
from proof_search_fixtures_p72_packages_proof_tests_test_proof_search import PAIR as PAIR
from proof_search_http_p72_packages_proof_tests_test_proof_search import (
    check_http_home_alias_new_search_rerun_and_local_origin_boundary,
)
from proof_search_plateau_p72_packages_proof_tests_test_proof_search import (
    check_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate,
    check_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit,
    check_mark_change_including_first_validation_session_not_exit_trade_sum,
    check_missing_exposure_names_each_period_and_counts_a_carried_spread,
    check_one_axis_probes_with_constraints_missing_and_negative_results,
    check_tpe_selects_the_highest_score_even_at_an_unstable_peak,
)
from proof_search_service_p72_packages_proof_tests_test_proof_search import (
    check_0_and_50_share_change_result_not_history_or_signals,
    check_funding_and_both_calendar_strategies_use_only_active_fields,
    check_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one,
    check_journal_only_rows_do_not_move_the_validation_boundary,
    check_pair_replay_reproducibility_and_holdout_is_not_a_selection_input,
    check_short_series_and_invalid_values_never_produce_a_fake_plateau,
    check_validation_liquidity_failure_does_not_veto_calendar_training_plateau,
)

__all__ = [
    "CALENDAR",
    "PAIR",
    "PlateauTests",
    "ProofSearchIntegrationTests",
]


class PlateauTests(unittest.TestCase):
    """Landscape, tuning and measurement regressions."""

    def test_one_axis_probes_with_constraints_missing_and_negative_results(self):
        check_one_axis_probes_with_constraints_missing_and_negative_results(self)

    def test_tpe_selects_the_highest_score_even_at_an_unstable_peak(self):
        check_tpe_selects_the_highest_score_even_at_an_unstable_peak(self)

    def test_missing_exposure_names_each_period_and_counts_a_carried_spread(self):
        check_missing_exposure_names_each_period_and_counts_a_carried_spread(self)

    def test_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(self):
        check_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(self)

    def test_mark_change_including_first_validation_session_not_exit_trade_sum(self):
        check_mark_change_including_first_validation_session_not_exit_trade_sum(self)

    def test_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(self):
        check_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(self)


class ProofSearchIntegrationTests(unittest.TestCase):
    """Research-run, settings and loopback HTTP regressions."""

    def fixture(self, builder):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        builder(root)
        return ProofService(root)

    def test_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(self):
        check_global_seed_makes_a_run_reproducible_and_zero_draws_a_new_one(self)

    def test_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(self):
        check_pair_replay_reproducibility_and_holdout_is_not_a_selection_input(self)

    def test_0_and_50_share_change_result_not_history_or_signals(self):
        check_0_and_50_share_change_result_not_history_or_signals(self)

    def test_journal_only_rows_do_not_move_the_validation_boundary(self):
        check_journal_only_rows_do_not_move_the_validation_boundary(self)

    def test_validation_liquidity_failure_does_not_veto_calendar_training_plateau(self):
        check_validation_liquidity_failure_does_not_veto_calendar_training_plateau(self)

    def test_funding_and_both_calendar_strategies_use_only_active_fields(self):
        check_funding_and_both_calendar_strategies_use_only_active_fields(self)

    def test_short_series_and_invalid_values_never_produce_a_fake_plateau(self):
        check_short_series_and_invalid_values_never_produce_a_fake_plateau(self)

    def test_http_home_alias_new_search_rerun_and_local_origin_boundary(self):
        check_http_home_alias_new_search_rerun_and_local_origin_boundary(self)


if __name__ == "__main__":
    unittest.main()
