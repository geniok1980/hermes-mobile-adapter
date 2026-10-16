#!/usr/bin/env bash
# Тот же набор проверок, что и в CI, но локально — чтобы красный прогон ловился до пуша.
#
# Использование: scripts/ci/run.sh [каталог проекта]
set -uo pipefail

ROOT="${1:-.}"
cd "$ROOT" || exit 1

failed=0
step() {
  local name="$1"
  shift
  printf '  → %s… ' "$name"
  if output="$("$@" 2>&1)"; then
    printf 'ок\n'
  else
    printf 'ОШИБКА\n'
    printf '%s\n' "$output" | tail -25 | sed 's/^/      /'
    failed=$((failed + 1))
  fi
}

echo "Проверки репозитория: $(pwd)"
echo

# 1. Структурные стражи — одинаковые для всех проектов.
step "структурные проверки" python3 scripts/ci/guards.py

# 2. Синтаксис Python (без установки зависимостей: компиляция не требует импортов).
if compgen -G "**/*.py" >/dev/null 2>&1 || [ -d app ] || [ -d src ]; then
  step "синтаксис Python" python3 -m compileall -q .
fi

# 3. Линт Python, если ruff доступен.
if command -v ruff >/dev/null 2>&1; then
  step "линт ruff" ruff check .
else
  echo "  · ruff не установлен — линт пропущен (pip install ruff)"
fi

# 4. Тесты Python, если есть.
if [ -d tests ] || ls test_*.py >/dev/null 2>&1; then
  if python3 -c "import pytest" >/dev/null 2>&1; then
    step "тесты Python" python3 -m pytest -q
  else
    echo "  · pytest не установлен — тесты пропущены (pip install pytest)"
  fi
fi

# 5. Node/TypeScript, если есть package.json.
if [ -f package.json ]; then
  if [ -f tsconfig.json ] && [ -x node_modules/.bin/tsc ]; then
    step "типы TypeScript" node_modules/.bin/tsc --noEmit
  fi
  if [ -x node_modules/.bin/jest ]; then
    step "тесты Node" node_modules/.bin/jest --ci
  elif node -e "process.exit(require('./package.json').scripts?.test ? 0 : 1)" 2>/dev/null; then
    step "тесты Node" npm test --silent
  fi
fi

# 6. Документация: если настроен mkdocs и он доступен.
if [ -f mkdocs.yml ] && command -v mkdocs >/dev/null 2>&1; then
  step "сборка документации" mkdocs build --strict --quiet
fi

echo
if [ "$failed" -eq 0 ]; then
  echo "все проверки прошли"
else
  echo "проверок с ошибками: $failed"
fi
exit "$failed"
