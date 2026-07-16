from io import StringIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_legacy_0003_upgrades_convergently_to_head(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://migration:migration@localhost/migration"
    )
    output = StringIO()
    config = Config("alembic.ini", output_buffer=output)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_current_head() == "0006"
    assert scripts.get_revision("0004").down_revision == "0003"
    assert scripts.get_revision("0005").down_revision == "0004"

    command.upgrade(config, "0003:head", sql=True)
    sql = output.getvalue()

    assert "ADD COLUMN IF NOT EXISTS remote_candidates_baseline" in sql
    assert "ADD COLUMN IF NOT EXISTS max_attempts" in sql
    assert "ADD COLUMN IF NOT EXISTS crm_worker_id" in sql
