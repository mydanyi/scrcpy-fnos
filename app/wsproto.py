#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 WebSocket 服务端实现（RFC 6455），只用标准库。

为什么要自己写：飞牛宿主机的 python3 没装 websockets 库，而这个应用要直接跑在
宿主机上、不引入任何第三方依赖。协议本身不复杂，收/发各几十行就够。

只实现服务端需要的那部分：
  - 握手（校验 Sec-WebSocket-Key，回 Accept）
  - 收帧（客户端发来的帧必定带掩码）
  - 发帧（服务端发出的帧不带掩码）
  - ping / pong / close

不支持扩展、压缩和分片消息的聚合 —— 我们自己的前端不会发分片。
"""

import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# 单帧上限：视频帧最大也就几百 KB，4 MiB 已经非常宽松
MAX_FRAME = 1 << 22


class WSError(Exception):
    """连接层面的错误（对端关闭、协议不对），调用方据此结束循环。"""


def accept_key(key):
    """按 RFC 6455 计算 Sec-WebSocket-Accept。"""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake(headers, sock):
    """完成握手。成功返回 WebSocket，失败返回 None（调用方自己回 400）。"""
    key = headers.get("sec-websocket-key")
    if not key or (headers.get("upgrade") or "").lower() != "websocket":
        return None
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: " + accept_key(key) + "\r\n"
        "\r\n"
    )
    sock.sendall(resp.encode("ascii"))
    return WebSocket(sock)


class WebSocket:
    """在一个已经完成握手的 socket 上收发帧。"""

    def __init__(self, sock):
        self.sock = sock
        self.closed = False

    # ---------- 发送 ----------

    def _send_frame(self, opcode, payload):
        if self.closed:
            return
        n = len(payload)
        header = bytearray()
        header.append(0x80 | opcode)          # FIN + opcode
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        try:
            self.sock.sendall(bytes(header) + payload)
        except OSError:
            self.closed = True

    def send_bytes(self, data):
        self._send_frame(OP_BIN, data)

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, data=b""):
        self._send_frame(OP_PING, data)

    def close(self, code=1000):
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", code))
        finally:
            self.closed = True

    # ---------- 接收 ----------

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise WSError("connection closed")
            buf += chunk
        return bytes(buf)

    def recv(self):
        """收一帧数据。

        控制帧（ping/pong/close）在这里就地处理掉，不返回给调用方。
        返回 (opcode, payload)，其中 opcode 只会是 OP_TEXT 或 OP_BIN。
        """
        while True:
            head = self._recv_exact(2)
            b0, b1 = head[0], head[1]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            if length > MAX_FRAME:
                raise WSError("frame too large: %d" % length)

            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(payload[i] ^ mask[i & 3] for i in range(length))

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.close()
                raise WSError("closed by peer")
            if opcode == OP_CONT:
                raise WSError("continuation frame not supported")
            if not fin:
                raise WSError("fragmented message not supported")
            return opcode, payload
