import contextlib
import os
import socket
import socketserver
import sys
import time
import unittest
from unittest import mock

# Windows 兼容：socketserver 仅在提供 AF_UNIX 的平台上定义 UnixStreamServer /
# ThreadingUnixStreamServer，而 app/server.py 在模块级继承 ThreadingUnixStreamServer，
# 缺少这些名字会导致导入直接失败。这里按标准库的实现方式补齐 shim：
# 直接复用 TCPServer / ThreadingMixIn，只把地址族换成 AF_UNIX（绑定时即临时 Unix socket），
# 不改变任何生产代码，也不影响生命周期断言。
if not hasattr(socketserver, "UnixStreamServer"):

    class UnixStreamServer(socketserver.TCPServer):
        # 与标准库 socketserver.UnixStreamServer 等价：仅替换地址族。
        # 没有 AF_UNIX 时退回 AF_INET，保证类仍可定义（此测试只用它完成导入）。
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

import server  # noqa: E402


# 短 grace：断开后应很快触发停止；重连窗口要明显小于等待时间。
GRACE_SECONDS = 0.05
STOP_WAIT = 2.0
RECONNECT_SETTLE = GRACE_SECONDS * 4


def _make_entry():
    """构造一个 SessionEntry。"""
    return server.SessionEntry("test-session", {})


@contextlib.contextmanager
def _short_grace():
    """把服务端最后一个客户端 grace 缩短为可覆盖的短值。"""
    with mock.patch.object(
        server, "CLIENT_GRACE_SECONDS", GRACE_SECONDS, create=True
    ):
        yield


class SessionLifecycleTests(unittest.TestCase):
    def test_last_disconnect_stops_session_after_grace(self):
        entry = _make_entry()
        with _short_grace():
            with mock.patch.object(server, "_stop_session", autospec=True) as stop:
                entry.client_connected()
                entry.client_disconnected()

                deadline = time.time() + STOP_WAIT
                while not stop.called and time.time() < deadline:
                    time.sleep(0.005)

                self.assertTrue(
                    stop.called,
                    "最后一个客户端断开后应经过 grace 调用 server._stop_session",
                )

    def test_immediate_reconnect_cancels_pending_stop(self):
        entry = _make_entry()
        with _short_grace():
            with mock.patch.object(server, "_stop_session", autospec=True) as stop:
                entry.client_connected()
                entry.client_disconnected()
                # grace 内立刻重连，应取消挂起的停止。
                entry.client_connected()

                time.sleep(RECONNECT_SETTLE)

                self.assertFalse(
                    stop.called,
                    "grace 内重连应取消挂起的停止，不应调用 server._stop_session",
                )


if __name__ == "__main__":
    unittest.main()
