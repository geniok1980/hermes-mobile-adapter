#!/usr/bin/env python3
"""Проверка исходников документации до сборки.

Ловит дефекты, которые сборка --strict не видит, потому что они выглядят как обычный
текст:

1. Служебные номера строк в исходниках ("12|12|## Заголовок"). Появляются, когда страницу
   переводят/правят, читая файл инструментом с нумерацией строк, и записывают назад вместе
   с префиксами. Markdown тогда считает "# Заголовок" обычным абзацем: страница выезжает
   сплошным текстом со служебными цифрами.
2. Отсутствие или дубли заголовка H1 на странице.
3. Расхождение структуры переводов: у английской страницы должно быть столько же
   заголовков, сколько у русской (перевод — зеркало, а не пересказ).
4. ```-блоки, у которых нет закрытия.

Запуск: python3 scripts/check-docs.py
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / 'site-docs'
LINE_NUMBER = re.compile(r'^\s*\d+\|')
HEADING = re.compile(r'^(#{1,6})\s+\S', re.MULTILINE)
FENCE = re.compile(r'^\s*```')

errors: list[str] = []


def prose(text: str) -> str:
    """Текст страницы без блоков кода: внутри ``` строки вида `# комментарий` — не заголовки."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return '\n'.join(out)


def pages() -> list[pathlib.Path]:
    return sorted(DOCS.rglob('*.md'))


def check_line_numbers(path: pathlib.Path, text: str) -> None:
    bad = [i for i, line in enumerate(text.splitlines(), 1) if LINE_NUMBER.match(line)]
    if bad:
        errors.append(
            f'{path.relative_to(ROOT)}: служебные номера строк в {len(bad)} строках '
            f'(первая — {bad[0]}): страница отрендерится как сплошной текст'
        )


def check_h1(path: pathlib.Path, text: str) -> None:
    levels = [len(m.group(1)) for m in HEADING.finditer(prose(text))]
    h1 = levels.count(1)
    if h1 == 0:
        errors.append(f'{path.relative_to(ROOT)}: нет заголовка H1')
    elif h1 > 1:
        errors.append(f'{path.relative_to(ROOT)}: заголовков H1 — {h1}, должен быть один')


def check_fences(path: pathlib.Path, text: str) -> None:
    fences = sum(1 for line in text.splitlines() if line.lstrip().startswith('```'))
    if fences % 2:
        errors.append(f'{path.relative_to(ROOT)}: не закрыт блок кода (``` — {fences})')


def headings(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r'^(#{1,6})\s+(.*)$', prose(text), re.MULTILINE)]


def check_translation_parity(pairs: list[tuple[pathlib.Path, pathlib.Path]]) -> None:
    for ru, en in pairs:
        a, b = headings(ru.read_text(encoding='utf-8')), headings(en.read_text(encoding='utf-8'))
        if len(a) != len(b):
            errors.append(
                f'{en.relative_to(ROOT)}: заголовков {len(b)}, а в русском оригинале '
                f'{len(a)} — перевод должен повторять структуру'
            )


def main() -> int:
    ru_pages = [p for p in pages() if not p.name.endswith('.en.md')]
    en_pages = [p for p in pages() if p.name.endswith('.en.md')]

    for path in pages():
        text = path.read_text(encoding='utf-8')
        check_line_numbers(path, text)
        check_h1(path, text)
        check_fences(path, text)

    pairs = []
    for en in en_pages:
        ru = en.with_name(en.name.replace('.en.md', '.md'))
        if not ru.exists():
            errors.append(f'{en.relative_to(ROOT)}: нет русского оригинала рядом')
            continue
        pairs.append((ru, en))
    check_translation_parity(pairs)

    print(f'страниц: {len(ru_pages)} русских + {len(en_pages)} английских')
    if errors:
        print('\nнайдено проблем:')
        for e in errors:
            print(f'  - {e}')
        return 1
    print('проверки пройдены: номера строк, H1, блоки кода, структура переводов')
    return 0


if __name__ == '__main__':
    sys.exit(main())
