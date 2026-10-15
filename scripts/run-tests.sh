#!/usr/bin/env bash
# Прогон тестов адаптера.
#
# Почему не `unittest discover`: файлы тестов импортируют друг друга и модули ядра Hermes,
# поэтому нужен и каталог тестов в пути импорта, и сам каталог ядра (`hermes_yaml` живёт
# там, а не в site-packages). Discovery этого не делает и падает на импортах.
#
# Использование: scripts/run-tests.sh [каталог-ядра-hermes]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${1:-/usr/local/lib/hermes-agent}"
PYTHON="${PYTHON:-$HERMES_HOME/venv/bin/python}"

if [ ! -x "$PYTHON" ]; then
  echo "не найден python ядра Hermes: $PYTHON (передайте каталог ядра или задайте PYTHON)"
  exit 1
fi

export PYTHONPATH="$HERMES_HOME${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT/tests"

status=0
total=0
for file in test_*.py; do
  out="$("$PYTHON" -m unittest "$file" -q 2>&1)" || status=1
  ran="$(printf '%s' "$out" | sed -n 's/^Ran \([0-9]\+\) tests\?.*/\1/p' | tail -1)"
  if printf '%s' "$out" | grep -q '^OK'; then
    printf '  %-28s OK (%s)\n' "$file" "${ran:-0}"
  else
    printf '  %-28s СБОЙ\n' "$file"
    printf '%s\n' "$out" | tail -15
  fi
  total=$(( total + ${ran:-0} ))
done

echo
if [ "$status" -eq 0 ]; then
  echo "все тесты прошли: $total"
else
  echo "есть сбои (тестов всего: $total)"
fi
exit "$status"
