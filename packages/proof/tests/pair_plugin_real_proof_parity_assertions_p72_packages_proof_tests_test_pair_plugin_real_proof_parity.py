"""Assertions for the real proof pair-plugin/native-accounting parity test (P7.2)."""


def assert_loaded_pair_plugin_matches_native_accounting(actual, expected):
    """Keep the parity contract explicit while the test remains a thin scenario runner."""
    assert actual["trades"]
    assert len(actual["trades"]) == len(expected["trades"])
    for actual_trade, expected_trade in zip(actual["trades"], expected["trades"], strict=True):
        for key in (
            "side",
            "quantity",
            "exit_reason",
            "open_at_end",
            "funding_rub",
            "gross_rub",
            "costs_rub",
            "net_rub",
        ):
            assert actual_trade[key] == expected_trade[key], key
        for event in ("entry", "exit"):
            for key in ("date", "near_price", "far_price", "spread_fill", "costs"):
                assert actual_trade[event][key] == expected_trade[event][key], (event, key)
    for actual_row, expected_row in zip(actual["ledger"], expected["ledger"], strict=True):
        for key in (
            "date",
            "quantity",
            "mark_pnl_rub",
            "funding_rub",
            "costs_rub",
            "cumulative_net_rub",
        ):
            assert actual_row[key] == expected_row[key], key
    assert actual["summary"]["ledger_reconciliation_rub"] == 0
