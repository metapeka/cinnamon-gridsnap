#!/usr/bin/env python3
"""Print the `[gallery]` cards as gettext entries; `make pot` merges them into the template.

The titles and descriptions of the ready-made layouts live in `gallery/*.conf`, not in
the code: the catalog is data, and `GRIDSNAP_GALLERY` may point at layouts of one's own.
`xgettext` can only see literals, so `zone_config.gallery_sources()` reads the words out
of the files and this script writes them as a `.po` fragment — `msgcat` puts it together
with the template of the code. The same words reach the interface through
`zone_i18n.gettext` at read time (`zone_config.gallery_layouts`), so an entry that is
missing from a catalog simply keeps its source text.

Run through `make pot`; nothing here writes to the repository.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_config as zc  # noqa: E402


def po_string(text):
    """A `.po` quoted string: backslashes and quotes escaped, the rest as UTF-8."""
    return '"{}"'.format(text.replace("\\", "\\\\").replace('"', '\\"'))


def main():
    # `msgcat` refuses an input without a header: the charset lives there. The code
    # template is the first input of `make pot`, so its header is the one that stays.
    print('msgid ""')
    print('msgstr ""')
    print('"Content-Type: text/plain; charset=UTF-8\\n"')
    print('"Content-Transfer-Encoding: 8bit\\n"')
    print()
    for card in zc.gallery_sources():
        for line in (card["title"], card["description"]):
            if not line:
                continue
            print("#: gallery/{}.conf".format(card["id"]))
            print("msgid {}".format(po_string(line)))
            print('msgstr ""')
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
