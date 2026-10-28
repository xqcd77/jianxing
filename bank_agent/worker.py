"""Standalone durable request worker: python -m bank_agent.worker."""
import asyncio
from dataclasses import replace

from .app import create_app
from .config import Settings


async def run():
    settings = replace(Settings.from_env(),chat_worker_mode='embedded',scheduler_mode='disabled')
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.Event().wait()


if __name__=='__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
