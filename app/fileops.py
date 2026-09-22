#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备文件操作：列目录 / 下载 / 上传 / 删除 / 装 APK / 跑脚本。

## 两条路：自研 sync（快） vs 真 adb 子进程（稳）

我们对**不加密**的 adbd（容器 / 模拟器，如 `127.0.0.1:5555`）自己说 adb 协议，
走 `OPEN("sync:")` 直传 —— 不落临时文件、二进制安全、进度就是真发出去的字节数。

但 **Android 11+ 的无线调试，adbd 是 TLS 的**，明文协议连握手都过不去
（设备回 `A_STLS`，见 `adblink.py`）。所以那种设备一律改走
`adb -s <serial> push / pull / exec-out / shell` 子进程 —— 真 adb 自己会处理 TLS。

选哪条由 `adblink.needs_real_adb(serial)` 决定（探测一次、缓存住），
下面每个公开函数开头都分了岔；**两条路的返回值和异常类型保持一致**，
调用方（server.py / shellws.py）不用知道走的是哪条。

## 为什么列目录不走 sync:LIST

`/sdcard` 是个软链（-> /storage/self/primary），sync 的 LIST 对软链的处理不好，
而且它回的是 mode 位、要自己解释。`ls -lA` 的 9 字段格式实测很稳（toybox 0.8.6），
解析成本更低。所以要拿到的只是「名字 / 大小 / 是不是目录」，走 shell 更划算。

⚠️ `ls -l /sdcard`（不带尾斜杠）列出的是**软链自己**，不是它指向的目录 —— 必须补一个 `/`。
"""

import os
import posixpath
import re
import struct
import subprocess
import tempfile
import time

import adblink
import adbproto
import adbtool

# 允许访问的根。**只放用户数据区**：/sdcard 是应用存东西的地方，/data/local/tmp 是 adb 自己的临时区。
# 绝不放 /、/system、/data —— 一个 rm -rf 就能把设备搞成砖。
ALLOWED_ROOTS = ("/sdcard", "/storage", "/data/local/tmp")

# 上传时给文件落的权限位（0o100644 = 33188）。
# /sdcard 那层是 sdcardfs/FUSE，权限多半会被它自己覆盖，这里只是给个合理默认。
FILE_MODE = 0o100644

CHUNK = 64 * 1024
SHELL_TIMEOUT = 30.0
XFER_TIMEOUT = 300.0


class FileOpError(Exception):
    pass


class StreamClosed(FileOpError):
    """设备端把这条流关了。

    ⚠️ 它必须和「读超时」分开：`run_shell` 里超时要**继续等**（`pm install` 中途可能几十秒没输出），
    而"流关了"才是真的结束。一开始两者都归成 FileOpError，于是命令只要安静 5 秒就被判成结束，
    退出码拿不到（返回 -1），`pm install` 明明装成功了也会被当成失败。
    """


# ==================== 工具 ====================


def _shquote(s):
    """把路径包成设备端 shell 能安全吃掉的形式（单引号 + 转义内部单引号）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def safe_path(p):
    """路径白名单校验。

    这不是"防君子"的装饰 —— `/api/files` 是对网关曝露的，任何一个手滑的
    `path=/` 配上 DELETE 就是灾难。所以必须在服务端把住，不能只靠前端不传坏值。
    """
    if not p:
        raise FileOpError("路径不能为空")
    p = str(p).strip()
    if "\x00" in p or "\n" in p:
        raise FileOpError("路径里有非法字符")
    if not p.startswith("/"):
        raise FileOpError("路径要以 / 开头")
    norm = posixpath.normpath(p)
    if norm == "/":
        raise FileOpError("不能直接操作根目录")
    if not any(norm == r or norm.startswith(r + "/") for r in ALLOWED_ROOTS):
        raise FileOpError("只能访问 %s 下的路径" % "、".join(ALLOWED_ROOTS))
    return norm


def _adb_connect(serial):
    """兜底：直连 adbd 不通时，叫 adb 帮我们连一下（跨网络的无线 adb 可能要先 connect）。

    ⚠️ 这里**绝不能用 capture_output=True**：`adb` 会把 adb server 拉到后台，
    而那个 daemon 会**继承我们这两个管道**。父进程自己退出了，管道却还被 daemon 握着，
    于是 `communicate()` 永远等不到 EOF —— 实测就是卡死在这里（整个探测挂 120 秒没有任何输出）。
    输出本来也不需要，直接丢给 DEVNULL 最干净。
    """
    try:
        adbtool.run("connect", serial,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15)
    except Exception:
        pass


def _open(serial, timeout=15.0):
    """连一台设备的 adbd（**只对不加密的 adbd 有效**）。

    优先**直连** —— 我们本来就说的是 adb 协议，`host:port` 就是设备端 adbd 的地址，
    根本不需要经过 adb server。只有直连失败了才退回 `adb connect` 兜一次。

    ⚠️ TLS 设备（Android 11+ 无线调试）在这条路上必然失败，所以先拦一道，
    给一句能照做的话，别让调用方拿到 `A_STLS` 那种天书。
    """
    if adblink.needs_real_adb(serial):
        raise FileOpError(
            "这台设备（%s）的 adbd 要求加密连接，不能自己直连；"
            "请走真 adb 那条路（fileops 的公开函数已经自动分了岔，"
            "直接用 `_open` 的地方需要一起改）。" % serial)
    host, port = adbproto.split_host_port(serial)
    try:
        c = adbproto.AdbClient(host, port, timeout=timeout)
        c.connect()
        return c
    except Exception:
        _adb_connect(serial)
        c = adbproto.AdbClient(host, port, timeout=timeout)
        c.connect()
        return c


# ==================== adb 帧收发 ====================
# sync 的帧是 id(4 字节 ASCII) + len(4 字节小端) + 载荷；
# shell v2 的帧是 id(1 字节)    + len(4 字节小端) + 载荷。
# 除了 id 宽度，其余一样，所以一个类同时服务两边。


class FramedStream:
    def __init__(self, client, stream_id, id_width):
        self._c = client
        self._sid = stream_id
        self._w = id_width
        self._buf = bytearray()

    def _need(self, n, timeout):
        deadline = time.time() + timeout
        while len(self._buf) < n:
            remain = deadline - time.time()
            if remain <= 0:
                raise FileOpError("读设备数据超时")
            try:
                chunk = self._c.read(self._sid, timeout=min(remain, 5.0))
            except adbproto.AdbError as e:
                raise StreamClosed(str(e))
            if chunk is None:
                continue
            self._buf += chunk

    def read_exact(self, n, timeout=XFER_TIMEOUT):
        self._need(n, timeout)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send(self, fid, payload=b""):
        self._c.write(self._sid, fid + struct.pack("<I", len(payload)) + payload)

    def send_raw(self, data):
        """原样写出去，**不加长度字段**。

        sync 里有几条帧是"值即参数"的，结构就是 `id(4) + 值(4)`，值后面不跟载荷：
        `DONE` 就是典型 —— 它的第二个 4 字节是 **mtime 本身**，不是长度。
        走 `send()` 会多发一个长度字段（8 字节变 12 字节），
        adbd 就会把我那个"长度=4"当成时间戳，落下去的 mtime 变成 4
        （实测就是这么骗过去的：文件时间显示 1970-01-01）。
        """
        self._c.write(self._sid, data)

    def read_frame(self, timeout=XFER_TIMEOUT):
        """读一帧 shell v2：id(1) + len(4) + 载荷。**shell v2 每帧都带长度。**"""
        head = self.read_exact(self._w + 4, timeout)
        fid = head[:self._w]
        ln = struct.unpack("<I", head[self._w:self._w + 4])[0]
        payload = self.read_exact(ln, timeout) if ln else b""
        return fid, payload

    def read_sync_reply(self, timeout=XFER_TIMEOUT):
        """读一帧 sync 应答。**sync 的帧型和 shell v2 完全不同，别把两套混起来。**

        真机倒字节的结论（探测脚本 tools/_sync_probe.sh，日志留在 memory 里）：
          * 统一 8 字节头：id(4) + 值(4)
          * `OKAY` / `DONE` 就 8 字节到头，**没有载荷**（值字段是 0）
          * `FAIL`  8 字节头 + 值字段那么多字节的原因文本
          * `DATA`  8 字节头 + 值字段那么多字节的载荷
          * `STAT`  特例：值字段其实是 **mode**，后面再跟 size / mtime 各 4 字节 ——
                    整个结构 16 字节，**没有长度字段**。

        两个踩过的坑，都写在这里免得重犯：
          1. 按"id(4)+len(4)+载荷"去读 STAT，会把 mode 当成长度（真机上读到 17912）。
          2. 上传时**等每个 DATA 的 OKAY**：这版 adbd 的 `handle_send_file` 整条传输只在
             DONE 之后回**一次** OKAY，中途一个字节都不回。那个 read 于是永远等不到数据，
             整个用例卡死在第一个 DATA 后面（实测 100 秒被 timeout 砍掉，日志停在 mkdir 之后）。
        """
        head = self.read_exact(8, timeout)
        fid, val = head[:4], struct.unpack("<I", head[4:8])[0]
        if fid == b"STAT":
            size, mtime = struct.unpack("<II", self.read_exact(8, timeout))
            return fid, (val, size, mtime)
        if fid == b"DATA":
            return fid, (self.read_exact(val, timeout) if val else b"")
        if fid == b"FAIL":
            msg = self.read_exact(val, timeout) if val else b""
            raise FileOpError(msg.decode("utf-8", "replace") or "传输失败")
        return fid, val                  # OKAY / DONE

    def close(self):
        try:
            self._c.close_stream(self._sid)
        except Exception:
            pass


# ==================== 一次性 shell 命令 ====================
# 走 shell v2 而不是 v1：v2 会把 stdout / stderr 分开单独成帧，**而且末尾带退出码**。
# v1 只有一条裸流，想判断 `pm install` 到底成没成，就只能去猜输出文本 —— 不可靠。


def run_shell(serial, cmd, timeout=SHELL_TIMEOUT):
    """跑一条命令，返回 (stdout, stderr, 退出码)。

    退出码拿不到时返回 -1（设备端没给 exit 帧的情形），调用方按输出文本兜底。
    TLS 设备走 `adb shell`：platform-tools 会把设备端退出码透传成本地退出码（实测）。
    """
    if adblink.needs_real_adb(serial):
        try:
            return adblink.cli_shell_retry(serial, cmd, timeout=timeout)
        except Exception as e:
            raise FileOpError(adblink.humanize(str(e)) or str(e))
    c = _open(serial)
    try:
        sid = c.open("shell,v2:" + cmd, timeout=10.0)
        fs = FramedStream(c, sid, 1)
        out, err = bytearray(), bytearray()
        code = -1
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                fid, payload = fs.read_frame(timeout=min(5.0, max(0.2, deadline - time.time())))
            except StreamClosed:
                break                      # 流关了 = 命令跑完了
            except FileOpError:
                continue                   # 只是这一段没数据，还没到点就别急着收工
            if fid == b"\x01":
                out += payload
            elif fid == b"\x02":
                err += payload
            elif fid == b"\x03":
                code = payload[0] if payload else 0
                break
        fs.close()
        return (out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"), code)
    finally:
        c.close()


def _run_ok(serial, cmd, timeout=SHELL_TIMEOUT):
    out, err, code = run_shell(serial, cmd, timeout=timeout)
    return code == 0, (out + err).strip()


# ==================== 列目录 ====================
# toybox `ls -lA` 一行的样子：
#   drwxrwx--x  4 root sdcard_rw   4096 2025-09-20 11:22 Download
#   -rw-rw----  1 root sdcard_rw 142879 2025-09-20 11:23 a.apk
#   lrwxrwxrwx  1 root root         21 2024-05-27 13:38 sdcard -> /storage/self/primary
#   crw-rw-rw-  1 root root     10, 200 2024-05-27 13:38 device        <- 设备节点，size 字段是两个数
# 所以 size 不能按"单个 token"去取：要在日期之前那段里，取**最后一个**数字。
_LS_HEAD = re.compile(r"^([dlbcps-])([rwxsStT-]{9})\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)$")
_LS_TAIL = re.compile(r"^(.*?)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(.*)$")


def _parse_ls_line(line):
    m = _LS_HEAD.match(line)
    if not m:
        return None                       # "total 12" 这类行直接跳过
    kind, _perm, _links, _owner, _grp, rest = m.groups()
    t = _LS_TAIL.match(rest)
    if not t:
        return None
    size_raw, date, clock, name = t.groups()

    directory = kind == "d"
    link = kind == "l"
    if link and " -> " in name:
        name = name.split(" -> ", 1)[0]
    if directory:
        size = 0
    else:
        nums = re.findall(r"\d+", size_raw)
        size = int(nums[-1]) if nums else 0
    return {"name": name, "size": size, "directory": directory, "link": link,
            "mtime": "%s %s" % (date, clock)}


def list_dir(serial, path):
    """列一个目录。返回 {"path": 规范化后的路径, "entries": [...]}"""
    path = safe_path(path)
    # ⚠️ 尾斜杠不能省：`ls -l /sdcard` 列的是软链自己，`ls -lA /sdcard/` 才是它指向的目录内容
    target = path.rstrip("/") + "/"
    out, err, code = run_shell(serial, "ls -lA " + _shquote(target), timeout=20.0)
    if code != 0 and not out.strip():
        raise FileOpError((err or out or "目录读取失败").strip())

    entries = []
    for line in out.splitlines():
        row = _parse_ls_line(line)
        if row is None:
            continue
        row["path"] = posixpath.join(path, row["name"])
        entries.append(row)
    # 目录在前，再按名字排（和竞品一致的直觉顺序）
    entries.sort(key=lambda e: (not e["directory"], e["name"].lower()))
    return {"path": path, "entries": entries}


# ==================== sync 传输 ====================


def _open_sync(serial):
    c = _open(serial)
    try:
        sid = c.open("sync:", timeout=10.0)
    except Exception:
        c.close()
        raise
    return c, FramedStream(c, sid, 4)


def _sync_stat(fs, remote):
    """问设备要一个文件的 mode / size / mtime 三件套。

    ⚠️ 这版 adbd 对**不存在的路径不报 FAIL**，而是回一个全零的结构体
    （实测：`STAT` + 12 个 0 字节，三个字段全 0）。
    所以"这个文件在不在"只能自己判：**看 mode**。
    真实文件/目录一定带类型位（文件 0o100770、目录 0o40770），不可能为 0；
    而 size 为 0 是"空文件"，跟"不存在"完全是两回事，拿 size 判会误杀空文件。
    """
    fs.send(b"STAT", remote.encode("utf-8"))
    fid, val = fs.read_sync_reply(timeout=SHELL_TIMEOUT)
    if fid != b"STAT":
        raise FileOpError("STAT 响应异常：%r" % (fid,))
    mode, size, mtime = val
    if mode == 0:
        raise FileOpError("文件或目录不存在：%s" % remote)
    return mode, size, mtime


def _push_failed_human(serial, exc):
    """上传失败时说清是**哪一种**失败 —— 这两种的处理方式完全不同。

      · **设备在传输中间掉线**（`read copy response: EOF` / `device 'x' not found`）：
        已经替用户重连上了，但**已经发出去的那半截没法续传**，只能重传。
      · 别的错（没权限、路径不对、空间不够）：原样翻人话。

    ⚠️ 别对上传做「重连后自动重试」：1.88 GB 的包静默重传一遍，
    用户只会觉得莫名其妙、还白白再等一次。重试只给便宜的操作（shell）。
    """
    text = str(exc)
    if adblink.looks_disconnected(text) or "EOF" in text:
        if adblink.reconnect(serial):
            return "设备在传输中间断线了（已自动重连上）——文件没传完整，请重新上传一次。"
        return ("设备在传输中间断线了，自动重连也没成功 —— "
                "看看手机的「无线调试」是不是被关掉了。")
    return adblink.humanize(text) or text


def _cli_push_stream(serial, remote, size, chunk_reader, mtime=None, progress=None):
    """TLS 设备那边的上传：`adb push` 只吃文件，所以先把流落到本地临时文件。

    代价比自研 sync 多一次落盘（那条路是边读边发、不落盘），换来的是 TLS 设备也能传。
    进度按**写入本地的字节数**报 —— 是"已接收"而不是"已送达设备"，
    但对用户来说比一个不动的进度条有用（最后 `adb push` 的返回码才是真判据）。
    """
    tmp = _tmp_path("up")
    sent = 0
    try:
        with open(tmp, "wb") as fh:
            while True:
                data = chunk_reader(CHUNK)
                if not data:
                    break
                fh.write(data)
                sent += len(data)
                if progress:
                    progress(sent, size or sent)
        # 真要动手传之前先确认设备还在 —— 这一步比传了 1.8 GB 之后再炸便宜得多，
        # 也正是用户"点安装就说 device not found"那一幕的正面解法。
        adblink.ensure_device(serial)
        try:
            adblink.cli_push_file(serial, tmp, remote)
        except adblink.AdbLinkError as e:
            raise FileOpError(_push_failed_human(serial, e))
        if mtime:
            adblink.cli_touch_mtime(serial, remote, mtime)
        return sent
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def sync_push(serial, remote, size, chunk_reader, mtime=None, progress=None):
    """把数据推给设备。`chunk_reader(n)` 每次返回至多 n 字节，返回空表示结束。

    边读边发是这个设计的关键：上传的字节直接从 HTTP 请求体里流出来，不落临时文件，
    所以进度就是真真切切已经发出去的字节数。

    ⚠️ **发完 DATA 不要等应答** —— 这版 adbd 只在收到 DONE 之后回一次 OKAY。
    中途那些 DATA 是"发了就算"，靠 TCP 自己的背压来限速。
    """
    remote = safe_path(remote)
    if adblink.needs_real_adb(serial):
        return _cli_push_stream(serial, remote, size, chunk_reader, mtime, progress)
    c, fs = _open_sync(serial)
    sent = 0
    try:
        fs.send(b"SEND", ("%s,%d" % (remote, FILE_MODE)).encode("utf-8"))
        while True:
            data = chunk_reader(CHUNK)
            if not data:
                break
            fs.send(b"DATA", data)
            sent += len(data)
            if progress:
                progress(sent, size or sent)
        # ⚠️ mtime 走 `send_raw`，**不能**走 `send` —— 这条帧的结构是
        # `DONE(4) + mtime(4)`，第二个 4 字节是时间戳本身而不是长度。
        # 用 send 会多发一个长度字段，adbd 就把"长度=4"当成时间戳，
        # 文件时间落成 1970-01-01（实测就是这么骗过去的）。
        fs.send_raw(b"DONE" + struct.pack("<I", int(mtime if mtime else time.time())))
        fid, _ = fs.read_sync_reply()
        if fid != b"OKAY":
            raise FileOpError("上传没有被确认：%r" % (fid,))
        return sent
    finally:
        try:
            fs.send(b"QUIT")
        except Exception:
            pass
        fs.close()
        c.close()


def sync_pull(serial, remote, sink, progress=None):
    """把设备上的文件取回来。`sink(bytes)` 拿到一段就写走一段。"""
    remote = safe_path(remote)
    if adblink.needs_real_adb(serial):
        adblink.ensure_device(serial)          # 设备掉了先捞回来，别等 adb 报 not found
        try:
            return adblink.cli_pull_stream(serial, remote, sink, progress=progress)
        except adblink.AdbLinkError as e:
            raise FileOpError(_push_failed_human(serial, e))
    c, fs = _open_sync(serial)
    got = 0
    try:
        # 先问一下大小：HTTP 那边要靠它设 Content-Length，
        # 不然浏览器的下载框显示不出进度，只能转圈。
        _mode, size, _mt = _sync_stat(fs, remote)
        fs.send(b"RECV", remote.encode("utf-8"))
        while True:
            fid, val = fs.read_sync_reply()
            if fid == b"DATA":
                sink(val)
                got += len(val)
                if progress:
                    progress(got, size or got)
            elif fid == b"DONE":
                break
            elif fid == b"OKAY":
                continue                  # 有的版本会在开头补一个 OKAY，无害
            else:
                raise FileOpError("意外响应：%r" % (fid,))
        return size if size else got
    finally:
        try:
            fs.send(b"QUIT")
        except Exception:
            pass
        fs.close()
        c.close()


def sync_stat(serial, remote):
    remote = safe_path(remote)
    if adblink.needs_real_adb(serial):
        try:
            return adblink.cli_stat(serial, remote)
        except adblink.AdbLinkError as e:
            raise FileOpError(str(e))
    c, fs = _open_sync(serial)
    try:
        mode, size, mtime = _sync_stat(fs, remote)
        return {"mode": mode, "size": size, "mtime": mtime,
                "directory": (mode & 0o170000) == 0o040000}
    finally:
        try:
            fs.send(b"QUIT")
        except Exception:
            pass
        fs.close()
        c.close()


# ==================== 系统 adb 退路 ====================
# sync 那条路万一对某个路径/某台设备不灵，就退回系统 adb。
# 代价是要落临时文件，所以只在主路径失败时走。


def _tmp_path(tag):
    d = os.environ.get("TRIM_PKGVAR") or tempfile.gettempdir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    return os.path.join(d, "scrcpy-%s-%d.tmp" % (tag, int(time.time() * 1000)))


_ADB_WARMED = False


def _warm_adb():
    """确保 adb server 已经在跑。

    为什么要先来这一下：`subprocess.run(..., capture_output=True)` 调 adb 时，
    如果 adb server 还没起来，被拉起来的那个 daemon 会**继承我们的管道**，
    `communicate()` 于是永远等不到 EOF —— 实测卡死 120 秒、一个字都吐不出来。
    先用 DEVNULL 把它叫起来（这次是 daemon 第一次 fork，唯一会有风险的一次），
    后面再 capture 就安全了：server 已经在了，adbd 交互不会再产生新的持有者。
    """
    global _ADB_WARMED
    if _ADB_WARMED:
        return
    _ADB_WARMED = True
    try:
        adbtool.run("start-server",
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass


def adb_push(serial, local, remote, timeout=XFER_TIMEOUT):
    _warm_adb()
    p = adbtool.run("-s", serial, "push", local, remote,
                    capture_output=True, timeout=timeout)
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    if p.returncode != 0:
        raise FileOpError(out.strip() or "adb push 失败")
    return out


def adb_pull(serial, remote, local, timeout=XFER_TIMEOUT):
    _warm_adb()
    p = adbtool.run("-s", serial, "pull", remote, local,
                    capture_output=True, timeout=timeout)
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    if p.returncode != 0:
        raise FileOpError(out.strip() or "adb pull 失败")
    return out


def push_bytes(serial, remote, data, mtime=None):
    """整段数据上传（小文件/退路用）。主路径请用 sync_push 流式那版。

    ⚠️ 这里的 reader 要**按游标一段段吐**。曾经写成 `return data[:n] if it else b""` ——
    第一段发完就把剩下的丢了：300 KB 的载荷只发出去 64 KB，设备端 md5 对不上，
    而返回码还是"成功"。这种"悄悄截断"比直接报错危险得多。
    """
    remote = safe_path(remote)
    try:
        pos = [0]

        def reader(n):
            chunk = data[pos[0]:pos[0] + n]
            pos[0] += len(chunk)
            return chunk
        return sync_push(serial, remote, len(data), reader, mtime=mtime)
    except (FileOpError, adbproto.AdbError):
        tmp = _tmp_path("up")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            adb_push(serial, tmp, remote)
            return len(data)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ==================== 动作 ====================


def delete(serial, path):
    path = safe_path(path)
    # 允许根本身绝对不能删：safe_path 放过了 "/sdcard"，但 `rm -rf /sdcard` 是清空整卡
    if path in ALLOWED_ROOTS:
        raise FileOpError("不能删除 %s 本身" % path)
    ok, msg = _run_ok(serial, "rm -rf -- " + _shquote(path), timeout=60.0)
    if not ok:
        raise FileOpError(msg or "删除失败")
    return msg or "已删除"


def mkdir(serial, path):
    path = safe_path(path)
    ok, msg = _run_ok(serial, "mkdir -p -- " + _shquote(path), timeout=30.0)
    if not ok:
        raise FileOpError(msg or "新建目录失败")
    return path


def rename(serial, src, dst):
    src, dst = safe_path(src), safe_path(dst)
    ok, msg = _run_ok(serial, "mv -- %s %s" % (_shquote(src), _shquote(dst)), timeout=60.0)
    if not ok:
        raise FileOpError(msg or "重命名失败")
    return dst


def install_apk(serial, path):
    """装 APK。

    `-r` 覆盖安装、`-d` 允许降级 —— 用 adb 装包时这两条几乎是必备的，
    少了它们「已经装了旧版」会直接失败，用户只会看到一句看不懂的报错。
    装一次可能要几十秒，超时给足。

    ⚠️ **包必须先搬到 `/data/local/tmp` 再让 pm 去开**（2026-09-23 修）。
    `pm install` 是这个文件的**读者是 system_server**，而 `/sdcard` 在 Android 10+
    是 FUSE（`u:object_r:fuse:s0`），SELinux 明确拒绝 system_server 读它：

        avc: denied { read } for scontext=u:r:system_server:s0 tcontext=u:object_r:fuse:s0
        Error: Can't open file: /sdcard/Download/xxx.apk

    真机上（黑鲨 SHARK PRS-A0，SELinux Enforcing）实测就是这个，而且
    `pm` 自己在报错里写了正解："Consider using a file under /data/local/tmp/"。
    以前直接把 /sdcard 的路径喂给 pm，于是**在这台机器上永远装不了**。
    中转文件用完就删（装失败也删），不给设备留垃圾。
    """
    path = safe_path(path)
    staged = _stage_for_pm(serial, path)
    try:
        out, err, code = run_shell(
            serial, "pm install -r -d " + _shquote(staged[0]), timeout=240.0)
    finally:
        _unstage_for_pm(serial, staged)
    text = (out + err).strip()
    if code == 0 and ("Success" in text or not text):
        return "安装成功"
    raise FileOpError(_pm_human(text, path))


# system_server 读得到的落点。别改成 /sdcard —— 那就是这个 bug 本身。
PM_STAGE_DIR = "/data/local/tmp/scrcpy-fnos-install"


def _stage_for_pm(serial, src):
    """把要装的包挪到 system_server 读得到的目录。返回 (安装用的路径, 要不要删)。

    已经在 `/data/local/tmp` 下的（用户就是从那装的）直接原地用，不多一次拷贝。
    """
    if src == "/data/local/tmp" or src.startswith("/data/local/tmp/"):
        return src, False
    name = posixpath.basename(src) or "install.apk"
    dst = posixpath.join(PM_STAGE_DIR, name)
    ok, msg = _run_ok(serial, "mkdir -p -- " + _shquote(PM_STAGE_DIR), timeout=30.0)
    if not ok:
        raise FileOpError("设备上建不出中转目录 %s：%s" % (PM_STAGE_DIR, msg or "未知原因"))
    # cp 一个大包（实测 205 MB）要几秒，超时给足
    ok, msg = _run_ok(serial, "cp -f -- %s %s" % (_shquote(src), _shquote(dst)), timeout=300.0)
    if not ok:
        raise FileOpError(
            "把安装包搬到设备中转目录失败：%s\n"
            "（设备上的 /data 空间不够时会这样；也可以直接把包放到 /data/local/tmp 下再装）"
            % (msg or "未知原因"))
    return dst, True


def _unstage_for_pm(serial, staged):
    """删掉中转的包。装不成也要删 —— 那是几百 MB，留着会占满 /data。

    顺手把空的目录也收掉（`rmdir` 失败无所谓：里面要是还有别的包，本来就不该动）。
    """
    dst, created = staged
    if not created:
        return
    try:
        _run_ok(serial,
                "rm -f -- %s; rmdir -- %s 2>/dev/null; true"
                % (_shquote(dst), _shquote(PM_STAGE_DIR)), timeout=30.0)
    except Exception:
        pass


# `pm install` 失败时那坨 Java 栈对用户毫无意义，能翻成人话的就翻一句。
_PM_FAIL_HINTS = {
    "INSTALL_FAILED_USER_RESTRICTED":
        "设备禁止了 USB 装应用（小米/红米系要在开发者选项里打开「USB 安装」）",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "设备存储空间不够",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
        "设备上已经有一个签名不一样的同名应用，得先卸载它",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "设备上装的版本比这个包还新",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "这个包没有适配本机的 CPU 架构",
    "INSTALL_FAILED_OLDER_SDK": "这个包要求的系统版本比设备高",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "这个包没有签名，装不了",
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES": "和设备上已装版本的签名对不上",
    "INSTALL_FAILED_INVALID_APK": "这个包本身有问题（下载不完整或不是 APK）",
}


def _pm_human(text, path=""):
    """把 `pm install` 的输出翻成一句能照着做的话（翻不动就把原文带上）。"""
    text = (text or "").strip()
    m = re.search(r"Failure\s*\[([A-Z_]+)\]", text)
    if m:
        reason = m.group(1)
        hint = _PM_FAIL_HINTS.get(reason)
        return "安装失败：%s%s" % (reason, ("—— " + hint) if hint else "")
    # ⚠️ 真机上这句是 `avc:  denied`（冒号后**两个空格**，内核日志对齐用的），
    # 按 `"avc: denied"` 去匹配会漏 —— 于是就退化成一墙 Java 栈了。
    if "avc:" in text and "denied" in text and "fuse" in text:
        # 中转这条路要是哪天又被人绕过去了，至少让用户看到一句人话
        return ("安装失败：设备系统不让直接装 /sdcard 上的包（SELinux 拒绝 system_server "
                "读 FUSE）。把包挪到 /data/local/tmp 下再装。")
    if not text:
        return "安装失败（pm install 没给理由）"
    return text[:400]


def execute_sh(serial, path):
    """在设备上跑一个 .sh。把 stdout 与 stderr 一起带回去给用户看。"""
    path = safe_path(path)
    out, err, code = run_shell(serial, "sh " + _shquote(path), timeout=240.0)
    text = "\n".join(x for x in (out.strip(), err.strip()) if x).strip()
    if code not in (0, -1):
        raise FileOpError(text or ("脚本退出码 %d" % code))
    return {"message": text or "脚本已执行完（没有输出）", "code": code}
