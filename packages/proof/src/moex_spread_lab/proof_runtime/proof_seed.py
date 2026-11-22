"""Random-seed derivation for deterministic proof searches."""

import hashlib
import secrets

from moex_spread_lab.core.w0 import W0Error

SEED = 41


def resolve_seed(configured: int | None) -> int:
    """Turn the global seed preference into the base seed of this run.

    A configured seed of 0 means "no fixed seed": every run draws a fresh base
    seed so repeated runs explore different parameter proposals. Any other value
    is used verbatim, which makes every random choice of the run reproducible.
    """
    if configured is None:
        return SEED
    if type(configured) is not int or configured < 0:
        raise W0Error("Сид должен быть целым неотрицательным числом.")
    if configured == 0:
        return secrets.randbelow(2_147_483_647) + 1
    return configured


def derive_seed(base: int, *parts: object) -> int:
    """Derive a stable child seed from the run's base seed and a label.

    Independent searches inside one run (one per strategy family) must not
    propose the same parameter sequence, yet they must stay reproducible
    together: the child seed depends only on the base seed and the label.
    """
    digest = hashlib.sha256(
        "|".join([str(base), *(str(part) for part in parts)]).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") or 1
