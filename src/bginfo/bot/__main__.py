import asyncio
import logging

from bginfo.bot.app import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO печатает полный URL запроса, а в нём токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
