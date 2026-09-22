#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adb 协议最小客户端：直接和 adbd 对话，把设备端的 localabstract socket 开成一条流。

⚠️ 2026-09-22 更正：以前这里写着"这台机器上 `adb forward ... localabstract:` 转发不出数据，
所以自己说协议"。**那个结论是错的**（当时只开了一条连接造成的假象，见 adblink.py）。
而且自己说协议在**无线调试**上根本走不通 —— Android 11+ 的 adbd 是 **TLS** 的，
会先回一个 `A_STLS`（`0x534C5453`）要求升级连接。

**投屏现在改走 `adb forward`（见 adblink.py），本模块只剩「探测 + 明文设备（容器/模拟器）的退路」用途。**
新代码请优先用 adblink。

设备 `ro.adb.secure=0`（不要求认证）时握手只需要 CNXN，不用做 RSA 签名：

    TCP 连上 adbd → 发 CNXN（带 system identity）→ 收 CNXN
    → 发 OPEN("localabstract:xxx") → 收 OKAY
    → 之后用 WRTE 收发，收到 WRTE 要回 OKAY 做流控
    → 结束时发 CLSE

一条连接上可以同时开多条流（用 local_id 区分），这正是 scrcpy 三个 socket
能连同一个 abstract socket 名的原因。
"""

import queue
import socket
import struct
import threading

# 命令字必须按"小端读出来"的 uint32 来写：协议里是 4 个 ASCII 字节，
# 用小端打包传输。直接由字节推导，免得手抄成"按字符顺序"的值（踩过这个坑：
# OKAY 写成 0x4F4B4159 就一直收不到响应，因为判断永远不成立）。
def _cmd(word):
    return int.from_bytes(word.encode("ascii"), "little")


CMD_CNXN = _cmd("CNXN")
CMD_OPEN = _cmd("OPEN")
CMD_OKAY = _cmd("OKAY")
CMD_CLSE = _cmd("CLSE")
CMD_WRTE = _cmd("WRTE")
CMD_AUTH = _cmd("AUTH")
# 设备要求把连接升级成 TLS。Android 11+ 的**无线调试** adbd 一定会先回这个，
# 明文客户端到这一步就走不下去了（详见 adblink.py 的解释）。
CMD_STLS = _cmd("STLS")

A_VERSION = 0x01000001
MAXDATA = 256 * 1024


class AdbError(Exception):
    pass


def _checksum(data):
    return sum(data) & 0xFFFFFFFF


def _build(cmd, arg0, arg1, data=b""):
    # WRTE 带校验和；其余包校验位填 0（adb 见 0 即跳过校验）
    crc = _checksum(data) if cmd == CMD_WRTE else 0
    header = struct.pack("<IIIIII", cmd, arg0, arg1, len(data), crc,
                         cmd ^ 0xFFFFFFFF)
    return header + data


def split_host_port(serial):
    """把 serial 换算成设备端 adbd 的 (host, port)。

    ⚠️ `emulator-XXXX` 这种本地传输**没有 TCP 地址**，一开始直接拿它当主机名去解析，
    结果就是这个 serial 永远连不上（实测报「adbd 连接已断开」）。
    它其实是有约定的：控制台占 XXXX，adb 占 XXXX+1 ——
    所以 emulator-5554 的 adbd 就在 127.0.0.1:5555。
    """
    if serial.startswith("emulator-"):
        tail = serial[len("emulator-"):]
        if tail.isdigit():
            return "127.0.0.1", int(tail) + 1
    if ":" in serial:
        host, _, port = serial.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            pass
    return serial, 5555


class AdbClient:
    """一条到 adbd 的连接，上面可以开多条流。"""

    def __init__(self, host, port=5555, timeout=15.0, debug=False):
        self.host = host
        self.port = port
        self._timeout = timeout
        self.debug = debug
        self.sock = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._queues = {}       # local_id -> Queue（None 表示流结束）
        self._events = {}       # local_id -> Event（OPEN 结果就绪）
        self._opened = {}       # local_id -> True(成功) / 异常对象(失败)
        self._remote = {}       # local_id -> remote_id
        self._stop = threading.Event()
        self._error = None
        self._reader = None

    # ---------- 底层收发 ----------

    def _send(self, cmd, arg0, arg1, data=b""):
        if self.sock is None:
            raise AdbError("连接未建立")
        if self.debug:
            print("[adb] -> %s a0=%d a1=%d len=%d" % (
                struct.pack("<I", cmd).decode("ascii", "replace"),
                arg0, arg1, len(data)), flush=True)
        self.sock.sendall(_build(cmd, arg0, arg1, data))

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise AdbError("adbd 连接已断开")
            buf += chunk
        return bytes(buf)

    def _recv_packet(self):
        head = self._recv_exact(24)
        cmd, arg0, arg1, length, _crc, magic = struct.unpack("<IIIIII", head)
        if (cmd ^ 0xFFFFFFFF) != magic:
            raise AdbError("包 magic 不匹配，协议错乱")
        data = self._recv_exact(length) if length else b""
        return cmd, arg0, arg1, data

    # ---------- 握手 ----------

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self._timeout)
        self.sock.settimeout(self._timeout)
        identity = b"host::features=shell_v2,cmd,stat_v2,ls_v2,apex,abb,abb_exec" \
                   b",remount_shell,track_app,sendrecv_v2,device_usb,device_banner\x00"
        self._send(CMD_CNXN, A_VERSION, MAXDATA, identity)

        cmd, arg0, _arg1, _data = self._recv_packet()
        if cmd == CMD_AUTH:
            raise AdbError("设备要求 adb 认证（本实现未做 RSA 签名）")
        if cmd == CMD_STLS:
            raise AdbError(
                "设备要求先把连接升级成 TLS（收到 A_STLS 0x%08x）。"
                "Android 11+ 的无线调试 adbd 是加密的，自己直连 adbd 走不通；"
                "投屏走 `adb forward`、文件与终端走真 adb（见 adblink.py）。" % cmd)
        if cmd != CMD_CNXN:
            raise AdbError("握手失败，收到命令 0x%08x" % cmd)

        self.sock.settimeout(None)
        self._reader = threading.Thread(target=self._reader_loop,
                                        name="adb-reader", daemon=True)
        self._reader.start()
        return True

    def _reader_loop(self):
        """统一收包并分发：WRTE 投递到对应流的队列，OKAY/CLSE 唤醒等待者。"""
        try:
            while not self._stop.is_set():
                cmd, arg0, arg1, data = self._recv_packet()
                if self.debug:
                    print("[adb] <- %s a0=%d a1=%d len=%d" % (
                        struct.pack("<I", cmd).decode("ascii", "replace"),
                        arg0, arg1, len(data)), flush=True)

                if cmd == CMD_OKAY:
                    local = arg1
                    if self.debug:
                        print("[adb] OKAY分支: local=%d in_events=%s in_remote=%s events=%s"
                              % (local, local in self._events, local in self._remote,
                                 list(self._events.keys())), flush=True)
                    if local in self._events and local not in self._remote:
                        self._remote[local] = arg0
                        self._opened[local] = True
                        self._events[local].set()
                    elif data:
                        # 对端确认收到我们发的数据，无需处理
                        pass

                elif cmd == CMD_WRTE:
                    local = arg1
                    q = self._queues.get(local)
                    if q is not None and data:
                        q.put(data)
                    # 回 OKAY 做流控。参数顺序按实测来：arg0 是**自己**的 local_id，
                    # arg1 是对端的。写反了对方会认为协议错乱，直接把流关掉。
                    self._send(CMD_OKAY, local, arg0)

                elif cmd == CMD_CLSE:
                    # CLSE 的参数顺序跟 OKAY/WRTE 相反，不能想当然 ——
                    # 认哪个 id 是我们注册过的 local_id 就行。
                    local = arg0 if arg0 in self._events else arg1
                    q = self._queues.get(local)
                    if q is not None:
                        q.put(None)
                    if local in self._events and local not in self._remote:
                        self._opened[local] = AdbError("设备关闭了这条流")
                        self._events[local].set()

        except Exception as exc:
            self._error = exc
            for q in list(self._queues.values()):
                q.put(None)
            for local, ev in list(self._events.items()):
                if local not in self._remote:
                    self._opened[local] = exc
                ev.set()

    # ---------- 流 ----------

    def open(self, destination, timeout=15.0):
        """打开一条到设备端的流（如 localabstract:scrcpy_xxx），返回 local_id。"""
        if self._error is not None:
            raise self._error
        with self._lock:
            local_id = self._next_id
            self._next_id += 1
            ev = threading.Event()
            self._events[local_id] = ev
            self._queues[local_id] = queue.Queue()

        self._send(CMD_OPEN, local_id, 0, destination.encode("utf-8") + b"\x00")
        if not ev.wait(timeout):
            raise AdbError("打开 %s 超时" % destination)
        result = self._opened.get(local_id)
        if result is not True:
            raise AdbError("打开 %s 失败：%s" % (destination, result or "未知原因"))
        return local_id

    def read(self, local_id, timeout=15.0):
        """读一段数据。

        超时返回 None（调用方可以继续等 —— 画面静止时设备端本来就不发帧）；
        流真正结束时抛 AdbError。
        """
        q = self._queues.get(local_id)
        if q is None:
            raise AdbError("流不存在")
        try:
            item = q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise AdbError("流已结束")
        return item

    def write(self, local_id, data):
        """写一段数据。控制指令都很小，直接一次发完。

        参数顺序同 OKAY：arg0 是自己的 local_id，arg1 是对端的。
        """
        if not data:
            return
        remote = self._remote.get(local_id, 0)
        for i in range(0, len(data), MAXDATA):
            self._send(CMD_WRTE, local_id, remote, data[i:i + MAXDATA])

    def close_stream(self, local_id):
        remote = self._remote.get(local_id, 0)
        try:
            self._send(CMD_CLSE, remote, local_id)
        except Exception:
            pass
        self._queues.pop(local_id, None)
        self._events.pop(local_id, None)

    def close(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
