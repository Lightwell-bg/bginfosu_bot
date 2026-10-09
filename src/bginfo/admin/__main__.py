"""``python -m bginfo.admin``: запуск админки на host:port из config.ini (host можно переопределить ADMIN_HOST)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from bginfo.admin.app import create_app
from bginfo.admin.common import AdminConfigError
from bginfo.config import get_config, get_settings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = create_app()
    except AdminConfigError as e:
        print(e, file=sys.stderr)
        return 1
    cfg = get_config().admin
    host = get_settings().ADMIN_HOST or cfg.host
    # proxy_headers выключен: X-Forwarded-For учитывает только наш флаг [admin] trust_proxy
    uvicorn.run(app, host=host, port=cfg.port, proxy_headers=False, server_header=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
