#!/usr/bin/env python3
"""Делает встроенную диагностику панели пригодной для запуска в контейнере.

Панель в Umbrel живёт в контейнере python:3.12-slim:
  * нет systemd — сервисом управляет umbrelOS;
  * нет systemctl/nginx/iptables/ipset.

Родная диагностика на первой отсутствующей утилите падала с FileNotFoundError,
и /api/site-diagnostics/run отдавал 500 вместо отчёта.

Патч 1: _default_run_cmd ловит отсутствие бинарника и возвращает код 127 —
        пункт помечается как «не удалось проверить», отчёт идёт дальше.
Патч 2: проверка systemd в контейнере помечается как «не применимо» (warn),
        а не «Unit-файл не найден» (fail) — иначе отчёт всегда красный.

Идемпотентно: повторный запуск ничего не меняет.
"""
import sys

PATH = "/data/app/backend/app/services/site_diagnostics.py"

PATCHES = [
    (
        "HERMES-PATCH: resilient run_cmd",
        '''def _default_run_cmd(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )''',
        '''def _default_run_cmd(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # HERMES-PATCH: resilient run_cmd
    # В контейнере нет systemd/nginx: отсутствие утилиты должно попадать в отчёт
    # как «не удалось проверить», а не ронять весь запрос в 500.
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))''',
    ),
    (
        "HERMES-PATCH: container systemd",
        '''    unit_path = f"/etc/systemd/system/{ctx.service_name}.service"
    app_port = env.get("BACKEND_PORT", env.get("APP_PORT", "8000"))

    if os.path.isfile(unit_path):''',
        '''    unit_path = f"/etc/systemd/system/{ctx.service_name}.service"
    app_port = env.get("BACKEND_PORT", env.get("APP_PORT", "8000"))

    # HERMES-PATCH: container systemd
    # В контейнере Umbrel systemd нет: процессом управляет umbrelOS.
    # Отсутствие unit-файла здесь — норма, а не ошибка установки.
    if shutil.which("systemctl") is None and not os.path.isfile(unit_path):
        _append_result(
            report,
            CheckResult(
                "warn",
                "Systemd не используется — запуск в контейнере Umbrel",
                detail="процессом управляет umbrelOS (docker), unit-файл не требуется",
                hint_ru="Проверка не применима в контейнере",
            ),
        )
        return

    if os.path.isfile(unit_path):''',
    ),
]


def main() -> int:
    try:
        with open(PATH, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"[patch-diagnostics] файл недоступен: {exc}", file=sys.stderr)
        return 0

    changed = 0
    for marker, old, new in PATCHES:
        if marker in src:
            print(f"[patch-diagnostics] уже есть: {marker}")
            continue
        if old not in src:
            print(f"[patch-diagnostics] якорь не найден: {marker}")
            continue
        src = src.replace(old, new, 1)
        changed += 1
        print(f"[patch-diagnostics] применён: {marker}")

    if changed:
        with open(PATH, "w", encoding="utf-8") as fh:
            fh.write(src)
        print(f"[patch-diagnostics] записано, патчей применено: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
