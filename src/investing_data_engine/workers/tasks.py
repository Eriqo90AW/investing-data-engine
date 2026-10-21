import procrastinate

from ..db import get_pool
from ..queue import app


@app.task(queue="extraction", retry=procrastinate.RetryStrategy(max_attempts=3))
async def extract_company_financials(job_id: int):
    """Run explicitly requested extraction. Publish only after verified adapters are installed."""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT * FROM extraction_jobs WHERE id=$1 FOR UPDATE", job_id)
            if job is None or job["status"] in ("published", "failed"):
                return
            await conn.execute("UPDATE extraction_jobs SET status='running', started_at=coalesce(started_at,now()), progress=$2 WHERE id=$1", job_id, {"stage": "discovery", "completed": 0})
    # There is no source adapter yet. Fail clearly; never publish placeholder financial data.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE extraction_jobs SET status='failed', completed_at=now(), errors=$2, progress=$3 WHERE id=$1", job_id,
                           [{"code": "extractor_unavailable", "message": "No verified SEC/issuer extraction adapter is configured."}],
                           {"stage": "failed", "completed": 0})
