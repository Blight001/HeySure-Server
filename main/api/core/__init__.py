"""Cross-cutting infrastructure: configuration, paths, migrations.

Modules in this package are intentionally framework-agnostic and have no
dependency on FastAPI/SQLModel application code, so they can be imported
from anywhere without creating cycles.
"""
