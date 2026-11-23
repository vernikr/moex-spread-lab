"""Shared request shapes for the proof-search regressions (P7.2 leaf #131).

The constants are extracted verbatim from ``test_proof_search.py`` and re-exported
there, so both the owning test module and the extracted checks use one definition.
"""

PAIR = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
CALENDAR = {"universe": "calendar_native", "asset": "SR", "pair": "SRH3/SRM3"}
