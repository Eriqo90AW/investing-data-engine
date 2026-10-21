import asyncio

import asyncpg

from . import db
from .config import get_settings
from .queue import app
from .workers import tasks  # noqa: F401 - registers tasks on the shared app


async def main():
    settings = get_settings()
    db.pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    try:
        async with app.open_async():
            await app.run_worker_async(queues=["extraction"])
    finally:
        await db.pool.close()
        db.pool = None


if __name__ == "__main__":
    asyncio.run(main())
