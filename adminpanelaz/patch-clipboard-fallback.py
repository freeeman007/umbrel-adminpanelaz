#!/usr/bin/env python3
"""Копирование в буфер обмена работает и по HTTP (без secure context).

Панель открывают как http://<ip>:8000 — это не secure context, и в браузере
`navigator.clipboard` отсутствует. Все кнопки «копировать» во фронтенде зовут
`navigator.clipboard.writeText(...)` без запасного пути, поэтому падают с
«Не удалось скопировать имя» / «…код» / «…ссылку».

Патч вставляет в <head> небольшой inline-скрипт (по HTTP он получает nonce от
html_csp.inject_csp_nonce автоматически), который подменяет navigator.clipboard
на реализацию через скрытый textarea + document.execCommand('copy'), если
нативного API нет. Работают все кнопки копирования сразу: имя профиля, код
разблокировки, ссылка портала, 2FA-ключ, логи и т. д.

Правится и собранный dist/index.html (сразу), и шаблон frontend/index.html —
чтобы «Обновить/Пересобрать панель» собирали файл уже с шимом.

Идемпотентно: повторный запуск ничего не меняет.
"""
from __future__ import annotations

import os
import sys

MARKER = "HERMES-CLIPBOARD-SHIM"

TARGETS = [
    os.environ.get("PANEL_DIST_HTML", "/data/app/frontend/dist/index.html"),
    os.environ.get("PANEL_SRC_HTML", "/data/app/frontend/index.html"),
]

SNIPPET = """    <!-- HERMES-CLIPBOARD-SHIM: копирование при доступе по HTTP (без secure context) -->
    <script>
      (function () {
        if (typeof navigator === 'undefined') return;
        var native = navigator.clipboard;
        if (native && typeof native.writeText === 'function') return;

        function legacyWrite(text) {
          return new Promise(function (resolve, reject) {
            var area = null;
            try {
              area = document.createElement('textarea');
              area.value = text == null ? '' : String(text);
              area.setAttribute('readonly', '');
              area.style.position = 'fixed';
              area.style.top = '0';
              area.style.left = '0';
              area.style.width = '1px';
              area.style.height = '1px';
              area.style.padding = '0';
              area.style.border = 'none';
              area.style.outline = 'none';
              area.style.boxShadow = 'none';
              area.style.background = 'transparent';
              area.style.opacity = '0';
              document.body.appendChild(area);
              area.focus();
              area.select();
              area.setSelectionRange(0, area.value.length);
              var ok = false;
              try {
                ok = document.execCommand('copy');
              } catch (err) {
                ok = false;
              }
              if (ok) {
                resolve();
              } else {
                reject(new Error('execCommand copy failed'));
              }
            } catch (err) {
              reject(err);
            } finally {
              if (area && area.parentNode) area.parentNode.removeChild(area);
            }
          });
        }

        try {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: {
              writeText: legacyWrite,
              readText: function () {
                return Promise.reject(new Error('clipboard read is unavailable over HTTP'));
              }
            }
          });
        } catch (err) {
          /* старый браузер — оставляем как есть */
        }
      })();
    </script>
"""


def patch_html(html: str) -> str | None:
    if MARKER in html:
        return None
    lower = html.lower()
    idx = lower.find("</head>")
    if idx == -1:
        return None
    return html[:idx] + SNIPPET + html[idx:]


def main() -> int:
    changed = 0
    for path in TARGETS:
        try:
            with open(path, encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            continue
        if MARKER in html:
            print(f"[patch-clipboard] уже есть: {path}")
            continue
        patched = patch_html(html)
        if patched is None:
            print(f"[patch-clipboard] нет </head>: {path}", file=sys.stderr)
            continue
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(patched)
        changed += 1
        print(f"[patch-clipboard] вставлен шим: {path}")
    if changed:
        print(f"[patch-clipboard] файлов изменено: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
