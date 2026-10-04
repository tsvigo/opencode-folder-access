#!/usr/bin/env python3
"""Ежедневная синхронизация разрешённых папок в opencode.jsonc.

Читает список папок из 'разрешения для папок.txt' (по одной на строку),
нормализует в форму '<path>/**' и добавляет в
permission.external_directory файла opencode.jsonc, если записи ещё нет.
Дубли (внутри txt и уже имеющиеся в конфиге) пропускаются.

Форматы строк в txt:
  - голый путь:        /media/v/2TB-VNESHNIY/Downloads_real
  - путь без слэша:    usr/share/foo        -> /usr/share/foo
  - окно разрешений:   '←Access external directory /usr/share/foo'
  - bullet Patterns:   '- /usr/share/foo/*'
Мусор ('☐ Permission required', 'Patterns', '→Read .env') игнорируется.

Перед записью проверяется: путь существует и является папкой
(с раскрытием '~'). Несуществующие пути и файлы пропускаются с пометкой
в логе.

Запуск вручную:
    python3 sync-folders.py
Через cron (раз в сутки), пример:
    0 3 * * * /usr/bin/python3 "/home/v/projects/доступ к папкам в опенкоде/sync-folders.py" >> "/home/v/projects/доступ к папкам в опенкоде/sync.log" 2>&1
"""
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TXT_FILE = BASE_DIR / "разрешения для папок.txt"
CONFIG_FILE = Path("/home/v/.config/opencode/opencode.jsonc")


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def extract_path(raw: str) -> str | None:
    """Извлекает папку из строки любого из поддерживаемых форматов.

    Понимает:
      - голый путь: /media/v/2TB-VNESHNIY/Downloads_real
      - путь без ведущего слэша: usr/share/foo  -> /usr/share/foo
      - строку из окна разрешений OpenCode:
          '←Access external directory /usr/share/foo'
      - bullet-строку из блока Patterns:
          '- /usr/share/foo/*'
    Мусорные строки ('☐ Permission required', 'Patterns', '→Read .env')
    возвращают None.
    """
    s = raw.strip()
    if not s:
        return None
    if s.startswith("#") or s.startswith(";") or s.startswith("//"):
        return None
    cand = None
    m = re.search(r"Access external directory\s+(.+)$", s)
    if m:
        cand = m.group(1)
    elif s.startswith("-") or s.startswith("•"):
        cand = s[1:]
    else:
        cand = s
    cand = cand.strip().strip('"').strip("'").strip()
    if not cand:
        return None
    if cand.startswith("/") or cand.startswith("~"):
        return cand
    # Путь без ведущего слэша: только если без пробелов и со слэшем внутри
    # (мусорные строки вида '☐ Permission required' сюда не попадают)
    if " " not in cand and "/" in cand and not cand.startswith(("☐", "☑", "←", "→")):
        return "/" + cand
    return None


def normalize_pattern(raw: str) -> str | None:
    path = extract_path(raw)
    if path is None:
        return None
    # Убрать хвосты: /**, /*, одиночный /
    if path.endswith("/**"):
        path = path[:-3]
    elif path.endswith("/*"):
        path = path[:-2]
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path in ("/", "~", ""):
        return None
    return path + "/**"


def load_wanted_patterns() -> list[str]:
    if not TXT_FILE.exists():
        log(f"ОШИБКА: файл списка не найден: {TXT_FILE}")
        return []
    wanted: list[str] = []
    seen: set[str] = set()
    for line in TXT_FILE.read_text(encoding="utf-8").splitlines():
        norm = normalize_pattern(line)
        if norm is None:
            continue
        if norm in seen:
            log(f"Дубль в txt пропущен: {norm}")
            continue
        seen.add(norm)
        wanted.append(norm)
    return wanted


def find_external_block(text: str) -> tuple[int, int, int, int]:
    """Возвращает (start_brace, end_brace, block_start, block_end).

    start_brace — индекс '{' блока external_directory,
    end_brace — индекс парной '}'.
    """
    m = re.search(r'"external_directory"\s*:\s*\{', text)
    if not m:
        raise ValueError("Блок permission.external_directory не найден в opencode.jsonc")
    start_brace = m.end() - 1
    depth = 0
    end_brace = -1
    in_str = False
    esc = False
    for i in range(start_brace, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_brace = i
                    break
    if end_brace == -1:
        raise ValueError("Не найдена закрывающая скобка блока external_directory")
    return start_brace, end_brace, m.start(), end_brace


def strip_jsonc_comments(text: str) -> str:
    # Убираем // комментарии вне строк (для валидации результата)
    out_lines = []
    for line in text.splitlines():
        res = []
        in_str = False
        esc = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_str:
                res.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                i += 1
            else:
                if ch == '"':
                    in_str = True
                    res.append(ch)
                    i += 1
                elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                    break  # остаток строки — комментарий
                else:
                    res.append(ch)
                    i += 1
        out_lines.append("".join(res))
    return "\n".join(out_lines)


def main() -> int:
    log("=== Старт синхронизации ===")
    wanted = load_wanted_patterns()
    log(f"В txt уникальных записей: {len(wanted)}")
    if not wanted:
        log("Нечего добавлять. Завершено.")
        return 0
    if not CONFIG_FILE.exists():
        log(f"ОШИБКА: конфиг не найден: {CONFIG_FILE}")
        return 1

    text = CONFIG_FILE.read_text(encoding="utf-8")
    try:
        start_brace, end_brace, _, _ = find_external_block(text)
    except ValueError as e:
        log(f"ОШИБКА: {e}")
        return 1

    block_text = text[start_brace : end_brace + 1]
    missing: list[str] = []
    dup_count = 0
    skip_count = 0
    for pat in wanted:
        # Точное совпадение ключа "..." внутри блока = дубль
        if f'"{pat}"' in block_text:
            log(f"Уже есть (дубль, пропуск): {pat}")
            dup_count += 1
            continue
        # Проверка перед записью: путь должен существовать и быть папкой
        folder = pat[:-3] if pat.endswith("/**") else pat
        if not Path(folder).expanduser().is_dir():
            log(f"Пропущено (не папка или не существует): {pat}")
            skip_count += 1
            continue
        missing.append(pat)

    if not missing:
        log("Нет новых существующих папок для добавления. Изменений нет.")
        return 0

    # Бэкап конфига перед изменением
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG_FILE.parent / f"opencode.jsonc.bak-sync-{ts}"
    shutil.copy2(CONFIG_FILE, backup)
    log(f"Бэкап создан: {backup}")

    # Вставляем новые записи перед закрывающей скобкой блока.
    # Обеспечиваем запятую у предыдущей последней записи.
    before = text[:end_brace].rstrip()
    after = text[end_brace:]
    # Найти последнюю непустую строку в before внутри блока
    lines = before.split("\n")
    # Идём с конца: пропускаем пустые строки, первая строка с '"' — последняя
    # запись; добавить запятую если её нет. Строка с '{' — пустой блок.
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if stripped.endswith("{"):
            break
        if '"' in stripped:
            if not stripped.endswith(","):
                lines[idx] = lines[idx] + ","
            break
    new_entries = "".join(f'      "{pat}": "allow",\n' for pat in missing)
    # У последней вставляемой записи запятую можно оставить (JSONC допускает висячую запятую)
    new_text = "\n".join(lines) + "\n" + new_entries + after.lstrip("\n")
    # Валидация: конфиг должен парситься как JSON после снятия // комментариев
    try:
        cleaned = strip_jsonc_comments(new_text)
        # Убрать висячие запятые перед } и ] для строгого json.loads
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        json.loads(cleaned)
    except Exception as e:
        log(f"ОШИБКА валидации результата, откат к бэкапу: {e}")
        shutil.copy2(backup, CONFIG_FILE)
        return 1

    CONFIG_FILE.write_text(new_text, encoding="utf-8")
    for pat in missing:
        log(f"Добавлено: {pat}")
    log(
        f"Итого добавлено: {len(missing)}, дублей: {dup_count}, "
        f"пропущено (не папка): {skip_count}"
    )
    log("=== Готово ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
