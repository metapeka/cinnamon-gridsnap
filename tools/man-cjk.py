#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""man-cjk.py — расставить точки переноса в локализованной man-странице.

groff не разрывает строку между иероглифами: в CJK нет пробелов, а без пробела
у troff нет точки переноса. Перевод в каталоге po4a — обычная проза, и в абзаце
она выливается в одну строку на 150+ колонок: man-db ругается «cannot break
line», а страница уезжает за ширину терминала. Замер до правки: 378 символов в
самой длинной строке `man …/zh_CN/man1/gridsnap.1`.

Лечится экранированием `\\:` — groff понимает его как точку переноса нулевой
ширины (проверено: страница укладывается в колонку, `man --warnings` молчит).
Скрипт ставит `\\:` между двумя соседними CJK-символами и после CJK-символа в
конце строки: troff склеивает строки абзаца, поэтому точка переноса нужна и на
стыке строк, иначе ровно на стыках остаются неразрывные куски.

Управляющие строки roff (`.` и `'`), комментарии (`\\.\\"`) и пустые строки не
трогаются; внутрь чужих escape-последовательностей (`\\fB`, `\\[u4e00]`) вставка
не попадает — `\\:` ставится только между двумя литеральными CJK-символами.

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


def add_breaks(line):
    """Строка с `\\:` между соседними CJK-символами и на стыке строк."""
    out = []
    previous = ""
    for char in line:
        if is_cjk(previous) and is_cjk(char):
            out.append(BREAK)
        out.append(char)
        previous = char
    if line and is_cjk(line[-1]):
        out.append(BREAK)
    return "".join(out)


def convert(text):
    """Текст страницы с расставленными точками переноса."""
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        newline = "\n" if line.endswith("\n") else ""
        if (not body.strip() or body.startswith(".") or body.startswith("'")
                or body.startswith('.\\"')):
            lines.append(line)
            continue
        lines.append(add_breaks(body) + newline)
    return "".join(lines)


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
