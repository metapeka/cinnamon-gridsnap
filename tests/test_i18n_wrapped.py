#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строки интерфейса обёрнуты в `_()` — и это проверяет тест, а не надежда.

Правило волны локализации: каждая пользовательская строка — один литерал
внутри `_()` (подстановки — `.format(...)`), а не f-строка и не склейка
фрагментов: иначе строки нет в каталоге, а перевод ломает порядок слов.

Тест разбирает исходники через `ast` и ругается на кириллицу в строковых
литералах, которые не являются аргументом `_()`/`translate`. Не считаются
нарушением:

* докстринги (это документация кода, а не текст для пользователя);
* строки на строке с пометкой ``# i18n: не переводится`` — регулярки, ключи
  конфига и заголовки, которые код пишет в файлы (они не текст интерфейса).

Запуск:
    python3 tests/test_i18n_wrapped.py
    python3 -m unittest discover -s tests -v
"""

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Файлы с пользовательскими строками (апплет — JavaScript, у него свой тест).
# Список — все модули приложения, а не только те, где строки были на момент
# обёртки: иначе следующая строка, добавленная в «тихий» модуль, осталась бы
# русской только на русском (так и случилось с `zone_geom.set_origin_text`,
# который видно в статусе редактора и на демонстрации).
SOURCES = (
    "window_backend.py",
    "zone-editor.py",
    "zone-picker.py",
    "zone-tabstrip.py",
    "zone-hotcorners.py",
    "zone-tiler.py",
    "zone-daemon.py",
    "zone_config.py",
    "zone_actions.py",
    "zone_geom.py",
    "zone_ipc.py",
    "zone_reflow.py",
    "zone_tabs.py",
    "zone_picker_nav.py",
    "zone_tabstrip_nav.py",
    "setup-hotkeys.py",
    "make-icon.py",
    "gridsnap-ctl",
)

# Пометка «эта строка не текст для пользователя» (регулярка, ключ, заголовок файла).
SKIP_MARK = "i18n: не переводится"

CYRILLIC = re.compile(r"[\u0400-\u04ff]")

# Имена, которыми оборачивают строку: `_` из `zone_i18n` и полное `translate`.
WRAPPERS = ("_", "translate")


def is_cyrillic(text):
    """Есть ли в строке кириллица (текст для пользователя, а не идентификатор)."""
    return bool(CYRILLIC.search(text))


def docstring_ids(tree):
    """`id()` строковых констант-докстрингов: модуль, класс, функция."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


def wrapper_arg_ids(tree):
    """`id()` констант, которые стоят прямым аргументом `_()`/`translate()`."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in WRAPPERS:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(id(arg))
    return found


def marked_lines(source):
    """Номера строк с пометкой «не переводится» (1-нумерация)."""
    return {n for n, line in enumerate(source.splitlines(), start=1) if SKIP_MARK in line}


def unwrapped(source, path):
    """Список нарушений: `(строка, текст, почему)` — по порядку строк."""
    tree = ast.parse(source)
    skip_doc = docstring_ids(tree)
    skip_arg = wrapper_arg_ids(tree)
    skip_mark = marked_lines(source)

    # Родитель нужен, чтобы отличить обычную строку от части f-строки.
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not is_cyrillic(node.value):
            continue
        if id(node) in skip_doc or id(node) in skip_arg:
            continue
        if any(line in skip_mark for line in range(node.lineno, node.end_lineno + 1)):
            continue
        if isinstance(parents.get(node), ast.JoinedStr):
            found.append((node.lineno, node.value, "f-строка: шаблон надо собрать литералом"))
        else:
            found.append((node.lineno, node.value, "строка не обёрнута в _()"))
    return sorted(found)


class TestStringsWrapped(unittest.TestCase):
    def test_every_user_string_is_wrapped(self):
        problems = []
        for name in SOURCES:
            path = os.path.join(ROOT, name)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for line, text, why in unwrapped(source, path):
                problems.append("%s:%d: %s — %r" % (name, line, why, text))
        self.assertEqual(
            problems, [],
            "\nОбёрнуты не все пользовательские строки:\n  " + "\n  ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
