"""Compatibility alias for the canonical :mod:`moex_spread_lab.proof` namespace.

The implementation moved to ``moex_spread_lab.proof`` (issue #16, F5); this
package keeps ``moex_spread_lab.proof_runtime.<module>`` importable during the
migration window. The alias resolves lazily through a meta-path finder and binds
each alias name to the *same* module object, so monkey-patches, ``is`` checks and
runner imports keep one identity. Removal window: after the Phase 7 waves have
soaked.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from types import ModuleType

_ALIAS_ROOT = __name__
_CANONICAL_ROOT = "moex_spread_lab.proof"


class _AliasLoader(importlib.abc.Loader):
    """Bind an alias module name to an already-imported canonical module."""

    def __init__(self, module: ModuleType) -> None:
        self._module = module
        self._spec = module.__spec__
        self._name = module.__name__
        self._package = module.__package__
        self._loader = module.__loader__

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType:
        # ``module_from_spec`` stamps the alias name onto the shared module object;
        # ``exec_module`` restores the canonical metadata below.
        return self._module

    def exec_module(self, module: ModuleType) -> None:
        module.__spec__ = self._spec
        module.__name__ = self._name
        module.__package__ = self._package
        module.__loader__ = self._loader


class _AliasFinder(importlib.abc.MetaPathFinder):
    """Resolve ``moex_spread_lab.proof_runtime[.module]`` to ``moex_spread_lab.proof``."""

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != _ALIAS_ROOT and not fullname.startswith(_ALIAS_ROOT + "."):
            return None
        canonical = _CANONICAL_ROOT + fullname[len(_ALIAS_ROOT) :]
        module = importlib.import_module(canonical)
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(module), is_package=hasattr(module, "__path__")
        )


sys.meta_path.append(_AliasFinder())
