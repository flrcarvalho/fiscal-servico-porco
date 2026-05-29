import asyncio
import logging
import os
from bot import build_app
from monitor import monitor_loop
from database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    init_db()
    logger.info("🐷 Fiscal de Serviço Porco iniciando...")

    # Bot do Telegram
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Bot do Telegram iniciado")

    # Monitor em background
    monitor_task = asyncio.create_task(monitor_loop())
    logger.info("✅ Monitor iniciado")

    try:
        await asyncio.Event().wait()  # roda para sempre
    except (KeyboardInterrupt, SystemExit):
        logger.info("Encerrando...")
    finally:
        monitor_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
