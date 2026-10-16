#!/usr/bin/env python3
"""Структурные проверки репозитория — одинаковые для всех наших проектов.

Идея взята из проверок ядра Hermes: они не про код проекта, а про то, что в репозиторий
не просочилось лишнего и он остаётся читаемым. Каждая проверка печатает, ГДЕ проблема и
ЧЕМ она плоха, а не просто «fail»: иначе локальный прогон бесполезен.

Запуск:
    python3 scripts/ci/guards.py                # все проверки, человеческий вывод
    python3 scripts/ci/guards.py --json         # машинный вывод (для отчётов)
    python3 scripts/ci/guards.py --only secrets  # одна проверка
Код возврата: 0 — чисто, 1 — есть находки.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ── что считаем секретом ──────────────────────────────────────────────────────
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "приватный ключ"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "ключ вида OpenAI"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "токен GitHub"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"), "токен GitHub (fine-grained)"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "ключ Google"),
    (re.compile(r"\b\d{9,10}:AA[A-Za-z0-9_\-]{30,}\b"), "токен Telegram-бота"),
    (re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{20,}\b"), "ключ платёжного сервиса"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*"
                r"['\"][^'\"]{12,}['\"]"), "пароль или ключ прямо в коде"),
]

# Файлы, которых в репозитории быть не должно (даже если в них «ничего важного»).
FORBIDDEN_TRACKED = [
    (re.compile(r"(^|/)\.env$"), "файл .env с настройками окружения"),
    (re.compile(r"(^|/)\.env\.(?!example|sample|template)[a-z]+$"), "файл окружения"),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"), "SSH-ключ"),
    (re.compile(r"\.(pem|p12|pfx|jks|keystore)$"), "ключ или хранилище сертификатов"),
    (re.compile(r"(^|/)(node_modules|__pycache__|\.venv|venv|dist|build)/"), "сборка или зависимости"),
    (re.compile(r"\.(pyc|pyo|class|o|so|dylib|dll|exe|apk|ipa|aab)$"), "собранный артефакт"),
]

INLINE_ALLOW = re.compile(r"ci-allow(?:\s*:\s*(?P<reason>[^#]*))?")
IGNORE_FILE = ".ci-ignore"
MAX_BINARY_BYTES = 1024 * 1024          # крупные бинарники в истории репозитория не нужны
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yml", ".yaml", ".toml", ".md", ".txt",
    ".sh", ".bash", ".sql", ".html", ".css", ".scss", ".ini", ".cfg", ".conf", ".env",
    ".dockerfile", ".gradle", ".kt", ".swift", ".dart", ".xml", ".mk", ".rb", ".go", ".rs",
    ".java", ".cs", ".php", ".vue", ".svelte", ".mjs", ".cjs", ".lock",
}


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def tracked_files() -> list[str]:
    """Файлы под контролем версий (без игнорируемых)."""
    return [line for line in git("ls-files").splitlines() if line.strip()]


def load_ignores() -> list[tuple[str, str, str]]:
    """Исключения из файла .ci-ignore: строки «проверка|маска пути|причина»."""
    path = Path(IGNORE_FILE)
    if not path.is_file():
        return []
    rules = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            continue
        rules.append((parts[0], parts[1], parts[2]))
    return rules


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def ignored(rule_path: str, path: str) -> bool:
    """Подходит ли путь под маску исключения: «*» — любой хвост, «**» — где угодно."""
    import fnmatch

    if fnmatch.fnmatch(path, rule_path):
        return True
    if rule_path.endswith("/**") and path.startswith(rule_path[:-3]):
        return True
    if rule_path.startswith("**/") and fnmatch.fnmatch(path, rule_path[3:]):
        return True
    return False


def is_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if path.name.lower() in {"dockerfile", "makefile", "procfile", "license"}:
        return True
    try:
        chunk = path.open("rb").read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    return True


# Значения, которые секретами не являются: заготовки, примеры, повторы одного символа.
PLACEHOLDER = re.compile(
    r"(?i)^(?:<[^>]*>|\$\{[^}]*\}|\{\{[^}]*\}\}|x{3,}|\*{3,}|changeme|change_me|placeholder"
    r"|example|sample|dummy|fake|test|none|null|todo|your[_-]?\w+|secret|password|token|0123456789abcdef)$"
)


def looks_like_real_secret(value: str) -> bool:
    """Грубая, но полезная оценка: настоящий ключ обычно длинный и смешанный."""
    if PLACEHOLDER.match(value.strip()):
        return False
    body = value.strip().strip("'\"")
    if len(body) < 16:
        return False
    has_digit = any(ch.isdigit() for ch in body)
    has_upper = any(ch.isupper() for ch in body)
    has_lower = any(ch.islower() for ch in body)
    if not (has_digit and has_upper and has_lower):
        return False
    # Значение из одного и того же фрагмента («0123456789abcdef» * 2) — тоже не секрет.
    for size in range(4, len(body) // 2 + 1):
        piece = body[:size]
        if piece * (len(body) // size) == body:
            return False
    return True


class Findings:
    """Находки одной проверки: файл → что именно не так."""

    def __init__(self, name: str, why: str) -> None:
        self.name = name
        self.why = why
        self.items: list[dict] = []

    def add(self, path: str, detail: str, line: int | None = None) -> None:
        self.items.append({"path": path, "detail": detail, "line": line})

    @property
    def ok(self) -> bool:
        return not self.items


# ── проверки ──────────────────────────────────────────────────────────────────

def check_secrets(files: list[str]) -> Findings:
    """Секреты не должны попадать в репозиторий никогда — даже «на минутку»."""
    found = Findings("secrets", "секрет, попавший в коммит, считается утёкшим и требует перевыпуска")
    allowed = 0
    for name in files:
        path = Path(name)
        if not path.is_file() or not is_text(path):
            continue
        for number, line in enumerate(read_text(path).splitlines(), start=1):
            for pattern, what in SECRET_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                # Заготовки и повторы отсеиваем: они дают ложные срабатывания каждый день.
                if not looks_like_real_secret(match.group(0)):
                    continue
                # Отметка в строке: осознанное исключение с причиной прямо в коде.
                if INLINE_ALLOW.search(line):
                    allowed += 1
                    continue
                found.add(name, what, number)
                break
    if allowed:
        found.add(IGNORE_FILE, f"в коде {allowed} отметок ci-allow (проверьте причины)", None)
    return found


def check_ignored_env(files: list[str]) -> Findings:
    """Файлы окружения и ключи не должны быть под контролем версий."""
    found = Findings("forbidden", "такие файлы не должны попадать в историю: их читают все, кто видит репозиторий")
    for name in files:
        for pattern, what in FORBIDDEN_TRACKED:
            if pattern.search(name):
                found.add(name, what)
                break
    return found


def check_large_files(files: list[str]) -> Findings:
    """Крупные бинарники раздувают историю и не читаются в дифе."""
    found = Findings("large-files", "крупный файл в репозитории раздувает клонирование и прячет изменение от ревью")
    for name in files:
        path = Path(name)
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > MAX_BINARY_BYTES and not is_text(path):
            found.add(name, f"{size / 1048576:.1f} МБ")
    return found


def check_case_collisions(files: list[str]) -> Findings:
    """`README.md` и `readme.md` на Linux — разные файлы, на Windows и macOS — нет."""
    found = Findings("case-collisions", "имена, различающиеся только регистром, ломают клонирование на Windows и macOS")
    by_lower: dict[str, list[str]] = {}
    for name in files:
        by_lower.setdefault(name.lower(), []).append(name)
    for variants in by_lower.values():
        if len(variants) > 1:
            found.add(variants[0], "похожие имена: " + ", ".join(sorted(variants)))
    return found


def check_conflict_markers(files: list[str]) -> Findings:
    """Неразрешённый конфликт слияния в коде — признак незавершённого мержа."""
    found = Findings("conflicts", "маркеры конфликта в файле означают, что слияние не довели до конца")
    for name in files:
        path = Path(name)
        if not path.is_file() or not is_text(path):
            continue
        for number, line in enumerate(read_text(path).splitlines(), start=1):
            if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line.rstrip() == "=======":
                found.add(name, "маркер конфликта слияния", number)
                break
    return found


def check_line_endings(files: list[str]) -> Findings:
    """Windows-переводы строк превращают каждую строку файла в изменение."""
    found = Findings("line-endings", "CRLF в текстовом файле делает дифи нечитаемыми: меняется каждая строка")
    for name in files:
        path = Path(name)
        if not path.is_file() or not is_text(path):
            continue
        try:
            raw = path.open("rb").read(65536)
        except OSError:
            continue
        if b"\r\n" in raw:
            found.add(name, "переводы строк CRLF")
    return found


def check_lockfiles(files: list[str]) -> Findings:
    """Манифест зависимостей без замка — сборки невоспроизводимы."""
    found = Findings("lockfiles", "без замка версии зависимостей, значит сборка «как получится»")
    names = set(files)
    pairs = [
        ("package.json", ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"]),
        ("pyproject.toml", ["uv.lock", "poetry.lock", "requirements.txt", "pdm.lock"]),
        ("Pipfile", ["Pipfile.lock"]),
        ("composer.json", ["composer.lock"]),
        ("Gemfile", ["Gemfile.lock"]),
    ]
    for manifest, locks in pairs:
        if manifest in names and not any(lock in names for lock in locks):
            found.add(manifest, f"нет замка ({', '.join(locks)})")
    return found


def check_docs_pairs(files: list[str]) -> Findings:
    """Если документация двуязычная, у каждой русской страницы должна быть английская."""
    found = Findings("docs-pairs", "двуязычная документация расходится, если у страницы нет пары")
    if "mkdocs.yml" not in files:
        return found
    for name in files:
        if not name.endswith(".md") or name.endswith(".en.md"):
            continue
        if not name.startswith(("site-docs/", "docs/")):
            continue
        pair = name[: -len(".md")] + ".en.md"
        if pair not in files:
            found.add(name, f"нет английской версии ({pair})")
    return found


CHECKS = [
    check_secrets,
    check_ignored_env,
    check_large_files,
    check_case_collisions,
    check_conflict_markers,
    check_line_endings,
    check_lockfiles,
    check_docs_pairs,
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Структурные проверки репозитория")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--only", help="выполнить только одну проверку (по имени)")
    parser.add_argument("--root", default=".", help="каталог репозитория")
    args = parser.parse_args()

    os.chdir(args.root)
    files = tracked_files()
    if not files:
        print("это не репозиторий git (или в нём нет файлов под контролем версий)")
        return 1

    rules = load_ignores()
    results = []
    for check in CHECKS:
        findings = check(files)
        if args.only and findings.name != args.only:
            continue
        # Исключения применяем после проверки: в отчёт попадает только то, что не исключено.
        kept = []
        skipped = []
        for item in findings.items:
            rule = next((r for r in rules if r[0] == findings.name and ignored(r[1], item["path"])), None)
            if rule:
                skipped.append((item, rule))
            else:
                kept.append(item)
        if skipped:
            print(f"  … {findings.name}: пропущено по .ci-ignore — {len(skipped)}")
            for item, rule in skipped[:5]:
                print(f"     {item['path']} — причина: {rule[2]}")
        findings.items = kept
        results.append(findings)

    if args.json:
        print(json.dumps(
            {
                "files": len(files),
                "checks": [
                    {"name": f.name, "ok": f.ok, "items": f.items} for f in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0 if all(f.ok for f in results) else 1

    failed = 0
    for findings in results:
        if findings.ok:
            print(f"  ✅ {findings.name}")
            continue
        failed += 1
        print(f"  ❌ {findings.name} — {len(findings.items)} находок")
        print(f"     почему это важно: {findings.why}")
        for item in findings.items[:12]:
            where = f"{item['path']}:{item['line']}" if item["line"] else item["path"]
            print(f"       {where} — {item['detail']}")
        if len(findings.items) > 12:
            print(f"       … и ещё {len(findings.items) - 12}")

    print()
    print(f"  файлов проверено: {len(files)}; проверок: {len(results)}; с находками: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
