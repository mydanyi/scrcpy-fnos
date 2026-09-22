# -*- coding: utf-8 -*-
"""adblink 的纯逻辑契约：通路选择 / 转发端口解析 / 错误人话化 / 收尾清理。

**不连设备、不走网络**：`adbtool.run` 一律 mock；需要真 socket 的地方只用
`socket.socketpair()`（本机回环，不经网卡）。

覆盖的是这轮改造最要紧的几条判断，任何一条被改坏都应该在这里红掉：

  1. `A_STLS` 的常量值 = 线上那 4 个字节按小端读出的 uint32（"STLS" → 0x534C5453），
     而且握手撞上它时抛出的必须是**能照做**的说明，不是"握手失败 0x…"。
  2. 通路选择：无线/TLS 设备（或认不出 IP 的 serial）一律判给真 adb；结果带缓存，
     `refresh` / `forget` 能作废。
  3. `ForwardLink` 用 `tcp:0` 让 adb 自己分端口、并把端口号解析回来；失败时给人话。
  4. `read()` 超时返回 None（调用方靠它循环），**流断了才抛**。
  5. 清残留只清 `localabstract:scrcpy_*` —— 不许误伤别人的转发。
  6. session 不再自己开 socket（结构断言：源码里不许再出现 adbproto.AdbClient）。
"""

import os
import re
import socket
import struct
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import adblink   # noqa: E402
import adbproto  # noqa: E402


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeSock:
    """喂给 AdbClient 的假 socket：recv 按预先切好的块吐，用完返回空（EOF）。"""

    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def settimeout(self, _t):
        pass

    def close(self):
        self.closed = True


def _packet(cmd, arg0=0, arg1=0, data=b""):
    return struct.pack("<IIIIII", cmd, arg0, arg1, len(data), 0,
                       cmd ^ 0xFFFFFFFF) + data


class TestStls(unittest.TestCase):
    def test_constant_is_the_wire_value(self):
        # 线上是 4 个 ASCII 字节 "STLS"，按小端读出的 uint32 就是 0x534C5453。
        # 写成 0x53544C53（按字符顺序拼）就永远判不出来 —— 这正是当年 `OKAY` 踩过的坑。
        self.assertEqual(adbproto.CMD_STLS, 0x534C5453)
        self.assertEqual(adbproto.CMD_STLS, adblink.A_STLS)
        self.assertEqual(adbproto.CMD_STLS.to_bytes(4, "little"), b"STLS")

    def test_handshake_gives_actionable_message(self):
        fake = _FakeSock([_packet(adbproto.CMD_STLS, arg0=0x01000000)])
        with mock.patch("socket.create_connection", return_value=fake):
            c = adbproto.AdbClient("1.2.3.4", 5555, timeout=1.0)
            with self.assertRaises(adbproto.AdbError) as cm:
                c.connect()
        msg = str(cm.exception)
        self.assertIn("TLS", msg)
        self.assertIn("0x534c5453", msg.lower())
        # 只报"握手失败"是没用的，必须指明该往哪走
        self.assertIn("adblink", msg)

    def test_normal_cnxn_still_passes(self):
        fake = _FakeSock([_packet(adbproto.CMD_CNXN, arg0=0x01000001)])
        with mock.patch("socket.create_connection", return_value=fake):
            c = adbproto.AdbClient("1.2.3.4", 5555, timeout=1.0)
            self.assertTrue(c.connect())


class TestNeedsRealAdb(unittest.TestCase):
    def setUp(self):
        adblink._NEEDS_CLI.clear()

    def tearDown(self):
        adblink._NEEDS_CLI.clear()

    def test_emulator_name_goes_to_real_adb(self):
        # `emulator-5554` 没有 TCP 地址可用，自己连不上 —— 必须交给真 adb。
        self.assertTrue(adblink.needs_real_adb("emulator-5554"))

    def test_unresolvable_host_goes_to_real_adb(self):
        # `emulator-5554:5555` 这种残留条目，host 解析出来不是 IP，
        # 拿去解析 DNS 必炸（复审抓出来的）。不许走到 socket 那一步。
        self.assertTrue(adblink.needs_real_adb("emulator-5554:5555"))

    def test_result_is_cached_and_refreshable(self):
        with mock.patch.object(adblink, "_probe", return_value=True) as m:
            self.assertTrue(adblink.needs_real_adb("1.2.3.4:5555"))
            self.assertTrue(adblink.needs_real_adb("1.2.3.4:5555"))
            self.assertEqual(m.call_count, 1, "同一个 serial 不该探测两次")
            adblink.needs_real_adb("1.2.3.4:5555", refresh=True)
            self.assertEqual(m.call_count, 2)
        adblink.forget("1.2.3.4:5555")
        self.assertNotIn("1.2.3.4:5555", adblink._NEEDS_CLI)


class TestHumanize(unittest.TestCase):
    def test_device_not_found_with_quotes(self):
        # 真实原文带引号：device '...' not found —— 整串匹配会漏（踩过）
        out = adblink.humanize("adb: error: failed to get feature set: "
                               "device '192.168.1.100:41449' not found")
        self.assertIn("配对", out)

    def test_failed_to_connect(self):
        out = adblink.humanize("failed to connect to 192.168.1.100:41449")
        self.assertIn("连接端口", out)

    def test_offline_and_unauthorized(self):
        self.assertIn("离线", adblink.humanize("error: device offline"))
        self.assertIn("未授权", adblink.humanize("error: device unauthorized"))

    def test_unknown_text_passes_through(self):
        self.assertEqual(adblink.humanize("something weird"), "something weird")
        self.assertEqual(adblink.humanize(""), "")


class TestForwardLink(unittest.TestCase):
    def _link(self):
        return adblink.ForwardLink("1.2.3.4:5555", log=lambda *a: None)

    def test_port_is_asked_from_adb_not_chosen_by_us(self):
        # tcp:0 = 让 adb 自己分端口；它把端口号打到 stdout
        link = self._link()
        with mock.patch.object(adblink.adbtool, "run",
                               return_value=_FakeProc(stdout=b"27401\n")) as m:
            self.assertEqual(link.open_for("scrcpy_5a5a5a5a"), 27401)
        argv = list(m.call_args.args)
        self.assertIn("tcp:0", argv)
        self.assertIn("localabstract:scrcpy_5a5a5a5a", argv)
        self.assertEqual(link.port, 27401)

    def test_open_for_does_not_add_a_prefix_of_its_own(self):
        # 名字原样用 —— 底层**不补**前缀。补错了 adb 也不报错（forward 不校验目标），
        # 只会在读的时候表现成「流已结束」，非常难查。
        link = self._link()
        with mock.patch.object(adblink.adbtool, "run",
                               return_value=_FakeProc(stdout=b"27401\n")) as m:
            link.open_for("whatever_socket")
        argv = list(m.call_args.args)
        self.assertIn("localabstract:whatever_socket", argv)
        self.assertNotIn("localabstract:scrcpy_whatever_socket", argv)

    def test_failure_becomes_human(self):
        link = self._link()
        with mock.patch.object(
                adblink.adbtool, "run",
                return_value=_FakeProc(stdout=b"",
                                       stderr=b"adb: device '1.2.3.4:5555' not found",
                                       returncode=1)):
            with self.assertRaises(adblink.AdbLinkError) as cm:
                link.open_for("scrcpy_x")
        self.assertIn("配对", str(cm.exception))

    def test_garbage_output_is_reported_not_guessed(self):
        link = self._link()
        with mock.patch.object(adblink.adbtool, "run",
                               return_value=_FakeProc(stdout=b"huh?\n")):
            with self.assertRaises(adblink.AdbLinkError):
                link.open_for("scrcpy_x")

    def test_close_removes_the_forward(self):
        link = self._link()
        calls = []

        def fake_run(*a, **k):
            calls.append(a)
            return _FakeProc(stdout=b"27401\n")

        with mock.patch.object(adblink.adbtool, "run", side_effect=fake_run):
            link.open_for("scrcpy_abc")
            link.close()
        removals = [c for c in calls if "--remove" in c]
        self.assertEqual(len(removals), 1)
        self.assertIn("tcp:27401", removals[0])
        self.assertIsNone(link.port)

    def test_read_timeout_is_none_and_eof_raises(self):
        a, b = socket.socketpair()
        try:
            link = self._link()
            # 没人写 → 超时返回 None（调用方靠它做循环，不能当成错误）
            self.assertIsNone(link.read(a, timeout=0.15))
            b.sendall(b"hello")
            self.assertEqual(link.read(a, timeout=2.0), b"hello")
            b.close()
            # 对端关了 → 必须抛，别让上层以为只是"暂时没数据"
            with self.assertRaises(adblink.AdbLinkError):
                link.read(a, timeout=2.0)
        finally:
            a.close()
            try:
                b.close()
            except OSError:
                pass


class TestCleanupStaleForwards(unittest.TestCase):
    def test_only_our_own_forwards_are_removed(self):
        listing = (b"1.2.3.4:5555 tcp:27401 localabstract:scrcpy_5a5a5a5a\n"
                   b"1.2.3.4:5555 tcp:27402 tcp:8080\n"
                   b"1.2.3.4:5555 tcp:27403 localabstract:someone_elses_thing\n")
        calls = []

        def fake_run(*a, **k):
            calls.append(a)
            if "--list" in a:
                return _FakeProc(stdout=listing)
            return _FakeProc(stdout=b"")

        with mock.patch.object(adblink.adbtool, "run", side_effect=fake_run):
            n = adblink.cleanup_stale_forwards("1.2.3.4:5555")
        self.assertEqual(n, 1)
        removals = [c for c in calls if "--remove" in c]
        self.assertEqual(len(removals), 1)
        self.assertIn("tcp:27401", removals[0])

    def test_another_devices_forward_is_left_alone(self):
        # `forward --list` 不受 -s 约束（37.0.1 实测会列出别的设备）。
        # 连 B 的时候绝不能把 A 的转发清掉，否则 A 正在跑的流会断。
        listing = (b"5.6.7.8:5555 tcp:27401 localabstract:scrcpy_aaaaaaaa\n"
                   b"1.2.3.4:5555 tcp:27402 localabstract:scrcpy_bbbbbbbb\n")
        calls = []

        def fake_run(*a, **k):
            calls.append(a)
            if "--list" in a:
                return _FakeProc(stdout=listing)
            return _FakeProc(stdout=b"")

        with mock.patch.object(adblink.adbtool, "run", side_effect=fake_run):
            n = adblink.cleanup_stale_forwards("1.2.3.4:5555")
        self.assertEqual(n, 1)
        removals = [c for c in calls if "--remove" in c]
        self.assertEqual(len(removals), 1)
        self.assertIn("tcp:27402", removals[0])
        self.assertNotIn("tcp:27401", removals[0])


class TestSourceContracts(unittest.TestCase):
    """结构性契约：这次换通路，别哪天又被改回自研协议。"""

    def _src(self, name):
        with open(os.path.join(ROOT, "app", name), encoding="utf-8") as f:
            return f.read()

    def test_session_uses_forward_link(self):
        src = self._src("session.py")
        self.assertIn("adblink.ForwardLink", src)
        self.assertNotIn("adbproto.AdbClient", src)

    def test_session_socket_name_carries_the_prefix_once(self):
        # 设备端 socket 叫 `scrcpy_<scid>`（不是裸 scid）。
        # 名字只在 session 里拼一次，`_wait_socket_ready` 的 grep 直接用整个名字，
        # 别在底层再补一次前缀 —— 补错了两边指向的不是同一个 socket。
        src = self._src("session.py")
        self.assertIn('"scrcpy_%08x" % self.scid', src)
        self.assertNotIn("grep -c 'scrcpy_%s'", src)

    def test_session_connect_is_not_swallowed(self):
        # 连接失败必须往上抛，不能再 check=False 吞掉然后让 push 背锅。
        # 只查「代码」，不查注释/docstring —— 文档里恰恰要写明当年错在哪。
        src = self._src("session.py")
        body = src.split("def _connect_device(self):", 1)[1].split("def ", 1)[0]
        code = re.sub(r'"""(?:.|\n)*?"""', "", body)
        code = re.sub(r"#[^\n]*", "", code)
        self.assertIn("get-state", code)
        self.assertIn("SessionError", code)
        self.assertNotIn("check=False", code)

    def test_fileops_has_no_plaintext_fallthrough(self):
        # TLS 设备上自研明文 sync 一律走不通，fileops 的 _open 必须当场拦下
        src = self._src("fileops.py")
        body = src.split("def _open(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("needs_real_adb", body)

    def test_fileops_routes_tls_devices_to_real_adb(self):
        src = self._src("fileops.py")
        for fn in ("def run_shell", "def sync_push", "def sync_pull", "def sync_stat"):
            seg = src.split(fn, 1)[1].split("\ndef ", 1)[0]
            self.assertIn("needs_real_adb", seg, "%s 少了 TLS 分岔" % fn)

    def test_shellws_has_cli_path(self):
        src = self._src("shellws.py")
        self.assertIn("needs_real_adb", src)
        self.assertIn('"-t", "-t"', src)

    def test_start_failure_stops_the_session(self):
        # 起不来也必须 stop()：不然会话已经建的 adb forward 会永久留在转发
        # 表里（实测踩过），设备端 scrcpy-server 也没人收。
        src = self._src("server.py")
        head = src.split('log("ERROR: %s 启动失败：%s"', 1)[0]
        self.assertIn("dead.stop()", head[-800:])

    def test_no_module_still_claims_forward_is_broken(self):
        # 那条错结论不许留在注释里继续误导人
        for name in ("adbproto.py", "session.py", "adblink.py"):
            src = self._src(name)
            self.assertNotIn("转发不出任何数据", src.replace("以前这里写着", ""))


if __name__ == "__main__":
    unittest.main()
