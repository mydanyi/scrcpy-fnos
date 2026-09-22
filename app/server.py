#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scrcpy on 飞牛 —— 服务端（多设备版）。

对外提供：
    GET  /                    前端页面（frontend/ 下的静态文件）
    GET  /healthz             探活，给 cmd/main 用
    GET  /api/devices         设备列表 = adb devices ∪ 已建立的会话（含状态）
    GET  /api/status          全部会话状态（按 serial 索引）
    POST /api/connect         开始投屏（后台启动，立即返回），body 带 serial + 投屏参数
    POST /api/disconnect      停止投屏，body 带 serial
    GET  /ws/video?serial=x   WebSocket：视频流（二进制）
    GET  /ws/control?serial=x WebSocket：控制指令（JSON）

    ---- 文件管理（serial + path 走 query，不在 body 里）----
    GET    /api/files?serial=&path=            列目录
    GET    /api/files/download?serial=&path=   下载（直接回文件字节流）
    POST   /api/files/upload?serial=&path=     上传（**请求体就是文件本身**，不是 multipart）
    POST   /api/files/mkdir | rename | delete  目录与文件动作（JSON body）
    POST   /api/files/install | execute        装 APK / 跑 .sh（JSON body）
    DELETE /api/files?serial=&path=            删除（等价于上面那个 delete）

    ---- 交互终端 ----
    GET /ws/shell?serial=&rows=&cols=  WebSocket：二进制帧 = 键盘输入，
                                      文本帧 = 控制指令（{"t":"resize",...} / {"t":"close"}）

和上一版的区别：会话**按设备分开管**（SESSIONS[serial]），每台设备有自己的
视频 hub 和状态。左边设备列表能同时挂多台，切到哪台就开哪台的视频流。

监听的是 unix socket，不是 TCP 端口 —— 飞牛网关按 app/ui/config 里的
gatewaySocket 找过来，所以本服务不出现在任何端口上。

视频 WS 上发的就是 scrcpy 原生的 12 字节头 + 载荷，不做二次封装：
    第 0 字节最高位 = 1 → 会话头（宽高变了，无载荷），否则是媒体包。
前端按同一套规则解析即可，后端零转换。
"""

import json
import os
import queue
import select
import signal
import socketserver
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import scrcpy_proto as P
import wsproto
import adbtool
import fileops
import shellws
from session import (ScrcpySession, list_devices, adb_pair,
                     probe_video_encoders, device_screen,
                     device_profile, profile_summary)

BASE = os.path.dirname(os.path.abspath(__file__))
SOCK_PATH = os.path.join(BASE, "app.sock")
FRONTEND_DIR = os.path.join(BASE, "frontend")
JAR_PATH = os.path.join(BASE, "tool", "scrcpy-server.jar")

# 飞牛网关转发过来时会带上应用前缀（对照 ScrcpyNas 日志里的 "base /app/scrcpyNas"）。
# 所以要先把前缀剥掉，否则 /app/scrcpy-fnos/ 会被当成一个静态路径去 frontend/ 下面找 → 404。
BASE_PATH = "/app/scrcpy-fnos"

# 文件管理那几个动作的中文名，只给日志用。
# 以前这几个接口失败时**只把错误回给前端、一个字都不落盘** ——
# 用户在界面上看到"打不开：adb: device 'X' not found"，
# 而 info.log 里什么都搜不到，排查等于从零开始（2026-09-23 查黑鲨掉线时发现的）。
FILE_ACTIONS = {
    "/api/files/mkdir": "新建目录",
    "/api/files/rename": "重命名",
    "/api/files/delete": "删除",
    "/api/files/install": "安装",
    "/api/files/execute": "执行",
}

# 投屏参数的默认值与取值范围。前端会按设备传一份，服务端再兜底夹一遍 ——
# 宁可夹到合法区间，也不要让设备端因为一个离谱值直接起不来。
DEFAULTS = {
    # 画质
    # 码率只是个兜底值：前端会按「设备原生分辨率 × 帧率」算出一个推荐值再传上来，
    # 只有前端没传的时候才用这个数。
    "bitRate": 8000000,
    "maxFps": 60,
    "maxSize": 0,          # 0 = 原画
    "codec": "auto",       # auto / h264 / h265 / av1 / vp8 / vp9
    "angle": 0,            # 0 / 90 / 180 / 270
    "crop": "",            # "宽:高:横偏移:纵偏移"，空 = 不裁
    # 音频
    "audio": True,
    "audioBitRate": 128000,
    "audioCodec": "aac",   # aac / opus
    "audioSource": "output",
    # 画面与设备
    "showTouches": True,
    "stayAwake": False,
    "powerOffOnClose": False,
    "keepActive": False,
    "startScreenOff": False,   # 走控制消息，不是 server 参数
    "powerOn": True,
    "clipboardAutosync": True,
    "screenOffTimeout": 0,     # 0 = 不改动
}
VALID_CODECS = ("auto",) + P.VIDEO_CODECS
VALID_AUDIO_CODECS = ("aac", "opus")
VALID_AUDIO_SOURCES = ("output", "playback", "mic", "voice_call", "voice_recognition", "voice_communication")
VALID_ANGLES = (0, 90, 180, 270)
VALID_SIZES = (0, 480, 720, 1080, 1440, 2160)
SESSION_SIZE_TIMEOUT = 15          # 等设备端回会话尺寸帧的上限（秒）
CLIENT_GRACE_SECONDS = 3.0         # 最后一个客户端断开后，等这么久没新客户端就自动停会话（秒）


# ==================== 设备记录 ====================
# 左侧列表放的是「我添加过的设备」，不是 adb 扫到的一切。
# 以前列表 = adb devices ∪ 会话表，于是列表里一堆自己没加过的设备，
# 点删除也只是停个投屏、设备还在 adb 里，下一轮刷新又回来了 —— 看起来就是「删不掉」。
# 记录存成一个 JSON，放应用的可写目录（$TRIM_PKGVAR，卸载应用也不会丢）。
def _var_dir():
    for k in ("TRIM_PKGVAR", "TRIM_APPDEST"):
        d = os.environ.get(k)
        if d:
            return d
    return BASE


RECORDS_PATH = os.path.join(_var_dir(), "devices.json")
RECORDS_LOCK = threading.Lock()


def _load_records():
    try:
        with open(RECORDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        serial = str(it.get("serial") or "").strip()
        if not serial:
            continue
        rec = {
            "serial": serial,
            "name": str(it.get("name") or "").strip(),
            "addedAt": int(it.get("addedAt") or 0),
        }
        # 设备状态（型号/系统版本/分辨率/编码器）：每次连上都重读一遍存这儿。
        # 老记录里没有这一项，读出来就是空 dict —— 界面按「还没读到」显示，不是错误。
        prof = it.get("profile")
        if isinstance(prof, dict) and prof:
            rec["profile"] = dict(prof)
        out.append(rec)
    return out


def _save_records(items):
    try:
        d = os.path.dirname(RECORDS_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = RECORDS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RECORDS_PATH)      # 原子替换，别让半截文件把列表清空
    except OSError:
        pass


def merge_profile(old, new):
    """把新读到的设备状态并进记录：新值非空才覆盖，空值不把旧值抹掉。

    为什么不整块替换：读状态要连设备跑几条 adb，偶尔会读回半截（设备刚被拔、
    shell 超时）。整块替换就会把上次好好的记录清成空 —— 而「读不到」和
    「设备本来就没有这个字段」从结果上分不开，那宁可留着旧的。
    """
    merged = dict(old or {})
    for k, v in (new or {}).items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        merged[str(k)] = v
    return merged


def record_add(serial, name="", top=False, profile=None):
    """记下这台设备。已经记过就更新名字/设备状态（不重复加、不改位置）。"""
    with RECORDS_LOCK:
        items = _load_records()
        old = next((it for it in items if it["serial"] == serial), None)
        if old is not None:
            if name:
                old["name"] = name
            if profile:
                old["profile"] = merge_profile(old.get("profile"), profile)
            if top:
                items.remove(old)
                items.insert(0, old)
            _save_records(items)
            return items
        item = {"serial": serial, "name": name, "addedAt": int(time.time())}
        if profile:
            item["profile"] = merge_profile(None, profile)
        if top:
            items.insert(0, item)
        else:
            items.append(item)
        _save_records(items)
        return items


def record_remove(serial):
    with RECORDS_LOCK:
        items = [it for it in _load_records() if it["serial"] != serial]
        _save_records(items)
        return items


def record_rename(serial, name):
    with RECORDS_LOCK:
        items = _load_records()
        hit = False
        for it in items:
            if it["serial"] == serial:
                it["name"] = name
                hit = True
        if not hit:
            # 只活在会话里、没落过记录的那台（直接 POST /api/connect 建出来的）：
            # 补一条，改名才存得住，否则刷新一下名字就弹回去了。
            items.append({"serial": serial, "name": name, "addedAt": int(time.time())})
        _save_records(items)
        return items


def strip_base(path):
    if path == BASE_PATH:
        return "/"
    if path.startswith(BASE_PATH + "/"):
        return path[len(BASE_PATH):]
    return path


def norm_settings(raw):
    """把前端传来的参数夹到合法范围，缺的补默认值。"""
    raw = raw or {}
    out = dict(DEFAULTS)

    def _int(key, lo, hi):
        try:
            v = int(raw.get(key) if raw.get(key) is not None else DEFAULTS[key])
        except (TypeError, ValueError):
            return DEFAULTS[key]
        return max(lo, min(hi, v))

    out["bitRate"] = _int("bitRate", 100000, 100000000)
    out["maxFps"] = _int("maxFps", 0, 240)
    size = _int("maxSize", 0, 4096)
    out["maxSize"] = size if size in VALID_SIZES else 0

    codec = str(raw.get("codec") or DEFAULTS["codec"]).lower()
    out["codec"] = codec if codec in VALID_CODECS else "auto"

    angle = _int("angle", 0, 270)
    out["angle"] = angle if angle in VALID_ANGLES else 0

    # 裁剪只接受 "宽:高:横偏移:纵偏移" 四个非负整数，格式不对就当没填 ——
    # 设备端拿到不合法的 crop 会直接抛异常退出，不能原样转发。
    crop = str(raw.get("crop") or "").strip()
    parts = crop.split(":")
    if len(parts) == 4 and all(p.strip().isdigit() for p in parts):
        out["crop"] = ":".join(p.strip() for p in parts)
    else:
        out["crop"] = ""

    acodec = str(raw.get("audioCodec") or DEFAULTS["audioCodec"]).lower()
    out["audioCodec"] = acodec if acodec in VALID_AUDIO_CODECS else "aac"
    asrc = str(raw.get("audioSource") or DEFAULTS["audioSource"]).lower()
    out["audioSource"] = asrc if asrc in VALID_AUDIO_SOURCES else "output"
    out["audioBitRate"] = _int("audioBitRate", 8000, 512000)

    snap = _int("screenOffTimeout", 0, 2147483647)
    out["screenOffTimeout"] = 0 if snap == 0 else max(60000, snap)

    for key in ("audio", "showTouches", "stayAwake", "powerOffOnClose", "keepActive",
                "startScreenOff", "powerOn", "clipboardAutosync"):
        out[key] = bool(raw.get(key, DEFAULTS[key]))
    return out


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    # xterm.js 本身不需要字体文件，但离线内嵌的东西以后可能带一份，
    # 缺了这一条浏览器会拿 octet-stream 去当字体解析、直接静默不生效。
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

# 上传时多大以内可以整包读进内存走"能兜底"的那条路。
# 超过它就只能纯流式 —— 那时候请求体已经边收边发给设备了，没法回头重来。
SMALL_UPLOAD_MAX = 8 * 1024 * 1024


# ==================== 日志：时间戳 + 轮转 ====================
# 我们的行**必须带时间戳**：appcenter 只给它自己那两行启停记录打时间戳，
# 我们原来是不打的，于是跨天排查只能拿启停行当锚点往回数（2026-09-23 查黑鲨掉线时踩到）。
#
# 轮转：stdout 是 appcenter 开好递进来的 fd，**不能换文件** —— 换了 fd 还指着旧 inode，
# 后面写的全进一个已被删除的 inode 里（"日志不涨了"就是这么来的）。
# 所以只能原地截断；截断前先把整份留档成 `<日志>.1`，不然历史全丢。
LOG_ROTATE_BYTES = int(float(os.environ.get("SCRCPY_LOG_ROTATE_MB") or 8) * 1024 * 1024)
_LOG_TICK = [0]


def _stdout_path(fd=1):
    """fd 指向的真实文件（appcenter 把 info.log 开好递给我们）。"""
    try:
        p = os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return None
    return p if p.startswith("/") else None


def _rotate_log(fd, path, limit):
    """fd 指向的日志超过 limit 就原地截断。返回 True 表示真转了。

    `ftruncate` 之后**再 lseek 一次**：fd 若是 O_APPEND（appcenter 大概率用 `>>`），
    写入位置本来就会被强制到文件末尾、lseek 无害；若**不是** O_APPEND，
    不 lseek 就会从原偏移接着写 —— 文件直接变成一个大空洞（稀疏文件）。
    两边都照顾，才敢在这儿动刀。
    """
    try:
        if os.fstat(fd).st_size < limit:
            return False
        with open(path, "rb") as src:
            data = src.read()
        with open(path + ".1", "wb") as out:
            out.write(data)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        return True
    except OSError:
        return False            # 日志转不动绝不许影响主流程


def _maybe_rotate_log():
    """每 200 行才 stat 一次，别让看日志这件事把主流程拖慢。"""
    if LOG_ROTATE_BYTES <= 0:
        return
    _LOG_TICK[0] += 1
    if _LOG_TICK[0] % 200:
        return
    p = _stdout_path()
    if p and os.path.isfile(p):
        _rotate_log(1, p, LOG_ROTATE_BYTES)


def log(msg):
    print("%s [scrcpy-fnos] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)
    _maybe_rotate_log()


# 视频流的问题（花屏 / 卡住 / 起点不对）只看客户端是查不出来的 ——
# 得知道服务端到底把哪些帧按什么顺序发给了谁。设 SCRCPY_DEBUG=1 打开。
DEBUG = os.environ.get("SCRCPY_DEBUG") == "1"


def dbg(msg):
    if DEBUG:
        print("[scrcpy-fnos][dbg] %s" % msg, flush=True)


def _pkt_pts(data):
    """媒体包的 pts（微秒）。scrcpy 的头是全大端：高 32 位里低 29 位是 pts 高位。"""
    if len(data) < 12:
        return -1
    hi = struct.unpack(">I", data[0:4])[0]
    lo = struct.unpack(">I", data[4:8])[0]
    return (hi & 0x1FFFFFFF) * 4294967296 + lo


def _packet_kind(data):
    """给视频包分类：session / config / key / delta。认不出来返回 None。"""
    if len(data) < 12:
        return None
    hi = struct.unpack(">I", data[0:4])[0]
    if hi & 0x80000000:
        return "session"
    if hi & 0x40000000:
        return "config"
    if hi & 0x20000000:
        return "key"
    return "delta"


def _drain(q):
    """清空队列。**不塞哨兵** —— None 是「会话结束」的信号，别误报出去。"""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def _force_put(q, item):
    """塞不进去就腾个位置再塞；再失败就算了。"""
    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    _drain(q)
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def _shell_ctrl(payload):
    """终端 WS 上收到的文本帧：是控制指令就返回解析好的对象，否则返回 None。

    ⚠️ 不能只看"能不能 json.loads" —— 用户往终端里粘一段 `{"a": 1}` 也是合法 JSON，
    只看解析成功就会被当成指令吞掉、粘不进去。所以还要求它带 `t` 这个键。
    """
    try:
        msg = json.loads(payload.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(msg, dict):
        return None
    t = msg.get("t")
    return msg if isinstance(t, str) and t else None


def _int(msg, key, fallback):
    try:
        return int(msg.get(key))
    except (TypeError, ValueError, AttributeError):
        return fallback


class _Sub:
    """一个订阅者：自己的小队列 + 一个「正在等关键帧」的标记。"""

    __slots__ = ("q", "resync")

    def __init__(self, maxsize):
        self.q = queue.Queue(maxsize=maxsize)
        self.resync = False

    def get(self, timeout=None):
        return self.q.get(timeout=timeout)


class VideoHub:
    """把视频帧分发给订阅者。

    ⚠️ 队列满了**不能盲丢**。H.264 的帧之间是互相参考的：丢掉一帧，后面所有帧都从
    残缺的参照里预测，画面直接变成一片宏块花屏，而且要等到下一个关键帧才恢复 ——
    scrcpy 的 IDR 间隔默认能到十秒，那就是十几秒的糊图。
    所以这里的策略是：某个订阅者一旦跟不上，就把它标成「等关键帧」，
    期间只放行 session / config / 关键帧，其余一律丢；关键帧一到自动恢复。
    最坏情况变成画面短暂停一下，而不是花掉。

    ⚠️ 但**也不能只发实时帧**：config 包（SPS/PPS）和关键帧只在会话开头出现一次，
    而前端是等会话就绪后才来连 WebSocket 的，那时候它们早过去了 ——
    结果就是收到一堆 P 帧、解码器却永远配置不上，画面全黑。
    所以这里缓存「会话头 + config + 最近一个关键帧」，新订阅时先补发。

    ⚠️ 两个容量参数**必须一起看**（2026-09-21 调）：
      · `maxsize`    = 每个订阅者的队列深度。
      · `gop_limit`  = `_gop` 最多缓存多少帧；超了就整段丢掉并标记 `_gop_stale`。
    **不变量：`gop_limit + 2 <= maxsize`**（补发最多是 `session + config + 整个 GOP`）。
    一旦违反，补发自己就会把订阅队列塞满 → `put_nowait` 抛 `queue.Full` → `break` 掉，
    新订阅者拿到的是**被截断的 GOP**（参考链断在半截，画面花）。
    而 `gop_limit` 还要**装得下设备一个自然 GOP**：IDR 间隔是按**秒**配的，
    帧数 = fps × 间隔 —— 2026-09-21 在生产上实测（h264 / 1080p / 60fps，屏幕持续动画）：
    关键帧间隔稳定在 **150 帧 / 约 2.5s**（多轮采样 min = 中位 = max = 150）。
    旧值 `gop_limit=120` 装不下这个自然 GOP ⇒ `_gop` 每轮都被判超长清空、
    `_gop_stale` 经常为真 ⇒ 新订阅者拿不到回放，只能干等下一个自然 IDR（每个回收周期卡一下）。
    现在 240 帧 ≈ 覆盖到 96fps × 2.5s，300 的队列深度留出余量，补发永不截断。
    """

    def __init__(self, maxsize=300, gop_limit=240):
        assert gop_limit + 2 <= maxsize, \
            "gop_limit + 2 不能超过 maxsize，否则补发会被自身截断"
        self._lock = threading.Lock()
        self._subs = []
        self._maxsize = maxsize
        self._gop_limit = gop_limit
        self._session = None      # 最近一次会话头（宽高）
        self._config = None       # 最近一次 SPS/PPS
        self._gop = []            # 从最近一个关键帧起、到最新为止的整段帧
        self._gop_stale = False    # _gop 曾经有过但被丢掉了（太长缓存不下）→ 需要新的 IDR
        self._closed = False
        # ⚠️ 刻意留空：这里曾经接的是「发一条 MSG_RESET_VIDEO 让设备重出 IDR」，
        # 实测会把硬编重启成配不回来的状态。现在两个调用点都只是记日志 + 等自然 IDR。
        self._key_requester = None

    def set_key_requester(self, fn):
        self._key_requester = fn

    def subscribe(self):
        sub = _Sub(self._maxsize)
        need_key = False
        # ⚠️ 补发必须和 publish 的入队**排在同一个临界区里**。
        # 先把 sub 挂进 _subs、再在锁外补发的话，publish 会抢在补发前面把**实时帧**
        # 塞进队列 —— 客户端于是先吃到现在的新帧、再吃到几帧更早的补发帧，
        # 时间顺序整个倒过来，解码器参照错位 → 花屏。
        with self._lock:
            if self._closed:
                sub.q.put_nowait(None)      # 直接告诉调用方：结束了
                return sub
            self._subs.append(sub)
            replay = []
            if self._session is not None:
                replay.append(self._session)
            if self._config is not None:
                replay.append(self._config)
            if self._gop:
                # ⚠️ 必须补**整段**（关键帧一直到最新），不能只补头部几帧。
                # 只补 3 帧的话，补发段和实时帧之间就断了一截 ——
                # 解码器从关键帧起步、解完补发的那两帧，然后直接跳到几秒后的实时帧，
                # 中间那些帧的参考全没有 → 宏块花屏，而且静止画面上会一直花下去
                # （屏幕不变就没有新帧，也就永远等不到下一个自然 IDR）。
                replay.extend(self._gop)
            else:
                sub.resync = True           # 没有可用起点，先别放行 P 帧
                if self._gop_stale:
                    need_key = True         # 缓存里本该有、但被丢了 → 等下一个自然 IDR
            for item in replay:
                try:
                    sub.q.put_nowait(item)
                except queue.Full:
                    # 正常永远走不到这里：`__init__` 断言了 `gop_limit + 2 <= maxsize`，
                    # 而补发最多就是「session + config + 一整个 GOP」。
                    # 真走到这儿说明两个容量参数被人调歪了 —— 补发被自己截断、
                    # 客户端拿到半截 GOP（参考链断在中间 → 花屏），必须喊出来。
                    dbg("补发被队列截断！maxsize=%d 装不下 %d 项补发，请调 maxsize/gop_limit"
                        % (self._maxsize, len(replay)))
                    break
        if need_key:
            dbg("新订阅者拿不到干净的起点，等编码器自然的 IDR")
            self._request_key()
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def close(self):
        """会话结束时调用：唤醒所有还挂着的订阅者，让它们的循环退出。"""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            _force_put(sub.q, None)

    def publish(self, data):
        kind = _packet_kind(data)
        dropped = []
        with self._lock:
            # 先维护缓存，再分发 —— 两件事在同一个锁里，顺序才不会乱
            if kind is not None:
                if kind == "session":           # 新的流（尺寸变了），等新的关键帧
                    self._session = data
                    self._gop = []
                    self._gop_stale = False
                elif kind == "config":          # SPS/PPS
                    self._config = data
                elif kind == "key":
                    self._gop = [data]
                    self._gop_stale = False
                else:
                    if self._gop:               # 只在有起点之后才攒
                        self._gop.append(data)
                        if len(self._gop) > self._gop_limit:
                            # 这一个 GOP 太长，缓存不下「从关键帧起的完整链条」。
                            # 绝不能从头 pop —— 留下个不以关键帧开头的半截，
                            # 谁拿到谁花屏。整段丢掉，标记住，等新订阅者来了再要 IDR。
                            self._gop = []
                            self._gop_stale = True
                            dbg("GOP 超过 %d 帧，清空缓存并标记需要新的关键帧" % self._gop_limit)

            for sub in self._subs:
                # 正在等关键帧：这期间的 P 帧一律不发 —— 没有起点，收到也只能解出花屏
                if sub.resync and kind not in ("session", "config", "key"):
                    continue
                try:
                    sub.q.put_nowait(data)
                except queue.Full:
                    # 这个订阅者跟不上了：清掉积压、标记等关键帧。
                    # ⚠️ 要放回去的是**所有能重新起步的包**：会话头 / config / 关键帧。
                    # 少放 config 就麻烦了：`config` 一个会话只发一次，
                    # 而它被丢的那一刻恰恰是「客户端刚 reset 过、正等着配置」的典型场景
                    # ——（此前只回放 session/key，于是只要队满刚好撞在 config 上，
                    # 客户端就永远配不上；而且撞不撞得上取决于 maxsize 的巧合，太脆。）
                    _drain(sub.q)
                    sub.resync = True
                    dropped.append(sub)
                    if kind in ("session", "config", "key"):
                        try:
                            sub.q.put_nowait(data)
                        except queue.Full:
                            pass
                        if kind == "key":
                            sub.resync = False
                else:
                    if kind == "key":
                        sub.resync = False
        if dropped:
            dbg("%d 个订阅者跟不上，已清空积压改等关键帧" % len(dropped))
            # ⚠️ 这里**不再**向设备要关键帧（曾经 emit 一条 MSG_RESET_VIDEO）。
            # 那个动作会把硬编重启成「只出 IDR、不再补 CONFIG」的状态 —— 治不了花屏，
            # 反倒把整条流弄死（详见 `_start_session` 里那段实测）。现在就是干等：
            # 订阅者已带 `resync` 标记，只放行 session/config/key，编码器自然的 IDR
            # （本机约 1.7 秒一个）一到就自己接上。
            #
            # 只有「根本没帧可等」才需要担心：屏幕完全静止时编码器不出帧、自然 IDR 也不来。
            # 但这条路径的前提是**队列被灌满** —— 有帧进来才会满，有帧就说明画面在动、
            # IDR 会在两秒内到。所以不会卡死。

    def _request_key(self):
        """订阅者拿不到干净起点（GOP 缓存被丢过）时的兜底。

        ⚠️ 这里**不再主动向设备要关键帧**。以前它发 MSG_RESET_VIDEO，但实测
        （`tools/_t29e_out.txt`）设备端编码器被重启后**只出 IDR、不再补 CONFIG**，
        前端 resetDecoder 后 configured=false 且 H.264 没有关键帧兜底 ⇒ 永久黑。
        现在只记一条日志，然后**等编码器自然的 IDR** —— 订阅者带着 `resync` 标记，
        只放行 session/config/key，下一个 IDR（本机约 1.7 秒）一到就自动接上。

        特意不调 `self._key_requester`：哪怕哪天有人又把它接回 resetVideo，
        也不会从这个入口发出去。（`_start_session` 里已显式 `set_key_requester(None)`。）
        """
        dbg("订阅者需要关键帧：等编码器自然的 IDR，不下发 resetVideo")


class AudioHub:
    """把音频包分发给订阅者。

    和视频不一样，这里**没有关键帧那一套**：AAC 每帧都是独立可解的，
    丢一帧只是「咔」一下，不会像 H.264 那样牵连后面一大片。
    所以队列满了就丢最老的，不等 IDR、也不去要关键帧。

    唯一要补发的是 config 包（AAC 的 AudioSpecificConfig）——
    前端要靠它才能建出正确的 AudioDecoderConfig / 音频初始化分片，
    而它只在流开头出现一次。
    """

    def __init__(self, maxsize=160):
        self._lock = threading.Lock()
        self._subs = []
        self._maxsize = maxsize
        self._config = None
        self._closed = False

    def subscribe(self):
        sub = _Sub(self._maxsize)
        with self._lock:
            if self._closed:
                sub.q.put_nowait(None)
                return sub
            self._subs.append(sub)
            if self._config is not None:
                try:
                    sub.q.put_nowait(self._config)
                except queue.Full:
                    pass
        return sub

    def unsubscribe(self, sub):
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def close(self):
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            _force_put(sub.q, None)

    def publish(self, data):
        kind = _packet_kind(data)
        with self._lock:
            if kind == "config":
                self._config = data
            for sub in self._subs:
                try:
                    sub.q.put_nowait(data)
                except queue.Full:
                    # 跟不上就丢最老的，音频不值得为它攒积压 ——
                    # 攒下去只会让声音越来越滞后。
                    _drain(sub.q)
                    try:
                        sub.q.put_nowait(data)
                    except queue.Full:
                        pass


class SessionEntry:
    """一台设备的投屏会话 + 它自己的视频/音频 hub。"""

    def __init__(self, serial, settings):
        self.serial = serial
        self.settings = settings
        self.phase = "starting"      # starting / running / error / idle
        self.error = ""
        self.session = None
        self.hub = VideoHub()
        self.audio_hub = AudioHub()
        # 这台设备有没有 H.265 编码器 —— 只有真的试过才知道（试失败才有这个标记）。
        # 记在 entry 上而不是 session 上：失败那次 session 会被丢掉，但结论要留下来。
        self.no_h265 = False
        # 探测出来的可用视频编码器（["h264","vp8","vp9"] 这种）。None = 没探测出来。
        self.encoders = None
        # 用户点名要的编码器（"auto" / "vp9" ……），用来和实际用上的做对照。
        self.codec_requested = settings.get("codec") or "auto"
        # 实际用上的和用户要的不一致时，这里放一句人话解释（给界面显示）。
        self.codec_note = ""
        # 客户端连接计数 + 最后一个人走了之后的自动停会话定时器。
        self._clients_lock = threading.Lock()
        self._client_count = 0
        self._stop_timer = None
        self._stop_token = 0

    def client_connected(self):
        """有人连上（视频/音频/控制 WS）：计数 +1，并取消待停会话的定时器。"""
        with self._clients_lock:
            self._client_count += 1
            self._stop_token += 1          # 让已经排队的定时器失效
            t = self._stop_timer
            self._stop_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def client_disconnected(self):
        """有人断开：计数 -1。只有最后一个走了、且会话还在跑，才安排延迟停会话。"""
        with self._clients_lock:
            if self._client_count > 0:
                self._client_count -= 1
            if self._client_count != 0:
                return
            if self.phase not in ("starting", "running"):
                return
            self._stop_token += 1
            token = self._stop_token
            t = threading.Timer(CLIENT_GRACE_SECONDS, self._stop_if_unused, args=(token,))
            t.daemon = True
            self._stop_timer = t
        t.start()

    def _stop_if_unused(self, token):
        """宽限期到了：确认期间没有新客户端、会话还在跑，才真的停。"""
        with self._clients_lock:
            if token != self._stop_token:
                return                     # 期间有客户端来连过，作废
            if self._client_count != 0:
                return
            if self.phase not in ("starting", "running"):
                return
        _stop_session(self.serial, expected_entry=self)

    def info(self):
        s = self.session
        d = {
            "serial": self.serial,
            "phase": self.phase,
            "error": self.error,
            "noH265": self.no_h265,
            "encoders": self.encoders,
            "codecRequested": self.codec_requested,
            "codecNote": self.codec_note,
        }
        if s is not None:
            d.update({
                "device": s.device_name,
                "codec": s.codec,
                "width": s.width,
                "height": s.height,
                "lastError": s.last_error,
                "audio": s.audio_state,
                "audioCodec": s.audio_codec,
            })
        return d


SESSIONS = {}                                   # serial -> SessionEntry
SESSIONS_LOCK = threading.Lock()


def get_entry(serial):
    with SESSIONS_LOCK:
        return SESSIONS.get(serial)


def _start_session(serial, settings):
    """在后台线程里拉起会话，避免阻塞 HTTP 请求。"""
    entry = SessionEntry(serial, settings)
    with SESSIONS_LOCK:
        SESSIONS[serial] = entry

    def worker():
        # 会话尺寸帧是 _video_loop 线程异步读的，读到之前 width/height 还是 0。
        # 所以「running」要等真的收到尺寸帧再置 —— 否则前端一看到 running 就连 WS，
        # 而这时候后端连设备分辨率都还不知道，状态栏会闪一下 0x0。
        sized = threading.Event()

        def on_session(w, h, resized):
            entry.hub.publish(struct.pack(">III", 0x80000000 | (1 if resized else 0), w, h))
            sized.set()

        # 先问设备「你到底有哪些编码器」，再把选择落到一个真的能用的上面。
        # 以前是「auto 就干脆不传、让设备端自己猜；用户点名 h265 就等它崩了再回退」——
        # 既然能提前问清楚，就没必要浪费一次注定失败的连接，更不该让用户在界面上
        # 选一个这台设备根本没有的编码器却什么都不知道（选了 AV1 实际在看 H.264）。
        use = dict(settings)
        avail = probe_video_encoders(serial, JAR_PATH, log)
        entry.encoders = avail or None
        want = str(use.get("codec") or "auto")
        if avail:
            picked = want if want in avail else \
                next((c for c in P.VIDEO_CODEC_PREFERENCE if c in avail), avail[0])
            use["codec"] = picked
            if want == "auto":
                entry.codec_note = "自动 → %s" % picked.upper()
            elif picked != want:
                entry.codec_note = "这台设备没有 %s 编码器，已改用 %s" % (want.upper(), picked.upper())
        # avail 为空 = 探测失败（设备离线之类），**不等于设备没有编码器**：
        # 原样的选择照送下去，让设备端自己回答，别替设备做决定。

        def attempt(now_settings):
            """按给定参数拉一次会话；起不来就直接抛，由调用方决定要不要换参数重来。"""
            sized.clear()
            sess = ScrcpySession(
                serial, JAR_PATH, log=log,
                on_session=on_session,
                on_packet=lambda pts_flags, data: entry.hub.publish(
                    struct.pack(">QI", pts_flags, len(data)) + data),
                on_audio_packet=lambda pts_flags, data: entry.audio_hub.publish(
                    struct.pack(">QI", pts_flags, len(data)) + data),
            )
            entry.session = sess
            entry.settings = now_settings
            sess.start(now_settings)
            # ⚠️ 刻意**不**把「要关键帧」接到 MSG_RESET_VIDEO 上（这里以前是这么接的）。
            # 实测（tools/_t29e_reset_adb.py，用 adb 通知栏开合造确定的动画）：
            # 这台设备的 OMX.redroid.h264.encoder 被 resetVideo 重启之后**不再补 CONFIG**，
            # reset 前 82.8 帧/秒，reset 后 30 秒只剩 1 帧；而前端 resetDecoder 后
            # `configured=false`、H.264 又没有「拿关键帧当配置」的兜底 ⇒ 之后每一帧都被丢掉，
            # 就是主人报的「连上一段时间就黑」，而且**永久**黑。
            # 所以订阅者拿不到干净起点时，就等编码器自然的那个 IDR —— 本机约 1.7 秒一个，
            # 很密，代价只是丢一小段画面，不下发任何控制命令。
            entry.hub.set_key_requester(None)
            if not sized.wait(timeout=SESSION_SIZE_TIMEOUT):
                if sess.last_error:
                    raise RuntimeError(sess.last_error)
                log("WARN: %s 等了 %d 秒还没收到会话尺寸，直接按就绪处理"
                    % (serial, SESSION_SIZE_TIMEOUT))
            return sess

        try:
            try:
                sess = attempt(dict(use))
            except Exception:
                # 设备端在建编码器那一步会直接杀掉自己，光靠重试同一个参数接不住。
                # 把死掉那次的设备输出读干净，看它明确说「建不出哪个编码器」，再换一个重来。
                # （这句话往往比我们察觉「流断了」晚一线，所以先 drain 再下结论。）
                dead = entry.session
                if dead is not None:
                    try:
                        dead.drain_server_log()
                    except Exception:
                        pass
                bad = getattr(dead, "bad_codec", "") or ""
                if bad == "h265":
                    entry.no_h265 = True
                rest = [c for c in (avail or []) if c != bad]
                if bad and rest:
                    # 从可用清单里剔掉它，按偏好挑下一个
                    nxt = next((c for c in P.VIDEO_CODEC_PREFERENCE if c in rest), rest[0])
                    log("%s 建不出 %s 编码器，改用 %s 重试" % (serial, bad.upper(), nxt.upper()))
                    entry.session = None
                    if dead is not None:
                        try:
                            dead.stop()
                        except Exception:
                            pass
                    entry.codec_note = "这台设备建不出 %s 编码器，已改用 %s" % (bad.upper(), nxt.upper())
                    retry = dict(use)
                    retry["codec"] = nxt
                    sess = attempt(retry)
                elif bad:
                    # 没有别的可选了。不擅自替他改设置，但也不能只丢一句「流已结束」——
                    # 把原因和这台设备到底能编哪些讲清楚。
                    if dead is not None:
                        try:
                            dead.stop()
                        except Exception:
                            pass
                    raise RuntimeError(
                        "这台设备建不出 %s 编码器（可用：%s），请换一个编码协议"
                        % (bad.upper(), "、".join(avail) if avail else "未知"))
                else:
                    raise

            entry.phase = "running"
            entry.error = ""
            log("会话已就绪：%s (%s) %dx%d %s" % (serial, sess.device_name,
                                                  sess.width, sess.height, sess.codec))
            if entry.settings.get("startScreenOff"):
                # 设备端 4.1 没有 turn_screen_off 这个启动参数了，改在连上之后
                # 用控制消息关屏 —— 效果一样，还少一个会被认成未知参数的坑。
                try:
                    sess.send_control(P.build_set_display_power(False))
                    log("已按设置关闭设备屏幕")
                except Exception as exc:
                    log("WARN: 关屏指令发送失败：%s" % exc)

            # 连上了就把这台设备的状态读一遍、存进设备记录（型号/品牌、Android 版本与
            # SDK、分辨率与密度、可用编码器），设置弹窗的「设备信息」一栏用的就是它。
            # 放在 running 之后：读状态还要再跑几条 adb，别让它拖慢「画面什么时候出来」。
            # 编码器直接用上面探好的那份，不再跑一次 app_process（那一下要好几秒）。
            try:
                prof = device_profile(serial, encoders=entry.encoders, log=log)
                if prof:
                    record_add(serial, profile=prof)
                    log("已记录设备状态：%s" % profile_summary(prof))
                else:
                    log("WARN: 没读到 %s 的设备状态（设备这次没答话）" % serial)
            except Exception as exc:
                log("WARN: 读取设备状态失败：%s" % exc)
        except Exception as exc:
            entry.phase = "error"
            entry.error = str(exc)
            # 起不来的这次也得**收干净**：会话可能已经建了本地转发、也在设备上拉起了
            # scrcpy-server。以前这里只把 entry.session 置空就走人 —— 自研协议那会儿
            # 没有转发，留下的只有设备端进程（stop() 里的兜底 pkill 也一起被跳过）；
            # 改走 adb forward 之后，adb 的转发表里会永久挂着一条死条目
            # （实测：日志报完「流已结束」，`forward --list` 里还留着 tcp:41813）。
            dead = entry.session
            entry.session = None
            if dead is not None:
                try:
                    dead.stop()          # 幂等：已经停过的会直接返回
                except Exception:
                    pass
            entry.hub.set_key_requester(None)
            entry.hub.close()
            entry.audio_hub.close()
            log("ERROR: %s 启动失败：%s" % (serial, exc))

    threading.Thread(target=worker, name="scrcpy-start", daemon=True).start()
    return entry


def _stop_session(serial, expected_entry=None):
    with SESSIONS_LOCK:
        entry = SESSIONS.get(serial)
        # 只有明确点名了 expected_entry 时，才拒绝停一个已经被换掉的会话：
        # 延迟停会话的定时器是按「当时那个 entry」排的，期间可能已经被重连换新了。
        if expected_entry is not None and entry is not expected_entry:
            return False
        entry = SESSIONS.pop(serial, None)
    if entry is None:
        return False
    entry.phase = "idle"
    entry.hub.close()
    entry.audio_hub.close()
    sess = entry.session
    entry.session = None
    if sess is not None:
        try:
            sess.stop()
        except Exception as exc:
            log("WARN: 停止会话出错：%s" % exc)
    log("会话已停止：%s" % serial)
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "scrcpy-fnos"

    # ---------- 基础输出 ----------

    def _reply(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        # 整个回写过程都要包住：客户端提前断开时，send_response/end_headers
        # 一样会抛 BrokenPipe —— 只在 write 上兜底是不够的。
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, obj, code=200):
        self._reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        # 防目录穿越
        if target != FRONTEND_DIR and not target.startswith(FRONTEND_DIR + os.sep):
            self._reply(404, "not found", "text/plain; charset=utf-8")
            return
        if not os.path.isfile(target):
            self._reply(404, "not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(target)[1].lower()
        try:
            with open(target, "rb") as fh:
                self._reply(200, fh.read(), MIME.get(ext, "application/octet-stream"))
        except OSError:
            self._reply(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass

    # ---------- GET ----------

    def _redirect(self, location):
        try:
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = parsed.path

        # 网关打开的地址是 /app/scrcpy-fnos（没有尾斜杠）。
        # 这时页面里的相对路径会被解析到上一层变成 /app/app.js，直接 404 ——
        # 表现就是「页面出来了，但点什么都没反应」（JS 压根没加载）。
        # 所以这里重定向到带尾斜杠的规范形式。ScrcpyNas 也是这么做的。
        if raw_path == BASE_PATH:
            self._redirect(BASE_PATH + "/")
            return

        path = strip_base(raw_path)
        query = urllib.parse.parse_qs(parsed.query)
        serial = (query.get("serial") or [""])[0].strip()

        if path == "/healthz":
            self._reply(200, "ok", "text/plain; charset=utf-8")
            return

        if path == "/api/status":
            with SESSIONS_LOCK:
                items = [e.info() for e in SESSIONS.values()]
            self._json({"ok": True, "sessions": items})
            return

        if path == "/api/devices":
            self._json({"ok": True, "devices": self._devices()})
            return

        if path == "/api/screen":
            # 给前端算「推荐码率」用：码率该给多少，取决于真正要编多少像素。
            # 竞品那套「不管什么分辨率都 8 Mbps」就是在这儿出的问题 ——
            # 同样 8 Mbps，720p 绰绰有余，1080p 明显不够。
            info = device_screen(serial) if serial else None
            self._json({"ok": bool(info), "serial": serial, "screen": info})
            return

        if path == "/api/adb":
            # 「添加设备」弹窗里列出来的候选：adb 扫到的设备 + 已经添加过的（标记一下）
            known = {r["serial"] for r in _load_records()}
            found = []
            for d in self._adb_devices():
                s = d.get("serial") or ""
                if s:
                    found.append({"serial": s, "state": d.get("state") or "unknown",
                                  "added": s in known})
            self._json({"ok": True, "devices": found})
            return

        if path == "/api/files":
            path_v = (query.get("path") or ["/sdcard"])[0]
            try:
                self._json({"ok": True, "serial": serial,
                            **fileops.list_dir(serial, path_v)})
            except fileops.FileOpError as e:
                # 界面会显示这句，日志里也得留同一句 —— 不然「打不开」只能靠猜。
                log("列目录失败 %s %s：%s" % (serial, path_v, e))
                self._json({"ok": False, "error": str(e)})
            return

        if path == "/api/files/download":
            self._api_download(serial, (query.get("path") or [""])[0])
            return

        if path == "/ws/video":
            self._ws_video(serial)
            return

        if path == "/ws/audio":
            self._ws_audio(serial)
            return

        if path == "/ws/control":
            self._ws_control(serial)
            return

        if path == "/ws/shell":
            self._ws_shell(serial,
                           (query.get("rows") or ["24"])[0],
                           (query.get("cols") or ["80"])[0])
            return

        self._static(path)

    def _devices(self):
        """左侧列表 = 我添加过的设备（记录）∪ 正在跑的会话。

        记录是主角：没记录过的设备不进列表，「删除」才有可能真删掉。
        会话表也并进来，是为了照顾老用法（直接 POST /api/connect 建了会话却没记录），
        免得连上了列表里反而找不到这台设备。
        """
        with SESSIONS_LOCK:
            entries = {s: e.info() for s, e in SESSIONS.items()}

        def row(serial, name, info, profile=None):
            item = {
                "serial": serial,
                "name": name or "",
                "state": "device",
                "phase": (info or {}).get("phase") or "idle",
                "error": (info or {}).get("error"),
                "noH265": bool((info or {}).get("noH265")),
                # 设备状态：每次连上都会重读并落盘，这里原样下发（没读到就是 {}）。
                "profile": profile or {},
            }
            for k in ("device", "codec", "width", "height", "audio", "audioCodec"):
                if (info or {}).get(k) is not None:
                    item[k] = info[k]
            # encoders / codecNote：让界面能如实显示「这台设备能编哪些、实际在用哪个」，
            # 而不是像以前那样只靠一句写死的「自动＝优先 H.265」。
            item["encoders"] = (info or {}).get("encoders")
            item["codecRequested"] = (info or {}).get("codecRequested")
            item["codecNote"] = (info or {}).get("codecNote") or ""
            return item

        out = []
        seen = set()
        for rec in _load_records():
            serial = rec["serial"]
            seen.add(serial)
            out.append(row(serial, rec.get("name"), entries.get(serial), rec.get("profile")))
        for serial, info in entries.items():
            if serial not in seen:
                out.append(row(serial, "", info))
        return out

    def _adb_devices(self):
        """adb 里能看到的设备（「添加设备」弹窗用；不进左侧列表）。"""
        try:
            return list_devices()
        except Exception:
            return []

    def do_HEAD(self):
        path = strip_base(urllib.parse.urlparse(self.path).path)
        try:
            self.send_response(200 if path in ("/", "/index.html", "/healthz") else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ---------- POST ----------

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = strip_base(parsed.path)

        # ⚠️ 上传这条必须在 `_read_json()` **之前**分流。
        # 它的请求体就是文件本身（裸字节流），一旦交给 _read_json 去 json.loads，
        # 整包会被它吃干、而且会被它 64 KB 的上限截断 —— 表现就是"上传成功但文件残缺"。
        if path == "/api/files/upload":
            self._api_upload(parsed)
            return

        body = self._read_json()

        if path == "/api/connect":
            serial = (body.get("serial") or "").strip()
            if not serial:
                self._json({"ok": False, "error": "缺少设备 serial"})
                return
            existing = get_entry(serial)
            if existing is not None and existing.phase in ("starting", "running"):
                self._json({"ok": True, "phase": existing.phase, "serial": serial, "reused": True})
                return
            settings = norm_settings(body.get("settings") or body)
            record_add(serial)          # 直接填地址连上的，也顺手记进列表
            _start_session(serial, settings)
            log("开始连接：%s %s" % (serial, settings))
            self._json({"ok": True, "phase": "starting", "serial": serial, "settings": settings})
            return

        if path == "/api/disconnect":
            serial = (body.get("serial") or "").strip()
            if serial:
                stopped = _stop_session(serial)
            else:                       # 不带 serial 就全停，给「全部断开」用
                with SESSIONS_LOCK:
                    serials = list(SESSIONS.keys())
                for s in serials:
                    _stop_session(s)
                stopped = bool(serials)
            self._json({"ok": True, "stopped": bool(stopped)})
            return

        # ---- 无线配对（Android 11+） ----
        if path == "/api/adb/pair":
            host = (body.get("host") or "").strip()
            port = str(body.get("port") or "").strip()
            code = (body.get("code") or "").strip()
            if not (host and port and code):
                self._json({"ok": False, "error": "主机、配对端口、配对码都要填"})
                return
            ok, msg = adb_pair(host, port, code)
            log("无线配对 %s:%s → %s" % (host, port, "成功" if ok else "失败"))
            self._json({"ok": ok, "message": msg,
                        "error": None if ok else (msg or "配对失败")})
            return

        # ---- 设备记录：添加 / 删除 / 改名 ----
        if path in ("/api/devices/add", "/api/devices/rename"):
            serial = (body.get("serial") or "").strip()
            if not serial:
                self._json({"ok": False, "error": "缺少设备 serial"})
                return
            name = (body.get("name") or "").strip()[:60]
            if path.endswith("/add"):
                record_add(serial, name, top=bool(body.get("top")))
                log("已添加设备：%s%s" % (serial, ("（" + name + "）") if name else ""))
            else:
                record_rename(serial, name)
                log("设备改名：%s → %r" % (serial, name))
            self._json({"ok": True, "devices": self._devices()})
            return

        if path == "/api/devices/remove":
            serial = (body.get("serial") or "").strip()
            if not serial:
                self._json({"ok": False, "error": "缺少设备 serial"})
                return
            # 先停投屏再删记录：反过来会让「正在跑的会话」把列表项又带回来
            _stop_session(serial)
            record_remove(serial)
            log("已删除设备：%s" % serial)
            self._json({"ok": True, "devices": self._devices()})
            return

        # ---- 文件动作（都走 JSON body）----
        if path in ("/api/files/mkdir", "/api/files/rename", "/api/files/delete",
                    "/api/files/install", "/api/files/execute"):
            self._api_file_action(path, body)
            return

        self._reply(404, "not found", "text/plain; charset=utf-8")

    # ---------- 文件管理 ----------

    def _api_file_action(self, path, body):
        """文件类动作：都只用 serial + path（+ rename 的 to），返回一句人话。"""
        serial = (body.get("serial") or "").strip()
        target = (body.get("path") or "").strip()
        if not serial or not target:
            self._json({"ok": False, "error": "缺少 serial 或 path"})
            return
        try:
            if path == "/api/files/mkdir":
                where = fileops.mkdir(serial, target)
                self._json({"ok": True, "message": "已新建目录", "path": where})
            elif path == "/api/files/rename":
                to = (body.get("to") or "").strip()
                if not to:
                    self._json({"ok": False, "error": "缺少目标路径 to"})
                    return
                where = fileops.rename(serial, target, to)
                self._json({"ok": True, "message": "已重命名", "path": where})
            elif path == "/api/files/delete":
                self._json({"ok": True, "message": fileops.delete(serial, target)})
            elif path == "/api/files/install":
                msg = fileops.install_apk(serial, target)
                log("安装成功 %s：%s" % (target, msg))
                self._json({"ok": True, "message": msg})
            else:                                   # execute
                self._json({"ok": True, **fileops.execute_sh(serial, target)})
        except fileops.FileOpError as e:
            log("%s失败 %s → %s：%s"
                % (FILE_ACTIONS.get(path, "文件操作"), serial, target, e))
            self._json({"ok": False, "error": str(e)})

    def _api_upload(self, parsed):
        """上传：**请求体就是文件字节流**，不做 multipart（省掉一整层解析）。

        两条通道都在这里用上：
          - 小文件（≤ SMALL_UPLOAD_MAX）先整包读进内存，走 `push_bytes` ——
            它内部是「自研 sync 优先、失败退系统 adb push」，所以这条路上有真正的兜底。
          - 大文件只能纯流式走 sync：边从请求体读边往设备写，**不落临时文件**、
            进度是真实字节数。代价是一旦 sync 不灵，没法回头重来（体已经发出去一半了），
            只能老实报错。
        """
        q = urllib.parse.parse_qs(parsed.query)
        serial = (q.get("serial") or [""])[0].strip()
        target = (q.get("path") or [""])[0].strip()
        if not serial or not target:
            self._json({"ok": False, "error": "缺少 serial 或 path"})
            return
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        # 0 字节是合法的：前端拖一个空文件上来，Content-Length 就是 0，
        # 该老老实实给他建一个空文件，而不是回一句"请求体是空的"。
        if total < 0:
            self._json({"ok": False, "error": "Content-Length 不合法"})
            return
        try:
            mtime = int(self.headers.get("X-File-Mtime") or 0) or None
        except ValueError:
            mtime = None

        try:
            if total <= SMALL_UPLOAD_MAX:
                data = self._read_raw(total)
                written = fileops.push_bytes(serial, target, data, mtime=mtime)
            else:
                left = [total]

                def reader(n):
                    if left[0] <= 0:
                        return b""
                    got = self.rfile.read(min(n, left[0]))
                    left[0] -= len(got)
                    return got or b""

                written = fileops.sync_push(serial, target, total, reader, mtime=mtime)
        except fileops.FileOpError as e:
            log("上传失败 %s → %s：%s" % (serial, target, e))
            self._json({"ok": False, "error": str(e)})
            return
        log("上传完成 %s：%s（%d 字节）" % (serial, target, written))
        self._json({"ok": True, "path": target, "bytes": written})

    def _read_raw(self, n):
        """按声明的长度把请求体读干净。

        `rfile` 是带缓冲的，`read(n)` 正常一次就够；但客户端可能分片到达，
        所以这里循环读到够数或读空为止 —— 少读几个字节会让下一个请求从头错位。
        """
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def _api_download(self, serial, remote):
        """下载：先把大小问出来，再边收边发。

        顺序很关键：**Content-Length 必须在开始发正文之前定下来**，
        所以先做一次 STAT。顺带这一下也验证了 sync 这条通道是通的 ——
        等都开始往外吐字节了才发现通道不通，那时候响应头已经发出去了，没法改口。
        """
        if not serial or not remote:
            self._json({"ok": False, "error": "缺少 serial 或 path"})
            return
        try:
            info = fileops.sync_stat(serial, remote)
        except fileops.FileOpError as e:
            log("下载失败（问大小）%s %s：%s" % (serial, remote, e))
            self._json({"ok": False, "error": str(e)})
            return
        if info["directory"]:
            self._json({"ok": False, "error": "这是一个目录，不能下载"})
            return

        size = info["size"]
        name = remote.rstrip("/").rsplit("/", 1)[-1] or "download"
        self.close_connection = True        # 流式发完就断，别让 keep-alive 复用
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        try:
            got = fileops.sync_pull(serial, remote, self.wfile.write)
        except Exception as e:
            log("下载中断 %s：%s（%s）" % (remote, e, serial))
            return
        log("下载完成 %s：%s（%d 字节）" % (serial, remote, got))

    def do_DELETE(self):
        """DELETE /api/files?serial=&path= —— 和 POST body 那版走同一个动作。"""
        parsed = urllib.parse.urlparse(self.path)
        path = strip_base(parsed.path)
        q = urllib.parse.parse_qs(parsed.query)
        if path == "/api/files":
            self._api_file_action("/api/files/delete", {
                "serial": (q.get("serial") or [""])[0],
                "path": (q.get("path") or [""])[0],
            })
            return
        self._reply(404, "not found", "text/plain; charset=utf-8")

    # ---------- WebSocket ----------

    def _ws_video(self, serial):
        ws = wsproto.handshake(self.headers, self.connection)
        if ws is None:
            self._reply(400, "bad websocket handshake", "text/plain; charset=utf-8")
            return
        self.close_connection = True       # 这个 socket 从此由我们自己管
        entry = get_entry(serial)
        if entry is None:
            try:
                ws.close()
            except Exception:
                pass
            return

        entry.client_connected()
        sock = self.connection
        sub = entry.hub.subscribe()
        log("视频 WebSocket 已连接：%s" % serial)
        try:
            while True:
                # 顺手读一下客户端方向：处理 ping，也顺便发现断开
                try:
                    readable, _, _ = select.select([sock], [], [], 0)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        ws.recv()
                    except wsproto.WSError:
                        break
                try:
                    frame = sub.get(timeout=0.5)
                except queue.Empty:
                    continue
                if frame is None:          # 会话结束
                    break
                try:
                    ws.send_bytes(frame)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            entry.hub.unsubscribe(sub)
            entry.client_disconnected()
            log("视频 WebSocket 已断开：%s" % serial)

    def _ws_audio(self, serial):
        """音频流。包格式和视频那条完全一样（12 字节头 + 负载）。"""
        ws = wsproto.handshake(self.headers, self.connection)
        if ws is None:
            self._reply(400, "bad websocket handshake", "text/plain; charset=utf-8")
            return
        self.close_connection = True
        entry = get_entry(serial)
        if entry is None:
            try:
                ws.close()
            except Exception:
                pass
            return

        entry.client_connected()
        sock = self.connection
        sub = entry.audio_hub.subscribe()
        log("音频 WebSocket 已连接：%s" % serial)
        try:
            while True:
                try:
                    readable, _, _ = select.select([sock], [], [], 0)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        ws.recv()
                    except wsproto.WSError:
                        break
                try:
                    frame = sub.get(timeout=0.5)
                except queue.Empty:
                    continue
                if frame is None:
                    break
                try:
                    ws.send_bytes(frame)
                except Exception:
                    break
        except Exception:
            pass
        finally:
            entry.audio_hub.unsubscribe(sub)
            entry.client_disconnected()
            log("音频 WebSocket 已断开：%s" % serial)

    def _ws_control(self, serial):
        ws = wsproto.handshake(self.headers, self.connection)
        if ws is None:
            self._reply(400, "bad websocket handshake", "text/plain; charset=utf-8")
            return
        self.close_connection = True
        entry = get_entry(serial)
        if entry is None:
            try:
                ws.close()
            except Exception:
                pass
            return
        entry.client_connected()
        log("控制 WebSocket 已连接：%s" % serial)
        try:
            while True:
                opcode, payload = ws.recv()
                if opcode == wsproto.OP_TEXT:
                    self._handle_control(serial, payload)
                # 二进制空包是保活，不处理
        except wsproto.WSError:
            pass
        except Exception:
            pass
        finally:
            ws.close()
            entry.client_disconnected()
            log("控制 WebSocket 已断开：%s" % serial)

    def _ws_shell(self, serial, rows, cols):
        """交互终端。

        收发共用一个循环，**不开线程**：`shellws` 的读是带超时的（超时返回空列表，
        终端安静时本来就没数据），所以"每轮 select 一下客户端 + 读一下设备"就够，
        不需要再引入一个读线程和它带来的一堆锁。
        """
        ws = wsproto.handshake(self.headers, self.connection)
        if ws is None:
            self._reply(400, "bad websocket handshake", "text/plain; charset=utf-8")
            return
        self.close_connection = True

        try:
            rows_i, cols_i = int(rows), int(cols)
        except ValueError:
            rows_i, cols_i = 24, 80
        try:
            sess = shellws.ShellSession(serial, rows=rows_i, cols=cols_i).open()
        except Exception as e:
            log("终端打开失败 %s：%s" % (serial, e))
            try:
                ws.send_text(json.dumps({"t": "error", "error": str(e)}, ensure_ascii=False))
                ws.close()
            except Exception:
                pass
            return

        sock = self.connection
        log("终端 WebSocket 已连接：%s（%dx%d）" % (serial, rows_i, cols_i))
        try:
            while True:
                # 1) 客户端方向：select 探一下，别让 recv 把输出方向堵死
                try:
                    readable, _, _ = select.select([sock], [], [], 0.01)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        opcode, payload = ws.recv()
                    except wsproto.WSError:
                        break
                    if opcode == wsproto.OP_BIN:
                        sess.write(payload)            # 键盘输入，原样进 stdin
                    else:
                        # ⚠️ 这里必须把**解析好的对象**交给 _int，别再传原始 bytes。
                        # 传 bytes 时 `_int` 会在 `.get()` 上抛 AttributeError、
                        # 被自己的兜底吞掉、返回默认的 24x80 —— 于是 resize 变成了
                        # "设成当前尺寸"，被短路掉，**看起来像 resize 完全没生效**。
                        msg = _shell_ctrl(payload)
                        t = msg.get("t") if msg else None
                        if msg is None:
                            sess.write(payload)        # 不是 JSON，就当输入（容忍笨客户端）
                        elif t == "close":
                            break
                        elif t == "resize":
                            sess.resize(_int(msg, "rows", sess.rows),
                                        _int(msg, "cols", sess.cols))

                # 2) 设备方向：一批帧原样往前端推
                for fid, payload in sess.read(timeout=0.03):
                    if fid in (shellws.ID_STDOUT, shellws.ID_STDERR):
                        ws.send_bytes(payload)
                    elif fid == shellws.ID_EXIT:
                        ws.send_text(json.dumps({"t": "exit", "code": sess.exit_code}))

                if not sess.alive:
                    break
                sess.tick()             # 兑现攒下的 resize（旁路/退路注入都靠它）
        except Exception:
            pass
        finally:
            sess.close()
            try:
                ws.close()
            except Exception:
                pass
            log("终端 WebSocket 已断开：%s" % serial)

    def _handle_control(self, serial, payload):
        """把前端发来的 JSON 指令翻成 scrcpy 的控制消息。"""
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        entry = get_entry(serial)
        if entry is None or entry.session is None:
            return
        sess = entry.session

        kind = msg.get("kind")
        w, h = sess.width, sess.height
        data = None

        if kind == "touch":
            data = P.build_touch(
                int(msg.get("action", 0)),
                int(msg.get("pointerId", 0)),
                int(msg.get("x", 0)), int(msg.get("y", 0)), w, h)
        elif kind == "key":
            data = P.build_keycode(int(msg.get("action", 0)), int(msg.get("keycode", 0)))
        elif kind == "scroll":
            data = P.build_scroll(int(msg.get("x", 0)), int(msg.get("y", 0)), w, h,
                                  vscroll=float(msg.get("vscroll", 0)))
        elif kind == "text":
            data = P.build_text(msg.get("text", ""))
        elif kind == "clipboard":
            data = P.build_set_clipboard(int(time.time() * 1000),
                                         msg.get("text", ""), bool(msg.get("paste")))
        elif kind == "rotate":
            data = P.build_rotate_device()
        elif kind == "resetVideo":
            # ⚠️ 遗留入口：**我们的前端已经不发了**（它现在只等编码器自然的 IDR）。
            # 设备端收到会 reset 编码器（signalEndOfInputStream），重启后第一帧是 IDR，
            # 但这台设备的 OMX.redroid.h264.encoder 重启后**不再补 CONFIG**，
            # 前端就永久配不上 —— 「连上一段时间就黑」的根因。
            # 保留这条只是为了协议完整性 / 手动排障；别再把它接回任何自动重起步路径。
            log("WARN: 收到 resetVideo 控制指令（会重启设备编码器，慎用）")
            data = P.build_reset_video()
        elif kind == "power":
            data = P.build_set_display_power(bool(msg.get("on")))
        elif kind == "panel":
            which = msg.get("which")
            if which == "notify":
                data = P.build_expand_notification_panel()
            elif which == "settings":
                data = P.build_expand_settings_panel()
            else:
                data = P.build_collapse_panels()
        elif kind == "nav":
            # 返回 / 主页：按下 + 抬起各一次
            code = 4 if msg.get("which") == "back" else 3
            sess.send_control(P.build_keycode(P.ACTION_DOWN, code))
            data = P.build_keycode(P.ACTION_UP, code)

        if data:
            sess.send_control(data)


class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # 客户端提前断开不是错误 —— 探活超时、页面刷新都会造成它。
        # 默认实现会把整段 traceback 打到 stderr，而 cmd/main 把 stderr 收进 info.log，
        # 于是 info.log 会被这种噪音淹掉，真正的问题反而看不见。
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def main():
    if os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass

    # 启动第一件事：把 adb server 那份日志捞到不会被重启清掉的地方。
    # 它是「设备怎么掉线的」唯一现场，而缺省位置在 /tmp（NAS 一重启就空）。
    src, dest = adbtool.handle_server_log()
    if dest:
        log("已留档 adb server 日志：%s → %s" % (src, dest))

    srv = UnixHTTPServer(SOCK_PATH, Handler)
    os.chmod(SOCK_PATH, 0o666)
    log("listening on %s" % SOCK_PATH)
    if not os.path.isfile(JAR_PATH):
        log("WARN: 找不到 %s，投屏会失败" % JAR_PATH)

    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with SESSIONS_LOCK:
            serials = list(SESSIONS.keys())
        for s in serials:
            _stop_session(s)
        srv.server_close()
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
