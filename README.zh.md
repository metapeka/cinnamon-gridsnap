# Gridsnap —— Cinnamon 的窗口网格：区域编辑器与快捷键

[English](README.md) · [**简体中文**](README.zh.md) · [Русский](README.ru.md)

本译文由机器生成，尚未经母语者校对。

已在 Debian 13、Cinnamon 6.4.10（Muffin、X11）、1920×1040 工作区上验证。

![Gridsnap 图标](icons/gridsnap-256.png)

![区域编辑器](site/assets/editor.png)

![区域编辑器运行中](site/assets/demo.gif)

<!-- section: languages -->
## 界面语言

界面支持**英语**、**简体中文**和**俄语**。源码中的字符串是俄语——这就是源语言，而英语是所有其他区域的默认语言：

- `ru*`（`ru`、`ru_RU.UTF-8`）——俄语，即源码字符串本身，不需要翻译文件；
- `zh*`（`zh_CN`、`zh_CN.UTF-8`、`zh-Hans`）——简体中文；
- 其他所有情况（`en`、`de`、`C`、未设置）——英语。

语言依次取自 `GRIDSNAP_LANG`、`LC_ALL`、`LC_MESSAGES`、`LANG`，因此可以只为一条命令切换：

```bash
GRIDSNAP_LANG=en gridsnap-tiler list
```

从代码仓库直接运行无需构建：没有编译好的翻译文件时，工具会直接读取仓库里的 `po/<语言>.po`
（`make mo` 负责编译，安装脚本把编译好的 `.mo` 放进 `share/locale`）。Cinnamon 小程序按 xlet
的惯例跟随会话区域设置，不看 `GRIDSNAP_LANG`。译文由机器生成，尚未经母语者校对。
man 手册页遵循同样的规则：`man gridsnap` 按区域设置挑选页面 —— 默认英语，`ru*` 下俄语，
`zh*` 下中文；英语和中文页面同样是机器翻译。

<!-- section: install -->
## 安装

两条互相独立的路径：**自己用**（从这个目录装，不经过软件包管理器）和**以软件包安装**
（Debian/Ubuntu 用 `.deb`，Arch 用 AUR 配方；发布版 `.tar.gz` 适用于任何系统）。
两条路装的是同一份代码；版本号取自 `zone-tiler.py`/`zone-daemon.py` 里的 `VERSION`。

<!-- section: install-self -->
### 自己用（从仓库安装）

除了 `python3`、PyGObject 和 `libX11`（Debian 里已经装好）之外不需要别的东西：

```bash
./install-launcher.sh             # 菜单里的 Gridsnap 快捷方式 + 图标
./install-launcher.sh --desktop   # 再加一个桌面快捷方式
./install-daemon.sh               # 自动启动自动布局守护进程（--list / --remove）
python3 setup-hotkeys.py          # 在 Cinnamon 中注册快捷键
```

快捷方式里存的是 `zone-editor.py` 的绝对路径；目录搬家之后请重新运行
`./install-launcher.sh`。作者自己机器上用的就是这条路径。

<!-- section: install-package -->
### 以软件包安装

**Debian 官方软件包。** 仓库里有 `debian/`（debhelper、compat 13，另外用 `dh-exec`
重命名图标）；构建依赖只来自发行版的 `main`：

```bash
sudo apt install debhelper dh-exec    # 一次性的构建依赖
dpkg-buildpackage -b -us -uc          # 生成 ../gridsnap_<版本>-1_all.deb
sudo apt install ../gridsnap_<版本>-1_all.deb
```

**通过 `Makefile`**（不需要 debhelper）：需要 `make` 和 `dpkg` 自带的 `dpkg-deb`。

```bash
make tarball      # dist/gridsnap-<版本>.tar.gz
make deb          # dist/gridsnap_<版本>_all.deb
make aur          # dist/PKGBUILD 里的 AUR 配方
```

两条路装的东西一样：代码进 `/usr/lib/gridsnap`，命令 `gridsnap-tiler`、`gridsnap-daemon`、
`gridsnap-editor`、`gridsnap-picker`、`gridsnap-hotcorners`、`gridsnap-ctl`、`setup-hotkeys` 进
`/usr/bin`，快捷方式和图标进 `/usr/share`。安装 `make deb` 的产物：

```bash
sudo apt install ./dist/gridsnap_<版本>_all.deb
```

`tar.gz` 由 `install.sh` 装进前缀（默认 `$HOME/.local`）：

```bash
tar -xzf gridsnap-<版本>.tar.gz && cd gridsnap-<版本>
./install.sh                    # ~/.local：代码、命令、菜单快捷方式、图标
./install.sh --prefix /usr/local
./install.sh --no-launcher      # 只装代码和命令，不带快捷方式和图标
./install.sh --uninstall        # 卸掉从这个前缀装进去的东西
```

AUR 配方（`PKGBUILD`）是给 Arch 的：把它放进软件包目录，用 `makepkg -si` 构建。

两种安装方式都先在 `~/.config/gridsnap/zones.conf` 找配置，没有才用代码旁边的那份。
系统软件包的代码目录是只读的，所以用 `deb` 装的时候想改网格，得把配置复制到 XDG 下：

```bash
mkdir -p ~/.config/gridsnap && cp /usr/lib/gridsnap/zones.conf ~/.config/gridsnap/
```

> 「自己用」和「以软件包安装」互不干扰，但也不会互相同步：`install-launcher.sh`
> 和 `install-daemon.sh` 操作的是仓库目录，而软件包用它自己的 `/usr` 路径。

<!-- section: install-help -->
### 参考

man 手册页包含在软件包里：英语在 `/usr/share/man/man1`（任何没有对应译文的区域设置都会拿到它），
俄语在 `/usr/share/man/ru/man1`，中文在 `/usr/share/man/zh_CN/man1`；`install.sh` 把同样这三份装到
`前缀/share/man` —— 加 `--no-launcher` 时也照样装：这是文档，不是快捷方式的一部分。
`man gridsnap` —— 软件包的命令、`zones.conf`、相关文件和返回码；`man gridsnap-tiler` ——
同一页（`man gridsnap-daemon`、`man gridsnap-editor`、`man gridsnap-picker`、
`man gridsnap-hotcorners`、`man gridsnap-ctl`、`man setup-hotkeys` 也一样）。

<!-- section: cinnamon-native -->
## 1. Cinnamon 开箱即有的功能

Cinnamon **没有**内置的网格编辑器（类似 Windows 的 FancyZones）。只有一套
固定的分屏：屏幕的一半和四分之一。

```bash
cinnamon-settings tiling     # 系统设置 →「窗口平铺」
cinnamon-settings keyboard   # 系统设置 → 键盘 → 快捷键 →「平铺」分组
```

| 操作 | 方式 |
|---|---|
| 半个屏幕 | `Super+←/→/↑/↓`，或把窗口拖到边缘 |
| 四分之一屏幕 | `Super+←`，再按 `Super+↑/↓`；或者拖到角落 |
| 最大化 / 还原 | `Alt+F10` / `Alt+F5` |

平铺设置里只有两个开关（启用平铺/贴边吸附，以及「在顶端最大化而不是平铺」）—— 在那里没法定义自己的网格。

<!-- section: gtile -->
## 2. 现成的「选格子」网格：gTile 扩展

`系统设置 → 扩展 →「下载」标签页 → gTile`。操作：`Super+G` 打开网格，
方向键移动选择，`Shift`+方向键扩展成多个格子，`空格` 把窗口放进去，
`1..4` 切换网格组，`Alt`+方向键换到另一台显示器
（[gTile 页面](https://cinnamon-spices.linuxmint.com/extensions/view/76)）。

<!-- section: zones -->
## 3. 自己的区域：可视化编辑器与窗口布局器

这个目录里有三个工具文件：

* `zone-editor.py` —— **可视化网格编辑器**（窗口里画出屏幕，区域用鼠标拖动）；
* `zone-tiler.py` —— 布局器：把活动窗口按名字放进区域；
* `zone-picker.py` —— 按 `Super+Z` **可视化选择区域**（覆盖层、方向键、`Enter`）。

依赖：`python3`、`python3-gi`（GTK 3）、`libX11`、`xprop` —— Debian 里已经装好。
重绘图标、以及给区域选择覆盖层拍快照，需要 `python3-cairo`。

<!-- section: zones-run -->
### 3.1. 启动编辑器

菜单里的图标：**Gridsnap**（收藏和系统设置/附件）。快捷方式用这条命令安装：

```bash
./install-launcher.sh             # 菜单里的快捷方式
./install-launcher.sh --desktop   # 再加一个桌面快捷方式
./install-launcher.sh --remove    # 全部删掉
```

或者直接从终端启动：

```bash
python3 zone-editor.py             # 普通启动
python3 zone-editor.py --conf /路径/zones.conf
python3 zone-editor.py --shot ui.png   # 不显示窗口，只截一张界面图
```

如果目录搬到了别的地方，请重新运行 `./install-launcher.sh`，快捷方式里存的是绝对路径。

<!-- section: zones-editor -->
### 3.2. 编辑器能做什么

| 操作 | 方式 |
|---|---|
| 选中区域 | 点击区域，或点击右侧列表里的一行 |
| 移动 | 用鼠标拖动区域 |
| 改变大小 | 拖边缘或角（光标会变） |
| 新建区域 | 在空白处双击，或用「+」按钮 |
| 删除 | `Delete` 或「−」按钮 |
| 复制 / 清空 | 工具栏上的按钮 |
| 模板 | 「模板」按钮：两列、1+2、2×2、三列、主区域 + 堆叠、空白画布 |
| 自己的方案 | 「方案」按钮：把方案载入画布、「把当前布局保存为方案……」、删除 |
| 精确微调 | 方向键平移，`Ctrl`+方向键改大小，滚轮改宽度 |
| 吸附 | 吸附到屏幕边缘、相邻区域边缘，以及 1/4、1/3、1/2、2/3、3/4 线 |
| 区域的名字和按键 | 右侧面板里的「名字」和「按键」字段 |
| 保存 | 「保存」按钮或 `Ctrl+S` |
| 应用按键 | 「快捷键」按钮 → 更新 Cinnamon 的快捷键 |
| 试一下 | 「▶」按钮 —— 把活动窗口放进选中的区域 |

尺寸按工作区的比例保存，所以换分辨率时网格不会坏，而且会把任务栏算进去。

<!-- section: zones-conf -->
### 3.3. `zones.conf` 格式

```ini
[grid]
cols = 1.0
rows = 1.0

[zones]
main      = 0, 0, 0.5, 1        # x, y, w, h 用比例表示（0..1）
top-right = 0.5, 0, 0.25, 0.5
bottom    = 0.5, 0.5, 0.5, 0.5

[keys]
main      = <Super><Alt>1
top-right = <Super><Alt>2
```

如果有多台屏幕，每台显示器可以有自己的区域集合 —— 用 `[zones:<输出名>]` 段
（输出名取自 `xrandr --listmonitors`：`eDP-1`、`HDMI-1`）。没有单独段的显示器
使用公共的 `[zones]`，所以旧文件在任意数量的屏幕上都能继续用：

```ini
[zones]              # 默认区域集合
main = 0, 0, 0.5, 1
side = 0.5, 0, 0.5, 1

[zones:HDMI-1]       # 竖屏：自己的网格
top    = 0, 0, 1, 0.5
bottom = 0, 0.5, 1, 0.5
```

`--monitor` 既接受输出名也接受编号（`--monitor HDMI-1`、`--monitor 1`）；已断开显示器的段
会留在文件里，等它重新接上。

工作桌面则通过方案拿到各自的网格 —— `[workspaces]` 段把桌面编号
（和 Cinnamon 界面里一样，从 1 开始）和 `templates/` 里的方案对应起来：

```ini
[workspaces]
1 = work
2 = cinema
```

`zone-tiler.py apply main --workspace 1` 按第二个桌面的网格放窗口，`--workspace conf` 则总是
用公共的 `zones.conf`（编辑器就是这么做的），不带这个标志时用当前活动的桌面。

旧的「区域即网格格子区间」写法也支持：
`cols = 0.5, 0.75, 1.0`、`rows = 0.5, 1.0`、`main = 0:1, 0:2`。保存时编辑器
会把这些全部换算成 `x,y,w,h` 比例。

自动布局由 `[rules]` 段（选择器 → 区域）定义，而 GridSnap 不该动的窗口用
`[ignore]` 段（选择器写法相同）定义；两段都在第 5 节说明。

标签条的高度和区域内切换窗口的按键由可选的 `[tabs]` 段给出
（功能本身见 5.2 节）：

```ini
[tabs]
height = 22          # 标签条高度，px（默认 22）
key = <Super><Alt>g  # 「区域里的下一个窗口」，和别的快捷键一样注册
```

文件先找 `~/.config/gridsnap/zones.conf`（会考虑 `$XDG_CONFIG_HOME`），如果那里没有，
再用代码旁边的那份，即 `<仓库目录>/zones.conf`。这样现有安装不会坏，网格也能挪出仓库：

```bash
mkdir -p ~/.config/gridsnap && cp zones.conf ~/.config/gridsnap/
```

路径随时可以用 `--conf` 标志显式指定。布局方案放在 `zones.conf` 旁边的
`templates/` 子目录里（默认 `~/.config/gridsnap/templates/`）。

<!-- section: tiler -->
## 4. 从终端使用布局器

```bash
python3 zone-tiler.py list                  # 区域及其像素范围
python3 zone-tiler.py apply main --dry-run  # 计算范围
python3 zone-tiler.py apply main --verify   # 放入活动窗口并核对几何
python3 zone-tiler.py apply bottom --monitor 1
python3 zone-tiler.py arrange               # 把窗口分到各区域（规则 + 空闲区域）
python3 zone-tiler.py arrange --by order    # 按窗口列表顺序依次放进区域
python3 zone-tiler.py stack main            # 把 main 区域的窗口收成标签页
python3 zone-tiler.py tabs-next             # 切到活动窗口所在区域的下一个标签页
python3 zone-tiler.py undo                  # 恢复上一次布局前的窗口
python3 zone-tiler.py undo --all            # 回滚整个布局历史
python3 zone-tiler.py focus main            # 「只留这个区域」模式：窗口进 main，其余最小化
python3 zone-tiler.py focus --off           # 退出该模式（见 4.2）
python3 zone-tiler.py profile list          # 自己的布局方案
python3 zone-tiler.py profile save work     # 把当前 zones.conf 存成名为 work 的方案
python3 zone-tiler.py profile use work      # 启用方案并更新快捷键
python3 zone-tiler.py profile use work --no-hotkeys   # 只启用，不动快捷键
python3 zone-tiler.py profile show work      # 方案里有什么
python3 zone-tiler.py profile rm work       # 删除方案
python3 zone-tiler.py profile use-monitors  # 按各自的方案布置各屏幕（[monitors]）
python3 zone-tiler.py --version             # 布局器版本
```

`arrange` 取 `_NET_CLIENT_LIST` 里的客户窗口，把它们放进各区域：`--by auto`（默认）——
先按 `[rules]` 规则，其余按空闲区域；`--by rules` —— 只按规则；
`--by order` —— 按窗口顺序依次放进区域。没有 `WM_CLASS` 的窗口、工具类窗口（对话框、splash）
以及被 `[ignore]` 命中的窗口都不动。同一个区域里的多个窗口不再级联错位：它们以**标签页**
的形式站在同一个范围上 —— 一个完整可见，其余等切换（5.2 节），—— 而
`stack <区域>` 用同样的标签页把多个窗口收进一个区域。`--offset` 标志和级联都没有了：
带 `--offset` 调用会得到参数错误（返回码 2），而不是被悄悄忽略。

每次布局（`apply`、`arrange`、`stack`）都会写入历史；`undo` 恢复原来的范围
（`--all` —— 一次回滚整个历史）。历史放在 `~/.local/state/gridsnap/history.json`
（会考虑 `$XDG_STATE_HOME`），保存最近 20 次操作，重启后仍然存在。如果窗口
已经关掉，或者它的 XID 被别的程序复用，该条目会被跳过。守护进程运行时
`arrange`/`undo` 不做协调 —— 请先停掉守护进程。

返回码：`0` —— 成功，`1` —— 执行错误（没有 X、没有活动窗口），
`2` —— 参数错误（未知区域、方案名不合法），`3` —— 配置错误（方案缺失/损坏）。

**布局方案**是一整套：`[zones]` 段（+ `[keys]` 里的绑定）、`[rules]` 和 `[ignore]`。
`[grid]` 和 `[daemon]` 段不属于方案。方案放在 `~/.config/gridsnap/templates/<名字>.conf`
（会考虑 `$XDG_CONFIG_HOME`；用 `--conf PATH` 时是那个文件旁边的 `templates` 目录），格式
和 `zones.conf` 一样，可以直接读、直接拷。`profile save` 取当前的 `zones.conf`，
`profile use` 把方案里的区域/规则/例外/按键搬到活动的 `zones.conf` 里，不动
`[grid]`、`[daemon]` 和其他段。`use` 之后会更新快捷键（`setup-hotkeys.py --conf …`）；
`--no-hotkeys` 关掉这一步。JSON 导出/导入（`profile export`/`import`）搬的是同一套东西。
细节见 `docs/specs/zone-tiler.md`。

它通过 muffin 的 EWMH 消息工作：取消最大化和全屏，补偿窗口边框
（`_NET_FRAME_EXTENTS`），所以区域和窗口边框能像素级一致。

<!-- section: tiler-monitors -->
### 4.1. 按屏幕的方案：「2 号显示器 —— 观影」（`[monitors]`）

每台屏幕都可以有自己的方案 —— 于是右边的显示器用观影布局，左边用工作布局：

```ini
[monitors]
HDMI-1 = work       # 输出名和 xrandr --listmonitors 里一样
HDMI-2 = cinema
```

- **区域在窗口所在的那台屏幕的方案里找。** 不带 `--monitor` 的 `apply`、`move`、`stack` 和
  `arrange` 都在窗口所在的屏幕上工作（`move` 一直如此），而守护进程按窗口所在屏幕的方案里的
  规则和区域放窗口；屏幕优先于 `[workspaces]` 里的桌面，桌面优先于公共的 `zones.conf`。以前
  不带标志的 `apply`/`stack` 总是针对第一个显示器 —— 有两台屏幕时窗口会跑到左边去。
- **启用这套布局** —— 一条命令：`zone-tiler.py profile use-monitors`。它组装出活动的
  `zones.conf`：每台屏幕的区域来自它自己的方案，而 `[rules]`/`[ignore]`/`[keys]` 合并
  （有冲突时由第一个屏幕的方案决定，命令会打印出它覆盖了什么）。`--dry-run` 显示将要写入的内容，
  `--no-hotkeys` 不动快捷键。
- **登录会话时**由自启动来应用绑定（`install-daemon.sh` 会放一个
  `gridsnap-monitors.desktop`）；编辑器里用「屏幕方案……」按钮设置，它就在
  「桌面方案……」旁边。
- 屏幕的方案改过又不想再跑一次命令 —— 守护进程在
  `gridsnap-ctl reload` 之后会重新读取它，而选择器和编辑器本来就是自己读方案的。

<!-- section: tiler-focus -->
### 4.2. 「只留这个区域」模式（`focus`）

快速聚焦模式：窗口放进区域，而**同一个工作桌面和同一台显示器上的**其他窗口最小化。
其他屏幕不动 —— 第二个显示器上可能正在放电影。

```bash
python3 zone-tiler.py focus main            # 活动窗口进 main，其余最小化
python3 zone-tiler.py focus main --window 0x1a00007
python3 zone-tiler.py focus                 # 一样，但不移动窗口：「只留这个窗口」
python3 zone-tiler.py focus main            # 再按一次 —— 退出并还原被本模式最小化的窗口
python3 zone-tiler.py focus --off           # 显式退出（给脚本和第二个按键用）
python3 zone-tiler.py focus main --dry-run  # 看看会发生什么，什么都不做
```

- 进入和退出是**同一条命令**：再按一次 `focus` 只还原被本模式最小化的窗口；
  手动最小化的窗口仍然最小化，而被 `[ignore]` 命中的窗口和工具类窗口（对话框、
  splash）完全不会最小化 —— 目标程序的对话框能在进入模式后存活。
- 最小化的窗口仍在 `_NET_CLIENT_LIST` 里，几何也保留着，所以守护进程不会认为它
  离开了区域，也不会触发 reflow。
- 守护进程在运行时，这条命令把活儿交给它：只有守护进程才能管住**进入模式之后**
  才出现的窗口。该桌面的新窗口在模式激活时会立刻被最小化。没有守护进程时模式是
  一次性的（不跟踪新窗口），最小化窗口的名单放在
  `~/.local/state/gridsnap/focus.json`（会考虑 `$XDG_STATE_HOME`）。
- **模式活在自己的工作桌面上**，切换桌面不会取消它：那个桌面的窗口本来就看不见，
  回来时模式还在。在那个桌面上取消它反而是做不到的：muffin 会忽略非活动桌面上的
  还原请求（已验证），窗口就会留成没有主人的最小化状态。**退出**（`--off`
  或同一个按键）在任意桌面上都能用：命令自己切到模式所在桌面再还原窗口。
  只有**守护进程在运行时**，模式才会随目标窗口关闭而自行结束；停掉守护进程时
  （`Ctrl-C`、`gridsnap-ctl quit`），最小化的窗口会被还原，如果还原不了（窗口在
  别的桌面上）—— 名单连每个窗口的桌面一起交给 `focus.json`，由 `focus --off` 收尾。
- **目标区域的标签页仍然可见。** 本模式最小化其他区域的窗口，而目标区域自己的窗口 ——
  包括未激活的标签页 —— 一个都不动：否则它会把用户刚离开的那个区域的窗口藏起来。
  在模式运行期间进入该区域的窗口同样成为标签页并保持可见。
- 如果无法确定活动的工作桌面，返回码是 `1`；例外窗口不会进入模式（和 `apply` 一样）。

<!-- section: daemon -->
## 5. 守护进程：自动摆放新窗口
要让窗口自动进入区域（按规则），请启动守护进程：

```bash
python3 zone-daemon.py             # 一直监听事件直到 Ctrl-C（同时起 IPC 套接字）
python3 zone-daemon.py --verbose   # 显示它在做什么
python3 zone-daemon.py --once      # 处理一次窗口列表变化后退出
python3 zone-daemon.py --no-socket # 不要 IPC，只做自动布局
python3 zone-daemon.py --socket /路径/daemon.sock
python3 zone-daemon.py --version
```

守护进程盯着 EWMH 的 `_NET_CLIENT_LIST`，窗口一出现就按 `zones.conf` 里的规则放进去。
`[rules]` 段 ——「选择器 → 区域」：

守护进程既知道工作桌面也知道屏幕：网格按窗口的**屏幕**取，如果那台屏幕没有绑定 ——
就按窗口的桌面取（`[workspaces]` 见 3.3 节，`[monitors]` 见 4.1 节），占用情况按
「桌面 + 显示器」这一对来算，而 reflow 只在可见桌面上做 —— 不可见桌面上的窗口
不会被挪动。屏幕的方案守护进程会在 `gridsnap-ctl reload` 之后重新读取。

```ini
[rules]
Chrome = main                   # 对 WM_CLASS 做 glob，忽略大小写
TelegramDesktop = top-right
re:^(Navigator|firefox) = main  # 对 "类名|标题" 字符串做正则
```

第一个匹配胜出；规则指向不存在的区域是配置错误。

守护进程**不**碰什么，由两层决定。第一层是 `_NET_WM_WINDOW_TYPE` 里的工具类窗口
（`DIALOG`、`SPLASH`、`TOOLTIP`、`DROPDOWN_MENU`、`UTILITY`）—— 对弹窗的、关不掉的保护。
第二层是 `[ignore]` 段，选择器和 `[rules]` 一样；行右边是给人看、也写进守护进程日志的备注：

```ini
[ignore]
TelegramDesktop = Telegram 的对话框
re:^(Navigator|firefox).*(Library|Библиотека) = 库窗口
```

被排除的窗口，守护进程（`on_map`）不搬，`arrange`/`stack` 不搬，快捷键 `apply`/`move`
不搬，`undo` 不搬，选择器也不搬（`Enter` 会带提示地拒绝），拖拽 watcher
`zone-hotcorners` 也不动它（这种窗口不会出现 HUD）—— 到处用的是同一个检查
`window_excluded`。想把这样的窗口挪个位置 —— 请删掉或改掉那条规则。

用户自己把窗口移出区域的，守护进程不会把它放回去：它记住布局时的范围，
每次重新核对时都看到窗口已经离开。这能挺过守护进程重启（状态文件
`~/.local/state/gridsnap/placed.json`），窗口关掉再打开时清零。窗口在哪台显示器上
由守护进程自己判断 —— 按窗口中心 —— 并在该显示器的工作区内布局（它没有 `--monitor`
标志）；reflow 也只在同一台显示器内进行。

如果想在窗口关闭后让相邻的已占用区域扩张到空出来的面积（前提是两者合并后是
矩形），请打开 reflow：

```ini
[daemon]
reflow = neighbors   # off（默认）| neighbors
```

当空出来的区域又按规则进了窗口，扩张过的相邻区域会回到配置里的原始范围。

在 Cinnamon 会话里自启动：

```bash
./install-daemon.sh            # 启用自启动
./install-daemon.sh --list     # 看看装了什么
./install-daemon.sh --remove   # 关掉
```

脚本会写一个 `~/.config/autostart/gridsnap-daemon.desktop`，其中
`Exec=python3 <目录>/zone-daemon.py`。守护进程读同一个 `zones.conf`（先 XDG，再代码旁边）；
路径可以用 `--conf` 指定。

<!-- section: daemon-ipc -->
### 5.1. IPC：用 `gridsnap-ctl` 控制守护进程

守护进程运行时监听一个本地 UNIX 套接字（`$XDG_RUNTIME_DIR/gridsnap/daemon.sock`，权限
`0600`；守护进程和客户端都有 `--socket PATH` 标志，`--no-socket` —— 关闭 IPC）。单独的客户端
`gridsnap-ctl`（Python，无依赖）用 JSON 和它对话：

```bash
python3 gridsnap-ctl status                      # 区域、窗口→区域、显示器、reflow（JSON）
python3 gridsnap-ctl apply main                  # 把活动窗口放进 main 区域
python3 gridsnap-ctl apply main --window 0x1a00007
python3 gridsnap-ctl apply top-right --monitor 1
python3 gridsnap-ctl focus main                  # 「只留这个区域」模式（窗口进 main，其余最小化）
python3 gridsnap-ctl focus --off                 # 退出该模式
python3 gridsnap-ctl reload                      # 守护进程重新读取 zones.conf
python3 gridsnap-ctl quit                        # 停掉守护进程
```

协议是每条命令一行 JSON，回答也是 JSON（`{"ok": true, ...}` 或
`{"ok": false, "error": "..."}`）；套接字只在本机，没有 TCP。套接字出错（坏行、
断开）不会让守护进程崩溃 —— 它记日志并继续盯着窗口。`apply` 会写进守护进程的跟踪表，
因此之后关闭窗口会照常触发 reflow。软件包安装时客户端就是命令
`gridsnap-ctl`（`.deb` 和 `install.sh` 都会把它放到 `$PREFIX/bin`）。

<!-- section: daemon-tabs -->
### 5.2. 区域内的标签页：一个区域放多个窗口

一个区域里进了第二个窗口时，它们不会带偏移地叠在一起：区域持有
**标签页**。区域上方出现一条带各窗口标题的标签条（高度由 `[tabs] height` 决定），窗口
占据区域减去标签条的部分，标签页可以用鼠标点标签条切换，也可以用按键切换：

```bash
python3 zone-tiler.py tabs-next   # 把活动窗口所在区域的下一层窗口提到前面
```

- 窗口**自己**变成标签页 —— 一旦进入区域（`[rules]` 规则、`apply`、
  选择器、拖拽）。未激活的标签页原地不动，什么都不隐藏：切换只是把需要的窗口
  提到前面，所以在 Cinnamon 面板和 Alt+Tab 里能看到区域里的所有窗口。
- **两个窗口起才有标签条。** 区域里只有一个窗口时它占满整个区域；标签条从第二个窗口
  开始出现，而倒数第二个标签页离开后，剩下的那个又拿回整个区域。
- 标签条由单独的覆盖层 `zone-tabstrip.py` 绘制，由守护进程启动：它不抢焦点，
  也不妨碍抓住标题栏拖动窗口。窄区域里（标签条放不下）没有标签条，但
  标签页照常工作，可以用按键切换。
- 标签页只在守护进程运行时存在：只有它能看到窗口的出现和关闭。没有守护进程时
  `tabs-next` 会老实说没人可切（返回码 1）。
- 标签页的组成不保存在任何地方：它是由站在区域里的窗口推出来的，所以重启
  守护进程不会丢东西。
- 切换按键由 `setup-hotkeys.py` 从 `[tabs] key` 注册（见 9.1 节）。

<!-- section: daemon-actions -->
### 5.3. 动作：把自定义命令绑到快捷键（`[action:<名字>]`）

除了现成的命令，GridSnap 还有**动作**：`zones.conf` 里的一条记录把按键和「窗口与区域上
应该发生什么」绑起来。动作可以是内置的，也可以是你自己的命令。

```ini
[action:tidy]
run = builtin:minimize-others      ; 窗口进区域并最小化其余窗口（和 focus 一样）
key = <Super><Alt>d

[action:main-window]
run = builtin:show-window          ; 把站在 main 区域里的窗口提到前面
zone = main
key = <Super><Alt>m

[action:term-main]
run = builtin:terminal             ; 在 main 区域里开终端
zone = main
command = x-terminal-emulator      ; 这是默认值，可以换成自己的终端

[action:mirror]
run = builtin:mirror               ; 和镜像区域交换（axis = vertical —— 上下互换）
key = <Super><Alt>x

[action:note]
run = notify-send "区域 {zone}：{title}"   ; 带替换参数的自定义命令
```

按键照常安装 —— `python3 setup-hotkeys.py`（在 Cinnamon 的列表里显示为
`zone-tiler: action <名字>`），手动执行动作则是命令 `python3 zone-tiler.py action <名字>`；
`--dry-run` 很有用：它只显示会发生什么，什么都不做。

动作能拿到什么：窗口、区域、它的像素范围、显示器、工作桌面、窗口的类和标题 ——
放在变量 `GRIDSNAP_ZONE`、`GRIDSNAP_WINDOW`（hex）、`GRIDSNAP_X/Y/W/H`、`GRIDSNAP_MONITOR`、
`GRIDSNAP_WORKSPACE`、`GRIDSNAP_CLASS`、`GRIDSNAP_TITLE`、`GRIDSNAP_CONF` 里，也放在替换参数
`{zone}`/`{window}`/`{x}`… 里。自定义命令**不经 shell** 启动（所以管道和 `$VAR`
不会工作 —— 需要的话请显式写 `sh -c "…"`），而且**不等待**：终端要活到窗口关闭，
所以不能把按键挂在它上面。命令本身的错误 GridSnap 不捕获 —— 它们进会话的普通输出。

如果记录里没有 `zone`，处理对象就是活动窗口和它所在的区域；`zone` 预先指定
区域（那时就在该区域里找窗口），而 `--window`/`--monitor` 指定具体的窗口和屏幕。内置
动作不需要守护进程；`minimize-others` 在守护进程运行时把模式交给它 —— 和 `[focus]` 的按键一样。
写错的记录是配置错误（返回码 3），而不是一条默默不工作的按键。

每个内置动作具体做什么、镜像区域怎么找、以及为什么动作不进方案 ——
见 `docs/specs/actions.md`。

<!-- section: hotcorners -->
## 6. 热区：拖到网格上，窗口就位

抓住标题栏拖动窗口 —— 出现区域网格，并高亮光标落到的那个区域；松手 —— 窗口
就进那个区域。这是一个单独的进程，日常使用不需要它，它也不会妨碍守护进程：

```bash
python3 zone-hotcorners.py            # 监听拖拽（Ctrl-C 退出）
python3 zone-hotcorners.py --no-hud   # 不要高亮：只在松手时放入
python3 zone-hotcorners.py --dry-run --verbose   # 显示它会做什么
```

它是怎么做的：X11 不会通知「窗口正在被拖」，所以 watcher 监听 XI2 raw events（即使鼠标
被窗口管理器抓住，事件也会来），并把「在标题栏里按下按钮」当作拖拽。网格显示期间，
Cinnamon 原生的「甩到边缘」（`edge-tiling`）会被压住，以免窗口跳两次，显示完立刻
恢复 —— 这个设置不会一直关着。细节见 `docs/specs/zone-hotcorners.md`、
`man gridsnap-hotcorners`。

<!-- section: move -->
## 7. 方向键：把窗口送到相邻区域

`zone-tiler.py move left|right|up|down` 把活动窗口移到相邻区域 —— 没有覆盖层，规则和
选择器里的方向键一样（在网格边缘什么都不做，并说出来）。组合键用 `zones.conf` 里的 `[move]`
段设置，默认是 `Super+Left/Right/Up/Down`：

```ini
[move]
left  = <Super>Left
right = <Super>Right
up    = <Super>Up
down  = <Super>Down
```

这些组合在 Cinnamon 里被原生平铺占着（`push-tile-*`）。我们的 `setup-hotkeys.py` 会注册
自己的组合，并在组合冲突时往 stderr 打警告；Cinnamon 的设置不会被改动。如果生效的是原生平铺
而不是我们的 —— 请在「键盘 → 快捷键 → 窗口」里释放 `push-tile-*`，或者在 `[move]` 里
换一个组合。

<!-- section: picker -->
## 8. 可视化选择区域：`Super+Z`

`zone-picker.py` 在整个工作区上显示一个覆盖层，里面是 `zones.conf` 的区域：光标下的区域
和当前选择会高亮，**方向键**移动选择，**`Enter`** 把活动窗口放进选中的区域，
**`Esc`** 取消（像 gTile，但用的是你自己的网格）。显示器按活动窗口取，
也可以显式指定。

```bash
python3 zone-picker.py                    # 覆盖层（由快捷键启动）
python3 zone-picker.py --dry-run          # 选区域，但不移动窗口
python3 zone-picker.py --shot picker.png  # 拍一张覆盖层的快照然后退出（无交互）
python3 zone-picker.py --monitor 1        # 覆盖层放在第二台显示器上
python3 zone-picker.py --select bottom    # 初始选中指定的区域名
```

窗口的摆放走同一个布局器（`zone-tiler.apply_zone`/`Grid.rect`），所以范围
和区域像素级一致，而且按 `Enter` 之后 `undo` 也能用。

<!-- section: picker-hotkey -->
### 如何在 Cinnamon 中绑定 `Super+Z`

这个组合不会自动创建（`setup-hotkeys.py` 只装 `[keys]` 里的区域按键）——
请手动设置一次：**系统设置 → 键盘 → 快捷键 →「添加自定义快捷键」**，然后填写：

| 字段 | 值 |
|---|---|
| 名称 | `GridSnap: 选择区域` |
| 命令 | `python3 /到/目录/的/路径/zone-picker.py`（软件包安装则是 `gridsnap-picker`） |
| 组合键 | `Super+Z` |

同样的界面可以用命令 `cinnamon-settings keyboard` 打开。`Super+Z` 在 Cinnamon 里默认
是空闲的。改过 `zones.conf` 之后不需要改 `zone-picker.py` 的路径 —— 覆盖层每次都
自己读最新的配置。

<!-- section: hotkeys -->
## 9. 快捷键

你的会话里已经装好了：`Super+Alt+1` main、`+2` top-right、`+3` top-far、
`+4` bottom（在系统设置 → 键盘 → 快捷键里显示为「zone-tiler: …」）。
修改：改 `zones.conf` 里的 `[keys]`，然后在编辑器里点「快捷键」，或者：

```bash
python3 setup-hotkeys.py --list     # 将要安装什么
python3 setup-hotkeys.py            # 安装 / 更新
python3 setup-hotkeys.py --remove   # 删除所有 zone-tiler 组合键
```

<!-- section: hotkeys-focus -->
### 9.1. 用一个键切换「只留这个区域」

`zones.conf` 里有一个可选的 `[focus]` 段：

```ini
[focus]
key = <Super><Alt>F   ; 模式的组合键（可选）
zone = main           ; 可选：把窗口放进哪个区域
```

带 `zone` 时，按键把活动窗口放进该区域，并最小化这个桌面和这台显示器上的其他窗口；
不带 `zone` 时 —— 只做最小化（「只留这个窗口」），不移动任何东西。再按同一个键
退出模式。这一段是可选的：没有它，`setup-hotkeys.py` 装的组合和以前完全一样。
组合注册为 `zone-tiler: focus`，所以 `--remove` 会连它一起删掉。模式的细节见 4.2 节。

标签页切换键也是这么设置的 —— 用 `[tabs]` 段（见 5.2 节）：

```ini
[tabs]
key = <Super><Alt>g   ; 「活动窗口所在区域的下一个窗口」
```

<!-- section: hotkeys-profiles -->
### 9.2. 用一个键切换布局方案

自己的方案（比如「工作」「观影」「直播」）可以用命令
`zone-tiler.py profile save <名字>` 保存，也可以用编辑器里的「方案」菜单。想用
**一个键**切换布局，请建一个普通的 Cinnamon 自定义快捷键：

1. 系统设置 → 键盘 → 快捷键 →「添加自定义快捷键」。
2. 名称 —— 比如 `Gridsnap: 方案 work`。
3. 命令 —— `python3 /到/zone-tiler.py 的路径 profile use work`（必要时加上
   `--conf /到/zones.conf 的路径`；不想每次切换都动快捷键就加 `--no-hotkeys`）。
4. 指定组合键，比如 `Super+Alt+W`。

命令会从选中的方案重写你 `zones.conf` 里的 `[zones]`/`[rules]`/`[ignore]`/`[keys]`。如果
希望切换后区域的新按键立刻生效，就保留 `setup-hotkeys.py` 的调用
（默认就有），或者在编辑器里点「快捷键」。
<!-- section: applet -->
## 10. Cinnamon 面板小程序：从面板使用 GridSnap

`contrib/applet/` 目录里有一个面板小程序 `gridsnap@metapeka`：面板上的图标（也可以加上
文字），左键和右键弹出菜单 —— 打开编辑器、应用快捷键、区域列表（点击把活动窗口放进该区域）、
各工作区概览、`arrange` 和 `undo`。小程序是那些脚本的薄壳，自己没有布局逻辑；
GridSnap 目录在它的设置里指定（默认 `~/thinkrail/gridsnape`）。

安装：

```bash
mkdir -p ~/.local/share/cinnamon/applets
cp -r contrib/applet/gridsnap@metapeka ~/.local/share/cinnamon/applets/
cinnamon-settings applets        # 在小程序列表里启用 GridSnap
```

菜单里有什么、有哪些设置和限制 —— 见 `contrib/applet/README.md`。

<!-- section: troubleshooting -->
## 11. 窗口没有落到该落的地方时

* 程序自己决定尺寸（比如按字符步长排版的终端）—— 可能差几个像素；
* 面积为零的区域不会保存：最小尺寸是屏幕的 5 %；
* 不支持 Wayland，但 Cinnamon 跑在 X11 上；
* 手动改过 `zones.conf` 之后请重新运行 `setup-hotkeys.py`，否则新的区域名
  不会和按键关联起来。

<!-- section: checks -->
## 12. 跑过的检查

* 不需要 X 的单元测试（解析器、规则、reflow、守护进程、IPC `zone_ipc`、配置层和方案
  与屏幕的绑定、arrange/undo、标签页核心 `zone_tabs`、标签条导航 `zone_tabstrip_nav`、
  编辑器几何 `zone_geom`、区域选择导航 `zone_picker_nav`、动作核心 `zone_actions` 以及
  `[action:<名字>]` 的解析和执行动作）：
  `python3 -m unittest discover -s tests -v`；
* 在自己 Xvfb 上的冒烟测试：`sh tests/smoke.sh`（窗口像素级落进区域）；
* arrange+undo 冒烟测试：`sh tests/smoke-arrange.sh`（三个窗口被布局并还原）；
* 区域选择冒烟测试：`sh tests/smoke-picker.sh`（`Right` + `Enter` 把窗口
  像素级放进 `top-right`，`Esc` 取消）；
* 窗口例外冒烟测试：`sh tests/smoke-ignore.sh`（被排除的窗口，`apply`、
  `arrange`、守护进程和选择器都不动，而被移出区域的窗口即使守护进程重启也不回来）；
* 「只留这个区域」模式冒烟测试：`sh tests/smoke-focus.sh`（没有守护进程时 `focus main` 把窗口
  放进区域并最小化同一显示器的另外两个窗口，被排除的仍然可见，`--off` 和 toggle
  只还原自己最小化的那些；有守护进程时 `gridsnap-ctl focus` 也会最小化
  已在模式中出现的窗口）；
* 区域内标签页冒烟测试：`sh tests/smoke-tabs.sh`（区域里的第二个窗口成为标签页，
  并把第一个挪到后面，标签条出现在区域上方并随最后一个标签页消失，`tabs-next`
  把相邻标签页提到前面，「只留这个区域」模式不最小化目标区域的标签页 —— 包括
  在模式运行期间才进来的窗口 —— 而 `--offset` 返回 2）；
* 方案与屏幕绑定冒烟测试：`sh tests/smoke-monitor-profiles.sh`（两台虚拟
  显示器：`profile use-monitors` 按各自方案布置屏幕，不带 `--monitor` 的 `apply` 在
  窗口所在屏幕的方案里找区域，而改过方案并 `reload` 之后守护进程用的是新几何，
  不是配置里的旧快照）；
* 动作冒烟测试：`sh tests/smoke-actions.sh`（外部命令通过两个渠道拿到窗口、区域和区域
  范围，`terminal` 把终端窗口放进区域，`show-window` 把相邻标签页提到前面，
  `mirror` 交换两个区域的窗口，`minimize-others` 最小化别人的窗口并再次按下时
  还原它）；
* 无交互拍覆盖层快照：`xvfb-run -a python3 zone-picker.py --shot /tmp/picker.png`；
* CI（`.github/workflows/ci.yml`）：编译、单元测试、打包和 Xvfb + muffin 上的冒烟场景。所有 job 都在 GitHub 的 `ubuntu-latest` 上，不在自己的机器上；每个 job 都有 `timeout-minutes`（10/15/20/20），冒烟步骤通过 `tests/run-smoke.sh` 跑，并带一次重试：CI 里窗口有时来不及从 muffin 拿到几何，检查会卡在时间上，而不是卡在代码上；
* 盯着 CI 又不会睡过去：`tools/ci-watch.sh --pr 14`（或 `tools/ci-watch.sh <run-id>`）。
  它打印状态变化，并且总会自己结束：`0` —— 全绿，`1` —— 挂了，
  `2` —— 卡住了（状态超过 `--stall` 没变，默认 3 分钟）或到了
  `--deadline`（15 分钟）。它和 `gh run watch` 的区别就在于此：它说「卡住了」，
  而不是无限等下去；
* 配置：存在时从 `~/.config/gridsnap/zones.conf` 读取，否则读代码旁边的那份；
* 从终端布局：窗口范围和区域一致（包括从最大化状态开始）；
* 任意的矩形区域（`x,y,w,h`）—— 同样精确一致；
* 编辑器里的鼠标操作：移动、拖边缘改变大小、最小尺寸、吸附到屏幕边缘
  和相邻区域、双击、`Delete`、模板、保存和再次读取；
* 快捷方式启动、GTK 主题查找图标、`desktop-file-validate`；
* 软件包：`make tarball`（解包并 `./install.sh --prefix ...`）和 `make deb`
  （`dpkg-deb -I`、`dpkg-deb -c`、解包并从包里跑 `zone-tiler.py --version`）；这也
  在 CI 里单独一阶段跑（`.github/workflows/ci.yml`）；
* Cinnamon 小程序：对 `metadata.json` 和 `settings-schema.json` 跑
  `python3 -m json.tool`，`node --check applet.js`，并在活的 Cinnamon 里加载（无头 `Xvfb` + 私有 dbus）——
  `Loaded applet gridsnap@metapeka`，面板上正是它（`contrib/applet/check-cinnamon.sh`）。

* man 手册页：`make check-man-i18n` 用 po4a 构建三种语言，检查 po4a 模板的每一条都已翻译、
  `man/gridsnap.1` 里的 `--…` 选项与七条命令真实的 `--help` 一致、`man --warnings` 没有告警，
  并且俄语页面与源文件逐字节相同。

## 13. 许可证

<!-- section: license -->

MIT —— 见 [`LICENSE`](LICENSE)。版权所有 (c) 2026 metapeka
<188594035+metapeka@users.noreply.github.com>。
