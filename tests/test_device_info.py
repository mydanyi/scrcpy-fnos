# -*- coding: utf-8 -*-
"""设备状态（连上一次读一遍、存进设备记录、发给界面）的回归契约。

为什么值得单独立一层：这条链路横跨「adb 读系统属性 → 合成 profile → 落进
devices.json → 随 /api/devices 下发 → 设置弹窗只读展示」，其中任何一段静默
失败，用户看到的都是同一个现象 —— 设置里「设备信息」那一栏空着。所以每一段
都要有独立判据，别让「设备没答话」和「我们没存」混成一句话。

判据分层（沿用项目约定）：需要确定性成立的部分（挑哪些字段、空值怎么处理、
合并时旧值会不会被抹掉）全部落在这一层纯逻辑单测里 —— adb、屏幕、编码器
一律打桩，不连设备、不看时序。真机那一层只验「连一次之后记录里真的多了
profile」这一件事。

跑法：python -m unittest discover -s tests
"""

import contextlib
import json
import os
import re
import socket
import socketserver
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

# Windows 兼容：socketserver 只在提供 AF_UNIX 的平台上定义 UnixStreamServer /
# ThreadingUnixStreamServer，而 app/server.py 在模块级继承 ThreadingUnixStreamServer，
# 缺这些名字会让导入直接失败。按标准库的实现方式补个 shim（与 test_lifecycle.py 一致）。
if not hasattr(socketserver, "UnixStreamServer"):

    class UnixStreamServer(socketserver.TCPServer):
        address_family = getattr(socket, "AF_UNIX", socket.AF_INET)

    socketserver.UnixStreamServer = UnixStreamServer

if not hasattr(socketserver, "ThreadingUnixStreamServer"):

    class ThreadingUnixStreamServer(
        socketserver.ThreadingMixIn, socketserver.UnixStreamServer
    ):
        daemon_threads = True

    socketserver.ThreadingUnixStreamServer = ThreadingUnixStreamServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
FRONTEND_DIR = os.path.join(APP_DIR, "frontend")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import server    # noqa: E402
import session   # noqa: E402

APP_JS = os.path.join(FRONTEND_DIR, "app.js")
INDEX_HTML = os.path.join(FRONTEND_DIR, "index.html")


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# 一份真实的 `getprop` dump（redroid / 真机的输出格式都一样：[key]: [value]）。
# 刻意混进几条我们**不要**的属性，用来验证「挑」这件事，而不是「全存」。
GETPROP_DUMP = b"""\
[ro.build.date.utc]: [1690000000]
[ro.product.brand]: [Redmi]
[ro.product.manufacturer]: [Xiaomi]
[ro.product.model]: [M2101K9C]
[ro.product.name]: [ares]
[ro.product.device]: [ares]
[ro.product.board]: [ares]
[ro.build.version.release]: [13]
[ro.build.version.sdk]: [33]
[ro.build.id]: [TKQ1.221114.001]
[ro.build.type]: [user]
[ro.build.version.security_patch]: [2023-05-01]
[ro.product.cpu.abi]: [arm64-v8a]
[ro.soc.model]: [MT6893]
[ro.build.fingerprint]: [Redmi/ares/ares:13/TKQ1.221114.001/V14.0.5.0:user/release-keys]
[ro.debuggable]: [0]
[persist.sys.locale]: []
"""

PROFILE_PROPS_FROM_DUMP = {
    "brand": "Redmi",
    "manufacturer": "Xiaomi",
    "model": "M2101K9C",
    "product": "ares",
    "device": "ares",
    "board": "ares",
    "androidVersion": "13",
    "sdk": "33",
    "buildId": "TKQ1.221114.001",
    "buildType": "user",
    "securityPatch": "2023-05-01",
    "abi": "arm64-v8a",
    "soc": "MT6893",
    "fingerprint": "Redmi/ares/ares:13/TKQ1.221114.001/V14.0.5.0:user/release-keys",
}

SCREEN = {"width": 1080, "height": 2400, "density": 440}


class _FakeAdb:
    """替掉 adbtool.run：不碰真 adb，只回一段预置输出。"""

    def __init__(self, stdout=b"", raises=None):
        self.stdout = stdout
        self.raises = raises
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(stdout=self.stdout, stderr=b"", returncode=0)


@contextlib.contextmanager
def _fake_adb(stdout=b"", raises=None):
    fake = _FakeAdb(stdout, raises)
    with mock.patch.object(session.adbtool, "run", fake.run):
        yield fake


@contextlib.contextmanager
def _patched_profile_deps(props, screen=SCREEN, encoders=None, probe_calls=None):
    """把 device_profile 的三条外部依赖全打桩（adb 属性 / 屏幕 / 编码器探测）。"""
    def _probe(serial, jar_path=None, force=False):
        if probe_calls is not None:
            probe_calls.append(serial)
        return list(encoders or [])

    with mock.patch.object(session, "read_props",
                           lambda serial, *a, **k: dict(props or {})), \
            mock.patch.object(session, "device_screen",
                              lambda serial: (dict(screen) if screen else None)), \
            mock.patch.object(session, "video_encoder_summary", _probe):
        yield


@contextlib.contextmanager
def _temp_records(initial=None):
    """把设备记录指到临时文件 —— 绝不碰机器上那份真的 devices.json。"""
    tmpdir = tempfile.mkdtemp(prefix="scrcpy-records-")
    path = os.path.join(tmpdir, "devices.json")
    if initial is not None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(initial, fh, ensure_ascii=False)
    try:
        with mock.patch.object(server, "RECORDS_PATH", path):
            yield path
    finally:
        for name in (os.path.basename(path), os.path.basename(path) + ".tmp"):
            try:
                os.remove(os.path.join(tmpdir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


# ======================================================================
# 1. 读系统属性：一次 dump、本地挑字段
# ======================================================================
class ReadPropsTests(unittest.TestCase):
    def test_one_dump_is_parsed_into_the_wanted_fields(self):
        """一次 `getprop` 就能把要的字段全读回来（不是一条一条问设备）。"""
        with _fake_adb(GETPROP_DUMP) as fake:
            got = session.read_props("S1")
        self.assertEqual(got, PROFILE_PROPS_FROM_DUMP)
        self.assertEqual(len(fake.calls), 1, "读属性只该跑一条 adb 命令")
        self.assertEqual(list(fake.calls[0]), ["-s", "S1", "shell", "getprop"])

    def test_unwanted_and_empty_props_are_dropped(self):
        """dump 里那几百条不能整份存下来；空调也是空，不占位。"""
        with _fake_adb(GETPROP_DUMP):
            got = session.read_props("S1")
        self.assertNotIn("date.utc", " ".join(got.keys()))
        self.assertNotIn("deb", " ".join(got.keys()))
        self.assertFalse([k for k, v in got.items() if not v], "空值不该出现在结果里")
        # 键名用的是我们自己的短名，不是 ro.* 全名（界面要直接用）
        self.assertIn("androidVersion", got)
        self.assertNotIn("ro.build.version.release", got)

    def test_no_output_means_empty_not_fake(self):
        """设备没答话（离线 / 命令报错）时返回空 —— 不能编出一份「未知」的档案。"""
        with _fake_adb(b"error: device offline\n"):
            self.assertEqual(session.read_props("S1"), {})

    def test_adb_exception_is_swallowed(self):
        """读属性失败不许把会话带崩。"""
        with _fake_adb(raises=RuntimeError("adb 挂了")):
            self.assertEqual(session.read_props("S1"), {})

    def test_custom_key_list_is_honoured(self):
        with _fake_adb(GETPROP_DUMP):
            got = session.read_props("S1", keys=(("model", "ro.product.model"),))
        self.assertEqual(got, {"model": "M2101K9C"})


# ======================================================================
# 2. 合成设备状态：属性 + 屏幕 + 编码器
# ======================================================================
class DeviceProfileTests(unittest.TestCase):
    def test_full_profile_combines_props_screen_and_encoders(self):
        with _patched_profile_deps(PROFILE_PROPS_FROM_DUMP):
            prof = session.device_profile("S1", encoders=["h264", "h265"],
                                          log=lambda *a: None)
        self.assertEqual(prof["model"], "M2101K9C")
        self.assertEqual(prof["brand"], "Redmi")
        self.assertEqual(prof["androidVersion"], "13")
        self.assertEqual(prof["sdk"], 33)
        self.assertIsInstance(prof["sdk"], int, "SDK 号要能直接用来比大小")
        self.assertEqual((prof["width"], prof["height"], prof["density"]), (1080, 2400, 440))
        self.assertEqual(prof["encoders"], ["h264", "h265"])
        self.assertIsInstance(prof["updatedAt"], int)
        self.assertLessEqual(abs(time.time() - prof["updatedAt"]), 5)

    def test_screen_source_is_device_not_session(self):
        """屏幕尺寸走 device_screen（wm size），不是会话报上来的缩放后尺寸。"""
        with _patched_profile_deps({"model": "X"}, screen=SCREEN):
            prof = session.device_profile("S1", encoders=["h264"], log=lambda *a: None)
        self.assertEqual(prof["width"], 1080)
        self.assertEqual(prof["height"], 2400)

    def test_missing_screen_is_not_fatal(self):
        with _patched_profile_deps(PROFILE_PROPS_FROM_DUMP, screen=None):
            prof = session.device_profile("S1", encoders=["h264"], log=lambda *a: None)
        self.assertEqual(prof["model"], "M2101K9C")
        self.assertNotIn("width", prof, "拿不到屏幕就别写个 0 进去")
        self.assertNotIn("height", prof)

    def test_nothing_read_returns_empty(self):
        """全都没读到 → 返回 {}，调用方据此不写记录（别把旧状态覆盖成空）。"""
        with _patched_profile_deps({}, screen=None):
            self.assertEqual(
                session.device_profile("S1", encoders=None, log=lambda *a: None), {}
            )

    def test_provided_encoders_skip_the_expensive_probe(self):
        """会话启动时已经探过编码器（那一下要好几秒），别为了记录再探一遍。"""
        calls = []
        with _patched_profile_deps(PROFILE_PROPS_FROM_DUMP, encoders=["h264"],
                                   probe_calls=calls):
            session.device_profile("S1", jar_path="/tmp/jar",
                                   encoders=["h264"], log=lambda *a: None)
        self.assertEqual(calls, [], "传了 encoders 就不该再跑一次探测")

    def test_no_encoders_provided_falls_back_to_probe(self):
        calls = []
        with _patched_profile_deps(PROFILE_PROPS_FROM_DUMP, encoders=["h264", "vp9"],
                                   probe_calls=calls):
            prof = session.device_profile("S1", jar_path="/tmp/jar",
                                          encoders=None, log=lambda *a: None)
        self.assertEqual(calls, ["S1"])
        self.assertEqual(prof["encoders"], ["h264", "vp9"])

    def test_screen_failure_does_not_kill_the_rest(self):
        with mock.patch.object(session, "read_props",
                               lambda s, *a, **k: dict(PROFILE_PROPS_FROM_DUMP)), \
                mock.patch.object(session, "device_screen",
                                  side_effect=RuntimeError("wm 炸了")):
            prof = session.device_profile("S1", encoders=["h264"], log=lambda *a: None)
        self.assertEqual(prof["model"], "M2101K9C")

    def test_summary_line_is_human_readable(self):
        line = session.profile_summary({
            "brand": "Redmi", "model": "M2101K9C", "androidVersion": "13",
            "width": 1080, "height": 2400, "encoders": ["h264", "h265"],
        })
        self.assertEqual(line, "Redmi M2101K9C · Android 13 · 1080x2400 · h264/h265")
        self.assertEqual(session.profile_summary({}), "")
        self.assertEqual(session.profile_summary(None), "")


# ======================================================================
# 3. 合并语义：新值覆盖、空值不许抹掉旧值
# ======================================================================
class MergeProfileTests(unittest.TestCase):
    def test_non_empty_values_overwrite(self):
        merged = server.merge_profile({"model": "旧", "sdk": 33}, {"model": "新"})
        self.assertEqual(merged["model"], "新")
        self.assertEqual(merged["sdk"], 33, "没提到的字段要留着")

    def test_empty_values_never_wipe_old_data(self):
        """读回半截（设备刚被拔、shell 超时）不能把上次好好的状态清成空。"""
        old = {"model": "M2101K9C", "encoders": ["h264"], "width": 1080}
        merged = server.merge_profile(
            old, {"model": "", "encoders": [], "width": None, "density": {}})
        self.assertEqual(merged, old)

    def test_returns_a_copy(self):
        old = {"model": "A"}
        merged = server.merge_profile(old, {"brand": "B"})
        self.assertIsNot(merged, old)
        self.assertEqual(old, {"model": "A"}, "别就地改调用方那份")

    def test_none_inputs_are_safe(self):
        self.assertEqual(server.merge_profile(None, None), {})
        self.assertEqual(server.merge_profile(None, {"model": "X"}), {"model": "X"})


# ======================================================================
# 4. 落盘：devices.json 里真的存住了，而且下次只更新读到的那些
# ======================================================================
class RecordProfileTests(unittest.TestCase):
    def test_profile_is_persisted_and_read_back(self):
        with _temp_records():
            server.record_add("S1", "我的手机",
                              profile={"model": "M2101K9C", "sdk": 33})
            got = server._load_records()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "我的手机")
        self.assertEqual(got[0]["profile"], {"model": "M2101K9C", "sdk": 33})

    def test_reconnect_merges_instead_of_replacing(self):
        with _temp_records():
            server.record_add("S1", profile={"model": "M2101K9C", "sdk": 33,
                                             "encoders": ["h264"]})
            server.record_add("S1", profile={"androidVersion": "14", "sdk": 34})
            got = server._load_records()[0]["profile"]
        self.assertEqual(got["sdk"], 34, "新读到要覆盖旧的")
        self.assertEqual(got["androidVersion"], "14")
        self.assertEqual(got["model"], "M2101K9C", "这次没读到的要留着")
        self.assertEqual(got["encoders"], ["h264"])

    def test_adding_again_keeps_the_name_untouched(self):
        """补设备状态不能顺手把用户起的名字冲掉（record_add 的老语义要保住）。"""
        with _temp_records():
            server.record_add("S1", "客厅那台")
            server.record_add("S1", profile={"model": "X"})
            rec = server._load_records()[0]
        self.assertEqual(rec["name"], "客厅那台")
        self.assertEqual(rec["profile"], {"model": "X"})

    def test_no_profile_writes_no_profile_key(self):
        with _temp_records():
            server.record_add("S1")
            server.record_add("S2", profile={})
            recs = server._load_records()
        self.assertEqual([r["serial"] for r in recs], ["S1", "S2"])
        self.assertTrue(all("profile" not in r for r in recs),
                        "没读到就别写一个空 profile 进去")

    def test_garbage_profile_in_file_is_ignored(self):
        """别人手改过 devices.json（profile 写成了字符串）不能把列表带崩。"""
        with _temp_records([{"serial": "S1", "name": "x", "addedAt": 0,
                             "profile": "oops"}]):
            recs = server._load_records()
        self.assertEqual(recs[0]["serial"], "S1")
        self.assertNotIn("profile", recs[0])

    def test_rename_of_session_only_device_keeps_working(self):
        with _temp_records():
            server.record_rename("S9", "临时")
            recs = server._load_records()
        self.assertEqual(recs[0]["serial"], "S9")
        self.assertEqual(recs[0]["name"], "临时")


# ======================================================================
# 5. 下发：/api/devices 的每一行都带 profile（没读到就是空对象，不是缺字段）
# ======================================================================
class DevicesPayloadTests(unittest.TestCase):
    def setUp(self):
        self.handler = server.Handler.__new__(server.Handler)   # 只借方法，不建连接

    def _rows(self, records):
        with _temp_records(records), mock.patch.dict(server.SESSIONS, {}, clear=True):
            return self.handler._devices()

    def test_row_carries_the_saved_profile(self):
        rows = self._rows([{"serial": "S1", "name": "手机", "addedAt": 0,
                            "profile": {"model": "M2101K9C", "sdk": 33}}])
        self.assertEqual(rows[0]["profile"], {"model": "M2101K9C", "sdk": 33})

    def test_row_without_profile_gets_an_empty_object(self):
        """界面只认 row.profile —— 缺这个键会让它整段崩掉，必须恒有。"""
        rows = self._rows([{"serial": "S1", "name": "手机", "addedAt": 0}])
        self.assertIn("profile", rows[0])
        self.assertEqual(rows[0]["profile"], {})

    def test_session_only_device_also_has_the_key(self):
        entry = server.SessionEntry("S2", {})
        with _temp_records([]), mock.patch.dict(server.SESSIONS, {"S2": entry}):
            rows = self.handler._devices()
        self.assertEqual([r["serial"] for r in rows], ["S2"])
        self.assertEqual(rows[0]["profile"], {})


# ======================================================================
# 6. 前端契约：设置弹窗里那一节真的存在、真的从记录里取数
# ======================================================================
class DeviceInfoUiContractTests(unittest.TestCase):
    def setUp(self):
        self.js = _read(APP_JS)
        self.html = _read(INDEX_HTML)

    def test_modal_has_the_section(self):
        self.assertIn('id="devInfo"', self.html, "设置弹窗里要有「设备信息」的容器")
        self.assertRegex(
            self.html, r'<div class="sec">设备信息</div>',
            "要有「设备信息」这一节的小标题",
        )

    def test_rows_render_from_the_saved_profile(self):
        m = re.search(r"function\s+renderDevInfo\s*\([^)]*\)\s*\{", self.js)
        self.assertIsNotNone(m, "未找到 function renderDevInfo()")
        body = self.js[m.start():m.start() + 1800]
        self.assertIn(".profile", body, "要读设备记录里的 profile，不能另问一遍设备")
        self.assertIn("devInfo", body, "要渲染到 #devInfo 里")

    def test_opening_settings_fills_the_section(self):
        m = re.search(r"function\s+openSettings\s*\([^)]*\)\s*\{", self.js)
        self.assertIsNotNone(m, "未找到 function openSettings()")
        body = self.js[m.start():m.start() + 1200]
        self.assertIn("renderDevInfo()", body, "打开设置就该把设备信息填上")

    def test_empty_state_is_stated_not_faked(self):
        m = re.search(r"function\s+renderDevInfo\s*\([^)]*\)\s*\{", self.js)
        body = self.js[m.start():m.start() + 1800]
        self.assertIn("连上这台设备后会自动读一遍", body,
                      "还没读到时要说明什么时候会有，别摆一行「未知」")

    def test_codec_options_fall_back_to_the_saved_encoders(self):
        """没在投屏（会话 encoders 为空）时，也要用存下来的那份把选项标对。"""
        self.assertRegex(
            self.js, r"profile\s*&&\s*rec\.profile\.encoders",
            "设置里的编码器选项应能用设备记录里存的那份兜底",
        )

    def test_codec_names_are_written_the_way_the_dropdown_writes_them(self):
        """下拉框写 H.264、提示里却写 H264 —— 同一件事两种写法，像两个东西。"""
        m = re.search(r"function\s+codecLabel\s*\([^)]*\)\s*\{", self.js)
        self.assertIsNotNone(m, "应有统一的编码器显示名函数 codecLabel")
        body = self.js[m.start():m.start() + 400]
        self.assertIn("H.264", body)
        self.assertIn("H.265", body)
        self.assertGreater(body.find("H.264"), -1)
        self.assertLess(
            body.find("H.264"), body.find("toUpperCase()"),
            "h264 必须在走「其余一律大写」那条兜底之前被拦下来",
        )

    def test_encoder_lists_go_through_the_same_label_helper(self):
        """编码器清单一律走 codecLabel，不许各写各的。"""
        m = re.search(r"function\s+devInfoRows\s*\([^)]*\)\s*\{", self.js)
        body = self.js[m.start():m.start() + 2000]
        self.assertIn("map(codecLabel)", body, "「设备信息」里的编码器要用 codecLabel")
        m2 = re.search(r"function\s+openSettings\s*\([^)]*\)\s*\{", self.js)
        body2 = self.js[m2.start():m2.start() + 3000]
        self.assertIn("map(codecLabel)", body2, "「编码协议」那栏的提示也要用 codecLabel")


if __name__ == "__main__":
    unittest.main()
