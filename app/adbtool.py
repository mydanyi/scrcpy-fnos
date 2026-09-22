#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adb 可执行文件、家目录与调用参数的**唯一**出口 —— 全应用都从这里调 adb。

## 为什么要自带一份 adb（`tool/adb`）

系统那份是 Debian 的 **29.0.6**，而 `adb pair` 是 **platform-tools 30.0.0** 才有的子命令。
于是 Android 11+ 的「无线调试」永远配不上对，界面只会吐一句 `adb: unknown command pair`；
而**没配对过**的设备，`adb connect` 也只回一句光秃秃的 `failed to connect`（不给原因），
从报错里根本看不出是没配对。⇒ 自带一份官方 platform-tools，这条路才通。

## 为什么还要给它一个**独立端口**（默认 5038，不是 5037）

adb 客户端发现 server 版本和自己不一致时，会 **`host:kill` 掉现有 server 再重启一个**。
这台机器上 5037 是老早就在跑的（宿主/别人容器的 redroid、MAA 都在用），
自带 adb 一旦共用 5037，两边会互相把对方的 server 杀掉重启。
⇒ 自带 adb 固定用它自己的 server 端口，自成一套，谁也不碰谁。

## 为什么必须显式给 adb 指一个家目录（踩过，表现很隐蔽）

应用以 `scrcpy-fnos` 用户跑，而它在 passwd 里的家目录是 `/home/scrcpy-fnos` ——
**那个目录压根不存在**（飞牛建的是 nologin 系统用户，没给家目录）。
adb 要在 `$HOME/.android/` 里放 adbkey，建不出来就直接死：
    `F adb_utils.cpp:315 Cannot mkdir '/home/scrcpy-fnos/.android': No such file or directory`
界面看到的只是"配对失败"，原因藏在 adb 的 stderr 里。
⇒ 这里挑一个**确定存在且可写**的目录当 adb 的家（优先应用自己的数据区），
并把它显式塞进子进程 env，不去动整个进程的 `HOME`（别的代码可能靠它）。

## 用法

    adbtool.run("-s", serial, "push", a, b, capture_output=True, timeout=90)
    adbtool.popen("-s", serial, "shell", ...)
不要自己 `subprocess.run(["adb", ...])` —— 那样会漏掉端口和家目录这两件事。

调试用环境变量（生产不需要）：
    SCRCPY_ADB        指定 adb 可执行文件（当成"自带"处理，即带上独立端口）
    SCRCPY_ADB_PORT   改独立端口
    SCRCPY_ADB_HOME   指定 adb 的家目录
    SCRCPY_ADB_SYSTEM 置 1 时强制退回系统 adb（也不加端口，用于排查）
"""

import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = os.path.basename(HERE)

BUNDLED_ADB = os.path.join(HERE, "tool", "adb")
BUNDLED_PORT = "5038"
HOST_ADB = "adb"

# platform-tools 缺省把 server 日志扔这儿（NAS 一重启就空，所以要另存一份）。
LEGACY_TMP = "/tmp"


def _truthy(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _usable_dir(path):
    return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)


def adb_path():
    """返回 (可执行文件路径, 是否自带)。

    自带 = 用独立 server 端口。找不到自带那份就退回系统 adb（旧行为，不加端口）。
    """
    forced = (os.environ.get("SCRCPY_ADB") or "").strip()
    if forced:
        return forced, True
    if _truthy("SCRCPY_ADB_SYSTEM"):
        import shutil
        return (shutil.which(HOST_ADB) or HOST_ADB), False
    if os.path.isfile(BUNDLED_ADB) and os.access(BUNDLED_ADB, os.X_OK):
        return BUNDLED_ADB, True
    import shutil
    return (shutil.which(HOST_ADB) or HOST_ADB), False


def adb_home():
    """给 adb 用的家目录（里面会有 `.android/adbkey`）。

    顺序：显式指定 → 飞牛给的应用数据目录（TRIM_PKGVAR）→ `/var/apps/<app>/var`
    （安装时飞牛建的软链，指向应用数据目录）→ 进程自己的 $HOME（真存在才用）
    → 兜底放临时目录（这种情况密钥不持久，配对关系会丢，日志里能看出来）。
    """
    forced = (os.environ.get("SCRCPY_ADB_HOME") or "").strip()
    if _usable_dir(forced):
        return forced

    for cand in ((os.environ.get("TRIM_PKGVAR") or "").strip(),
                 os.path.join("/var/apps", APP_NAME, "var")):
        if _usable_dir(cand):
            return os.path.realpath(cand)

    home = (os.environ.get("HOME") or "").strip()
    if _usable_dir(home):
        return home

    return os.path.join(tempfile.gettempdir(), APP_NAME + "-home")


def adb_env():
    """adb 子进程要用的环境：只覆盖 HOME（和 TMPDIR），其余照抄。

    `.android` 提前建好 —— adb 自己 mkdir 失败时只会往 stderr 扔一行 F 级日志，
    调用方拿到的往往只是"配对失败"，排查成本很高。

    **TMPDIR 指到应用数据区**：adb server 会把自己的日志写成
    `$TMPDIR/adb.<uid>.log`，缺省就是 `/tmp` —— 而 NAS 一重启 `/tmp` 就空了，
    偏偏那份日志是「设备怎么掉线的」唯一现场（2026-09-23 查黑鲨掉线时，
    全靠它才看清是 transport 被判死、而不是设备死了）。
    ⚠️ 只对**之后新起的** server 生效：已经在跑的那个还是写在老地方，
    所以 `handle_server_log()` 会额外把老地方那份留档。
    """
    home = adb_home()
    try:
        os.makedirs(os.path.join(home, ".android"), exist_ok=True)
    except OSError:
        pass
    env = os.environ.copy()
    env["HOME"] = home
    tmp = tmp_dir()
    if tmp:
        env["TMPDIR"] = tmp
    return env


def tmp_dir():
    """给 adb 当临时目录用（它把 server 日志写这儿）。建不出来就返回 None。"""
    root = adb_home()
    if not _usable_dir(root):
        return None
    path = os.path.join(root, "adblog")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path if os.access(path, os.W_OK) else None


def _uid():
    """本进程的 uid（adb 用它给日志文件命名）。Windows 上没有，给 0 兜底。"""
    fn = getattr(os, "getuid", None)
    try:
        return int(fn()) if fn else 0
    except Exception:
        return 0


def handle_server_log():
    """把 adb server 那份日志捞到**不会被重启清掉**的地方。

    platform-tools 的 server 日志叫 `adb.<uid>.log`。我们虽然给新 server 派了
    TMPDIR（见 `adb_env`），但**已经在跑的那个 server 还在 `/tmp`**，
    所以启动时把 `/tmp` 里那份复制过来留档。

    返回 (来源, 落点)；没得捞返回 (None, None)。**绝不能因为这一步失败而挡启动。**
    """
    name = "adb.%d.log" % _uid()
    dest_dir = tmp_dir() or adb_home()
    legacy = os.path.join(LEGACY_TMP, name)
    if not os.path.isfile(legacy):
        return None, None
    dest = os.path.join(dest_dir, "adb-server.prev.log")
    try:
        with open(legacy, "rb") as src:
            data = src.read()
        with open(dest, "wb") as out:
            out.write(data)
        return legacy, dest
    except OSError:
        return None, None


def adb_argv(*args):
    """拼一条完整的 adb 命令（自带那份会带上它自己的 server 端口）。"""
    path, bundled = adb_path()
    argv = [path]
    if bundled:
        argv += ["-P", (os.environ.get("SCRCPY_ADB_PORT") or "").strip() or BUNDLED_PORT]
    argv += [str(a) for a in args]
    return argv


def run(*args, **kwargs):
    """subprocess.run 的 adb 版（自动补上端口与家目录）。"""
    kwargs["env"] = adb_env()
    return subprocess.run(adb_argv(*args), **kwargs)


def popen(*args, **kwargs):
    """subprocess.Popen 的 adb 版（自动补上端口与家目录）。"""
    kwargs["env"] = adb_env()
    return subprocess.Popen(adb_argv(*args), **kwargs)


def describe():
    """给日志/界面看的一行说明。"""
    path, bundled = adb_path()
    if not bundled:
        return "system adb: %s（自带 adb 不可用，无线配对会失败）" % path
    return "bundled adb: %s (-P %s, HOME=%s)" % (
        path, (os.environ.get("SCRCPY_ADB_PORT") or "").strip() or BUNDLED_PORT, adb_home())
