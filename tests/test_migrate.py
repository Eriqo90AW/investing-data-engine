"""Regression coverage for first boot, redeploys, and failed initialization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from investing_data_engine import migrate


@pytest.fixture
def database(monkeypatch):
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn = SimpleNamespace(
        transaction=MagicMock(return_value=transaction),
        execute=AsyncMock(),
        fetchval=AsyncMock(),
        close=AsyncMock(),
    )
    events = MagicMock()
    events.attach_mock(conn.execute, "execute")
    events.attach_mock(conn.fetchval, "fetchval")
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(migrate.asyncpg, "connect", connect)
    monkeypatch.setattr(migrate, "get_settings", lambda: SimpleNamespace(database_url="postgresql://test"))
    return conn, transaction, events, connect


@pytest.mark.parametrize(
    "queue_exists, app_applied",
    [(False, None), (True, None), (True, 1), (False, 1)],
)
async def test_initializes_only_missing_schemas(database, queue_exists, app_applied):
    conn, transaction, events, connect = database
    conn.fetchval.side_effect = [queue_exists, app_applied]

    await migrate.main()

    connect.assert_awaited_once_with("postgresql://test")
    calls = events.mock_calls
    # Lock before checking either schema, and initialize the tracking table before querying it.
    assert calls[0] == call.execute("SELECT pg_advisory_xact_lock($1)", migrate.MIGRATION_LOCK_ID)
    tracking_created = next(
        i for i, event in enumerate(calls)
        if event[0] == "execute" and event.args[0].startswith("CREATE TABLE IF NOT EXISTS schema_migrations")
    )
    tracking_queried = calls.index(call.fetchval("SELECT 1 FROM schema_migrations WHERE version='001_initial'"))
    assert tracking_created < tracking_queried
    statements = [event.args[0] for event in conn.execute.await_args_list]
    queue_sql = migrate.SchemaManager.get_schema()
    assert statements.count(queue_sql) == (0 if queue_exists else 1)
    app_statements = [sql for sql in statements if "CREATE TABLE companies" in sql]
    assert len(app_statements) == (0 if app_applied else 1)
    if app_statements:
        assert "INSERT INTO schema_migrations(version) VALUES ('001_initial')" in app_statements[0]
    transaction.__aexit__.assert_awaited_once_with(None, None, None)
    conn.close.assert_awaited_once()


async def test_redeploy_preserves_existing_queue_and_application_tables(database):
    conn, transaction, _, _ = database
    conn.fetchval.side_effect = [False, None, True, 1]
    await migrate.main()
    first_boot_statements = conn.execute.await_count

    await migrate.main()

    second_boot = conn.execute.await_args_list[first_boot_statements:]
    assert len(second_boot) == 2  # Advisory lock and CREATE TABLE IF NOT EXISTS only.
    assert all("CREATE TABLE companies" not in event.args[0] for event in second_boot)
    assert all(event.args[0] != migrate.SchemaManager.get_schema() for event in second_boot)
    assert transaction.__aexit__.await_count == 2
    assert conn.close.await_count == 2


@pytest.mark.parametrize("failed_phase", ["queue", "application"])
async def test_initialization_errors_roll_back_and_close_connection(database, failed_phase):
    conn, transaction, _, _ = database
    conn.fetchval.side_effect = [False, None]
    failure = RuntimeError("migration failed")

    async def execute(sql, *args):
        if failed_phase == "queue" and sql == migrate.SchemaManager.get_schema():
            raise failure
        if failed_phase == "application" and "CREATE TABLE companies" in sql:
            raise failure

    conn.execute.side_effect = execute
    with pytest.raises(RuntimeError, match="migration failed"):
        await migrate.main()

    error_type, error, _ = transaction.__aexit__.await_args.args
    assert error_type is RuntimeError and error is failure
    conn.close.assert_awaited_once()
    if failed_phase == "queue":
        assert conn.fetchval.await_count == 1
