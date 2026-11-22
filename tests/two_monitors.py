#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Два виртуальных монитора для дымовых сценариев — общий helper.

Xvfb на один экран даёт ровно один выход (`screen`), сколько бы `-screen` ни
передать: каждый `-screen` — это отдельный X-экран, а не второй выход. Отдельный
процесс с `XRRSetMonitor` мониторы создаёт и держит соединение живым — пока он
работает, у X-сервера два монитора, и muffin (Mutter) видит оба.

Аргументы: ``<файл-список> <ширина> <высота> ИМЯ:X [ИМЯ:X ...]``. Последнему
монитору достаётся настоящий выход Xvfb: если отдать его первому, GDK покажет
один монитор. В файл пишутся строки ``ИМЯ X 0 ШИРИНА ВЫСОТА``, чтобы сценарий не
разбирал вывод xrandr регулярками.

Сценарии, которым он нужен: ``tests/smoke-monitors.sh`` (зоны на монитор) и
``tests/smoke-monitor-profiles.sh`` (профиль на экран).
"""

import ctypes, ctypes.util, os, sys, time


class XRRMonitorInfo(ctypes.Structure):
    """struct _XRRMonitorInfo из <X11/extensions/Xrandr.h>."""

    _fields_ = [
        ("name", ctypes.c_ulong),
        ("primary", ctypes.c_int),
        ("automatic", ctypes.c_int),
        ("noutput", ctypes.c_int),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("mwidth", ctypes.c_int),
        ("mheight", ctypes.c_int),
        ("outputs", ctypes.POINTER(ctypes.c_ulong)),
    ]


class XRRScreenResources(ctypes.Structure):
    """struct _XRRScreenResources (нужен только список выходов)."""

    _fields_ = [
        ("timestamp", ctypes.c_ulong),
        ("configTimestamp", ctypes.c_ulong),
        ("ncrtc", ctypes.c_int),
        ("crtcs", ctypes.POINTER(ctypes.c_ulong)),
        ("noutput", ctypes.c_int),
        ("outputs", ctypes.POINTER(ctypes.c_ulong)),
        ("nmode", ctypes.c_int),
        ("modes", ctypes.c_void_p),
    ]


def fail(text):
    print(text, file=sys.stderr)
    sys.exit(1)


x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
xrr = ctypes.CDLL(ctypes.util.find_library("Xrandr"))

x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XInternAtom.restype = ctypes.c_ulong
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
xrr.XRRGetScreenResourcesCurrent.restype = ctypes.POINTER(XRRScreenResources)
xrr.XRRGetScreenResourcesCurrent.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
xrr.XRRSetMonitor.restype = None
xrr.XRRSetMonitor.argtypes = [
    ctypes.c_void_p,
    ctypes.c_ulong,
    ctypes.POINTER(XRRMonitorInfo),
]

# Аргументы: <файл-список> <ширина> <высота> ИМЯ:X [ИМЯ:X ...]
# Последнему монитору достаётся настоящий выход Xvfb (первому нельзя: тогда
# GDK показывает один монитор).
if len(sys.argv) < 5:
    fail("helper: нужны <файл> <ширина> <высота> ИМЯ:X ...")
out_path, width, height = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
all_specs = sys.argv[4:]

dpy = x11.XOpenDisplay(None)
if not dpy:
    fail("helper: не удалось открыть DISPLAY")
root = x11.XDefaultRootWindow(dpy)
res = xrr.XRRGetScreenResourcesCurrent(dpy, root)
outputs = [res.contents.outputs[i] for i in range(res.contents.noutput)]
if not outputs:
    fail("helper: у X-сервера нет ни одного выхода")

keep = []  # массивы выходов должны жить до конца работы
lines = []
for index, spec in enumerate(all_specs):
    name, _, xpos = spec.partition(":")
    atom = x11.XInternAtom(dpy, name.encode(), 0)
    mon = XRRMonitorInfo()
    mon.name = atom
    mon.primary = 0
    mon.automatic = 0
    mon.x = int(xpos)
    mon.y = 0
    mon.width = width
    mon.height = height
    mon.mwidth = width // 4
    mon.mheight = height // 4
    if index == len(all_specs) - 1:
        outputs_arr = (ctypes.c_ulong * len(outputs))(*outputs)
        keep.append(outputs_arr)
        mon.noutput = len(outputs)
        mon.outputs = outputs_arr
    xrr.XRRSetMonitor(dpy, root, ctypes.byref(mon))
    x11.XSync(dpy, 0)
    lines.append("%s %d %d %d %d" % (name, int(xpos), 0, width, height))
    print("helper: монитор %s: %d,%d %dx%d (выход: %s)"
          % (name, int(xpos), 0, width, height,
             "да" if index == len(all_specs) - 1 else "нет"))
    sys.stdout.flush()

with open(out_path + ".tmp", "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
os.replace(out_path + ".tmp", out_path)
print("helper: готово, держу соединение")
sys.stdout.flush()
# Соединение живёт до конца теста: с ним живут и мониторы.
while True:
    time.sleep(3600)
