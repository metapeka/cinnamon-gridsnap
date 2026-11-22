#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тесты чистого ядра действий (`zone_actions`) — без X и GTK.

Здесь проверяется только то, что ядро обещает снаружи: контекст окна и зоны в
двух каналах (`values`/`env`), разбор команды с подстановками, распознавание
встроенных действий, зеркальная рамка и поиск зеркальной зоны по геометрии, а
также «окно в зоне» и «зона окна». Всё это — арифметика рамок и разбор строк,
поэтому X-сервер и GTK не нужны: раскладчик (`zone-tiler.py`) сюда не входит.

Запуск:
    python3 tests/test_zone_actions.py
    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import zone_actions as za  # noqa: E402
import zone_tabs  # noqa: E402

# Полный контекст: окно 0x1a00007, зона main, её пиксельная рамка, монитор, стол.
FULL = za.ActionContext(
    window=0x1A00007,
    zone="main",
    rect=(100, 50, 800, 600),
    monitor="HDMI-1",
    workspace=2,
    wm_class="Gnome-terminal",
    wm_title="Терминал — «заметки»",
    conf="/home/user/.config/gridsnap/zones.conf",
)

# Имена переменных окружения — из докстринга `ActionContext.env`. Список
# выписан руками, а не собран из `PLACEHOLDERS`: тест должен падать, если имя
# переменной поменяют, а не подтверждать любое содержимое константы.
ENV_NAMES = (
    "GRIDSNAP_WINDOW",
    "GRIDSNAP_ZONE",
    "GRIDSNAP_X",
    "GRIDSNAP_Y",
    "GRIDSNAP_W",
    "GRIDSNAP_H",
    "GRIDSNAP_MONITOR",
    "GRIDSNAP_WORKSPACE",
    "GRIDSNAP_CLASS",
    "GRIDSNAP_TITLE",
    "GRIDSNAP_CONF",
)


class ActionContextTest(unittest.TestCase):
    """Оба канала контекста: подстановки и окружение."""

    def test_full_context_values(self):
        self.assertEqual(
            FULL.values(),
            {
                "window": "0x1a00007",
                "zone": "main",
                "x": "100",
                "y": "50",
                "w": "800",
                "h": "600",
                "monitor": "HDMI-1",
                "workspace": "2",
                "class": "Gnome-terminal",
                "title": "Терминал — «заметки»",
                "conf": "/home/user/.config/gridsnap/zones.conf",
            },
        )

    def test_empty_context_gives_empty_strings(self):
        # Пустой контекст — не «None» в чужой команде, а «ничего»: подстановка
        # попадает в argv, и выдуманное слово там выглядело бы как значение.
        self.assertEqual(
            za.ActionContext().values(),
            {name: "" for name in za.PLACEHOLDERS},
        )

    def test_window_is_hex_and_missing_window_is_empty(self):
        self.assertEqual(za.ActionContext(window=0x1A00007).values()["window"], "0x1a00007")
        self.assertEqual(za.ActionContext().values()["window"], "")
        self.assertEqual(za.ActionContext(workspace=0).values()["workspace"], "0")

    def test_rect_parts_are_taken_from_rect(self):
        values = FULL.values()
        self.assertEqual(
            (values["x"], values["y"], values["w"], values["h"]), ("100", "50", "800", "600")
        )

    def test_env_names_are_exactly_the_documented_ones(self):
        self.assertEqual(tuple(za.ActionContext().env()), ENV_NAMES)
        self.assertEqual(
            set(za.ActionContext().env()), {"GRIDSNAP_" + n.upper() for n in za.PLACEHOLDERS}
        )

    def test_env_values_are_the_same_as_values(self):
        env = FULL.env()
        values = FULL.values()
        for name in za.PLACEHOLDERS:
            self.assertEqual(env["GRIDSNAP_" + name.upper()], values[name])


class ExpandCommandTest(unittest.TestCase):
    """Команда из конфига: `shlex` и подстановки `{имя}`."""

    def context(self, **kwargs):
        defaults = {"zone": "main", "rect": (10, 20, 300, 400)}
        defaults.update(kwargs)
        return za.ActionContext(**defaults)

    def test_no_placeholders_split_like_shlex(self):
        self.assertEqual(
            za.expand_command("notify-send -t 3000 'окно в зоне'", self.context()),
            ["notify-send", "-t", "3000", "окно в зоне"],
        )

    def test_placeholders_are_substituted_in_each_argument(self):
        context = self.context(window=0x1A00007, monitor="HDMI-1", workspace=1)
        self.assertEqual(
            za.expand_command(
                "my-cmd --window {window} --zone {zone} --monitor {monitor} "
                "--desk {workspace} --rect {x},{y} --size {w}x{h}",
                context,
            ),
            [
                "my-cmd",
                "--window",
                "0x1a00007",
                "--zone",
                "main",
                "--monitor",
                "HDMI-1",
                "--desk",
                "1",
                "--rect",
                "10,20",
                "--size",
                "300x400",
            ],
        )

    def test_title_with_spaces_and_quote_stays_one_argument(self):
        # Заголовок приходит из X и может содержать что угодно; в кавычках
        # конфига он должен остаться одним аргументом, а не рассыпаться.
        title = 'Заметки "на день" — файл'
        argv = za.expand_command('notify-send "{title}"', self.context(wm_title=title))
        self.assertEqual(argv, ["notify-send", title])

    def test_whole_argument_placeholder_is_an_empty_argument(self):
        # Подстановка без значения — пустая строка, а не выброшенный аргумент:
        # иначе чужая команда получила бы сдвинутые позиционные параметры.
        argv = za.expand_command("my-cmd {zone} {window}", za.ActionContext())
        self.assertEqual(argv, ["my-cmd", "", ""])

    def test_unknown_placeholder_is_action_error(self):
        with self.assertRaises(za.ActionError) as caught:
            za.expand_command("my-cmd {wndow}", self.context())
        self.assertIn("{wndow}", str(caught.exception))
        self.assertIn("{window}", str(caught.exception))

    def test_cyrillic_braces_are_not_placeholders(self):
        # `_PLACEHOLDER_RE` знает только `[a-z_]+`, поэтому `{зона}` проходит
        # в команду дословно: ни подстановки, ни ошибки. Так ядро устроено
        # сейчас (докстринг говорит «неизвестное `{что-то}` — ActionError»);
        # тест фиксирует фактическое поведение и сторожит его изменение.
        self.assertEqual(
            za.expand_command("echo {зона}", self.context()), ["echo", "{зона}"]
        )

    def test_empty_command_is_action_error(self):
        for text in ("", "   ", None):
            with self.assertRaises(za.ActionError):
                za.expand_command(text, self.context())

    def test_broken_quotes_are_action_error(self):
        with self.assertRaises(za.ActionError) as caught:
            za.expand_command('my-cmd "не закрыто', self.context())
        self.assertIn("не разобрать", str(caught.exception))

    def test_non_string_command_is_handled_like_text(self):
        self.assertEqual(za.expand_command(" {zone} ", self.context()), ["main"])


class BuiltinNameTest(unittest.TestCase):
    """`builtin:<имя>` против внешней команды."""

    def test_external_command_is_not_builtin(self):
        for run in ("notify-send привет", "x-terminal-emulator", "", None, "my-builtin:x"):
            self.assertFalse(za.is_builtin(run), run)
            self.assertIsNone(za.builtin_name(run), run)

    def test_builtin_prefix_is_case_and_space_insensitive(self):
        for run in ("builtin:mirror", "BUILTIN:Mirror", "  builtin:mirror  "):
            self.assertTrue(za.is_builtin(run), run)
            self.assertEqual(za.builtin_name(run), "mirror", run)

    def test_every_declared_builtin_is_recognized(self):
        for name in za.BUILTINS:
            self.assertEqual(za.builtin_name("builtin:" + name), name)

    def test_unknown_builtin_is_action_error_with_the_list(self):
        with self.assertRaises(za.ActionError) as caught:
            za.builtin_name("builtin:mirror2")
        message = str(caught.exception)
        self.assertIn("builtin:mirror2", message)
        for name in za.BUILTINS:
            self.assertIn("builtin:" + name, message)

    def test_empty_builtin_name_is_action_error(self):
        # `builtin:` — та же опечатка, что `builtin:нетакого`: имени нет.
        with self.assertRaises(za.ActionError):
            za.builtin_name("builtin:")
        self.assertTrue(za.is_builtin("builtin:"))


class MirrorFractionsTest(unittest.TestCase):
    """Зеркальная рамка в долях: горизонталь, вертикаль, ось по умолчанию."""

    def test_horizontal_is_left_right(self):
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 0.5, 1.0)), (0.5, 0.0, 0.5, 1.0)
        )
        self.assertEqual(
            za.mirror_fractions((0.25, 0.1, 0.25, 0.5), "horizontal"),
            (0.5, 0.1, 0.25, 0.5),
        )

    def test_vertical_is_top_bottom(self):
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 1.0, 0.5), "vertical"), (0.0, 0.5, 1.0, 0.5)
        )
        self.assertEqual(
            za.mirror_fractions((0.1, 0.2, 0.3, 0.3), "vertical"), (0.1, 0.5, 0.3, 0.3)
        )

    def test_default_axis_is_horizontal(self):
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 0.5, 1.0)),
            za.mirror_fractions((0.0, 0.0, 0.5, 1.0), za.DEFAULT_MIRROR_AXIS),
        )
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 1.0, 0.4)), (0.0, 0.0, 1.0, 0.4)
        )

    def test_axis_case_and_none_are_tolerated(self):
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 1.0, 0.5), "Vertical"), (0.0, 0.5, 1.0, 0.5)
        )
        self.assertEqual(
            za.mirror_fractions((0.0, 0.0, 1.0, 0.5), None), (0.0, 0.0, 1.0, 0.5)
        )

    def test_unknown_axis_is_action_error(self):
        with self.assertRaises(za.ActionError) as caught:
            za.mirror_fractions((0.0, 0.0, 1.0, 1.0), "diagonal")
        self.assertIn("diagonal", str(caught.exception))
        self.assertIn("horizontal", str(caught.exception))

    def test_full_screen_zone_is_its_own_mirror(self):
        self.assertEqual(za.mirror_fractions((0.0, 0.0, 1.0, 1.0)), (0.0, 0.0, 1.0, 1.0))

    def test_arguments_are_accepted_as_strings(self):
        self.assertEqual(
            za.mirror_fractions(("0", "0", "0.5", "1")), (0.5, 0.0, 0.5, 1.0)
        )


class FindMirrorTest(unittest.TestCase):
    """Зеркало по геометрии: площадь пересечения, близость центров, порядок зон."""

    # Реальная несимметричная сетка: главная зона слева, две мелкие справа.
    # Зеркало `main` — (0.1642, 0, 0.8358, 1): с `top-right` пересечение
    # 0.1642*0.5, с `bottom` — 0.2871*0.1932, то есть `top-right` выигрывает
    # по площади, хотя обе зоны попадают в зеркальную рамку.
    REAL = {
        "main": (0.0, 0.0, 0.8358, 1.0),
        "top-right": (0.8358, 0.0, 0.1642, 0.5),
        "bottom": (0.7129, 0.8068, 0.2871, 0.1932),
    }

    def test_exact_pair_finds_each_other(self):
        zones = {"left": (0.0, 0.0, 0.5, 1.0), "right": (0.5, 0.0, 0.5, 1.0)}
        self.assertEqual(za.find_mirror(zones, "left"), "right")
        self.assertEqual(za.find_mirror(zones, "right"), "left")

    def test_real_grid_overlap_picks_the_bigger_one(self):
        self.assertEqual(za.find_mirror(self.REAL, "main"), "top-right")
        self.assertEqual(za.find_mirror(self.REAL, "top-right"), "main")

    def test_equal_area_tie_is_broken_by_center_distance(self):
        # `s` совпадает со своей зеркальной рамкой (0.4, 0, 0.2, 1). Оба
        # кандидата перекрывают её на 0.1, но центр `near` ближе к центру
        # зеркала (0.15 против 0.25), и `far` записан первым: близость важнее
        # порядка словаря.
        zones = {
            "s": (0.4, 0.0, 0.2, 1.0),
            "far": (0.4, 0.0, 0.2, 0.5),
            "near": (0.2, 0.4, 0.6, 0.5),
        }
        self.assertEqual(za.find_mirror(zones, "s"), "near")

    def test_full_tie_is_resolved_by_dict_order(self):
        zones = {
            "s": (0.4, 0.0, 0.2, 1.0),
            "first": (0.4, 0.0, 0.2, 0.5),
            "second": (0.4, 0.0, 0.2, 0.5),
        }
        self.assertEqual(za.find_mirror(zones, "s"), "first")
        # Тот же словарь, собранный в другом порядке, даёт другой ответ —
        # и это единственное, что решает: площадь и центры равны.
        reversed_zones = {"s": zones["s"], "second": zones["second"], "first": zones["first"]}
        self.assertEqual(za.find_mirror(reversed_zones, "s"), "second")

    def test_no_intersection_is_none(self):
        zones = {"left": (0.0, 0.0, 0.2, 1.0), "middle": (0.4, 0.0, 0.2, 1.0)}
        self.assertIsNone(za.find_mirror(zones, "left"))

    def test_subject_is_never_its_own_mirror(self):
        self.assertIsNone(za.find_mirror({"all": (0.0, 0.0, 1.0, 1.0)}, "all"))
        zones = {"all": (0.0, 0.0, 1.0, 1.0), "side": (0.0, 0.0, 0.1, 1.0)}
        self.assertEqual(za.find_mirror(zones, "all"), "side")

    def test_unknown_subject_is_action_error(self):
        with self.assertRaises(za.ActionError) as caught:
            za.find_mirror({"left": (0.0, 0.0, 1.0, 1.0)}, "нетакой")
        self.assertIn("нетакой", str(caught.exception))

    def test_unknown_axis_is_action_error(self):
        with self.assertRaises(za.ActionError):
            za.find_mirror({"a": (0.0, 0.0, 1.0, 1.0)}, "a", "diagonal")

    def test_vertical_axis_is_honoured(self):
        # По горизонтали у `a` зеркала нет (полоса во всю ширину и две зоны
        # снизу), по вертикали зеркало — полоса снизу; равные площади двух
        # нижних зон разрешает порядок словаря.
        zones = {
            "a": (0.0, 0.0, 1.0, 0.4),
            "b": (0.0, 0.6, 0.5, 0.4),
            "c": (0.5, 0.6, 0.5, 0.4),
        }
        self.assertIsNone(za.find_mirror(zones, "a"))
        self.assertEqual(za.find_mirror(zones, "a", "vertical"), "b")


class ZoneWindowsTest(unittest.TestCase):
    """Окна, стоящие в зоне: рамка зоны, рамка вкладки, допуск, порядок."""

    ZONE = (100.0, 50.0, 800.0, 600.0)

    def setUp(self):
        self.tab = zone_tabs.stack_rect(self.ZONE, 2)

    def windows(self, frames):
        return za.zone_windows(frames, self.ZONE, self.tab)

    def test_window_on_the_zone_frame(self):
        self.assertEqual(self.windows({11: self.ZONE}), [11])

    def test_window_on_the_shared_tab_frame(self):
        self.assertEqual(self.windows({12: self.tab}), [12])
        self.assertEqual(self.windows({12: self.tab, 13: self.tab}), [12, 13])

    def test_two_windows_one_in_zone_and_one_in_tabs(self):
        self.assertEqual(self.windows({11: self.ZONE, 12: self.tab}), [11, 12])

    def test_unread_frame_is_skipped(self):
        self.assertEqual(self.windows({11: None, 12: self.tab}), [12])
        self.assertEqual(self.windows({11: None}), [])

    def test_tolerance_accepts_a_small_shift(self):
        # Рамку ставит оконный менеджер: точного равенства float недостаточно.
        shift = float(zone_tabs.FRAME_TOLERANCE)
        shifted = tuple(v + shift for v in self.ZONE)
        self.assertEqual(self.windows({11: shifted}), [11])

    def test_shift_beyond_tolerance_does_not_match(self):
        shift = float(zone_tabs.FRAME_TOLERANCE) + 1.0
        shifted = tuple(v + shift for v in self.ZONE)
        self.assertEqual(self.windows({11: shifted}), [])

    def test_window_of_another_zone_is_not_here(self):
        other = (0.0, 0.0, 100.0, 100.0)
        self.assertEqual(self.windows({11: other}), [])

    def test_order_of_the_frames_dict_is_kept(self):
        frames = {22: self.ZONE, 11: self.ZONE}
        self.assertEqual(self.windows(frames), [22, 11])


class ZoneOfFrameTest(unittest.TestCase):
    """Зона окна по его рамке: зона, вкладка, узкая зона без полосы."""

    ZONES = {
        "main": (0.0, 0.0, 800.0, 600.0),
        "side": (800.0, 0.0, 400.0, 600.0),
    }

    def test_window_on_the_zone_frame(self):
        self.assertEqual(za.zone_of_frame(self.ZONES["main"], self.ZONES), "main")
        self.assertEqual(za.zone_of_frame(self.ZONES["side"], self.ZONES), "side")

    def test_window_on_the_tab_frame(self):
        tab = zone_tabs.stack_rect(self.ZONES["main"], 2)
        self.assertEqual(za.zone_of_frame(tab, self.ZONES), "main")

    def test_tab_height_comes_from_the_tabs_section(self):
        zone = self.ZONES["main"]
        tab = zone_tabs.stack_rect(zone, 2, 40)
        self.assertEqual(za.zone_of_frame(tab, {"main": zone}, height=40), "main")
        # С умолчанием полосы та же рамка уже не совпадает.
        self.assertIsNone(za.zone_of_frame(tab, {"main": zone}))

    def test_window_outside_zones_is_none(self):
        self.assertIsNone(za.zone_of_frame((0.0, 0.0, 10.0, 10.0), self.ZONES))

    def test_unread_frame_is_none(self):
        self.assertIsNone(za.zone_of_frame(None, self.ZONES))

    def test_narrow_zone_without_strip_still_owns_its_window(self):
        # Полоса не влезает в узкую зону (`MIN_WINDOW_H` + высота полосы), и
        # окна стоят на рамке самой зоны. Это всё ещё окна этой зоны.
        zone = (0.0, 0.0, 400.0, 50.0)
        self.assertIsNone(zone_tabs.strip_rect(zone), "полоса не должна влезать")
        self.assertEqual(zone_tabs.stack_rect(zone, 2), zone)
        self.assertEqual(za.zone_of_frame(zone, {"narrow": zone}), "narrow")

    def test_first_zone_in_grid_order_wins(self):
        # Зоны не перекрываются, но если рамка подходит двум (одинаковые зоны
        # в словаре), ответ должен быть один и тот же — первая по порядку.
        zones = {"first": (0.0, 0.0, 800.0, 600.0), "second": (0.0, 0.0, 800.0, 600.0)}
        self.assertEqual(za.zone_of_frame((0.0, 0.0, 800.0, 600.0), zones), "first")


if __name__ == "__main__":
    unittest.main()
