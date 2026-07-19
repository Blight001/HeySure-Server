from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


SERVER_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_revision_graph_has_single_head() -> None:
    config = Config(str(SERVER_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(SERVER_ROOT / "other" / "migrations"),
    )

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"expected one Alembic head, found: {heads}"
