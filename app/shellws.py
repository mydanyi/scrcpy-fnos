#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交互式终端：一个真 PTY。

## 两条路

- **不加密的 adbd**（容器 / 模拟器）：走 `shell,v2,pty:` —— adbd 用 `openpty()` 起真 pty。
  stdout / stderr / exit 分帧，尺寸从旁边改（见下）。这是主路径。
- **TLS 的 adbd**（Android 11+ 无线调试）：协议这条路连不上（明文握手就挂了），
  退回 `adb -s <serial> shell -t -t`，由 adb 去分配 pty。
  ⚠️ `-t` 要传**两次**才会强制分配 —— 只传一次时 adb 会说「stdin 不是终端、不分配 pty」。
  这条路上 stdout/stderr 合并成一条原始字节流，帧 id 统一按 `ID_STDOUT` 给出去。

## 为什么不用 `adb shell`（对不加密的设备）

`adb shell` 给的是**管道**，不是终端 —— `test -t 0` 是假，`stty` / `top` / `vi` 全都不正常。
设备上也没有 `script`，没有 busybox 可以拿管道包一个 pty 出来。
想要真终端只能走 `shell,v2,pty:`：adbd 会用 `openpty()` 起一个真 pty（`tty` 返回 `/dev/pts/N`）。

## 分帧（只对 `shell,v2` 那条路）

`id(1) + len(4, 小端) + 载荷`。**shell v2 每帧都带长度**，这点和 sync 不一样（sync 见 fileops）：

    0 = stdin         1 = stdout        2 = stderr
    3 = exit（载荷 1 字节退出码）          4 = 关掉 stdin
    5 = 改窗口大小 —— 见下

## 改窗口大小：协议那条路是死的

`id=5` 实测**完全无效**。六种写法全试过（u16 对 / u16 四元组 / 大端 u16 / u32 对 /
ASCII 的 `40:100` 和 `40x100`），`stty size` 从头到尾纹丝不动。
（探测脚本 `tools/_pty_resize_probe.sh`，结论记在 memory 里。）

活着的是**从旁边改**：pty 本身就是设备上的一个设备节点 `/dev/pts/N`，
谁打开它谁就能设 winsize。所以 resize 走 `stty -F /dev/pts/N rows R cols C`，
用**另一条 adb 连接**发一条一次性命令 —— 不往交互流里注入任何字符，
哪怕前台正跑着 `vi` / `top` 也能改准，而且不会在画面上留下任何痕迹。
这条旁路在**两条路上都通用**（它本来就走 `fileops._run_ok`，会自动选对通路）。

那个 `N` 从哪来：开 pty 时让 shell 先把自己的 tty 塞进一个 OSC 标题
（`ESC ] 777 ; tty:/dev/pts/0 BEL`），这段序列在这里就被解析掉了，永远到不了终端画面。
万一拿不到 N（比如 `tty` 不认这个 pty），就退回"等终端安静下来再把 `stty` 当命令注入"。
"""

import queue
import re
import subprocess
import threading
import time

import adblink
import adbtool
import fileops

OPEN_TIMEOUT = 10.0

# 退路注入的门槛：终端安静这么久之后，才敢往 stdin 里塞命令。
# 前台有程序在跑的时候注入会被它吃掉（在 vi 里就是一堆乱码），所以要等。
IDLE_BEFORE_INJECT = 1.2

# 旁路命令的节流。拖一下窗口能触发几十次 fit，每次都连一条 adb 太重了。
RESIZE_MIN_GAP = 0.15

ID_STDIN = b"\x00"
ID_STDOUT = b"\x01"
ID_STDERR = b"\x02"
ID_EXIT = b"\x03"
ID_CLOSE_STDIN = b"\x04"
ID_WINSIZE = b"\x05"

# OSC 标题：ESC ] 777 ; <内容> (BEL 或 ST)。用一个不常见的编号，
# 免得把 xterm 真正的窗口标题给覆盖了。
_OSC = re.compile(rb"\x1b\]777;([^\x07\x1b]*)(?:\x07|\x1b\\)")


class ShellError(Exception):
    pass


class ShellSession:
    """一个交互式 shell。读写都在这一个对象上。"""

    def __init__(self, serial, rows=24, cols=80):
        self.serial = serial
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.pty = None
        self.exit_code = None
        self.exited = False

        self._c = None
        self._fs = None
        # TLS 设备那条路：一个 `adb shell -t -t` 子进程 + 一个读线程喂队列。
        # （adb 的管道是阻塞的，而主循环还得同时伺候 WebSocket，所以不能直接 read。）
        self._proc = None
        self._q = None
        self._pump = None
        self._hold = b""                       # 被拆成两半的 OSC 序列
        self._last_out = time.time()
        self._applied = None                   # 上一次真正设上去的尺寸
        self._pending = None                   # 想设但还没设上去的尺寸
        self._next_try = 0.0

    # ---------- 生命周期 ----------

    def open(self):
        """连设备、开 pty。失败抛 ShellError / fileops.FileOpError。"""
        if adblink.needs_real_adb(self.serial):
            return self._open_cli()
        self._c = fileops._open(self.serial)
        # 起始尺寸必须在**开 pty 的时候**就定好：协议那条 resize 路已经证明是死的，
        # 而这一步是唯一能把尺寸带进去的时机，且第一帧就是干净的提示符。
        cmd = self._bootstrap_cmd()
        sid = self._c.open("shell,v2,pty:" + cmd, timeout=OPEN_TIMEOUT)
        self._fs = fileops.FramedStream(self._c, sid, 1)
        self._applied = (self.rows, self.cols)
        return self

    def _bootstrap_cmd(self):
        """开壳时要跑的那串：先把尺寸定好，再把 pty 路径用 OSC 标题带回来。"""
        return ("stty rows %d cols %d; printf '\\033]777;tty:%%s\\007' \"$(tty)\"; exec sh"
                % (self.rows, self.cols))

    def _open_cli(self):
        """TLS 设备上的终端：`adb shell -t -t` 要一个真 pty。

        `-t` 传两次才会强制分配（只传一次时 adb 会说「stdin 不是终端」）。
        起始尺寸照旧塞进开壳命令里 —— 这是唯一能带进去的时机。
        """
        cmd = self._bootstrap_cmd()
        try:
            self._proc = adbtool.popen(
                "-s", self.serial, "shell", "-t", "-t", cmd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
        except Exception as e:
            raise ShellError("启动 adb shell 失败：%s" % e)
        self._q = queue.Queue()
        self._pump = threading.Thread(target=self._cli_pump, name="shellws-adb",
                                      daemon=True)
        self._pump.start()
        self._applied = (self.rows, self.cols)
        return self

    def _cli_pump(self):
        """把子进程的输出搬进队列。EOF 时投一个哨兵，让读的那边知道结束了。"""
        stream = self._proc.stdout
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._q.put(chunk)
        except Exception:
            pass
        finally:
            self._q.put(None)

    @property
    def alive(self):
        if self._proc is not None:
            return not self.exited and self._proc.poll() is None
        return self._fs is not None and not self.exited

    def close(self):
        if self._fs is not None:
            try:
                self._fs.send(ID_CLOSE_STDIN)
            except Exception:
                pass
            try:
                self._fs.close()
            except Exception:
                pass
            self._fs = None
        if self._proc is not None:
            p, self._proc = self._proc, None
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if self._c is not None:
            try:
                self._c.close()
            except Exception:
                pass
            self._c = None
        self.exited = True

    # ---------- 读 ----------

    def read(self, timeout=0.05):
        """收一批帧，返回 [(帧 id, 载荷)]。

        终端大部分时间是安静的，**读超时返回空列表不是错误** ——
        这是这个循环能和 WebSocket 共处一室的前提：每轮只占几十毫秒。
        """
        if self._q is not None:
            return self._read_cli(timeout)
        out = []
        if self._fs is None:
            return out
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                fid, payload = self._fs.read_frame(timeout=max(0.01, remain))
            except fileops.StreamClosed:
                self.exited = True
                self._fs = None
                break
            except fileops.FileOpError:
                break
            if fid == ID_STDOUT:
                payload = self._filter(payload)
                if not payload:
                    continue
            out.append((fid, payload))
            if fid == ID_EXIT:
                self.exit_code = payload[0] if payload else 0
                self.exited = True
                break
        if out:
            self._last_out = time.time()
        return out

    def _read_cli(self, timeout):
        """CLI 那条路：stdout/stderr 是合并的一条裸流，统一按 ID_STDOUT 往外给。"""
        out = []
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                chunk = self._q.get(timeout=max(0.01, remain))
            except queue.Empty:
                break
            if chunk is None:
                self.exit_code = -1
                self.exited = True
                break
            payload = self._filter(chunk)
            if payload:
                out.append((ID_STDOUT, payload))
        if out:
            self._last_out = time.time()
        return out

    def _filter(self, payload):
        """把 OSC 777 标题挖出来（里面有 pty 路径），剩下的字节才发给前端。

        半截序列要先扣住 —— 一帧的分界完全可能正好落在 ESC 和 `]777;` 中间，
        直接发出去就会在后面某帧的头几个字节上冒出一截乱码。
        """
        if self._hold:
            payload = self._hold + payload
            self._hold = b""
        while True:
            m = _OSC.search(payload)
            if m:
                self._note_pty(m.group(1))
                payload = payload[:m.start()] + payload[m.end():]
                continue
            cut = payload.rfind(b"\x1b]777;")
            if cut >= 0:
                self._hold = payload[cut:]
                payload = payload[:cut]
            break
        return payload

    def _note_pty(self, raw):
        text = raw.decode("ascii", "replace")
        if ":" in text:
            text = text.split(":", 1)[1]
        if text.startswith("/dev/pts/"):
            self.pty = text

    # ---------- 写 ----------

    def write(self, data):
        if not data:
            return False
        if self._proc is not None:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
                return True
            except Exception:
                self.exited = True
                return False
        if self._fs is None:
            return False
        try:
            self._fs.send(ID_STDIN, data)
            return True
        except Exception:
            self.exited = True
            return False

    def close_stdin(self):
        """告诉设备"输入到头了"（等价于按 Ctrl-D）。"""
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            return
        if self._fs is not None:
            try:
                self._fs.send(ID_CLOSE_STDIN)
            except Exception:
                pass

    # ---------- 尺寸 ----------

    def resize(self, rows, cols):
        """改尺寸。返回 True 表示这一轮就已经落到设备上了。"""
        rows, cols = max(1, int(rows)), max(1, int(cols))
        if self._applied == (rows, cols):
            return True                     # 值没变就别去打扰设备
        self.rows, self.cols = rows, cols
        self._pending = (rows, cols)
        return self._apply()

    def tick(self):
        """在主循环里定期叫一下 —— 攒下的 resize 和退路注入都靠它兑现。"""
        self._apply()

    def _apply(self):
        if self._pending is None or (self._fs is None and self._proc is None):
            return True
        rows, cols = self._pending
        now = time.time()

        if self.pty and now >= self._next_try:
            self._next_try = now + RESIZE_MIN_GAP
            try:
                ok, _msg = fileops._run_ok(
                    self.serial, "stty -F %s rows %d cols %d" % (self.pty, rows, cols))
            except Exception:
                ok = False
            if ok:
                self._applied = (rows, cols)
                self._pending = None
                self._last_out = now
                return True

        # 旁路没成（还不知道 pts，或者 stty -F 不认），
        # 就退回往里注入 —— 但只能在终端安静的时候做，否则会砸到前台全屏程序上。
        if now - self._last_out >= IDLE_BEFORE_INJECT:
            if self.write(b"stty rows %d cols %d\n" % (rows, cols)):
                self._applied = (rows, cols)
                self._pending = None
                self._last_out = now
                return True
        return False
