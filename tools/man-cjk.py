#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""man-cjk.py — расставить точки переноса в локализованной man-странице.

groff не разрывает строку между иероглифами: в CJK нет пробелов, а без пробела у
troff нет точки переноса. Перевод в каталоге po4a — обычная проза, и в абзаце она
выливается в одну строку на 190+ колонок: man-db ругается «cannot break line», а
страница уезжает за ширину терминала. Замер по установленному префиксу: 191
колонка в самой длинной строке `man gridsnap` под `zh_CN.UTF-8`.

Лечится экранированием `\\:` — groff понимает его как точку переноса нулевой
ширины (проверено: страница укладывается в 79 колонок, «cannot break line» ноль).
Точка ставится между двумя соседними символами, из которых **хотя бы один** CJK,
если между ними нет пробела: иероглифы и полноширинные знаки препинания стоят
вплотную к латинице, поэтому `GRIDSNAP_WINDOW、GRIDSNAP_ZONE` — один неразрывный
кусок, и без точки он и вылезает за колонку.

Стык строк тоже требует точки: troff склеивает строки абзаца, и без неё ровно на
стыках остаются неразрывные куски. Поэтому для каждой текстовой строки смотрится
и первый символ следующей.

Управляющие строки roff (`.` и `'`), комментарии (`\\.\\"`), пустые строки и чужие
escape-последовательности не трогаются: `\\:` ставится только между двумя
литеральными символами строки, а не внутри `\\fB` или `\\[u4e00]`.

    tools/man-cjk.py man/build/zh_CN/man1/gridsnap.1
"""

import os
import re
import sys
import tempfile

# CJK: иероглифы, кана, полноширинные формы и знаки препинания. Границы диапазонов
# — из Unicode-блоков CJK; латиница, кириллица и служебные символы сюда не входят.
CJK_RE = re.compile(
    "["
    "\u2e80-\u2eff"      # ключи Канси и дополнения
    "\u3000-\u303f"      # знаки препинания CJK
    "\u3040-\u30ff"      # хирагана и катакана
    "\u3400-\u4dbf"      # CJK, дополнение A
    "\u4e00-\u9fff"      # CJK, основной блок
    "\uf900-\ufaff"      # совместимые идеографы
    "\ufe30-\ufe4f"      # формы совместимости CJK
    "\uff00-\uffef"      # полноширинные формы
    "\U00020000-\U0002ffff"
    "]"
)

BREAK = "\\:"


def is_cjk(char):
    return bool(CJK_RE.match(char))


def needs_break(left, right):
    """Нужна ли точка переноса между двумя соседними символами.

    Достаточно CJK с одной стороны: пробела между ними нет, а значит у troff нет и
    точки переноса. Полноширинные знаки препинания стоят вплотную к латинице, и
    без этого правила список `GRIDSNAP_WINDOW、GRIDSNAP_ZONE` остаётся одним
    неразрывным куском.
    """
    if not left or not right:
        return False
    if left.isspace() or right.isspace():
        return False
    return is_cjk(left) or is_cjk(right)


def add_breaks(line, next_char=""):
    """Строка с `\\:` между соседними символами и на стыке со следующей."""
    out = []
    previous = ""
    for char in line:
        if needs_break(previous, char):
            out.append(BREAK)
        out.append(char)
        previous = char
    # Конец строки: после иероглифа — всегда (вреда нет), а на стыке абзаца —
    # если строка продолжается (troff склеивает строки).
    if is_cjk(previous) or needs_break(previous, next_char):
        out.append(BREAK)
    return "".join(out)


def is_macro(body):
    """Управляющая строка roff: макрос, запрос, комментарий или пустая строка."""
    return (not body.strip() or body.startswith(".") or body.startswith("'"))


def convert(text):
    """Текст страницы с расставленными точками переноса."""
    rows = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        rows.append((body, "\n" if line.endswith("\n") else ""))

    out = []
    for index, (body, newline) in enumerate(rows):
        if is_macro(body):
            out.append(body + newline)
            continue
        # Первый символ следующей строки — но только если она продолжает тот же
        # абзац: пустая строка и макрос абзац заканчивают.
        next_body = rows[index + 1][0] if index + 1 < len(rows) else ""
        next_char = "" if is_macro(next_body) else next_body[:1]
        out.append(add_breaks(body, next_char) + newline)
    return "".join(out)


def main(argv):
    if len(argv) != 2:
        print("использование: man-cjk.py ФАЙЛ.1", file=sys.stderr)
        return 2
    path = argv[1]
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    result = convert(text)
    directory = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False) as handle:
        handle.write(result)
        temporary = handle.name
    os.replace(temporary, path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
