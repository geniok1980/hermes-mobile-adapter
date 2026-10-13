#!/usr/bin/env bash
# Сборка и локальный просмотр сайта документации (MkDocs Material).
#
#   ./scripts/build-docs.sh serve   # http://127.0.0.1:8010 с автоперезагрузкой
#   ./scripts/build-docs.sh build   # статика в site/ (проверка перед публикацией)
#   ./scripts/build-docs.sh check   # собрать и проверить все страницы (битые ссылки/страницы)
#
# Публикация идёт автоматически: .github/workflows/docs.yml собирает сайт и выкладывает
# на GitHub Pages при каждом коммите в site-docs/ или mkdocs.yml.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-serve}"
VENV="${DOCS_VENV:-$ROOT/.docs-venv}"

if [ ! -x "$VENV/bin/mkdocs" ]; then
  echo "→ создаю виртуальное окружение документации в $VENV"
  # На хосте может не быть ensurepip (python3 -m venv не работает) — тогда берём uv.
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python 3.12
    uv pip install --python "$VENV/bin/python" -r docs-requirements.txt
  else
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -r docs-requirements.txt
  fi
fi

case "$MODE" in
  serve)
    echo "→ http://127.0.0.1:8010 (Ctrl+C — остановить)"
    exec "$VENV/bin/mkdocs" serve --dev-addr 127.0.0.1:8010
    ;;
  build)
    python3 scripts/check-docs.py
    "$VENV/bin/mkdocs" build -d site --strict
    echo "→ статика собрана в site/"
    ;;
  check)
    python3 scripts/check-docs.py
    "$VENV/bin/mkdocs" build -d site --strict
    COUNT=$(find site -name '*.html' | wc -l)
    RU=$(find site -name 'index.html' -not -path 'site/en/*' | wc -l)
    EN=$(find site/en -name 'index.html' | wc -l)
    echo "→ страниц собрано: $COUNT (русских $RU, английских $EN)"
    test "$COUNT" -gt 5 || { echo "ошибка: страниц слишком мало — проверьте mkdocs.yml"; exit 1; }
    test "$EN" -gt 5 || { echo "ошибка: английская ветка не собралась — проверьте mkdocs.yml"; exit 1; }
    echo "→ проверка пройдена"
    ;;
  *)
    echo "использование: $0 [serve|build|check]" >&2
    exit 2
    ;;
esac
