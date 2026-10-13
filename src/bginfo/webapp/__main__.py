"""``python -m bginfo.webapp``: запуск мини-аппа на host:port из config.ini [webapp]."""

from __future__ import annotations

import logging
import sys

import uvicorn

from bginfo.config import get_config, get_settings
from bginfo.webapp.app import create_app


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO печатает полный URL запроса, а в нём токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if get_settings().TELEGRAM_BOT_TOKEN is None:
        logging.getLogger(__name__).error("TELEGRAM_BOT_TOKEN is not set; see .env.example")
        return 1
    app = create_app()
    cfg = get_config().webapp
    # за Caddy: клиентский IP не нужен, заголовки прокси не разбираем
    uvicorn.run(app, host=cfg.host, port=cfg.port, proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
