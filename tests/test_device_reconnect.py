# -*- coding: utf-8 -*-
"""「设备掉线要自己捞回来」+「文件操作的错必须留下痕迹」的回归契约。

2026-09-23 现场：黑鲨上推 1.88 GB 的包，收尾 `failed to read copy response: EOF`，
之后这台设备从 `adb devices` 里消失，用户在文件管理里点什么都是
`adb: device '192.168.1.100:41449' not found` —— 一句"设备没找到"完全看不出该干什么。

而**设备其实活着**（ping 通、adbd 端口还开着、`adb connect` 一次就回来、
设备端 adbd 的 pid 一直没换）。只是 adb server 里那条 transport 被判死了。

四条契约：

  1. 掉线 ⇒ 自己 `adb connect` 一次再重试；重连不成才报人话。
     非掉线的错（权限 / 文件不存在 / INSTALL_FAILED_*）**不许**白重连 —— 那会把真原因盖掉。
  2. `adb connect` **绝不能用 capture_output**：常驻的 adb server 会继承管道，
     `communicate()` 永远等不到 EOF，整个调用卡死（`fileops._adb_connect` 踩过）。
  3. 文件管理那几个接口失败必须落日志。以前只回前端，
     info.log 里一个字都搜不到，排查等于从零开始。
  4. 日志自己不能出事：带时间戳、别无限涨，且截断**不许留空洞**。
"""

import contextlib
import io
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

# Windows 兼容：socketserver 只在提供 AF_UNIX 的平台上定义 UnixStreamServer /
# ThreadingUnixStreamServer，而 app/server.py 在模块级继承后者（与 test_device_info.py 一致）。
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
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import adblink   # noqa: E402
import adbtool   # noqa: E402
import fileops   # noqa: E402
import server    # noqa: E402

SERIAL = "192.168.1.100:41449"
NOT_FOUND_ERR = ("adb: error: failed to get feature set: "
                 "device '%s' not found" % SERIAL)
EOF_ERR = "adb: error: failed to read copy response: EOF"

# Windows 上 `os.open` 不给 O_BINARY 就是**文本模式**，会把 \n 悄悄翻成 \r\n
# （断言会以"多了个 \r"的形式红掉，跟被测逻辑毫无关系）。Linux 上没有这个常量。
_BIN = getattr(os, "O_BINARY", 0)


class _Proc(object):
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class LookDisconnectedTests(unittest.TestCase):
    def test_real_phrasing_with_quotes_is_recognized(self):
        self.assertTrue(adblink.looks_disconnected(NOT_FOUND_ERR))

    def test_offline_is_recognized(self):
        self.assertTrue(adblink.looks_disconnected("error: device offline"))
        self.assertTrue(adblink.looks_disconnected(
            "192.168.1.100:41449\t\toffline transport_id:10"))

    def test_other_errors_are_not_disconnects(self):
        """非掉线的错不许被判成掉线 —— 否则白重连一次，还把真原因盖掉。"""
        for text in ("sh: pm: Permission denied",
                     "cp: write error: No space left on device",
                     "Failure [INSTALL_FAILED_USER_RESTRICTED]",
                     "文件或目录不存在：/sdcard/x",
                     "", None):
            self.assertFalse(adblink.looks_disconnected(text),
                             "不该把 %r 当掉线" % (text,))


class ReconnectTests(unittest.TestCase):
    def test_connect_never_captures_output(self):
        """capture_output 会让常驻 adb server 继承管道 → 调用卡死（实测挂满 120 秒）。"""
        seen = {}

        def fake_run(*a, **kw):
            seen.update(kw)
            return _Proc()

        with mock.patch.object(adblink.adbtool, "run", fake_run):
            self.assertTrue(adblink.reconnect(SERIAL))

        self.assertNotIn("capture_output", seen, "绝不能用 capture_output")
        self.assertEqual(seen.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(seen.get("stderr"), subprocess.DEVNULL)
        self.assertEqual(seen.get("stdin"), subprocess.DEVNULL)

    def test_connect_failure_returns_false_instead_of_raising(self):
        def boom(*a, **kw):
            raise OSError("adb 不在")

        with mock.patch.object(adblink.adbtool, "run", boom):
            self.assertFalse(adblink.reconnect(SERIAL))


DEVICES_OUT = ("List of devices attached\n"
               "127.0.0.1:5555\tdevice product:redroid_x86_64\n"
               "%s\toffline transport_id:10\n" % SERIAL).encode()


class DeviceStateTests(unittest.TestCase):
    def test_reads_state_of_the_wanted_serial(self):
        with mock.patch.object(adblink.adbtool, "run", lambda *a, **kw: _Proc(DEVICES_OUT)):
            self.assertEqual(adblink.device_state(SERIAL), "offline")
            self.assertEqual(adblink.device_state("127.0.0.1:5555"), "device")

    def test_serial_not_in_list_is_none(self):
        with mock.patch.object(adblink.adbtool, "run", lambda *a, **kw: _Proc(DEVICES_OUT)):
            self.assertIsNone(adblink.device_state("10.0.0.9:5555"))

    def test_ensure_device_only_reconnects_when_not_ready(self):
        """已经在位的设备不该被多连一次（每次文件操作都多一次 adb 往返很浪费）。"""
        for state, want_connect in (("device", 0), ("offline", 1), (None, 1)):
            with self.subTest(state=state):
                with mock.patch.object(adblink, "device_state", lambda s, **kw: state), \
                     mock.patch.object(adblink, "reconnect") as rc:
                    rc.return_value = True
                    adblink.ensure_device(SERIAL)
                    self.assertEqual(rc.call_count, want_connect)


class ShellAutoRetryTests(unittest.TestCase):
    """`cli_shell_retry`：shell 那条路失败**不抛异常**，只能靠输出文本判。"""

    def _patch(self, replies, reconnect_ok=True):
        seen = {"cmds": [], "connects": 0}

        def fake_cli(serial, cmd, timeout=None):
            seen["cmds"].append(cmd)
            idx = min(len(seen["cmds"]), len(replies)) - 1
            return replies[idx]

        def fake_reconnect(serial, timeout=None):
            seen["connects"] += 1
            return reconnect_ok

        for p in (mock.patch.object(adblink, "cli_shell", fake_cli),
                  mock.patch.object(adblink, "reconnect", fake_reconnect)):
            p.start()
            self.addCleanup(p.stop)
        return seen

    def test_success_is_passed_through_untouched(self):
        seen = self._patch([("ok", "", 0)])
        self.assertEqual(adblink.cli_shell_retry(SERIAL, "ls"), ("ok", "", 0))
        self.assertEqual(seen["connects"], 0)
        self.assertEqual(len(seen["cmds"]), 1)

    def test_disconnect_triggers_reconnect_then_retry(self):
        seen = self._patch([("", NOT_FOUND_ERR, 1), ("ok", "", 0)])
        self.assertEqual(adblink.cli_shell_retry(SERIAL, "ls"), ("ok", "", 0))
        self.assertEqual(seen["connects"], 1, "掉线要自己重连一次")
        self.assertEqual(len(seen["cmds"]), 2, "重连之后要重试")

    def test_still_disconnected_raises_a_readable_message(self):
        self._patch([("", NOT_FOUND_ERR, 1), ("", NOT_FOUND_ERR, 1)])
        with self.assertRaises(adblink.AdbLinkError) as ctx:
            adblink.cli_shell_retry(SERIAL, "ls")
        msg = str(ctx.exception)
        self.assertIn("掉线", msg)
        self.assertIn("无线调试", msg, "要告诉用户能去关掉什么")
        self.assertNotIn("feature set", msg, "别把 adb 原文糊上去")

    def test_reconnect_failure_raises_a_readable_message(self):
        seen = self._patch([("", NOT_FOUND_ERR, 1)], reconnect_ok=False)
        with self.assertRaises(adblink.AdbLinkError) as ctx:
            adblink.cli_shell_retry(SERIAL, "ls")
        self.assertIn("掉线", str(ctx.exception))
        self.assertEqual(seen["connects"], 1)
        self.assertEqual(len(seen["cmds"]), 1, "重连都没成，就别再跑一遍了")

    def test_non_disconnect_failure_is_not_retried(self):
        """真原因（权限 / 文件不存在）必须原样留着，不能被"掉线"盖掉。"""
        seen = self._patch([("", "sh: pm: Permission denied", 1)])
        self.assertEqual(adblink.cli_shell_retry(SERIAL, "pm install"),
                         ("", "sh: pm: Permission denied", 1))
        self.assertEqual(seen["connects"], 0, "这不是掉线，不该重连")
        self.assertEqual(len(seen["cmds"]), 1)


class RunShellWiringTests(unittest.TestCase):
    def test_real_adb_path_goes_through_auto_retry(self):
        seen = []

        def fake_retry(serial, cmd, timeout=None):
            seen.append(cmd)
            return ("x", "", 0)

        with mock.patch.object(fileops.adblink, "needs_real_adb", lambda s: True), \
             mock.patch.object(fileops.adblink, "cli_shell_retry", fake_retry):
            self.assertEqual(fileops.run_shell(SERIAL, "echo x"), ("x", "", 0))
        self.assertEqual(seen, ["echo x"], "真 adb 这条路必须走带自愈的那个版本")

    def test_adb_link_error_becomes_fileop_error_with_human_text(self):
        def boom(*a, **kw):
            raise adblink.AdbLinkError("设备（%s）掉线了，自动重连也没成功" % SERIAL)

        with mock.patch.object(fileops.adblink, "needs_real_adb", lambda s: True), \
             mock.patch.object(fileops.adblink, "cli_shell_retry", boom):
            with self.assertRaises(fileops.FileOpError) as ctx:
                fileops.run_shell(SERIAL, "ls")
        self.assertIn("掉线", str(ctx.exception))


class PushFailureMessageTests(unittest.TestCase):
    def test_midway_disconnect_tells_you_to_upload_again(self):
        with mock.patch.object(fileops.adblink, "reconnect", lambda s, **kw: True):
            msg = fileops._push_failed_human(SERIAL, adblink.AdbLinkError(EOF_ERR))
        self.assertIn("重连", msg)
        self.assertIn("重新上传", msg)

    def test_reconnect_failed_says_what_to_check(self):
        with mock.patch.object(fileops.adblink, "reconnect", lambda s, **kw: False):
            msg = fileops._push_failed_human(SERIAL, adblink.AdbLinkError(EOF_ERR))
        self.assertIn("无线调试", msg)

    def test_other_errors_keep_their_own_reason(self):
        """不是掉线就别往"重连"上扯，原样翻人话。"""
        with mock.patch.object(fileops.adblink, "reconnect") as rc:
            rc.return_value = True
            msg = fileops._push_failed_human(
                SERIAL, adblink.AdbLinkError("error: device unauthorized"))
        self.assertIn("未授权", msg)
        self.assertEqual(rc.call_count, 0, "不是掉线就不该重连")

    def test_upload_never_silently_retries_a_big_file(self):
        """上传不许"重连后自动重试" —— 1.88 GB 静默重传一遍，用户只会更懵。"""
        with open(os.path.join(APP_DIR, "fileops.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _cli_push_stream"):src.index("def sync_push")]
        self.assertIn("cli_push_file", body)
        self.assertEqual(body.count("cli_push_file"), 1,
                         "上传这里只能调一次，别加自动重试")


class FileActionLoggingTests(unittest.TestCase):
    """界面上看得见的错，info.log 里必须搜得到同一句。"""

    def _handler(self):
        h = server.Handler.__new__(server.Handler)
        self.replies = []
        h._json = lambda obj, code=200: self.replies.append(obj)
        return h

    def _run_action(self, path, body, side_effect=None, ret=None):
        h = self._handler()
        logs = []
        with mock.patch.object(server, "log", lambda m: logs.append(str(m))), \
             mock.patch.object(server.fileops, "install_apk", side_effect=side_effect,
                               return_value=ret), \
             mock.patch.object(server.fileops, "delete", side_effect=side_effect,
                               return_value=ret):
            h._api_file_action(path, body)
        return logs, h

    def test_install_failure_is_logged(self):
        logs, _ = self._run_action(
            "/api/files/install", {"serial": SERIAL, "path": "/sdcard/Download/a.apk"},
            side_effect=server.fileops.FileOpError("设备掉线了"))
        self.assertTrue(any("安装失败" in m for m in logs), logs)
        self.assertTrue(any(SERIAL in m for m in logs), logs)
        self.assertTrue(any("/sdcard/Download/a.apk" in m for m in logs), logs)
        self.assertFalse(self.replies[-1]["ok"], "日志记了，前端也得拿到错")

    def test_delete_failure_is_logged_with_the_action_name(self):
        logs, _ = self._run_action(
            "/api/files/delete", {"serial": SERIAL, "path": "/sdcard/x"},
            side_effect=server.fileops.FileOpError("文件或目录不存在"))
        self.assertTrue(any("删除失败" in m for m in logs), logs)

    def test_install_success_is_logged(self):
        logs, _ = self._run_action(
            "/api/files/install", {"serial": SERIAL, "path": "/sdcard/Download/a.apk"},
            ret="安装成功")
        self.assertTrue(any("安装成功" in m for m in logs), logs)

    def test_listing_failure_is_logged(self):
        h = server.Handler.__new__(server.Handler)
        replies = []
        h._json = lambda obj, code=200: replies.append(obj)
        h.path = "/api/files?serial=%s&path=/sdcard/Download" % SERIAL
        h.headers = {}
        logs = []
        with mock.patch.object(server, "log", lambda m: logs.append(str(m))), \
             mock.patch.object(server.fileops, "list_dir",
                               side_effect=server.fileops.FileOpError(NOT_FOUND_ERR)):
            h.do_GET()
        self.assertTrue(any("列目录失败" in m for m in logs), logs)
        self.assertTrue(any(NOT_FOUND_ERR[:20] in m for m in logs),
                        "adb 的原话要留在日志里，不然对不上现场")


class LogHygieneTests(unittest.TestCase):
    def test_log_lines_carry_a_timestamp(self):
        """appcenter 只给它自己那两行启停记录打时间戳，我们不打就只能靠猜。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            server.log("hello")
        line = buf.getvalue().strip()
        self.assertRegex(line,
                         r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[scrcpy-fnos\] hello$")

    def _fd_on(self, path, mode):
        return os.open(path, mode)

    def test_rotates_only_when_over_the_limit(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "info.log")
            with open(p, "wb") as f:
                f.write(b"x" * 100)
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | _BIN)
            try:
                self.assertFalse(server._rotate_log(fd, p, 1000), "没超就别动它")
                self.assertTrue(server._rotate_log(fd, p, 50))
                os.write(fd, b"new\n")
            finally:
                os.close(fd)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"new\n")
            with open(p + ".1", "rb") as f:
                self.assertEqual(f.read(), b"x" * 100, "转之前要留一份完整档")

    def test_truncating_a_non_append_fd_leaves_no_hole(self):
        """fd 不是 O_APPEND 时，不 lseek 就会从原偏移接着写 → 文件变成大空洞。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "info.log")
            with open(p, "wb") as f:
                f.write(b"x" * 100)
            fd = os.open(p, os.O_WRONLY | _BIN)   # 故意不给 O_APPEND
            try:
                os.lseek(fd, 100, os.SEEK_SET)    # 模拟 appcenter 写到末尾
                self.assertTrue(server._rotate_log(fd, p, 50))
                os.write(fd, b"new\n")
            finally:
                os.close(fd)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"new\n", "截断后不许留空洞")

    def test_rotation_failure_never_breaks_logging(self):
        fd = os.open(os.devnull, os.O_WRONLY | _BIN)
        try:
            self.assertFalse(server._rotate_log(fd, "/no/such/dir/x.log", 1))
        finally:
            os.close(fd)


class AdbLogPersistenceTests(unittest.TestCase):
    """adb server 自己的日志是「设备怎么掉线的」唯一现场，不许躺在 /tmp 等重启清空。"""

    def test_tmpdir_goes_into_the_app_data_area(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(adbtool, "adb_home", lambda: d):
                env = adbtool.adb_env()
            self.assertTrue(env["TMPDIR"].endswith("adblog"), env.get("TMPDIR"))
            self.assertTrue(os.path.isdir(env["TMPDIR"]))
            self.assertEqual(env["HOME"], d, "HOME 那条老规矩不能丢")

    def test_tmpdir_is_left_alone_when_home_is_unusable(self):
        """建不出 adblog 就别乱指 TMPDIR —— 指到一个不存在的地方会让 adb 直接起不来。"""
        with mock.patch.object(adbtool, "tmp_dir", lambda: None):
            env = adbtool.adb_env()
        self.assertNotIn("adblog", env.get("TMPDIR") or "")
        self.assertTrue(env["HOME"], "HOME 那条老规矩不能丢")

    def test_legacy_tmp_log_is_archived_at_startup(self):
        with tempfile.TemporaryDirectory() as d:
            legacy_dir = os.path.join(d, "legacy")
            os.makedirs(legacy_dir)
            name = "adb.%d.log" % adbtool._uid()
            with open(os.path.join(legacy_dir, name), "wb") as f:
                f.write(b"transport.cpp:1222 emulator-5554:5555: connection terminated")
            with mock.patch.object(adbtool, "adb_home", lambda: d), \
                 mock.patch.object(adbtool, "LEGACY_TMP", legacy_dir):
                src, dest = adbtool.handle_server_log()
            self.assertTrue(src and dest, "该捞到那份日志")
            self.assertTrue(dest.endswith("adb-server.prev.log"))
            with open(dest, "rb") as f:
                self.assertIn(b"connection terminated", f.read())

    def test_nothing_to_archive_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(adbtool, "adb_home", lambda: d), \
                 mock.patch.object(adbtool, "LEGACY_TMP", os.path.join(d, "nope")):
                self.assertEqual(adbtool.handle_server_log(), (None, None))


if __name__ == "__main__":
    unittest.main()
