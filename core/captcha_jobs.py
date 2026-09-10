"""One tracked job per account; bound expensive solver concurrency process-wide."""
import asyncio
import os
import time


class CaptchaJobs:
    def __init__(self, concurrency=2, attempts=3, retry_delay=15):
        self.slots = asyncio.Semaphore(concurrency)
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.jobs = {}

    async def run(self, bot, solve):
        key = (bot.space_owner, str(bot.user_id))
        task = self.jobs.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._run(bot, solve))
            self.jobs[key] = task
            task.add_done_callback(lambda done: self.jobs.pop(key, None)
                                   if self.jobs.get(key) is done else None)
        return await asyncio.shield(task)

    async def _run(self, bot, solve):
        job = {'status': 'queued', 'attempt': 0, 'updated_at': time.time(), 'error': None}
        bot.stats['captcha_job'] = job
        try:
            for attempt in range(1, self.attempts + 1):
                job.update(status='queued', updated_at=time.time())
                async with self.slots:
                    if not bot.active:
                        return False
                    job.update(status='solving', attempt=attempt, updated_at=time.time())
                    try:
                        ok = await asyncio.wait_for(solve(), timeout=300)
                    except Exception as exc:
                        ok = False
                        job['error'] = type(exc).__name__
                    if ok:
                        job.update(status='solved', error=None, updated_at=time.time())
                        return True
                if attempt < self.attempts:
                    job.update(status='retrying', updated_at=time.time())
                    await asyncio.sleep(self.retry_delay * attempt)
            job.update(status='manual_required', updated_at=time.time())
            return False
        except asyncio.CancelledError:
            job.update(status='cancelled', updated_at=time.time())
            raise

    def cancel(self, owner, account_id):
        task = self.jobs.get((owner, str(account_id)))
        if task:
            task.cancel()


# Keep the default small: each extension solve launches a browser process.
jobs = CaptchaJobs()
