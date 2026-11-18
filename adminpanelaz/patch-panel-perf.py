#!/usr/bin/env python3
"""Ускоряет панель AdminPanelAZ и убирает два фоновых дефекта.

Симптомы, которые лечит:
1. Страница клиентов грузится секундами. Причина: /api/configs на каждый вызов
   делает до трёх HTTP-запросов к агенту узла (список файлов профилей + карта
   локальных IP), а фронт на одну загрузку дёргает /api/configs 8–9 раз. Слот
   узла один (node_http_gate), поэтому запросы выстраиваются в очередь: 180 мс
   на вызов превращаются в 1.1 с.
2. Каждые несколько минут в лог падает «awg2 monitoring failed … AZ-AWG2 не
   установлен на узле» — по всем узлам, где слоя AWG2 нет.
3. Периодически ловится StaleDataError на active_web_session: cleanup удаляет
   строку, которую touch только что обновил (pending UPDATE ещё не сброшен),
   commit падает, а логгер при форматировании исключения падает вторично.

Что делает патч (всё — рантайм-хуки, без правки апстрим-логики):
- кеш на 20 с для карты файлов профилей и карты локальных IP;
- отрицательный кеш «AWG2 нет» на 10 мин на узел + одна строка в лог;
- flush перед cleanup и исключение текущей сессии из удаления;
- middleware, сбрасывающий кеши на любом не-GET запросе к /api/.

Идемпотентно: повторный запуск ничего не дублирует.
"""
from __future__ import annotations

import os
import sys

APP_ROOT = os.environ.get("PANEL_APP_ROOT", "/data/app")
TARGET_DIR = os.path.join(APP_ROOT, "backend", "app")
MODULE_PATH = os.path.join(TARGET_DIR, "hermes_panel_patch.py")
MAIN_PATH = os.path.join(TARGET_DIR, "main.py")

MARKER = "HERMES-PATCH: panel perf hooks"

MODULE_SRC = '''"""HERMES-PATCH: ускорение панели AdminPanelAZ.

Ставится патч-скриптом patch-panel-perf.py из Umbrel-стора, подключается на
старте контейнера из app/main.py. Все хуки в try/except: если апстрим уедет,
панель всё равно стартует.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import logging
import sys
import threading
import time

log = logging.getLogger("uvicorn.error")

PROFILE_FILES_TTL_SECONDS = 20.0
LOCAL_IP_TTL_SECONDS = 20.0
AWG2_NEGATIVE_TTL_SECONDS = 600.0

_state = threading.local()
_epoch_lock = threading.Lock()
_epoch = 0


def _bump_epoch() -> None:
    global _epoch
    with _epoch_lock:
        _epoch += 1


def _current_epoch() -> int:
    return _epoch


def _adapter_key(adapter) -> str:
    for attr in ("base_url", "host", "name"):
        value = getattr(adapter, attr, None)
        if value:
            return f"{type(adapter).__name__}:{value}"
    return f"{type(adapter).__name__}:{id(adapter):x}"


def _cached(ttl: float, key_fn, label: str):
    def decorator(func):
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                key = (_current_epoch(),) + tuple(key_fn(*args, **kwargs))
            except Exception:
                return func(*args, **kwargs)
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit and hit[0] > now:
                    return copy.deepcopy(hit[1])
            value = func(*args, **kwargs)
            with lock:
                store[key] = (now + ttl, copy.deepcopy(value))
                if len(store) > 512:
                    store.clear()
            return value

        wrapper.hermes_cache_clear = lambda: store.clear()
        wrapper.hermes_cache_label = label
        return wrapper

    return decorator


def _rebind(original, replacement, name: str) -> int:
    """Перевешивает функцию во всех модулях, куда её импортировали напрямую."""
    count = 0
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            if getattr(module, name, None) is original:
                setattr(module, name, replacement)
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def _patch_profile_files_cache() -> str:
    from app.routers import configs as configs_router

    original = getattr(configs_router, "_fetch_profile_files_map", None)
    if original is None:
        return "нет _fetch_profile_files_map"
    if getattr(original, "hermes_cache_label", None):
        return "уже включён"

    def key_fn(adapter, configs, *, openvpn_group=None, visibility_policy=None):
        names = tuple(
            sorted(
                (
                    (getattr(c, "client_name", "") or "").strip().lower(),
                    str(getattr(c, "vpn_type", "")),
                )
                for c in (configs or [])
            )
        )
        return (_adapter_key(adapter), openvpn_group or "", names, repr(visibility_policy))

    configs_router._fetch_profile_files_map = _cached(
        PROFILE_FILES_TTL_SECONDS, key_fn, "profile-files"
    )(original)
    return "ок"


def _patch_local_ip_cache() -> str:
    from app.services import client_local_ip

    original = getattr(client_local_ip, "build_client_local_ip_map", None)
    if original is None:
        return "нет build_client_local_ip_map"
    if getattr(original, "hermes_cache_label", None):
        return "уже включён"

    def key_fn(adapter, db=None, *, node_id=None, configs=None):
        names = tuple(
            sorted(
                (
                    (getattr(c, "client_name", "") or "").strip().lower(),
                    str(getattr(c, "vpn_type", "")),
                )
                for c in (configs or [])
            )
        )
        return (_adapter_key(adapter), node_id, names)

    cached = _cached(LOCAL_IP_TTL_SECONDS, key_fn, "client-local-ip")(original)
    client_local_ip.build_client_local_ip_map = cached
    rebound = _rebind(original, cached, "build_client_local_ip_map")
    return f"ок (перевешено в {rebound} модулях)"


def _is_awg2_missing(exc) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 409:
        return True
    text = f"{getattr(exc, 'detail', '')} {exc}"
    lowered = text.lower()
    return "awg2" in lowered and ("не установлен" in lowered or "not installed" in lowered)


def _is_node_busy(exc) -> bool:
    """Слот узла занят фоновым воркером (node_http_gate) — это не дефект AWG2."""
    name = type(exc).__name__
    if name in ("NodeHttpBusy", "NodeHttpTimeout"):
        return True
    return "busy" in str(exc).lower()


def _patch_awg2_negative_cache() -> str:
    from app.services import awg2_noc

    if getattr(awg2_noc.fetch_awg2_peers_for_adapter, "hermes_awg2_cache", False):
        return "уже включён"
    peers_from = getattr(awg2_noc, "peers_from_awg2_monitoring", None)
    if peers_from is None:
        return "нет peers_from_awg2_monitoring"

    negative: dict[str, float] = {}
    announced: set[str] = set()
    last_log: dict[str, float] = {}
    lock = threading.Lock()

    @functools.wraps(awg2_noc.fetch_awg2_peers_for_adapter)
    def fetch_awg2_peers_for_adapter(adapter):
        key = _adapter_key(adapter)
        now = time.monotonic()
        with lock:
            if negative.get(key, 0.0) > now:
                return []
        try:
            payload = adapter.get_awg2_monitoring()
        except Exception as exc:  # noqa: BLE001
            if _is_awg2_missing(exc):
                with lock:
                    negative[key] = now + AWG2_NEGATIVE_TTL_SECONDS
                    first_time = key not in announced
                    announced.add(key)
                if first_time:
                    log.info(
                        "hermes-panel-patch: AWG2 не установлен на %s — опрос приостановлен на %d мин",
                        key,
                        int(AWG2_NEGATIVE_TTL_SECONDS // 60),
                    )
            elif _is_node_busy(exc):
                with lock:
                    negative[key] = now + 60.0
            else:
                with lock:
                    should_log = last_log.get(key, 0.0) <= now
                    if should_log:
                        last_log[key] = now + 600.0
                if should_log:
                    log.warning("awg2 monitoring failed for %s: %s", adapter, exc)
            return []
        with lock:
            negative.pop(key, None)
            announced.discard(key)
        try:
            return peers_from(payload)
        except Exception:
            return []

    fetch_awg2_peers_for_adapter.hermes_awg2_cache = True
    original_fetch = awg2_noc.fetch_awg2_peers_for_adapter
    awg2_noc.fetch_awg2_peers_for_adapter = fetch_awg2_peers_for_adapter
    rebound = _rebind(original_fetch, fetch_awg2_peers_for_adapter, "fetch_awg2_peers_for_adapter")
    return f"ок (перевешено в {rebound} модулях)"


def _patch_active_web_session() -> str:
    from datetime import datetime, timedelta, timezone

    from app.models import ActiveWebSession
    from app.services.active_web_session import ActiveWebSessionService

    if getattr(ActiveWebSessionService, "hermes_heartbeat_fix", False):
        return "уже включён"

    original_touch = ActiveWebSessionService.touch_active_web_session
    original_cleanup = ActiveWebSessionService.cleanup_stale_active_web_sessions

    def cleanup_stale_active_web_sessions(self, db, now=None):
        current_sid = getattr(_state, "session_id", None)
        if not current_sid:
            return original_cleanup(self, db, now=now)
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        ttl_seconds, _ = self.get_ttl_and_touch_interval()
        cutoff = now - timedelta(seconds=max(int(ttl_seconds) * 2, 300))
        (
            db.query(ActiveWebSession)
            .filter(
                self._stale(cutoff, now),
                ActiveWebSession.session_id != current_sid,
            )
            .delete(synchronize_session=False)
        )

    def touch_active_web_session(self, db, username, *, request, session_id, force=False):
        _state.session_id = session_id
        try:
            try:
                db.flush()
            except Exception:
                db.rollback()
            return original_touch(
                self, db, username, request=request, session_id=session_id, force=force
            )
        finally:
            _state.session_id = None

    ActiveWebSessionService.cleanup_stale_active_web_sessions = cleanup_stale_active_web_sessions
    ActiveWebSessionService.touch_active_web_session = touch_active_web_session
    ActiveWebSessionService.hermes_heartbeat_fix = True
    return "ок"


class _CacheEpochMiddleware:
    """Любой не-GET запрос к /api/ сбрасывает кеши (данные не залипают)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and (scope.get("method") or "GET").upper() not in (
            "GET",
            "HEAD",
            "OPTIONS",
        ):
            path = scope.get("path") or ""
            # Служебные POST (auth/refresh, stream-ticket, heartbeat, опрос
            # уведомлений) данные не меняют — кеш ими не сбрасываем.
            if path.startswith(("/api/configs", "/api/nodes")):
                _bump_epoch()
        await self.app(scope, receive, send)


class _ResponseCacheMiddleware:
    """Короткий кеш GET-ответов: фронт дёргает одни и те же данные пачкой."""

    RULES = (
        ("/api/configs", 3.0),
        ("/api/nodes", 5.0),
        ("/api/client-access/policies", 3.0),
        ("/api/monitoring/overview", 3.0),
    )

    def __init__(self, app):
        self.app = app
        self.store: dict = {}
        self.lock = threading.Lock()

    @staticmethod
    def _auth_key(scope):
        for name, value in scope.get("headers") or []:
            if name in (b"authorization", b"cookie"):
                return hashlib.sha1(name + b":" + value).hexdigest()
        return None

    def _ttl(self, path):
        for prefix, ttl in self.RULES:
            if path == prefix or path.startswith(prefix + "/"):
                return ttl
        return None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or (scope.get("method") or "").upper() != "GET":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if "/stream" in path:
            await self.app(scope, receive, send)
            return
        ttl = self._ttl(path)
        auth = self._auth_key(scope) if ttl else None
        if not ttl or not auth:
            await self.app(scope, receive, send)
            return

        key = (path, scope.get("query_string") or b"", auth, _current_epoch())
        now = time.monotonic()
        with self.lock:
            hit = self.store.get(key)
            if hit and hit[0] > now:
                await send({"type": "http.response.start", "status": 200, "headers": hit[1]})
                await send({"type": "http.response.body", "body": hit[2], "more_body": False})
                return

        captured = {"status": 0, "headers": [], "body": b"", "encoded": False}

        async def send_wrapper(message):
            mtype = message.get("type")
            if mtype == "http.response.start":
                captured["status"] = message.get("status") or 0
                captured["headers"] = message.get("headers") or []
                captured["encoded"] = any(
                    k.lower() == b"content-encoding" for k, _ in captured["headers"]
                )
            elif mtype == "http.response.body":
                captured["body"] += message.get("body") or b""
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if captured["status"] == 200 and not captured["encoded"] and captured["body"]:
            with self.lock:
                self.store[key] = (now + ttl, captured["headers"], captured["body"])
                if len(self.store) > 128:
                    self.store.clear()


def apply_hermes_patches(app) -> None:
    results = {}
    for label, func in (
        ("profile-files-cache", _patch_profile_files_cache),
        ("local-ip-cache", _patch_local_ip_cache),
        ("awg2-negative-cache", _patch_awg2_negative_cache),
        ("active-web-session", _patch_active_web_session),
    ):
        try:
            results[label] = func()
        except Exception as exc:  # noqa: BLE001
            results[label] = f"ошибка: {exc}"
    for label, middleware in (
        ("response-cache-middleware", _ResponseCacheMiddleware),
        ("cache-epoch-middleware", _CacheEpochMiddleware),
    ):
        try:
            app.add_middleware(middleware)
            results[label] = "ок"
        except Exception as exc:  # noqa: BLE001
            results[label] = f"ошибка: {exc}"
    print(f"[patch-panel-perf] хуки: {results}", flush=True)
'''

CONFIGS_PATH = os.path.join(TARGET_DIR, "routers", "configs.py")
OWNER_MARK = "HERMES-PATCH: owner без N+1"
OWNER_OLD = "    owner = db.query(User).filter(User.id == config.owner_id).first()"
OWNER_NEW = (
    "    # HERMES-PATCH: owner без N+1 (identity map вместо запроса на каждый конфиг)\n"
    "    owner = db.get(User, config.owner_id) if config.owner_id is not None else None"
)

MAIN_APPEND = f'''
# {MARKER}
from app.hermes_panel_patch import apply_hermes_patches as _hermes_apply_panel_patches

_hermes_apply_panel_patches(app)
'''


def write_module() -> str:
    current = ""
    if os.path.isfile(MODULE_PATH):
        with open(MODULE_PATH, encoding="utf-8") as fh:
            current = fh.read()
    if current.strip() == MODULE_SRC.strip():
        return "модуль без изменений"
    with open(MODULE_PATH, "w", encoding="utf-8") as fh:
        fh.write(MODULE_SRC)
    os.chmod(MODULE_PATH, 0o644)
    return "модуль записан"


def hook_main() -> str:
    if not os.path.isfile(MAIN_PATH):
        return f"нет {MAIN_PATH}"
    with open(MAIN_PATH, encoding="utf-8") as fh:
        text = fh.read()
    if MARKER in text:
        return "хук уже стоит"
    with open(MAIN_PATH, "a", encoding="utf-8") as fh:
        fh.write(MAIN_APPEND)
    return "хук добавлен в main.py"


def patch_owner_lookup() -> str:
    """_to_response тянул пользователя отдельным запросом на каждый конфиг."""
    if not os.path.isfile(CONFIGS_PATH):
        return f"нет {CONFIGS_PATH}"
    with open(CONFIGS_PATH, encoding="utf-8") as fh:
        text = fh.read()
    if OWNER_MARK in text:
        return "уже применён"
    if OWNER_OLD not in text:
        return "якорь не найден"
    with open(CONFIGS_PATH, "w", encoding="utf-8") as fh:
        fh.write(text.replace(OWNER_OLD, OWNER_NEW, 1))
    return "применён"


def main() -> int:
    print(f"[patch-panel-perf] {write_module()}")
    print(f"[patch-panel-perf] {hook_main()}")
    print(f"[patch-panel-perf] owner lookup: {patch_owner_lookup()}")
    import py_compile
    import tempfile

    for path in (MODULE_PATH, MAIN_PATH, CONFIGS_PATH):
        if not os.path.isfile(path):
            continue
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as tmp:
            try:
                py_compile.compile(path, doraise=True, cfile=tmp.name)
            except Exception as exc:  # noqa: BLE001
                print(f"[patch-panel-perf] ОШИБКА компиляции {path}: {exc}", file=sys.stderr)
                return 1
    print("[patch-panel-perf] синтаксис в порядке")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
