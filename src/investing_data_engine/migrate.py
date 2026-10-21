import asyncio
from pathlib import Path

import asyncpg

from .config import get_settings


async def main():
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        async with conn.transaction():
            applied = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE version='001_initial'")
            if not applied:
                await conn.execute(sql)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
