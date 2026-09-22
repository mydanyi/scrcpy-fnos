#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投屏会话：拉起设备端 scrcpy-server，并把它吐出来的裸流接管过来。

链路：
    adb connect / push / shell 启动 server   ← 用**自带**的 adb（见 adbtool.py：
                                               系统那份 29.0.6 没有 `adb pair`，
                                               Android 11+ 的无线调试配不上对）
    → `adb forward` 打开设备端的 localabstract:scrcpy_<scid>  ← 见 adblink.py
    → 读裸流（video / audio / control 三条）

⚠️ 2026-09-22 更正：以前这里"不走 adb forward、自己说 adb 协议"的理由（转发不出数据）
**是错的**。当时的现象是只开了一条连接造成的假象；而真正致命的是
**Android 11+ 无线调试的 adbd 是 TLS 的**，自己说的明文 adb 协议会被回一个 `A_STLS`
（0x534C5453）直接卡死 —— 真手机永远连不上。现在改成让真 adb 去转发，它自己会处理 TLS。

两个必须守住的顺序：
  1. 设备端的 accept 顺序是 video → audio → control，流必须按这个顺序开，
     而且要**按这个顺序开满**（audio 关了就是 2 条）——
     设备端要等全部 accept 完才发设备信息，少开一条就永远读不到设备名。
  2. dummy byte 只在**第一个**被 accept 的 socket 上收到，也就是只有 video 有。
"""

import random
import re
import subprocess
import threading
import time

import adblink
import adbtool
import h264norm
import scrcpy_proto as P

SERVER_JAR_REMOTE = "/data/local/tmp/scrcpy-server.jar"
SERVER_CLASS = "com.genymobile.scrcpy.Server"
SCRCPY_VERSION = "4.1"

DEVICE_NAME_LEN = 64
STREAM_OPEN_TIMEOUT = 25.0
ADB_SHELL_TIMEOUT = 30
# 无线 + TLS 的设备，`adb connect` 之后要过一会儿才真的可用（可能先 offline）。
# 以前只 sleep 0.4 秒就往下走，push 直接报 device not found —— 这就是那个假失败。
ADB_CONNECT_TIMEOUT = 25.0

# 设备可用编码器的探测结果缓存：serial -> (探测时间, [编码器名...])
# 「哪些编码器能用」是设备的固有属性，变不了几回；每连一次都跑一遍探测纯属白等。
_ENC_CACHE = {}
_ENC_CACHE_TTL = 300.0


def probe_video_encoders(serial, jar_path, log=print, force=False, timeout=30):
    """问设备「你到底有哪些视频编码器」，拿 scrcpy 自己会认的那一份清单。

    为什么不用 media_codecs*.xml：那里面登记了、和设备端真的能建起来，是两码事。
    这台 redroid 的 xml 里明明有 OMX.redroid.h264.encoder，但 MediaCodecList 根本不吐它，
    scrcpy 也不会选它 —— 照着 xml 判断就会得出错误结论。
    list_encoders 走的就是 scrcpy 选编码器的那条路（MediaCodecList），才是准的。

    返回 [] 表示「没探测出来」（设备离线、jar 没推上去、超时……），
    **不等于「设备没有编码器」** —— 调用方必须区分这两种情况，否则会误锁用户的选择。
    """
    now = time.time()
    if not force:
        hit = _ENC_CACHE.get(serial)
        if hit and now - hit[0] < _ENC_CACHE_TTL:
            return list(hit[1])

    if ":" in serial and not serial.startswith("emulator-"):
        try:
            adbtool.run("-s", serial, "connect", serial,
                        capture_output=True, timeout=10)
        except Exception:
            pass
    try:
        adbtool.run("-s", serial, "push", jar_path, SERVER_JAR_REMOTE,
                    capture_output=True, timeout=90)
    except Exception as exc:
        log("WARN: 推 server jar 失败，跳过编码器探测：%s" % exc)
        return []

    try:
        p = adbtool.run("-s", serial, "shell",
                        "CLASSPATH=" + SERVER_JAR_REMOTE, "app_process", "/",
                        SERVER_CLASS, SCRCPY_VERSION, "list_encoders=true", "log_level=info",
                        capture_output=True, timeout=timeout)
    except Exception as exc:
        log("WARN: 编码器探测没跑完：%s" % exc)
        return []

    text = (p.stdout or b"").decode("utf-8", "replace") + \
           (p.stderr or b"").decode("utf-8", "replace")
    names = []
    for m in re.finditer(r"--video-codec=([a-z0-9]+)", text):
        if m.group(1) not in names:
            names.append(m.group(1))

    if names:
        _ENC_CACHE[serial] = (now, list(names))
        log("设备可用视频编码器：%s" % " / ".join(names))
    else:
        log("WARN: 编码器探测没拿到清单（设备端输出：%s）" % (text.strip()[:200] or "(空)"))
    return names


def video_encoder_summary(serial, jar_path, force=False):
    """给前端用的一份「这台设备能编哪些视频」的小报告。探测失败返回 None。"""
    names = probe_video_encoders(serial, jar_path, log=lambda *a: None, force=force)
    return names or None


# 屏幕尺寸缓存：serial -> (时间戳, {"width","height","density"})
# 前端要靠它按「实际会编多少像素」来推荐码率 —— 720p 和 1080p 需要的码率差一倍多。
_SCREEN_CACHE = {}
_SCREEN_CACHE_TTL = 60.0


def device_screen(serial):
    """设备的物理屏尺寸与密度。拿不到返回 None。

    用的是 `wm size`，不是会话报上来的尺寸 —— 会话尺寸还叠了 max_size 这类缩放，
    拿来做「这台设备原生多少像素」的依据是错的。
    """
    now = time.time()
    hit = _SCREEN_CACHE.get(serial)
    if hit and now - hit[0] < _SCREEN_CACHE_TTL:
        return dict(hit[1])

    try:
        p = adbtool.run("-s", serial, "shell", "wm size; wm density",
                        capture_output=True, timeout=15)
    except Exception:
        return None
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    if not text.strip():
        return None

    # 输出形如（Override 那行是用户改过的逻辑尺寸，优先用它）：
    #   Physical size: 720x1280
    #   Override size: 1080x1920
    #   Physical density: 320
    #   Override density: 480
    def last_num(pattern):
        hits = re.findall(pattern, text)
        return int(hits[-1]) if hits else 0

    w = last_num(r"(?:Physical|Override) size:\s*(\d+)\s*x\s*\d+")
    h = last_num(r"(?:Physical|Override) size:\s*\d+\s*x\s*(\d+)")
    d = last_num(r"(?:Physical|Override) density:\s*(\d+)")
    if not w or not h:
        return None
    info = {"width": w, "height": h, "density": d}
    _SCREEN_CACHE[serial] = (now, dict(info))
    return info


# ==================== 设备状态（连上一次读一遍） ====================
# 会话自己能报上来的只有 device_name（型号）和视频尺寸（还叠了 max_size 缩放），
# 既没有厂商、也没有系统版本 —— 用户想知道「这台到底是安卓几」，界面上没来源。
# 所以连上之后单独读一遍系统属性，落进设备记录里。
#
# 读哪些：主人说「能存的都存，又用不了多少空间」。属性本身都是一行字符串，
# 几十条也才几 KB，没必要挑挑拣拣 —— 但也不能整份 dump 存下来（几百条里有
# 一堆 build 时间戳之类的东西，看不出人话）。下面这份是「一眼能看懂、且对
# 投屏有参考价值」的那一批。
PROFILE_PROPS = (
    ("brand", "ro.product.brand"),
    ("manufacturer", "ro.product.manufacturer"),
    ("model", "ro.product.model"),
    ("product", "ro.product.name"),
    ("device", "ro.product.device"),
    ("board", "ro.product.board"),
    ("androidVersion", "ro.build.version.release"),
    ("sdk", "ro.build.version.sdk"),
    ("buildId", "ro.build.id"),
    ("buildType", "ro.build.type"),
    ("securityPatch", "ro.build.version.security_patch"),
    ("abi", "ro.product.cpu.abi"),
    ("soc", "ro.soc.model"),
    ("fingerprint", "ro.build.fingerprint"),
)

_PROFILE_RE = re.compile(r"^\[([^\]]+)\]:\s*\[(.*)\]\s*$")


def read_props(serial, keys=None, timeout=15):
    """一次 `getprop` 把要的那批系统属性读回来；读不到返回 {}。

    为什么不 `getprop <key>` 一条一条问：设备上装着几百条属性，一次 dump 加
    本地解析只要一个来回，逐条问就是十几个来回 —— 白白拖长刚连上那段时间。
    """
    try:
        p = adbtool.run("-s", serial, "shell", "getprop",
                        capture_output=True, timeout=timeout)
    except Exception:
        return {}
    text = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    props = {}
    for line in text.splitlines():
        # 形如：[ro.product.model]: [SHARK PRS-A0]
        m = _PROFILE_RE.match(line.strip())
        if m:
            props[m.group(1)] = m.group(2).strip()
    if not props:
        return {}
    want = PROFILE_PROPS if keys is None else keys
    out = {}
    for name, key in want:
        val = props.get(key, "")
        if val:
            out[name] = val
    return out


def device_profile(serial, jar_path=None, encoders=None, log=print):
    """读一遍这台设备的状态：厂商/型号、Android 版本与 SDK、分辨率与密度、可用编码器。

    「每次连上都重新读一遍」是有意的：刷了机、换了系统版本、接了别的屏，
    下次连上就该是新的 —— 不能停在第一次那会儿的记录上。

    encoders 可以外部传进来（会话启动时已经探过一遍，见 probe_video_encoders），
    那一下 app_process 要好几秒，没必要为了记录再跑一次。

    三个依赖（adb / 屏幕 / 编码器）都走的是各自带缓存的入口，且都能在单测里
    被替换掉 —— 见 tests/test_device_info.py。
    """
    prof = {}
    try:
        prof.update(read_props(serial))
    except Exception as exc:                       # 读属性失败不该影响会话
        log("WARN: 读系统属性失败：%s" % exc)

    try:
        screen = device_screen(serial)
    except Exception:
        screen = None
    if screen:
        for k in ("width", "height", "density"):
            if screen.get(k):
                prof[k] = int(screen[k])

    if encoders is None and jar_path:
        try:
            encoders = video_encoder_summary(serial, jar_path)
        except Exception:
            encoders = None
    if encoders:
        prof["encoders"] = [str(c) for c in encoders]

    if not prof:
        return {}                                  # 什么都没读到：别写一条空记录进去
    sdk = str(prof.get("sdk") or "")
    if sdk.isdigit():
        prof["sdk"] = int(sdk)
    prof["updatedAt"] = int(time.time())
    return prof


def profile_summary(prof):
    """给日志/界面看的一行人话：型号 · Android 13 · 1080x2400 · h264/h265。"""
    prof = prof or {}
    bits = []
    model = " ".join(x for x in (prof.get("brand"), prof.get("model")) if x)
    if model:
        bits.append(model)
    if prof.get("androidVersion"):
        bits.append("Android %s" % prof["androidVersion"])
    if prof.get("width") and prof.get("height"):
        bits.append("%dx%d" % (prof["width"], prof["height"]))
    if prof.get("encoders"):
        bits.append("/".join(prof["encoders"]))
    return " · ".join(bits)


def _effective_max_size(serial, target_height, angle=0):
    """把界面上选的「目标高度」换算成 scrcpy 的 max_size（最长边）。

    ⚠️ 语义要点：target_height 是**视频当前方向的高度目标**（480/720/1080/1440/2160），
    不是「限制最长边」—— 超宽屏（如 3440x1440）按最长边限就和按高度限完全不是一回事：
    按最长边限会把高度压到 3440/1080 之后的可怜尺寸，按高度限才保住了用户想要的纵向清晰度。
    所以这里先按屏幕宽高比、以目标高度为基准算出缩放后的宽，再取宽高的最大值当 max_size。

    angle 为 90/270 时视频会先被旋转，宽高互换，基准要按旋转后的尺寸来算。

    返回 0 表示「不追加 max_size，交给 scrcpy 原画」：目标高度 <=0（没选/不限）、
    设备尺寸拿不到，或者目标高度比原生高度还大 —— 都不放大，避免糊成一片。
    """
    if not target_height or target_height <= 0:
        return 0
    screen = device_screen(serial)
    if not screen:
        return 0
    w = screen.get("width") or 0
    h = screen.get("height") or 0
    if w <= 0 or h <= 0:
        return 0
    # 90 / 270 会先把画面转过来，长边和短边对调，缩放基准跟着换。
    if angle in (90, 270):
        w, h = h, w
    # 目标高度高于原生高度就不放大（scrcpy 自己也不会放大），返回 0 = 原画。
    if target_height > h:
        return 0

    def _even(x):
        # 编码器要偶数宽高，四舍五入到最近的偶数。
        n = int(round(x))
        return n + (n % 2)

    scaled_w = _even(w * target_height / h)
    scaled_h = _even(target_height)
    # max_size 卡的是最长边，所以取宽高里更大的那个。
    return max(scaled_w, scaled_h)


class SessionError(Exception):
    pass


def _recv(data):
    return data


def list_devices():
    """给界面的设备列表用（走自带 adb，只读查询）。"""
    try:
        p = adbtool.run("devices", capture_output=True, timeout=10)
    except Exception:
        return []
    out = (p.stdout or b"").decode("utf-8", "replace")
    result = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "\t" not in line:
            continue
        serial, _, state = line.partition("\t")
        result.append({"serial": serial.strip(), "state": state.strip()})
    return result


def adb_pair(host, port, code):
    """无线配对：adb pair <host>:<port> <code>。

    返回 (ok, message)。配对成功不代表已连接 —— 之后还得用无线调试页面上
    显示的那个**连接端口**去 adb connect，两者不是一个端口。

    ⚠️ `pair` 是 platform-tools 30.0.0 才有的子命令。用的 adb 太老会直接回
    `unknown command pair` —— 那句话就是"adb 该升级了"，不是端口/配对码填错。
    应用自带 tool/adb（见 adbtool.py），所以正常不该再看到这条。
    """
    target = "%s:%s" % (host, port)
    try:
        p = adbtool.run("pair", target, code, capture_output=True, timeout=40)
    except Exception as exc:
        return False, "执行 adb pair 失败：%s" % exc
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace").strip()
    ok = p.returncode == 0 and "successfully paired" in out.lower()
    if not ok and "unknown command pair" in out.lower():
        # 走这条路说明自带 adb 没生效，退回系统那份了。给一句能直接照着做的提示。
        out = ("当前 adb 不支持 pair（%s）：这版太老，配对功能要 platform-tools 30.0.0 以上。"
               "应用自带的那份没被用上，检查 tool/adb 是否存在且可执行。" % adbtool.describe())
    elif not ok and "protocol fault" in out.lower():
        # adb 客户端读失败回复时有个毛病：它把 "FAIL" 这 4 个字符当十六进制长度去读
        # （strtoul("FAIL",16) 正好等于 0xFA=250），于是真正的失败原因被吞掉，
        # 只剩这一句天书。服务端其实是把原话发全了的（用 TCP 代理抓过原始字节确认）。
        # 所以这里别把它当"连接出问题"，直接给能落地排查的两条。
        out = ("配对没成。adb 没把原因说清楚，按经验就两种可能：\n"
               "· 配对码 / 配对端口不对（注意：配对弹窗里给的端口，跟「无线调试」主页面上那个"
               "连接端口不是同一个）；\n"
               "· 配对弹窗已经关了或超时了 —— 那个端口只在弹窗亮着的时候才开着。\n"
               "（adb 原文：%s）" % out)
    return ok, out


class StreamReader:
    """把 adb 流拼成"要多少给多少"的读取接口。"""

    def __init__(self, client, stream_id):
        self._client = client
        self._stream_id = stream_id
        self._buf = bytearray()

    def read_exact(self, n, timeout=30.0):
        deadline = time.time() + timeout
        while len(self._buf) < n:
            remain = deadline - time.time()
            if remain <= 0:
                raise SessionError("读取超时（还差 %d 字节）" % (n - len(self._buf)))
            chunk = self._client.read(self._stream_id, timeout=min(remain, 5.0))
            if chunk is None:
                continue
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class ScrcpySession:
    """一次投屏会话。start() 之后视频回调就开始跑了。"""

    def __init__(self, serial, jar_path, log=print, on_session=None, on_packet=None,
                 on_audio_packet=None):
        self.serial = serial
        self.jar_path = jar_path
        self.log = log
        self.on_session = on_session or (lambda w, h, resized: None)
        self.on_packet = on_packet or (lambda pts_flags, data: None)
        self.on_audio_packet = on_audio_packet or (lambda pts_flags, data: None)

        # scid 用十六进制传（设备端是 Integer.parseInt(value, 0x10)），
        # 且必须是有符号 int32 正数，所以限在 1..0x7FFFFFFF。
        self.scid = random.randint(1, 0x7FFFFFFF)

        self.adb = None
        self.server_proc = None
        self.video_stream = None
        self.audio_stream = None
        self.control_stream = None
        self._video_in = None
        self._audio_in = None
        # H.264 的包规范化器（见 h264norm.py）。只在 codec 真是 h264 时建，
        # 而且建好之后才启动 _video_loop —— 它看到的包一定已经是规范化过的。
        self._h264 = None

        self.device_name = ""
        self.codec = "h264"
        self.width = 0
        self.height = 0
        self.last_error = ""
        # 音频的实际结果："" = 没开 / 设备端说不支持；"aac"/"opus" = 真的在传。
        # 设备端采不到声音时会写一个 0 过来（不是错误，只是没声音可采），
        # 这种情况下画面照常，只是没有音频流。
        self.audio_codec = ""
        # 三态：pending = 还没读到音频头（别急着说「没声音」）；off = 设备端说没有；on = 在传
        self.audio_state = "off"
        self.audio_error = False
        # 设备端明确报过「没有 H.265 编码器」—— server.py 据此决定要不要退回 H.264
        self.no_h265 = False
        # 设备端明确报过建不出来的那个编码器名（h265 / av1 / vp9 ……），空 = 没报过。
        # 有了它，回退逻辑才不用一种一种写死。
        self.bad_codec = ""

        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()

    # ---------- 自带 adb（只用来 connect / push / 起进程） ----------

    def _adb(self, *args, timeout=ADB_SHELL_TIMEOUT, check=True):
        p = adbtool.run("-s", self.serial, *args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if check and p.returncode != 0:
            name = args[0] if args else "?"
            raise SessionError("adb %s 失败：%s" % (name, err or out or "退出码 %d" % p.returncode))
        return p.returncode, out, err

    def _connect_device(self):
        """确保 adb server **真的认到**这台设备，再往下走。

        以前这里是 `check=False` + `sleep(0.4)`：连接失败一个字都不说，
        紧接着 push 报一句 `device 'x' not found`，把锅甩给了 push ——
        用户看到的就是"adb push 失败"，去查 push、查 jar、查网络，全查错方向。
        而无线 + TLS 的握手比 0.4 秒慢得多，所以这里必须**轮询到真的是 device 为止**
        （刚连上还可能是 offline / unauthorized，那也不算好）。
        """
        if ":" not in self.serial or self.serial.startswith("emulator-"):
            return
        adblink.forget(self.serial)      # 重新连一把，之前的通路判断作废
        deadline = time.time() + ADB_CONNECT_TIMEOUT
        state = ""
        while time.time() < deadline and not self._stop.is_set():
            try:
                adbtool.run("connect", self.serial,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=15)
            except Exception:
                pass
            try:
                p = adbtool.run("-s", self.serial, "get-state",
                                capture_output=True, timeout=10)
                state = ((p.stdout or b"") + (p.stderr or b"")).decode(
                    "utf-8", "replace").strip()
            except Exception:
                state = ""
            if state == "device":
                return
            time.sleep(0.6)
        raise SessionError(
            "连不上设备 %s（adb 状态：%s）。%s"
            % (self.serial, state or "没认到设备",
               adblink.humanize(state) if state else
               "请确认「无线调试」开着、这台设备已经用配对码配对过，"
               "并且端口填的是**连接端口**（不是配对端口）。"))

    def _spawn_server(self, s):
        """拼 scrcpy-server 的启动参数。

        ⚠️ 这些 key 全部是从 scrcpy-server.jar 的 dex / 4.1 源码里的 Options 类核对出来的。
        写错一个就会被设备端当成未知参数直接退出 —— 别凭印象加。
        特别注意：4.1 已经没有 turn_screen_off 了，「连接后关屏」改走控制消息。
        """
        args = [
            "app_process", "/", SERVER_CLASS, SCRCPY_VERSION,
            "scid=%08x" % self.scid,
            "log_level=info",
            "tunnel_forward=true",        # 让 server 监听、等我们连
            "video_bit_rate=%d" % s["bitRate"],
            "audio=%s" % ("true" if s.get("audio") else "false"),
        ]
        if s.get("audio"):
            args.append("audio_bit_rate=%d" % s["audioBitRate"])
            args.append("audio_codec=%s" % s["audioCodec"])
            args.append("audio_source=%s" % s["audioSource"])

        # codec=auto 时不传 video_codec，让设备端按自己的默认走（通常是 h264）。
        # 想要别的就得显式传 —— 设备端不会替你猜。
        # 5 种编码器一个不漏地放行：h264 / h265 / av1 / vp8 / vp9。
        # 这里以前只收 h264、h265，于是 av1/vp8/vp9 被**静默丢掉**、
        # 设备照样编 h264 —— 用户以为自己选了 AV1，其实一直看的是 H.264。
        if s.get("codec") in P.VIDEO_CODECS:
            args.append("video_codec=%s" % s["codec"])
        if s.get("maxFps"):
            args.append("max_fps=%d" % s["maxFps"])
        # s['maxSize'] 存的是「目标高度」（界面语义），不能原样当 max_size 传给设备端。
        # 这里换算成 scrcpy 要的最长边；返回 0 = 不追加，走原画。
        max_size = _effective_max_size(self.serial, s.get("maxSize") or 0,
                                       s.get("angle") or 0)
        if max_size:
            args.append("max_size=%d" % max_size)
        if s.get("angle"):
            args.append("angle=%d" % s["angle"])
        if s.get("crop"):
            args.append("crop=%s" % s["crop"])
        if s.get("screenOffTimeout"):
            args.append("screen_off_timeout=%d" % s["screenOffTimeout"])

        for key, param in (("showTouches", "show_touches"), ("stayAwake", "stay_awake"),
                           ("powerOffOnClose", "power_off_on_close"), ("keepActive", "keep_active"),
                           ("powerOn", "power_on"), ("clipboardAutosync", "clipboard_autosync")):
            args.append("%s=%s" % (param, "true" if s.get(key) else "false"))

        # 把 server 的输出接住：设备端启动失败只会说在它自己的 stdout 里，
        # 丢掉它就只能干瞪眼（这个坑踩过一次）。
        self.server_proc = adbtool.popen(
            "-s", self.serial, "shell", "CLASSPATH=" + SERVER_JAR_REMOTE, *args,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1)
        self._start_thread(self._relay_server_output, "server-log")
        self.log("设备端 server 已启动 (scid=%08x)" % self.scid)

    def _relay_server_output(self):
        try:
            for line in self.server_proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                # 设备端建不出编码器时就会打印这一行，并把可用编码器列表一起列出来。
                # 这是「这台设备没有 XXX 编码器」唯一可靠的判据 —— 靠报错文本猜不可靠。
                # 以前只认 h265，现在认任意一种 —— av1 / vp9 / vp8 建不出来是同一句话。
                m = re.search(r"Could not create default video encoder for (\w+)", line)
                if m:
                    self.bad_codec = m.group(1)
                    if m.group(1) == "h265":
                        self.no_h265 = True
                self.log("[设备] " + line)
        except Exception:
            pass

    def drain_server_log(self, timeout=2.0):
        """把设备端剩余的输出读干净再返回。

        建不出编码器时设备端是「打印原因 → 杀掉自己」两步走：等我们这边
        察觉到流断了、判定了失败，那句「Could not create ... h265」很可能
        还堵在管道里没被转发线程读出来。谁先谁后全看线程调度 ——
        不等这一下，「这台设备有没有 H.265」的结论就会时有时无。
        """
        deadline = time.time() + timeout
        p = self.server_proc
        if p is not None:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                pass
        for t in list(self._threads):
            if t.name == "scrcpy-server-log":
                t.join(timeout=max(0.1, deadline - time.time()))
                break

    # ---------- 设备端 abstract socket 流（经 adb forward） ----------

    def _wait_socket_ready(self, name, timeout=STREAM_OPEN_TIMEOUT):
        """等设备端的 abstract socket 真的出现。

        `name` 是**完整的 socket 名**（`scrcpy_<scid>`），下面直接拿它去 grep。
        以前这里自己再拼一次 `scrcpy_` 前缀，结果调用方给的是 scid、转发那侧给的是
        裸名 —— 两边拼出来的不是同一个 socket（见下面 `_open_stream` 的注释）。

        必须先等：socket 还不存在时，`adb forward` 那条本地连接**会被立刻关掉**
        （设备端 adbd 找不到目标 socket 就直接断），接收方只会读到 EOF。
        所以「先等它出现、再建转发并连」这个顺序不能反。
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            try:
                _rc, out, _err = self._adb(
                    "shell", "cat /proc/net/unix | grep -c '%s'" % name,
                    check=False, timeout=8)
                if out.strip() and out.strip() != "0":
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _open_stream(self, name, timeout=STREAM_OPEN_TIMEOUT):
        """按 socket 名开一条流（`localabstract:<name>`）。

        ⚠️ `name` 必须带 `scrcpy_` 前缀（设备端 scrcpy-server 就是这么命名的）。
        这里栽过一次：`ForwardLink` 拿到裸 scid 拼出 `localabstract:17a585eb`，
        而设备端叫 `scrcpy_17a585eb` —— `adb forward` **不校验目标存在**、照样返回成功，
        于是日志上看着"转发就绪"，一读就是「流已结束（设备端关闭）」，很难往名字上想。

        先等 socket 出现再连（原因见 `_wait_socket_ready`）。
        """
        if not self._wait_socket_ready(name, timeout):
            raise SessionError("设备端 socket %s 一直没出现（设备端 server 没起来？）" % name)
        return self.adb.open(name, timeout=10.0)

    def _read_video_preamble(self):
        """video 流开头：dummy 1B → 设备名 64B → codec id 4B。

        dummy 和设备名都只发在**第一个被 accept 的 socket** 上（scrcpy 的
        DesktopConnection.open / sendDeviceMeta 就是这么写的），
        也就是只发在 video 上；音频流没有名字，只有下面那 4 字节 codec id。
        """
        self._video_in.read_exact(1, timeout=15)
        raw = self._video_in.read_exact(DEVICE_NAME_LEN, timeout=15)
        self.device_name = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        codec_id = int.from_bytes(self._video_in.read_exact(4, timeout=15), "big")
        self.codec = P.CODEC_NAMES.get(codec_id, "h264")

    def _read_audio_preamble(self):
        """audio 流开头只有 4 字节：codec id，或者设备端的状态码。

        0 = 设备采不到声音（正常继续，只是没有音频流）；1 = 音频配置出错。
        ⚠️ 这 4 字节是设备端**开始出数据时**才写的，所以可能等一会儿；
        等不到不算致命 —— 画面不能因为这个起不来。
        """
        try:
            codec_id = int.from_bytes(self._audio_in.read_exact(4, timeout=20), "big")
        except Exception as exc:
            self.audio_codec = ""
            self.audio_state = "off"
            self.log("WARN: 读音频头失败（不影响画面）：%s" % exc)
            return False
        if codec_id == P.AUDIO_DISABLED:
            self.audio_codec = ""
            self.audio_state = "off"
            self.log("设备端采不到声音，这次只有画面")
            return False
        if codec_id == P.AUDIO_ERROR:
            self.audio_codec = ""
            self.audio_state = "off"
            self.audio_error = True
            self.log("WARN: 设备端音频配置出错")
            return False
        self.audio_codec = P.CODEC_NAMES.get(codec_id, "")
        if not self.audio_codec:
            self.audio_state = "off"
            self.log("WARN: 不认识的音频 codec id 0x%08x" % codec_id)
            return False
        self.audio_state = "on"
        return True

    # ---------- 生命周期 ----------

    def start(self, settings):
        s = settings
        self.audio_state = "pending" if s.get("audio") else "off"
        self._connect_device()

        self._adb("push", self.jar_path, SERVER_JAR_REMOTE)
        self.log("已推送 server jar → %s" % SERVER_JAR_REMOTE)

        self._spawn_server(s)

        # 走 adb forward（见 adblink.py）：端口是 adb 自己分的（tcp:0），
        # 所以这里只要一个 socket 名，不再需要 host/port、也不再自己说 adb 协议。
        # 顺手把上次崩掉留下的转发清掉，免得多会话时互相踩。
        adblink.cleanup_stale_forwards(self.serial)
        self.adb = adblink.ForwardLink(self.serial, log=self.log)

        sockname = "scrcpy_%08x" % self.scid

        # 顺序很讲究：
        #  1. 设备端按 video → audio → control 的顺序 accept，必须照着开；
        #  2. 而且三条流都开好之后，server 才会往 video 上发设备信息。
        #     它的 DesktopConnection.open() 是等全部 accept 完才返回的，
        #     sendDeviceMeta 在那之后才执行。所以前导不能提前读，
        #     **少开一条也永远读不到**（会一直读超时）。
        self.video_stream = self._open_stream(sockname)
        self._video_in = StreamReader(self.adb, self.video_stream)
        if s.get("audio"):
            self.audio_stream = self._open_stream(sockname)
            self._audio_in = StreamReader(self.adb, self.audio_stream)
        self.control_stream = self._open_stream(sockname)

        self._read_video_preamble()
        self.log("视频就绪：device=%s codec=%s" % (self.device_name, self.codec))

        # 有些编码器（redroid 那个硬编组件就是）吐出来的 H.264 不置 CONFIG / KEY_FRAME
        # 标记，SPS/PPS 却混在普通视频包里 —— 我们不补标记，hub 会把它全当 P 帧滤掉、
        # 前端也永远等不到 CONFIG 包，结果就是整屏黑。这里按 codec 建规范化器，
        # 且必须**在 _video_loop 起来之前**建好（这一刻 self.codec 已经定了）。
        # 非 H.264 时 normalizer_for 返回 None，包原样透传。
        self._h264 = h264norm.normalizer_for(self.codec, self.log)

        self._start_thread(self._video_loop, "video")
        if self.audio_stream is not None:
            # 音频头要等设备端编码器出数据才来，可能慢 —— 放到线程里读，
            # 别把「会话就绪」卡在音频上。
            self._start_thread(self._audio_start, "audio")
        self._start_thread(self._control_reader, "control")
        return True

    def _audio_start(self):
        if not self._read_audio_preamble():
            return
        self.log("音频就绪：codec=%s" % self.audio_codec)
        self._audio_loop()

    def _audio_loop(self):
        try:
            while not self._stop.is_set():
                header = self._audio_in.read_exact(P.PACKET_HEADER_SIZE, timeout=60)
                kind = P.parse_video_header(header)
                if kind[0] != "packet":
                    continue
                _, pts_flags, size = kind
                if size <= 0:
                    continue
                self.on_audio_packet(pts_flags, self._audio_in.read_exact(size, timeout=60))
        except Exception as exc:
            if not self._stop.is_set():
                self.log("音频流结束：%s" % exc)

    def _start_thread(self, fn, name):
        t = threading.Thread(target=fn, name="scrcpy-" + name, daemon=True)
        t.start()
        self._threads.append(t)

    def _video_loop(self):
        """读视频流。

        ⚠️ 对 H.264，交给 on_packet 的**不是**设备原包，而是规范化之后的包
        （见 h264norm.py）—— 需要补 CONFIG 时，同一个原始包会变成两个包先后发出去。
        其它编码原样透传，`on_packet` 拿到的就是设备原包。
        """
        try:
            while not self._stop.is_set():
                header = self._video_in.read_exact(P.PACKET_HEADER_SIZE, timeout=60)
                kind = P.parse_video_header(header)
                if kind[0] == "session":
                    _, w, h, resized = kind
                    self.width, self.height = w, h
                    self.log("会话尺寸：%dx%d" % (w, h))
                    if self._h264 is not None:
                        # 新起的流 / 尺寸变了：缓存的 SPS/PPS 作废，等码流里再带出来
                        self._h264.reset()
                    self.on_session(w, h, resized)
                    continue
                _, pts_flags, size = kind
                if size <= 0:
                    continue
                payload = self._video_in.read_exact(size, timeout=60)
                if self._h264 is None:
                    self.on_packet(pts_flags, payload)
                else:
                    for flags, data in self._h264.normalize(pts_flags, payload):
                        self.on_packet(flags, data)
        except Exception as exc:
            if not self._stop.is_set():
                self.last_error = "视频流中断：%s" % exc
                self.log("WARN: " + self.last_error)

    def _control_reader(self):
        """设备端会在控制通道上回消息（剪贴板、UHID 输出等），必须持续读掉，
        否则对端写满缓冲区后整条连接会卡住。"""
        try:
            while not self._stop.is_set():
                if self.adb.read(self.control_stream, timeout=60) is None:
                    continue
        except Exception:
            pass

    def send_control(self, payload):
        if not payload:
            return False
        with self._lock:
            if self._stop.is_set() or self.adb is None or self.control_stream is None:
                return False
            try:
                self.adb.write(self.control_stream, payload)
                return True
            except Exception as exc:
                self.last_error = "控制通道发送失败：%s" % exc
                self.log("WARN: " + self.last_error)
                return False

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()

        if self.adb is not None:
            try:
                self.adb.close()
            except Exception:
                pass
            self.adb = None
        self.video_stream = None
        self.audio_stream = None
        self.control_stream = None
        self._video_in = None
        self._audio_in = None

        # 连接断了设备端 server 会自己退出；兜底再 kill 一次。
        # 只按本应用推上去的 jar 路径做精确匹配，别误伤设备上别的进程。
        try:
            self._adb("shell", "pkill -f scrcpy-server.jar", check=False, timeout=10)
        except Exception:
            pass

        if self.server_proc is not None:
            try:
                self.server_proc.terminate()
                self.server_proc.wait(timeout=5)
            except Exception:
                try:
                    self.server_proc.kill()
                except Exception:
                    pass
            self.server_proc = None

        self.log("会话已停止")
