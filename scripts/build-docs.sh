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
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r docs-requirements.txt
fi

case "$MODE" in
  serve)
    echo "→ http://127.0.0.1:8010 (Ctrl+C — остановить)"
    exec "$VENV/bin/mkdocs" serve --dev-addr 127.0.0.1:8010
    ;;
  build)
    "$VENV/bin/mkdocs" build -d site
    echo "→ статика собрана в site/"
    ;;
  check)
    "$VENV/bin/mkdocs" build -d site --strict
    COUNT=$(find site -name '*.html' | wc -l)
    MISSING=$(grep -rl "404" site/index.html 2>/dev/null | wc -l || true)
    echo "→ страниц собрано: $COUNT"
    test "$COUNT" -gt 5 || { echo "ошибка: страниц слишком мало — проверьте mkdocs.yml"; exit 1; }
    echo "→ проверка пройдена"
    ;;
  *)
    echo "использование: $0 [serve|build|check]" >&2
    exit 2
    ;;
esac
