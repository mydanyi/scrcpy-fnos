# -*- coding: utf-8 -*-
"""前端三个用户可见问题的回归契约：抽屉布局 / MSE 低延迟 / 音视频同步 / 输入背压。

这些测试针对**尚未实现**的目标行为编写，在当前代码上必须先失败（RED）：

  A. 抽屉布局
     - index.html 里还没有「打开抽屉时 .app 让出宽度」的布局占位契约
       （.app.has-drawer / [data-drawer] / --drawer-w-*），
       抽屉现在还是 position:fixed 直接压在画面右侧。
     - app.js 的 setDrawer() / closeDrawers() 还没有同步布局 class 或 CSS 变量，
       closeDrawers() 反而还在动 #mask。
  B. 低延迟视频
     - 没有 MSE_TARGET_LATENCY / MSE_MAX_LATENCY 这组独立常量，
       纠偏阈值写死在 tickLatency 里（落后 1.5 秒才纠）。
     - tickLatency 的节拍是 1000ms，不是 250ms 左右。
  C. 音视频同步
     - 没有 WebCodecs 的相对视频时钟（wcClockBaseTs / wcClockBaseNow /
       wcClockNote / wcClockSec / videoClockSec）。
     - audioAnchorSec / tickAudioSync 在没有 videoEl 时退化到 mseDts。
     - AUDIO_MAXQ 允许音频攒到约 2 秒。
  D. 输入延迟保护
     - flushMove() 里没有 controlWS.bufferedAmount 背压保护。

只做静态 / 结构断言（纯文本 + 花括号配对），不依赖浏览器、不连设备。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(ROOT, "app", "frontend")
APP_JS = os.path.join(FRONTEND_DIR, "app.js")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")

DRAWER_IDS = ["logPanel", "statPanel", "filePanel", "shellPanel"]

# 四个抽屉的宽度语义（原样保留）：(px 上限, 移动端 vw 上限)
DRAWER_WIDTH_SEMANTICS = {
    "log": ("460", "92"),
    "stat": ("460", "92"),
    "file": ("520", "94"),
    "shell": ("760", "96"),
}

# 「布局让步」的实现标记：class 或 CSS 变量，任一即可。
LAYOUT_SYNC_MARKERS = ("has-drawer", "--drawer-w", "data-drawer", "syncDrawerLayout")


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _brace_body(source, match):
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


def _js_function_body(source, name):
    """截取 `function name(...) { ... }` 的实现体（按花括号配对，不靠正则贪心）。"""
    match = re.search(r"function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name), source)
    return _brace_body(source, match)


def _js_member_body(source, name):
    """截取对象字面量里的 `name: function (...) { ... }` 实现体。"""
    match = re.search(
        r"\b%s\s*:\s*function\s*\([^)]*\)\s*\{" % re.escape(name), source
    )
    return _brace_body(source, match)


def _is_commented(source, pos):
    """pos 所在行的行首到 pos 之间是否有 // —— 用来把「注释里提到」和「代码里用了」分开。"""
    line_start = source.rfind("\n", 0, pos) + 1
    return "//" in source[line_start:pos]


def _strip_line_comments(text):
    """去掉 // 行注释：断言「代码里没有某某」时，不该被注释里回顾历史的那句话绊倒。"""
    out = []
    for line in text.splitlines():
        i = line.find("//")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


# ======================================================================
# A. 抽屉布局：为抽屉让出宽度，而不是盖住画面
# ======================================================================
class DrawerLayoutContractTests(unittest.TestCase):
    def setUp(self):
        self.html = _read(INDEX_HTML)
        self.js = _read(APP_JS)

    def test_index_has_layout_placeholder_contract(self):
        """index.html 必须有「抽屉让出宽度」的布局占位契约。"""
        self.assertRegex(
            self.html, r"\.app\.has-drawer",
            "index.html 应有 .app.has-drawer 这一档布局占位",
        )
        self.assertIn(
            "data-drawer", self.html, "index.html 应通过 [data-drawer] 选择抽屉宽度"
        )
        for did in DRAWER_IDS:
            self.assertRegex(
                self.html,
                r'\.app\.has-drawer\[data-drawer="?%s"?\]\s*\{[^}]*padding-right\s*:'
                % re.escape(did),
                "index.html 应为抽屉 %s 声明 padding-right 让步" % did,
            )

    def test_drawer_widths_keep_existing_semantics(self):
        """四个抽屉的宽度语义（px + 移动端 vw）必须原样保留。"""
        for key, (px, vw) in DRAWER_WIDTH_SEMANTICS.items():
            m = re.search(
                r"--drawer-w-%s\s*:\s*min\(\s*(\d+)px\s*,\s*(\d+)vw\s*\)" % key,
                self.html,
            )
            self.assertIsNotNone(
                m, "index.html 应声明 --drawer-w-%s 宽度变量（min(px, vw)）" % key
            )
            self.assertEqual(m.group(1), px, "--drawer-w-%s 的 px 上限变了" % key)
            self.assertEqual(m.group(2), vw, "--drawer-w-%s 的 vw 上限变了" % key)
        self.assertRegex(
            self.html,
            r"#filePanel\s*\{[^}]*width\s*:\s*var\(--drawer-w-file\)",
            "#filePanel 的宽度应引用 --drawer-w-file",
        )
        self.assertRegex(
            self.html,
            r"#shellPanel\s*\{[^}]*width\s*:\s*var\(--drawer-w-shell\)",
            "#shellPanel 的宽度应引用 --drawer-w-shell",
        )

    def test_drawer_still_pinned_to_right(self):
        """抽屉仍然固定在右侧（只是布局同时让出了那条宽度）。"""
        m = re.search(r"\.drawer\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(m, "index.html 应有 .drawer 基类规则")
        body = m.group(1)
        self.assertIn("position: fixed", body)
        self.assertRegex(body, r"right\s*:\s*0")

    def _assert_layout_sync(self, fn_body, label):
        self.assertTrue(
            any(m in fn_body for m in LAYOUT_SYNC_MARKERS),
            "%s 必须同步布局（%s 之一），否则 .app 不会为抽屉让出宽度"
            % (label, " / ".join(LAYOUT_SYNC_MARKERS)),
        )

    def test_set_drawer_syncs_layout(self):
        body = _js_function_body(self.js, "setDrawer")
        self.assertTrue(body, "未找到 function setDrawer(...) 函数体")
        self._assert_layout_sync(body, "setDrawer")

    def test_close_drawers_syncs_layout(self):
        body = _js_function_body(self.js, "closeDrawers")
        self.assertTrue(body, "未找到 function closeDrawers(...) 函数体")
        self._assert_layout_sync(body, "closeDrawers")

    def test_set_drawer_does_not_touch_mask(self):
        body = _js_function_body(self.js, "setDrawer")
        self.assertNotRegex(body, r"['\"]mask['\"]", "setDrawer 不许再把 #mask 当抽屉遮罩")

    def test_close_drawers_does_not_touch_mask(self):
        body = _js_function_body(self.js, "closeDrawers")
        self.assertNotRegex(
            body, r"['\"]mask['\"]", "closeDrawers 不许再把 #mask 当抽屉遮罩"
        )

    def test_layout_sync_recomputes_from_open_drawer(self):
        """布局必须按「当前开着的那个抽屉」重算，不能只 add/remove 一个通用 class。"""
        body = _js_function_body(self.js, "setDrawer")
        self.assertRegex(
            body,
            r"openDrawerId",
            "setDrawer 里同步布局时要按 openDrawerId 重算，避免留下 stale class",
        )

    def test_only_one_drawer_open_at_a_time(self):
        body = _js_function_body(self.js, "setDrawer")
        self.assertRegex(
            body,
            r"DRAWER_IDS\s*\.\s*forEach",
            "setDrawer 打开一个抽屉时应把其余抽屉关掉（只允许一个开着）",
        )

    def test_no_outside_click_closer_for_drawers(self):
        """点击画面 / 虚拟按键 / 抽屉按钮都不该关抽屉：不许有 document/window 级 click 关闭。"""
        self.assertIsNone(
            re.search(
                r"(document|window)\s*\.\s*addEventListener\(\s*['\"]click['\"]",
                self.js,
            ),
            "app.js 不应在任何 document/window click 里关抽屉（点击画面不该关抽屉）",
        )

    def test_mask_cannot_swallow_clicks(self):
        """#mask 只在 modal 显示时才可交互，否则它会吃掉画面上的点击。"""
        m = re.search(r"\.mask\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(m, "index.html 应有 .mask 规则")
        self.assertIn("pointer-events: none", m.group(1))


# ======================================================================
# B. MSE 低延迟：贴着直播边缘放
# ======================================================================
class MseLowLatencyContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)
        self.tick = _js_function_body(self.js, "tickLatency")

    def _numeric_const(self, name):
        m = re.search(
            r"\bvar\s+%s\s*=\s*([0-9]*\.?[0-9]+)\s*;" % re.escape(name), self.js
        )
        self.assertIsNotNone(m, "app.js 应用独立常量 %s 表达这个延迟目标" % name)
        return float(m.group(1))

    def test_target_latency_constant_is_tenths_of_a_second(self):
        v = self._numeric_const("MSE_TARGET_LATENCY")
        self.assertGreaterEqual(v, 0.05, "目标延迟不该小于 50ms")
        self.assertLessEqual(v, 0.25, "目标延迟应落在 0.1~0.2 秒一带")

    def test_max_latency_constant_is_sub_second(self):
        v = self._numeric_const("MSE_MAX_LATENCY")
        self.assertGreaterEqual(v, 0.3, "最大可见延迟不该小于 300ms")
        self.assertLessEqual(v, 0.7, "最大可见延迟应在 0.4~0.6 秒一带")

    def test_max_latency_is_larger_than_target(self):
        self.assertGreater(
            self._numeric_const("MSE_MAX_LATENCY"),
            self._numeric_const("MSE_TARGET_LATENCY"),
        )

    def test_tick_interval_around_quarter_second(self):
        m = re.search(
            r"setInterval\(\s*tickLatency\s*,\s*([A-Za-z_$][\w$]*|[0-9]+)\s*\)",
            self.js,
        )
        self.assertIsNotNone(m, "app.js 应有一处 setInterval(tickLatency, ...)")
        raw = m.group(1)
        if raw.isdigit():
            ms = int(raw)
        else:
            c = re.search(r"\bvar\s+%s\s*=\s*([0-9]+)\s*;" % re.escape(raw), self.js)
            self.assertIsNotNone(c, "找不到常量 %s 的定义" % raw)
            ms = int(c.group(1))
        self.assertGreaterEqual(ms, 150, "纠偏节拍不该慢于 150ms")
        self.assertLessEqual(ms, 400, "纠偏节拍应在 250ms 左右")

    def test_tick_uses_latency_constants(self):
        self.assertTrue(self.tick, "未找到 function tickLatency(...) 函数体")
        self.assertIn(
            "MSE_MAX_LATENCY", self.tick, "tickLatency 应用 MSE_MAX_LATENCY 判落后"
        )
        self.assertIn(
            "MSE_TARGET_LATENCY", self.tick, "tickLatency 纠偏点应用 MSE_TARGET_LATENCY"
        )

    def test_tick_drops_legacy_1_5s_threshold(self):
        self.assertNotIn("1.5", self.tick, "旧的「落后 1.5 秒才纠偏」阈值不许再回来")

    def test_tick_seeks_towards_live_edge(self):
        self.assertRegex(
            self.tick,
            r"currentTime\s*=",
            "tickLatency 应把播放点推回直播边缘（end - MSE_TARGET_LATENCY）",
        )

    def test_tick_does_not_trim_source_buffer(self):
        self.assertNotIn(
            "remove(",
            _strip_line_comments(self.tick),
            "纠偏不许再用 sourceBuffer.remove（会把流冻死）",
        )

    def test_source_buffer_trim_only_in_test_hook(self):
        hook = _js_member_body(self.js, "_forceFreeze")
        self.assertIn("sourceBuffer.remove(", hook, "测试钩子 _forceFreeze 应保留")
        base = self.js.index("_forceFreeze")
        for m in re.finditer(r"sourceBuffer\.remove\s*\(", self.js):
            if _is_commented(self.js, m.start()):
                continue                       # 注释里回顾历史不算
            self.assertTrue(
                base < m.start() < base + len(hook),
                "生产路径不许出现 sourceBuffer.remove（只允许 _forceFreeze 钩子里那一处）",
            )

    def test_reset_decoder_does_not_trim_buffer(self):
        body = _strip_line_comments(_js_function_body(self.js, "resetDecoder"))
        self.assertNotIn("remove(", body, "resetDecoder 不许再裁缓冲")

    def test_no_reset_video_request_in_production(self):
        """唯一的 resetVideo 出口只能是 __scrcpyTest 里那个排障钩子。"""
        hook = _js_member_body(self.js, "resetVideo")
        self.assertTrue(hook, "测试钩子 resetVideo 应保留（仅供诊断脚本取证）")
        base = self.js.index("resetVideo: function")
        for m in re.finditer(r"resetVideo", self.js):
            if _is_commented(self.js, m.start()):
                continue                       # 注释里回顾历史不算
            self.assertTrue(
                m.start() < base + len(hook),
                "应用自己的路径不许再给设备发 resetVideo（会把编码器重启成永久黑）",
            )
        for m in re.finditer(r"requestKeyFrame", self.js):
            self.assertTrue(
                _is_commented(self.js, m.start()),
                "不许再接回向设备要关键帧的老路（requestKeyFrame 只应出现在历史注释里）",
            )

    def test_queue_overflow_still_waits_for_natural_keyframe(self):
        body = _js_function_body(self.js, "feedMSE")
        self.assertTrue(body, "未找到 function feedMSE(...) 函数体")
        self.assertIn("mseNeedKey", body, "队列满时仍应进「等关键帧」状态")
        self.assertIn("keyWaits", body, "队列满时仍应计入 keyWaits 诊断")
        self.assertIsNotNone(
            re.search(r"\bvar\s+MSE_MAXQ\s*=\s*[0-9]+", self.js), "MSE_MAXQ 应保留"
        )


# ======================================================================
# C. 音视频同步：MSE 用播放头，WebCodecs 用相对时钟
# ======================================================================
class AvSyncClockContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)

    def test_webcodecs_clock_state_declared(self):
        self.assertRegex(
            self.js,
            r"\bvar\s+wcClockBaseTs\b",
            "app.js 应用 wcClockBaseTs 记录首个输出帧的 timestamp",
        )
        self.assertRegex(
            self.js,
            r"\bvar\s+wcClockBaseNow\b",
            "app.js 应用 wcClockBaseNow 记录那一刻的 performance.now()",
        )

    def test_note_function_records_first_frame_and_wall_clock(self):
        body = _js_function_body(self.js, "wcClockNote")
        self.assertTrue(body, "未找到 function wcClockNote(frame) —— 应记录首帧基准")
        self.assertIn("timestamp", body, "wcClockNote 应读 VideoFrame.timestamp")
        self.assertRegex(
            body,
            r"(performance\.now|nowMs)\s*\(",
            "wcClockNote 应记下 performance.now()（可用 nowMs 包一层）",
        )

    def test_reset_function_clears_webcodecs_clock(self):
        body = _js_function_body(self.js, "wcClockReset")
        self.assertTrue(body, "未找到 function wcClockReset()")
        self.assertIn("wcClockBaseTs", body)
        self.assertIn("wcClockBaseNow", body)

    def test_reset_decoder_clears_webcodecs_clock(self):
        body = _js_function_body(self.js, "resetDecoder")
        self.assertIn(
            "wcClockReset",
            body,
            "resetDecoder 应清理 WebCodecs 视频时钟（换会话时归零）",
        )

    def test_reset_audio_clears_webcodecs_clock(self):
        body = _js_function_body(self.js, "resetAudio")
        self.assertIn(
            "wcClockReset",
            body,
            "resetAudio 应清理 WebCodecs 视频时钟（与新会话一起归零）",
        )

    def test_decoder_output_notes_frames(self):
        body = _js_function_body(self.js, "ensureDecoder")
        self.assertIn(
            "wcClockNote", body, "解码器 output 回调应把输出帧交给 wcClockNote"
        )

    def test_webcodecs_clock_does_not_use_mse_video_element(self):
        body = _js_function_body(self.js, "wcClockSec")
        self.assertTrue(body, "未找到 function wcClockSec() —— WebCodecs 的相对视频时钟")
        self.assertNotIn("videoEl", body, "WebCodecs 时钟不许拿 MSE 的 <video> 当基准")
        self.assertNotIn("currentTime", body, "WebCodecs 时钟不许用 <video>.currentTime")

    def test_video_clock_falls_back_without_video_element(self):
        body = _js_function_body(self.js, "videoClockSec")
        self.assertTrue(body, "未找到 function videoClockSec() —— 统一的视频时钟入口")
        self.assertRegex(body, r"if\s*\(\s*videoEl\s*\)", "有 <video> 时用播放头")
        self.assertIn(
            "wcClockSec", body, "没有 <video>（WebCodecs）时必须回落到 wcClockSec"
        )

    def test_audio_anchor_uses_video_clock(self):
        body = _js_function_body(self.js, "audioAnchorSec")
        self.assertTrue(body, "未找到 function audioAnchorSec() 函数体")
        self.assertRegex(
            body,
            r"(videoClockSec|wcClockSec)\s*\(",
            "audioAnchorSec 必须走统一视频时钟，不能只看 videoEl",
        )

    def test_tick_audio_sync_uses_video_clock(self):
        body = _js_function_body(self.js, "tickAudioSync")
        self.assertTrue(body, "未找到 function tickAudioSync() 函数体")
        self.assertRegex(
            body,
            r"(videoClockSec|wcClockSec)\s*\(",
            "tickAudioSync 必须走统一视频时钟（WebCodecs 下没有 videoEl 也得能跑）",
        )
        self.assertNotRegex(
            body,
            r"!\s*videoEl\s*\)\s*return",
            "tickAudioSync 不许在没有 videoEl 时直接 return（那样音频永远漂移）",
        )

    def test_tick_audio_sync_has_no_hard_seek(self):
        body = _js_function_body(self.js, "tickAudioSync")
        self.assertNotIn(
            "currentTime =", body, "音画同步保持小幅 playbackRate 伺服，不做硬 seek"
        )

    def test_playback_rate_servo_kept(self):
        body = _js_function_body(self.js, "tickAudioSync")
        self.assertRegex(
            body, r"setAudioRate|playbackRate", "小幅 playbackRate 伺服要保留"
        )

    def test_audio_queue_bounded_under_one_second(self):
        m = re.search(r"\bvar\s+AUDIO_MAXQ\s*=\s*([0-9]+)\s*;", self.js)
        self.assertIsNotNone(m, "app.js 应有 AUDIO_MAXQ")
        frames = int(m.group(1))
        # AAC 一帧固定 1024 采样；48kHz 下每帧 21.33ms
        seconds = frames * 1024.0 / 48000.0
        self.assertGreater(frames, 0)
        self.assertLess(
            seconds, 1.0, "音频队列上限应收紧到小于约 1 秒（当前约 %.2fs）" % seconds
        )

    def test_audio_still_drops_oldest(self):
        body = _js_function_body(self.js, "feedAudio")
        self.assertIn(
            "audioQueue.shift()", body, "音频队列满了仍然丢最老的（不做等关键帧）"
        )


# ======================================================================
# D. 输入延迟保护：move 走背压丢弃，down/up/按键不受影响
# ======================================================================
class InputBackpressureContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)

    def test_move_backpressure_guard_exists(self):
        body = _js_function_body(self.js, "flushMove")
        self.assertTrue(body, "未找到 function flushMove(...) 函数体")
        self.assertRegex(
            body, r"bufferedAmount", "flushMove 应检查 controlWS.bufferedAmount 背压"
        )
        self.assertRegex(
            self.js,
            r"\bvar\s+MOVE_BACKLOG_BYTES\s*=\s*[0-9]+",
            "app.js 应用常量 MOVE_BACKLOG_BYTES 表达背压阈值",
        )

    def test_backpressure_guard_only_on_move_path(self):
        flush = _js_function_body(self.js, "flushMove")
        total = self.js.count("bufferedAmount")
        self.assertEqual(
            total,
            flush.count("bufferedAmount"),
            "bufferedAmount 背压只允许出现在 move 路径（flushMove）里",
        )
        for fn in ("sendControl", "endPointer"):
            body = _js_function_body(self.js, fn)
            self.assertTrue(body, "未找到 function %s(...) 函数体" % fn)
            self.assertNotIn(
                "bufferedAmount", body, "%s 是离散控制消息，不许被背压丢掉" % fn
            )

    def test_move_keeps_only_latest_per_pointer(self):
        self.assertRegex(
            self.js,
            r"movePending\.set\(e\.pointerId,\s*e\)",
            "pointermove 每个指针只应保留最新一个位置",
        )

    def test_move_flush_uses_frame_loop_not_timeout(self):
        flush = _strip_line_comments(_js_function_body(self.js, "flushMove"))
        self.assertNotIn("setTimeout", flush, "move 路径不许用 setTimeout 人为 debounce")
        self.assertNotIn("await", flush, "move 路径不许用 await 拖时间")

    def test_pointermove_handler_has_no_debounce(self):
        start = self.js.index("'pointermove'")
        end = self.js.index("function endPointer", start)
        region = self.js[start:end]
        self.assertNotIn("setTimeout", region, "pointermove 处理里不许有 debounce")
        self.assertNotRegex(
            region,
            r"setTimeout\([^,]+,\s*(4[0-9]|[5-9][0-9]|[0-9]{3,})\s*\)",
            "pointermove 里不许有 40ms 以上的等待",
        )

    def test_move_drop_is_counted(self):
        body = _js_function_body(self.js, "flushMove")
        self.assertRegex(body, r"moveDropped", "被背压丢掉的 move 应有计数，方便取证")
        self.assertRegex(self.js, r"moveDropped", "DIAG 里应有 moveDropped 字段")


# ======================================================================
# E. 零帧自诊断：「解码器配上了却一帧不出」不能再是静默失败
#    2026-09-22 的 H.265 全黑就是这么暴露出来的：configure 成功、帧照收、
#    画面全黑、控制台安静 —— 用户只能看见黑屏，看不见原因。
# ======================================================================
class ZeroFrameDiagnosisContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)

    def test_zero_frame_window_constant_declared(self):
        m = re.search(r"\bvar\s+ZERO_FRAME_MS\s*=\s*([0-9]+)\s*;", self.js)
        self.assertIsNotNone(
            m, "app.js 应用 ZERO_FRAME_MS 表达「配置成功后多久不出帧算异常」"
        )
        ms = int(m.group(1))
        self.assertGreaterEqual(ms, 2000, "窗口太短会把正常的起播延迟误判成故障")
        self.assertLessEqual(ms, 15000, "窗口太长用户已经等不下去了")

    def test_every_configure_success_arms_the_watchdog(self):
        """两条解码路（WebCodecs / MSE）成功时都必须打点，漏一条就等于没监控。"""
        calls = len(re.findall(r"\bnoteConfigured\s*\(\s*\)", self.js))
        self.assertGreaterEqual(
            calls, 3, "noteConfigured() 应有一处定义 + 两处调用（WebCodecs 与 MSE）"
        )
        finish = _js_function_body(self.js, "finishConfigure")
        self.assertEqual(
            finish.count("noteConfigured()"),
            2,
            "finishConfigure 里两个成功分支都应调用 noteConfigured()",
        )
        self.assertEqual(
            finish.count("configured = true"),
            finish.count("noteConfigured()"),
            "每一处 configured = true 都必须紧跟着打点（有配置成功没被监控到）",
        )

    def test_stat_tick_runs_the_check(self):
        body = _js_function_body(self.js, "statTick")
        self.assertIn("zeroFrameCheck", body, "每秒的 statTick 里应跑零帧检查")

    def test_check_distinguishes_no_frames_from_no_output(self):
        """必须分开「根本没收到帧」（取流问题）和「收到帧却不上屏」（解码问题）。"""
        body = _js_function_body(self.js, "zeroFrameCheck")
        self.assertTrue(body, "未找到 function zeroFrameCheck(...)")
        self.assertIn("zeroRxBase", body, "应拿配置时刻的收帧数当基准，而不是看累计")
        self.assertIn(
            "DIAG.posted", body, "应拿「已上屏帧数」判有没有出画面"
        )
        self.assertIn(
            "ZERO_MIN_RX", body, "帧收得太少时不该甩锅给解码器"
        )

    def test_watchdog_warns_only_once(self):
        """出过帧或报过警就不再重复 —— 起播慢一点就反复刷屏，比不报还烦。"""
        body = _js_function_body(self.js, "zeroFrameCheck")
        self.assertIn("zeroWarned", body, "报警后/出帧后应置位，避免重复刷屏")

    def test_reset_clears_watchdog_state(self):
        body = _js_function_body(self.js, "resetDecoder")
        self.assertIn("STATS.zeroAt", body, "resetDecoder 应清掉零帧监控的打点")


# ======================================================================
# F. 抽屉与弹窗各归各的：关弹窗不许连坐关抽屉，重开抽屉不许丢目录
#    2026-09-22 主人的两个现场：① 点「新建文件夹」文件管理会缩起来；
#    ② 再打开文件管理又回到根目录（他上传的文件看着"不见了"）。
# ======================================================================
class DrawerModalIsolationContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)

    def test_close_modal_does_not_close_drawers(self):
        """关弹窗不能顺手把抽屉关掉 —— 这条 bug 让「新建文件夹」把整个文件管理收走了。"""
        body = _strip_line_comments(_js_function_body(self.js, "closeModal"))
        self.assertTrue(body, "未找到 function closeModal(...)")
        self.assertNotIn(
            "closeDrawers", body,
            "closeModal 不许再调 closeDrawers（modal 与抽屉是两层，互不连坐）",
        )

    def test_escape_still_has_a_way_to_close_drawers(self):
        """去掉连坐之后，Escape 必须仍然能关抽屉（否则用户没法关面板了）。"""
        m = re.search(
            r"if\s*\(\s*openModalId\s*\)\s*closeModal\(\)\s*;\s*else\s*closeDrawers\(\)\s*;",
            self.js,
        )
        self.assertIsNotNone(
            m, "Escape 应保持「有弹窗先关弹窗，没有才关抽屉」"
        )

    def test_opening_file_panel_does_not_reset_unconditionally(self):
        """打开文件面板不许无条件回根目录；只有换设备才重置。"""
        body = _strip_line_comments(_js_function_body(self.js, "setDrawer"))
        self.assertTrue(body, "未找到 function setDrawer(...)")
        self.assertIn(
            "fileCwdSerial", body,
            "setDrawer 打开文件面板时要按 fileCwdSerial 判断是否换了设备",
        )
        self.assertNotIn(
            "fileReset()", body,
            "setDrawer 不许再无条件 fileReset()（会把用户刚进去的目录丢掉）",
        )

    def test_directory_is_tracked_per_device(self):
        """fileCwd 必须记着属于哪台设备，换设备才允许重置。"""
        self.assertRegex(
            self.js, r"\bvar\s+fileCwdSerial\b", "app.js 应有 fileCwdSerial 记录目录归属"
        )
        body = _js_function_body(self.js, "onActiveChanged")
        self.assertIn("fileCwdSerial", body, "换设备时应更新目录归属并回默认目录")

    def test_file_list_records_owner_and_falls_back(self):
        """列目录成功要记归属；目录打不开要能自己退回默认目录，别卡死。"""
        body = _js_function_body(self.js, "fileList")
        self.assertIn("fileCwdSerial = active", body, "列目录成功后应记下归属设备")
        self.assertRegex(
            body, r"fileCwd\s*=\s*'/sdcard'",
            "fileList 打不开时应退回 /sdcard 再试一次",
        )


# ======================================================================
# G. 选完文件必须真的传出去
#    2026-09-22 的现场：点「上传」→选文件→**什么都没发生**（没有请求、没有进度、
#    列表里也没东西）。真浏览器实测（tools/_t144）拿到：change 触发时
#    input.files.length = 1，但 XMLHttpRequest.send 被调用 0 次。
#    原因是 change 处理器先把 this.files 存下来、再 this.value='' 清空 input ——
#    FileList 是**活的**，那个引用跟着空了，fileUpload 收到空列表直接 return。
# ======================================================================
class FilePickContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)

    def _change_handler(self):
        m = re.search(
            r"\$\('filePick'\)\s*\.\s*addEventListener\(\s*'change'\s*,\s*function\s*\([^)]*\)\s*\{",
            self.js,
        )
        # 去注释：这段历史注释里原样写着旧的错误顺序，会把顺序断言带偏
        return _strip_line_comments(_brace_body(self.js, m))

    def test_files_are_copied_before_the_input_is_cleared(self):
        body = self._change_handler()
        self.assertTrue(body, "找不到 #filePick 的 change 处理器")
        copy_at = body.find("slice")
        clear_at = body.find("value = ''")
        self.assertGreater(copy_at, -1, "必须先把 files 拷出来（活 FileList 会被清空）")
        self.assertGreater(clear_at, -1, "清 input 的动作要保留（否则同一个文件选第二次不触发）")
        self.assertLess(
            copy_at, clear_at,
            "复制必须发生在清空之前 —— 顺序反了就等于没上传（这条 bug 就是这么来的）",
        )

    def test_upload_is_never_silently_skipped(self):
        body = _js_function_body(self.js, "fileUpload")
        self.assertTrue(body, "未找到 function fileUpload(...)")
        self.assertNotIn(
            "if (!active || !files || !files.length) return;", body,
            "fileUpload 不许再静默 return —— 点了没反应是没法排查的",
        )
        self.assertIn("setFileMsg", body, "拿不到文件/没选设备都要在界面上留句话")


if __name__ == "__main__":
    unittest.main()

