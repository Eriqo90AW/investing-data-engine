from contextlib import asynccontextmanager

import asyncpg

from .config import get_settings
from .queue import app as queue_app

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan_resources():
    global pool
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10, command_timeout=10)
    try:
        async with queue_app.open_async():
            yield
    finally:
        await pool.close()
        pool = None


def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("database pool is not initialized")
    return pool
