# -*- coding: utf-8 -*-
"""分辨率 / 码率契约 + 前端抽屉遮罩契约。

这些测试针对**当前尚未实现**的目标行为编写，在当前代码上必须先失败：

  1. app/session.py 里还没有 `_effective_max_size(serial, target_height, angle=0)`，
     调用即 AttributeError。它要按「目标高度」算出 scrcpy 的 max_size（最长边），
     而不是原样把最长边当上限。
  2. app/frontend/app.js 的 setDrawer() 现在把 #mask 当抽屉遮罩用
     （`$('mask').classList.toggle('show', ...)`），目标行为是不再用它。
  3. app/frontend/index.html 的分辨率提示仍写「限制最长边」，应改成按目标高度描述。
  4. 前端还没有「按目标高度缩放码率」的实现标记（明确的函数名或注释标识）。

不依赖 adb、不连设备：device_screen 一律 mock 掉。
"""

import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
FRONTEND_DIR = os.path.join(APP_DIR, "frontend")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import session  # noqa: E402

APP_JS = os.path.join(FRONTEND_DIR, "app.js")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")

# 设备原生屏（wm size 拿到的物理/逻辑尺寸）。
SCREEN_LANDSCAPE = {"width": 1920, "height": 1080, "density": 320}


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _js_function_body(source, name):
    """截取 `function name(...) { ... }` 的实现体（按花括号配对，不靠正则贪心）。"""
    match = re.search(
        r"function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), source
    )
    if not match:
        return ""
    start = source.index("{", match.start())
    depth = 0
    for i in range(start, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[match.start(): i + 1]
    return source[match.start():]


class EffectiveMaxSizeTests(unittest.TestCase):
    """_effective_max_size 按目标高度算出 scrcpy 的 max_size（最长边）。"""

    def _eff(self, screen, target_height, angle=0):
        with mock.patch.object(session, "device_screen", return_value=screen):
            return session._effective_max_size("SERIAL", target_height, angle=angle)

    def test_landscape_target_height_equals_native(self):
        # 1920x1080 目标高 1080：不缩放，返回最长边 1920。
        self.assertEqual(self._eff(SCREEN_LANDSCAPE, 1080), 1920)

    def test_ultrawide_scaled_by_height(self):
        # 3440x1440 目标高 1080：按高缩放，宽 3440*1080/1440 = 2580。
        screen = {"width": 3440, "height": 1440, "density": 320}
        self.assertEqual(self._eff(screen, 1080), 2580)

    def test_wide_screen_height_equals_target(self):
        # 2560x1080 目标高 1080：高已等于目标，返回最长边 2560。
        screen = {"width": 2560, "height": 1080, "density": 320}
        self.assertEqual(self._eff(screen, 1080), 2560)

    def test_downscale_to_720(self):
        # 1920x1080 目标高 720：宽 1920*720/1080 = 1280。
        self.assertEqual(self._eff(SCREEN_LANDSCAPE, 720), 1280)

    def test_no_upscale_returns_zero(self):
        # 目标高度高于原生高度时不放大，0 表示原画。
        self.assertEqual(self._eff(SCREEN_LANDSCAPE, 2160), 0)

    def test_angle_90_uses_rotated_dimensions(self):
        # 1920x1080 旋转 90° 后为 1080x1920：目标高 1920 正好贴合，返回 1920。
        # 若误用未旋转的宽高（高 1080 < 1920）会得到 0，借此锁定按旋转后尺寸计算。
        self.assertEqual(self._eff(SCREEN_LANDSCAPE, 2160, angle=0), 0)
        self.assertEqual(self._eff(SCREEN_LANDSCAPE, 1920, angle=90), 1920)


class DrawerMaskContractTests(unittest.TestCase):
    """setDrawer 不应把 #mask 当作抽屉遮罩来显示。"""

    def setUp(self):
        source = _read(APP_JS)
        self.body = _js_function_body(source, "setDrawer")
        if not self.body:
            self.fail("未在 app/frontend/app.js 找到 function setDrawer(...) { ... } 函数体")

    def test_set_drawer_does_not_toggle_mask(self):
        self.assertNotRegex(
            self.body,
            r"['\"]mask['\"]",
            "setDrawer 不应把 #mask 当作抽屉遮罩（目标：抽屉不再用 #mask 铺底）",
        )

    def test_set_drawer_does_not_read_mask_element(self):
        self.assertNotRegex(
            self.body,
            r"\$\(\s*['\"]mask['\"]\s*\)",
            "setDrawer 内不应再取 #mask 元素来 toggle",
        )


# 「按目标高度缩放码率」的实现标记：必须是明确的函数名或注释标识，
# 不是凑巧出现的任意字符串。未来实现至少满足其中一条。
BITRATE_HEIGHT_MARKERS = [
    # 函数名同时含 bitRate/Rate 与 Height：如 function presetBitRateHeight(...)
    re.compile(r"function\s+[A-Za-z_$][\w$]*[Bb]it[Rr]ate[A-Za-z_$]*[Hh]eight\s*\("),
    re.compile(r"function\s+[A-Za-z_$][\w$]*[Hh]eight[A-Za-z_$]*(?:Rate|BitRate)[A-Za-z_$]*\s*\("),
    # 显式注释标识
    re.compile(r"按目标高度缩放码率"),
    re.compile(r"@bitrate-by-target-height\b"),
]

# 分辨率提示里表示「按目标高度」的明确用词之一。
HINT_TARGET_HEIGHT_MARKERS = ["目标高度", "按高度"]


class ResolutionHintAndBitrateTests(unittest.TestCase):
    """分辨率提示不再写「限制最长边」，且码率按目标高度缩放有实现标记。"""

    def setUp(self):
        self.html = _read(INDEX_HTML)
        self.app_js = _read(APP_JS)

    def test_resolution_hint_drops_longest_side_wording(self):
        self.assertNotIn(
            "限制最长边",
            self.html,
            "分辨率提示不应再写「限制最长边」，应改为按目标高度描述",
        )

    def test_resolution_hint_mentions_target_height(self):
        self.assertTrue(
            any(m in self.html for m in HINT_TARGET_HEIGHT_MARKERS),
            "分辨率提示应改为按目标高度描述（含 %s 之一）"
            % " / ".join(HINT_TARGET_HEIGHT_MARKERS),
        )

    def test_bitrate_scales_by_target_height_marker(self):
        self.assertTrue(
            any(p.search(self.app_js) for p in BITRATE_HEIGHT_MARKERS),
            "app.js 应包含「按目标高度缩放码率」的实现标记"
            "（明确的函数名或注释标识，如按目标高度缩放码率）",
        )


class DrawerWidthContractTests(unittest.TestCase):
    """抽屉宽度只能有**一个**来源：`--drawer-w-*` 那个变量。

    index.html 里那段注释把话说死了：「抽屉自己用它当 width，.app 用它当 padding-right
    让位，一处定义两处对齐，不会出现让位的宽度和抽屉宽度不一致那种半截盖住画面的情况」。

    而 2026-09-23 主人看到的正是那句话描述的画面：点开「状态」，右侧多出一条
    56px 的黑带 —— `.app` 按 `--drawer-w-stat` 让位 460px，`#statPanel` 却被**后面
    一条硬编码的 `width: min(404px, 92vw)`** 盖成了 404px（同为 id 选择器，后写的赢），
    中间那 56px 露出页面底色。同一条规则写两遍，看代码根本看不出来。

    所以这条契约盯着两件事：
      · 每个抽屉**最多一处**宽度声明，且它引用的是自己那个变量
        （`#logPanel` 没有独立声明、吃 `.drawer` 基类的，也算合格）；
      · `.app` 让位用的变量，必须和抽屉宽度用的是同一个。

    纯静态解析 `<style>`，不起浏览器 —— 浏览器那层由 `tools/_t161_status_gap.py`
    真量一遍（量的是 `getBoundingClientRect`，判据是不依赖时序的最终值）。
    """

    # 面板 id → 变量名后缀（与 CSS 里的 --drawer-w-* 对应）
    PANELS = {
        "logPanel": "log",
        "statPanel": "stat",
        "filePanel": "file",
        "shellPanel": "shell",
    }

    WIDTH_RE = re.compile(r"(?<![\w-])width\s*:\s*([^;]+)")

    @classmethod
    def setUpClass(cls):
        html = _read(INDEX_HTML)
        blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)
        if not blocks:
            raise AssertionError("index.html 里没找到 <style> 块")
        css = "\n".join(blocks)
        # 先把注释摘掉：注释里的 `*/` 和文字会把"选择器"整个污染掉（第一版就栽在这）
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        cls.css = css
        cls.vars = dict(re.findall(r"(--drawer-w-[a-z]+)\s*:\s*([^;]+);", css))
        # 展开成 (选择器, 声明体)：选择器 = 上一个 `}` 到下一个 `{` 之间的文字
        cls.rules = [(re.sub(r"\s+", "", m.group(1)), m.group(2))
                     for m in re.finditer(r"([^{}]*?)\{([^{}]*)\}", css)]

    def _widths_of(self, selector):
        """选择器**整体等于**给它时的所有 width 声明（按出现顺序）。"""
        want = re.sub(r"\s+", "", selector)
        out = []
        for sel, body in self.rules:
            if sel == want:
                out += [w.strip() for w in self.WIDTH_RE.findall(body)]
        return out

    def test_every_drawer_has_its_width_variable(self):
        for panel, key in self.PANELS.items():
            self.assertIn("--drawer-w-" + key, self.vars,
                          "%s 的宽度变量 --drawer-w-%s 不见了" % (panel, key))

    def test_panel_width_comes_from_its_own_variable_only(self):
        base = self._widths_of(".drawer")
        self.assertTrue(
            any("var(--drawer-w-" in w for w in base),
            "`.drawer` 基类得从 --drawer-w-* 里取宽度，实际是 %r" % base)
        for panel, key in self.PANELS.items():
            with self.subTest(panel=panel):
                own = self._widths_of("#" + panel)
                self.assertLessEqual(
                    len(own), 1,
                    "`#%s` 有 %d 处宽度声明（%r）—— 重复定义谁在后面谁赢，"
                    "必然和 .app 的让位宽度走散，这就是那条黑带" % (panel, len(own), own))
                # 没有独立声明就吃基类那份；有就以它为准（id 选择器优先级更高）
                effective = own[-1] if own else base[-1]
                self.assertEqual(
                    effective, "var(--drawer-w-%s)" % key,
                    "`#%s` 的实际宽度来自 %r，必须只认 --drawer-w-%s"
                    % (panel, effective, key))

    def test_app_padding_uses_the_same_variable_as_the_panel(self):
        for panel, key in self.PANELS.items():
            with self.subTest(panel=panel):
                sel = '.app.has-drawer[data-drawer="%s"]' % panel
                bodies = [body for s, body in self.rules
                          if s == re.sub(r"\s+", "", sel)]
                self.assertEqual(len(bodies), 1,
                                 "找不到（或重复）了 %s 的让位规则" % sel)
                self.assertRegex(
                    bodies[0], r"padding-right\s*:\s*var\(--drawer-w-%s\)" % key,
                    "让位宽度必须和抽屉宽度是**同一个**变量，否则就是那条黑带")


if __name__ == "__main__":
    unittest.main()
