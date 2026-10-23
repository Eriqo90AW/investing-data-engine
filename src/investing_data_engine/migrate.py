import asyncio
import logging
from pathlib import Path

import asyncpg
from procrastinate.schema import SchemaManager

from .config import get_settings


logger = logging.getLogger(__name__)
# Serialize schema initialization when two deployments start at the same time.
MIGRATION_LOCK_ID = 4801605


async def main():
    migrations = sorted((Path(__file__).resolve().parents[2] / "migrations").glob("[0-9]*.sql"))
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK_ID)
            queue_exists = await conn.fetchval("SELECT to_regclass('procrastinate_jobs') IS NOT NULL")
            if not queue_exists:
                logger.info("Initializing the Procrastinate queue schema")
                await conn.execute(SchemaManager.get_schema())

            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for migration in migrations:
                version = migration.stem
                applied = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version=$1", version)
                if not applied:
                    logger.info("Applying application migration %s", version)
                    await conn.execute(migration.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations(version) VALUES($1) ON CONFLICT DO NOTHING", version)

    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
