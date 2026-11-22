"""Vulture allowlist for symbols resolved through plugin and framework dispatch."""

# CLI modules are registered through importlib/entry-point discovery.
register = PLUGIN = None

# The HTTP server calls BaseHTTPRequestHandler methods dynamically.
do_GET = do_POST = close_connection = None

# These attributes/locals are consulted by worker pools, filesystem hooks, and validators.
worker_pool = for_universe = _executor = mtime = linkname = finder = None
near_error = far_error = None
