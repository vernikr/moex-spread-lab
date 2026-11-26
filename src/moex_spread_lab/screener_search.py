"""Root compatibility shim for ``moex_spread_lab.proof.screener_search``.

Preserved public entry point: notebooks and older scripts import
``from moex_spread_lab.screener_search import optimize, screen_table``. The module body
rebinds ``sys.modules[__name__]`` to the proof implementation, so both import paths
resolve to one module object (pinned by
``packages/proof/tests/test_package_compatibility.py``). Retirement needs an
owner-approved migration plan; the live inventory of remaining callers is in
``packages/web/docs/README.md`` and issue #209.
"""

import sys

from moex_spread_lab.proof import screener_search as _module

sys.modules[__name__] = _module
