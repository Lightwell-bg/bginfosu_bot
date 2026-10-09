"""Загрузка начального наполнения базы знаний из YAML и сборка тестового (демо) набора.

Рабочий набор целиком черновик: ни одно требование не одобрено, одобрение ставит владелец.
Демо-набор — копия, где всё помечено как опубликованное и «проверенное» тестовым автором;
он нужен только для проверки бота и никогда не должен попадать в рабочую базу.
"""

from __future__ import annotations

from datetime import date
from importlib import resources

import yaml

from bginfo.kb import model as m

DEMO_AUTHOR = "ТЕСТОВЫЕ ДАННЫЕ (не проверено)"
DEMO_PREFIX = "[ТЕСТ] "


def _read_yaml(node) -> dict:
    data = yaml.safe_load(node.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{node.name}: ожидался словарь верхнего уровня")  # noqa: TRY004 - ValueError часть контракта загрузчика
    return data


def _load_raw() -> dict:
    root = resources.files("bginfo.seed")
    raw = _read_yaml(root / "catalog.yaml")
    grounds_dir = root / "grounds"
    files = sorted((f for f in grounds_dir.iterdir() if f.name.endswith(".yaml")), key=lambda f: f.name)
    raw["grounds"] = [_read_yaml(f) for f in files]
    return raw


def load_seed_bundle(demo: bool = False, today: date | None = None) -> m.Bundle:
    """Прочитать каталог и все карточки. ``demo=True`` — тестовая копия со статусом published."""
    bundle = m.Bundle.model_validate({**_load_raw(), "demo": False})
    if not demo:
        return bundle
    return _to_demo(bundle, today or date.today())  # noqa: DTZ011 - календарная дата без часового пояса


def _to_demo(bundle: m.Bundle, today: date) -> m.Bundle:
    data = bundle.model_dump(mode="json")
    stamp = today.isoformat()
    data["demo"] = True
    for g in data["grounds"]:
        g["status"] = "published"
        g["demo"] = True
        if not g["title"].startswith(DEMO_PREFIX):
            g["title"] = DEMO_PREFIX + g["title"]
        g["checked_at"] = stamp
        g["checked_by"] = DEMO_AUTHOR
        for r in g["requirements"]:
            r["verified"] = True
            r["verified_by"] = DEMO_AUTHOR
            r["verified_at"] = stamp
    for p in data["parameters"]:
        for v in p["values"]:
            v["verified"] = True
    return m.Bundle.model_validate(data)
