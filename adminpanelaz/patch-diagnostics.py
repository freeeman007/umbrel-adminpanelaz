#!/usr/bin/env python3
"""Делает встроенную диагностику панели устойчивой к отсутствию утилит.

Панель в Umbrel живёт в контейнере python:3.12-slim, где нет systemd и nginx.
Родная функция _default_run_cmd падала с FileNotFoundError на первой же
отсутствующей команде, из-за чего /api/site-diagnostics/run отдавал 500
вместо отчёта. Патч ловит отсутствие бинарника и возвращает код 127 —
диагностика показывает «не удалось проверить» по этому пункту и идёт дальше.

Идемпотентно: повторный запуск ничего не меняет.
"""
import sys

PATH = "/data/app/backend/app/services/site_diagnostics.py"
MARKER = "HERMES-PATCH: resilient run_cmd"

OLD = '''def _default_run_cmd(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )'''

NEW = '''def _default_run_cmd(args: list[str], timeout: float) -> subprocess.CompletedProcess:
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
        return subprocess.CompletedProcess(args=args, returncode=127, stdout="", stderr=str(exc))'''


def main() -> int:
    try:
        with open(PATH, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"[patch-diagnostics] файл недоступен: {exc}", file=sys.stderr)
        return 0

    if MARKER in src:
        print("[patch-diagnostics] уже применён")
        return 0

    if OLD not in src:
        print("[patch-diagnostics] якорь не найден — другая версия кода, пропускаю")
        return 0

    with open(PATH, "w", encoding="utf-8") as fh:
        fh.write(src.replace(OLD, NEW, 1))
    print("[patch-diagnostics] патч применён")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
