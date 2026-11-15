#!/usr/bin/env python3
"""Убирает два дефекта фонового обхода сертификатов OpenVPN (cert_sync).

Симптом в логах панели: узел занят интерактивным запросом (например, качаются
профили клиентов) — и вместо тихого пропуска в лог падает трейсбек:

    cert_sync: failed to load expiry map from node id=8 name=deha-AT01|...
    app.services.node_http_gate.NodeHttpBusy: 82.148.252.166:9100

Причина 1: _sync_node ловит общий `except Exception` и пишет logger.exception,
поэтому ожидаемый NodeHttpBusy («слот узла занят») выглядит как ошибка, а
предусмотренный внешний `except NodeHttpBusy` (debug-строка «skip node») никогда
не срабатывает.

Причина 2: после неудачи карты `expiry_by_cn = {}` включает повальный фолбэк —
для КАЖДОГО клиента узла по два HTTP-запроса (список профилей + чтение .ovpn).
На узле с 186 клиентами это до 372 запросов, каждый держит тот же слот: панель
сама себе устраивает очередь и ловит «Узел занят другой операцией».

Патч 1: NodeHttpBusy пробрасывается наружу — цикл тихо пропускает узел.
Патч 2: при отказе чтения карты узел пропускается целиком (return 0) — без
        фолбэка по клиентам. Фолбэк остаётся для штатного случая «CN нет в
        index.txt», когда карта загрузилась успешно.

Идемпотентно: повторный запуск ничего не меняет.
"""
from __future__ import annotations

import os
import sys

PATH = os.environ.get(
    "CERT_SYNC_PATH", "/data/app/backend/app/services/cert_sync_worker.py"
)

PATCHES = [
    (
        "HERMES-PATCH: cert-sync import gate",
        "from app.services.background_gate import run_background_step\n",
        "from app.services.background_gate import run_background_step\n"
        "from app.services.node_http_gate import NodeHttpBusy  "
        "# HERMES-PATCH: cert-sync import gate\n",
    ),
    (
        "HERMES-PATCH: cert-sync no fallback storm",
        '''    try:
        expiry_by_cn = load_cert_expiry_map(adapter)
    except Exception:
        logger.exception(
            "cert_sync: failed to load expiry map from node id=%s name=%s local=%s",
            node.id,
            node.name,
            node.is_local,
        )
        expiry_by_cn = {}
''',
        '''    try:
        expiry_by_cn = load_cert_expiry_map(adapter)
    except NodeHttpBusy:
        # HERMES-PATCH: cert-sync skip busy
        # Слот узла занят интерактивным запросом — это норма для фонового обхода:
        # внешний обработчик тихо пропустит узел до следующего цикла.
        raise
    except Exception:
        logger.exception(
            "cert_sync: failed to load expiry map from node id=%s name=%s local=%s",
            node.id,
            node.name,
            node.is_local,
        )
        # HERMES-PATCH: cert-sync no fallback storm
        # Карта не получена целиком: по клиентам не идём (2 запроса на клиента
        # держат слот узла и плодят «Узел занят другой операцией»).
        return 0
''',
    ),
]


def main() -> int:
    try:
        with open(PATH, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"[patch-cert-sync] файл недоступен: {exc}", file=sys.stderr)
        return 0

    changed = 0
    for marker, old, new in PATCHES:
        if marker in src:
            print(f"[patch-cert-sync] уже есть: {marker}")
            continue
        if old not in src:
            print(f"[patch-cert-sync] якорь не найден: {marker}")
            continue
        src = src.replace(old, new, 1)
        changed += 1
        print(f"[patch-cert-sync] применён: {marker}")

    if changed:
        with open(PATH, "w", encoding="utf-8") as fh:
            fh.write(src)
        import py_compile

        py_compile.compile(PATH, doraise=True)
        print(f"[patch-cert-sync] записано, патчей применено: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
