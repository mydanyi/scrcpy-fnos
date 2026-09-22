#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备数据通路的统一出口：让**真 adb** 去建连接，我们不再自己直连 adbd。

## 为什么不能再自己说 adb 协议（血泪，2026-09-22 查清）

`adbproto.py` 自己实现了 adb 协议、直连设备的 adbd。这对
`127.0.0.1:5555` 这类**不加密**的 adbd（容器 / 模拟器）没问题，
但对着 **Android 11+ 的无线调试**完全走不通：那条 adbd 是 **TLS** 的 ——
我们发完 `CNXN`，它回的是 `A_STLS`（`0x534C5453`，小端打包正好是 ASCII `STLS`），
要求**先把连接升级成 TLS**、之后才谈 adb 协议。明文客户端在这条路上永远过不去，
对外只报一句没用的「握手失败，收到命令 0x534c5453」。

症状极具误导性：**同一份代码，容器设备一直好使，真手机一接就挂** ——
很容易误判成"手机太新 / 手机的问题"，其实是这条路本身走不通。

## 所以分两类走

- **投屏那三条流**（video / audio / control）：走
  `adb forward tcp:<本地端口> localabstract:<name>` —— 抽象 socket 能转发，
  真 adb 自己会处理 TLS。实测连上后依次收到
  `dummy(1B)` → `设备名(64B)` → `codec_id(4B)` → 真码流。
- **文件 / shell / 终端**：`sync:` / `shell:` 这些是 adbd 的 *service*，
  **不是 socket**，forward 够不着，只能退回
  `adb -s <serial> push / pull / exec-out / shell` 子进程。

## `adb forward` 的两个坑（复审时确认过，别再踩）

1. 设备端 `DesktopConnection.open()` 要**等 video/audio/control 都 accept 完**才返回，
   之后才发设备名。所以**必须按顺序开满**连接数（`audio=false` → 2 条，`true` → 3 条）。
   只开一条只会收到那 1 个 dummy 字节、然后一直读超时 —— 看着就像"转发机制坏了"。
   （旧笔记里「forward ... localabstract: 转发不出数据」的结论就是这么来的，是错的。）
2. **端口让 adb 自己分**（`forward tcp:0` 会把分配到的端口打到 stdout），
   别自己挑，免得撞上别人占用的端口。
"""

import os
import re
import socket
import subprocess
import threading
import time

import adbtool

# 设备端 adbd 要求升级 TLS 的命令字（"STLS" 按小端读出的 uint32）。
# 单独写一份，免得 import adbproto 时又被它那串注释误导。
A_STLS = 0x534C5453

# 探测结果是「这台设备要不要经真 adb」——变不了几回，缓存住别每次都连一次设备。
_NEEDS_CLI = {}
_NEEDS_LOCK = threading.Lock()

# 掉线后重连的等待上限（`adb connect` 本身很快就回，留宽一点防它卡住）。
RECONNECT_TIMEOUT = 15.0


class AdbLinkError(Exception):
    pass


def stls_hint(serial):
    """给「自己直连 adbd 走不通」的一个能直接照做的解释。"""
    return ("这台设备（%s）的 adbd 要求加密连接（Android 11+ 的无线调试就是这种），"
            "不能自己直连——已经改走真 adb 那条路。" % serial)


# ==================== 这台设备要不要经真 adb ====================


def _looks_like_ip(host):
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host or ""))


def _probe(serial, timeout=8.0):
    """发一个 CNXN 看设备怎么回。只有**能跑通明文 adb 协议**的才算不需要真 adb。"""
    import adbproto                      # 只在探测时用，别把依赖铺到模块顶层

    if ":" not in serial:
        return True                      # emulator-5554 这种名字：我们连不上，交给真 adb
    host, port = adbproto.split_host_port(serial)
    if not (_looks_like_ip(host) or host in ("localhost",)):
        return True                      # 解析不出可信 IP（如 "emulator-5554:5555" 这种残留条目）
    c = adbproto.AdbClient(host, port, timeout=timeout)
    try:
        c.connect()
        return False                     # 明文协议走得通 → 用快的自研那条
    except Exception:
        # A_STLS / 需要 AUTH / 离线……一律交给真 adb。它慢一点但什么都能处理。
        return True
    finally:
        try:
            c.close()
        except Exception:
            pass


def needs_real_adb(serial, refresh=False):
    """这台设备是不是必须经真 adb（TLS 的无线调试就会是 True）。结果带缓存。"""
    if not refresh:
        with _NEEDS_LOCK:
            hit = _NEEDS_CLI.get(serial)
        if hit is not None:
            return hit
    verdict = _probe(serial)
    with _NEEDS_LOCK:
        _NEEDS_CLI[serial] = verdict
    return verdict


def forget(serial):
    """设备重连 / 重新配对之后，让缓存失效。"""
    with _NEEDS_LOCK:
        _NEEDS_CLI.pop(serial, None)


# ==================== 投屏三条流：adb forward + 本地 TCP ====================


def cleanup_stale_forwards(serial):
    """清掉本应用在这台设备上留下的转发。

    会话崩了 / 服务被重启时 `adb forward` 的表项会留在 adb server 里
    （server 自己重启则整表清空，但那种情况下我们也要能重建）。
    只清 `localabstract:scrcpy_*` —— 那是我们自己的命名，不会误伤别人的转发。

    ⚠️ `forward --list` **不受 `-s` 约束**：实测（platform-tools 37.0.1）
    `adb -s A forward --list` 会把 **B 设备的条目也列出来**（列的是全表）。
    所以必须自己拿第一列的 serial 过滤一遍 —— 否则多设备并存时，
    连 B 会把 A 的转发清掉、把 A 正在跑的流打断。
    列表每行格式：`<serial> <local> <remote>`。
    """
    try:
        p = adbtool.run("-s", serial, "forward", "--list", capture_output=True, timeout=15)
    except Exception:
        return 0
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    killed = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == serial \
                and parts[-1].startswith("localabstract:scrcpy_"):
            try:
                adbtool.run("-s", serial, "forward", "--remove", parts[1],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10)
                killed += 1
            except Exception:
                pass
    return killed


class ForwardLink:
    """投屏三条流的搬运工：一条 `adb forward` + 若干本地 TCP 连接。

    对外接口刻意做成和 `adbproto.AdbClient` 一样（`open/read/write/close`），
    这样 session.py 的 `StreamReader` 一行都不用改 —— 换的只是底下谁在搬字节。
    """

    def __init__(self, serial, log=print):
        self.serial = serial
        self.log = log
        self.port = None
        self._dest = None
        self._socks = []
        self._lock = threading.Lock()
        self._closed = False

    # ---------- forward ----------

    def open_for(self, name, timeout=15.0):
        """给 `localabstract:<name>` 建一条转发，记下 adb 分配的本地端口。

        `name` 是设备端 abstract socket 的**完整名字**（scrcpy 的是 `scrcpy_<scid>`），
        这里原样拼进 `localabstract:`，不做任何补前缀 —— 补错了 adb 也不会报错。
        ⚠️ `adb forward` **不校验目标 socket 是否存在**：名字写错照样返回 0 和端口号，
        真正的报错要等到读的时候（连接被设备端立刻关掉 → 读到 EOF）。
        所以名字这一环必须由调用方保证，别指望 adb 帮你把关。

        `tcp:0` = 让 adb 挑一个空闲端口，它会把端口号打到 stdout（实测就是一行数字）。
        """
        dest = "localabstract:" + name
        p = adbtool.run("-s", self.serial, "forward", "tcp:0", dest,
                        capture_output=True, timeout=timeout)
        out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace").strip()
        if p.returncode != 0:
            raise AdbLinkError(humanize(out) or "建立本地转发失败：%s" % out)
        m = re.search(r"\b(\d{2,5})\b", out)
        if not m:
            raise AdbLinkError("没从 adb 拿到转发端口（原文：%r）" % out)
        self.port = int(m.group(1))
        self._dest = dest
        self.log("本地转发就绪：tcp:%d → %s" % (self.port, dest))
        return self.port

    # ---------- 流 ----------

    def open(self, name=None, timeout=10.0):
        """开一条流（= 一条到本地转发端口的 TCP 连接）。name 只在没建过转发时才需要。"""
        if self._closed:
            raise AdbLinkError("连接已关闭")
        if self.port is None:
            if not name:
                raise AdbLinkError("还没建立转发，且没给 socket 名")
            self.open_for(name, timeout=timeout)
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        except OSError as e:
            raise AdbLinkError(humanize(str(e)) or ("连本地转发端口失败：%s" % e))
        s.settimeout(None)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        with self._lock:
            self._socks.append(s)
        return s

    def read(self, sock, timeout=30.0):
        """读一段。**超时返回 None**（调用方靠它做循环），流断了才抛异常。"""
        if sock is None:
            return None
        sock.settimeout(max(0.05, float(timeout)))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            return None
        except OSError as e:
            raise AdbLinkError("流已断开：%s" % e)
        if not data:
            raise AdbLinkError("流已结束（设备端关闭）")
        return data

    def write(self, sock, payload):
        if sock is None:
            raise AdbLinkError("流不存在")
        try:
            sock.sendall(payload)
            return True
        except OSError as e:
            raise AdbLinkError("写入失败：%s" % e)

    def close_stream(self, sock):
        try:
            sock.close()
        except Exception:
            pass

    # ---------- 收尾 ----------

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._lock:
            socks, self._socks = self._socks, []
        for s in socks:
            try:
                s.close()
            except Exception:
                pass
        if self.port is not None:
            try:
                adbtool.run("-s", self.serial, "forward", "--remove", "tcp:%d" % self.port,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=10)
            except Exception:
                pass
            self.port = None


# ==================== 文件 / shell / 终端：真 adb 子进程 ====================


def _shq(s):
    """posix shell 单引号转义（设备端那条命令用）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def humanize(text):
    """把 adb 那几句常见英文翻成用户能照做的话。认不出来就原样返回。

    ⚠️ 别用 `"device not found" in text` 这种整串匹配：adb 的原话是
    `device '192.168.1.100:41449' not found`，引号把两个词隔开了，
    整串永远匹配不上（踩过）。按关键词分别判。
    """
    t = (text or "").strip()
    low = t.lower()
    if "not found" in low and "device" in low:
        return ("adb 找不到这台设备。多半是还没配对、或者无线调试的连接端口变了 —— "
                "用配对码重新配一次再连。（原文：%s）" % t)
    if "device offline" in low or "offline" in low:
        return "设备处于离线状态，重新 connect 一次试试。（原文：%s）" % t
    if "failed to connect" in low:
        return ("连不上设备：无线调试的连接端口只在开关打开时有效，而且要先配对过；"
                "端口变了记得也更新这里。（原文：%s）" % t)
    if "unauthorized" in low:
        return "设备未授权：请在手机上确认这次调试请求。（原文：%s）" % t
    if "failed to authenticate" in low:
        return "设备拒绝认证：配对信息已经失效，请重新用配对码配一次。（原文：%s）" % t
    if "no such file" in low:
        return "设备上找不到对应的东西。（原文：%s）" % t
    return t


# ============ 设备掉了：先重连一次，别把 adb 的原文甩给用户 ============
# 长会话（尤其**传大文件**）之后最常见的一幕（2026-09-23 实测）：
# 黑鲨上推 1.88 GB 的包，收尾时 `failed to read copy response: EOF`，
# 紧接着这台设备的视频/音频/控制 WS 一起断、会话停 —— 设备从 `adb devices`
# 里消失了。此后点什么都是 `adb: device 'X' not found`，看着像设备坏了。
#
# 而真相是：**设备活得好好的**。同一时刻 ping 通、adbd 的端口还开着、
# `adb connect` 一次就回来，设备端 adbd 的 pid 一直没换。
# 只是 adb server 里那条 transport 被判死了。
# ⇒ 只要替用户补上那一次 `adb connect`，一切照常。


def looks_disconnected(text):
    """这段报错是不是「设备从 adb 列表掉了 / 离线了」。

    ⚠️ 别用 `"device not found" in text` 整串匹配：adb 的原话是
    `device '192.168.1.100:41449' not found`，**引号把两个词隔开了**，
    整串永远匹配不上（`humanize` 里踩过同一个坑）。按关键词分别判。

    别的错（权限不够、文件不存在、INSTALL_FAILED_*）**一律不算掉线** ——
    否则会白重连一次，还把真原因盖掉。
    """
    low = (text or "").lower()
    if "offline" in low:
        return True
    return ("not found" in low) and ("device" in low)


def reconnect(serial, timeout=RECONNECT_TIMEOUT):
    """叫 adb 把这台设备重新连上（无线调试掉线后，自愈就靠这一步）。

    ⚠️ **绝不能用 capture_output=True**：`adb connect` 会把 adb server 拉成常驻后台，
    那个 daemon 会继承我们这两个管道；父进程退了、管道却还被 daemon 握着，
    `communicate()` 永远等不到 EOF —— 实测整个调用挂满 120 秒、没有任何输出
    （`fileops._adb_connect` 里踩过同一个坑）。输出本来也不需要，直接 DEVNULL。
    """
    try:
        adbtool.run("connect", serial,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout)
        return True
    except Exception:
        return False


def device_state(serial, timeout=10.0):
    """问 adb 这台设备现在是什么状态：`device` / `offline` / `unauthorized`；
    **不在列表里返回 None**。"""
    try:
        p = adbtool.run("devices", capture_output=True, timeout=timeout)
    except Exception:
        return None
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    for line in text.splitlines()[1:]:            # 第一行是 "List of devices attached"
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial:
            return parts[1]
    return None


def ensure_device(serial, timeout=RECONNECT_TIMEOUT):
    """动手干**贵活**之前先确认设备还在列表里（传/取大文件前用）。

    不在列表里、或者状态不是 `device`（offline / unauthorized），就叫 adb 重连一次。
    返回 True 表示"现在可以试"。**注意这不保证一定成** —— 重连是异步的，
    真失败让后面那步自己去报错，比在这里猜准。
    """
    if device_state(serial) == "device":
        return True
    return reconnect(serial, timeout=timeout)


def cli_shell_retry(serial, cmd, timeout=30.0):
    """`cli_shell` + 「掉线就重连一次再跑一次」。

    shell 这条路的失败**不抛异常**，而是 code != 0 + stderr 里有 adb 的原话，
    所以判据得看输出文本，不能靠 except。
    重连之后还是掉线 ⇒ 抛一句人话，别让 `device 'x' not found` 直接糊到界面上
    （用户看到那句只会以为手机坏了）。
    """
    out, err, code = cli_shell(serial, cmd, timeout=timeout)
    if code == 0 or not looks_disconnected(out + err):
        return out, err, code
    if not reconnect(serial):
        raise AdbLinkError(
            "设备（%s）掉线了，自动重连也没成功 —— 看看手机的「无线调试」是不是被关掉了。"
            % serial)
    out2, err2, code2 = cli_shell(serial, cmd, timeout=timeout)
    if code2 != 0 and looks_disconnected(out2 + err2):
        raise AdbLinkError(
            "设备（%s）掉线了，重连之后还是连不上它 —— 看看手机的「无线调试」是不是被关掉了。"
            % serial)
    return out2, err2, code2


def cli_shell(serial, cmd, timeout=30.0, pty=False):
    """用真 adb 跑一条 shell 命令，返回 (stdout, stderr, 退出码)。

    退出码：platform-tools 的 `adb shell` 会把设备端退出码**透传成本地退出码**
    （实测 `adb shell 'exit 7'` → rc=7），所以不用往命令里塞 `echo $?` 标记。
    `pty=True` 要传两次 `-t`：只传一次时 adb 会说「stdin 不是终端、不分配 pty」。
    """
    argv = ["-s", serial, "shell"]
    if pty:
        argv += ["-t", "-t"]
    argv.append(cmd)
    p = adbtool.run(*argv, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return out, err, p.returncode


def cli_push_file(serial, local, remote, timeout=300.0, progress=None):
    """把本地文件推上去（`adb push`）。"""
    p = adbtool.popen("-s", serial, "push", local, remote,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = []
    try:
        while True:
            chunk = p.stdout.readline()
            if not chunk:
                break
            text.append(chunk.decode("utf-8", "replace"))
    finally:
        try:
            p.wait(timeout=timeout)
        except Exception:
            p.kill()
            raise AdbLinkError("推送超时")
    out = "".join(text)
    if p.returncode != 0:
        raise AdbLinkError(humanize(out) or "推送失败")
    return out


def cli_pull_stream(serial, remote, sink, timeout=300.0, progress=None):
    """流式把设备上的文件取回来：`adb exec-out cat`。

    用 `exec-out` 而不是 `shell`：它不分配 pty、原样透传字节，二进制安全。
    """
    size = 0
    try:
        size = cli_stat(serial, remote)["size"]
    except Exception:
        size = 0                      # 拿不到大小不算错，进度按已收字节报
    p = adbtool.popen("-s", serial, "exec-out", "cat " + _shq(remote),
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    got = 0
    try:
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            sink(chunk)
            got += len(chunk)
            if progress:
                progress(got, size or got)
    finally:
        try:
            p.wait(timeout=timeout)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    err = (p.stderr.read() or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0:
        raise AdbLinkError(humanize(err) or "下载失败")
    return size or got


def cli_stat(serial, remote):
    """问设备要 mode / size / mtime（toybox `stat -c '%f %s %Y'`，实测可用）。

    `%f` 是十六进制原始 mode（文件 0x8180、目录 0x41c0），`%s` 大小、`%Y` mtime。
    """
    out, err, code = cli_shell(serial, "stat -c '%f %s %Y' " + _shq(remote), timeout=20.0)
    m = re.search(r"^\s*([0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s*$", out.strip(), re.M)
    if code != 0 or not m:
        raise AdbLinkError(humanize((out + err).strip()) or ("文件或目录不存在：%s" % remote))
    mode = int(m.group(1), 16)
    size = int(m.group(2))
    mtime = int(m.group(3))
    return {"mode": mode, "size": size, "mtime": mtime,
            "directory": (mode & 0o170000) == 0o040000}


def cli_touch_mtime(serial, remote, mtime):
    """尽力把 mtime 设回去（不是所有 toybox 都吃 `-d @epoch`，失败就算）。"""
    try:
        cli_shell(serial, "touch -d @%d %s" % (int(mtime), _shq(remote)), timeout=15.0)
        return True
    except Exception:
        return False
