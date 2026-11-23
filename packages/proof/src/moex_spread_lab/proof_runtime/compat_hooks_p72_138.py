"""Resolve old and new module-level hook names without losing either patch point."""


def choose_hook[T](public: T, private: T, original: T) -> T:
    """Prefer a patched private hook; otherwise use the historical public binding."""
    return private if private is not original else public
