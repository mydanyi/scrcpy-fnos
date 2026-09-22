#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scrcpy 设备端协议：控制消息编码 + 视频包解析。

依据 scrcpy v4.1 官方源码（app/src/control_msg.c 的 sc_control_msg_serialize，
以及 app/src/demuxer.c 的包解析）。全部大端序。

只实现这个应用用得到的那几种消息，没用到的不做。
"""

import struct

# ---------- 控制消息类型（编号即协议，顺序不可改）----------
MSG_INJECT_KEYCODE = 0
MSG_INJECT_TEXT = 1
MSG_INJECT_TOUCH_EVENT = 2
MSG_INJECT_SCROLL_EVENT = 3
MSG_BACK_OR_SCREEN_ON = 4
MSG_EXPAND_NOTIFICATION_PANEL = 5
MSG_EXPAND_SETTINGS_PANEL = 6
MSG_COLLAPSE_PANELS = 7
MSG_GET_CLIPBOARD = 8
MSG_SET_CLIPBOARD = 9
MSG_SET_DISPLAY_POWER = 10
MSG_ROTATE_DEVICE = 11
MSG_RESET_VIDEO = 17

# ---------- 动作 ----------
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

BUTTON_PRIMARY = 1

# scrcpy 用 16 位定点数表示压力与滚动量
_FP_U16_MAX = 0xFFFF
_FP_I16_MAX = 0x7FFF

# ---------- 视频包 ----------
PACKET_HEADER_SIZE = 12
FLAG_SESSION = 1 << 63
FLAG_CONFIG = 1 << 62
FLAG_KEY_FRAME = 1 << 61
PTS_MASK = FLAG_KEY_FRAME - 1

CODEC_H264 = 0x68323634   # "h264"
CODEC_H265 = 0x68323635   # "h265"
# 下面三个前面的 0x00 不是笔误 —— scrcpy 的 VideoCodec 枚举就是这么定义的
# （AV1(0x00_61_76_31, "av1", ...)，id 是名字的 4 字节 ASCII，短名字左补 0）。照抄。
CODEC_AV1 = 0x00617631    # "\0av1"
CODEC_VP8 = 0x00767038    # "\0vp8"
CODEC_VP9 = 0x00767039    # "\0vp9"
CODEC_AAC = 0x00616163    # "aac"
CODEC_OPUS = 0x6f707573   # "opus"
CODEC_FLAC = 0x666c6163   # "flac"

CODEC_NAMES = {
    CODEC_H264: "h264", CODEC_H265: "h265",
    CODEC_AV1: "av1", CODEC_VP8: "vp8", CODEC_VP9: "vp9",
    CODEC_AAC: "aac", CODEC_OPUS: "opus", CODEC_FLAC: "flac",
}

# 视频编码器只有这 5 种（scrcpy 4.1 的 VideoCodec 枚举，一个不多一个不少）。
VIDEO_CODECS = ("h264", "h265", "av1", "vp8", "vp9")

# 「自动」模式下挑编码器的顺序：效率优先，H.264 兜底。
#   h265 —— 真机上最实际的升级（硬编普及、同码率比 H.264 省三四成）
#   av1  —— 效率更高，但目前几乎没有手机带 AV1 硬编
#   vp9  —— 效率介于两者之间；软件编码器常见（比如 redroid）
#   h264 —— 万能兜底，任何设备都有
#   vp8  —— 比 H.264 还差，只在设备只有它时才用
VIDEO_CODEC_PREFERENCE = ("h265", "av1", "vp9", "h264", "vp8")

# 音频流开头的 4 字节是 codec id；这两个特殊值不是编码格式，是设备端的状态通知
# （scrcpy 的 Streamer.writeDisableStream）。见 Streamer.java 的注释。
AUDIO_DISABLED = 0      # 设备端明说「采不到声音」，客户端继续只投画面
AUDIO_ERROR = 1         # 设备端音频配置出错，整个会话要停


def _u16fp(value):
    """float -> 16 位定点（对应 sc_float_to_u16fp）。"""
    v = max(0.0, min(1.0, float(value)))
    return int(v * _FP_U16_MAX) & 0xFFFF


def _i16fp(value):
    """float -> 16 位有符号定点（对应 sc_float_to_i16fp）。"""
    v = max(-1.0, min(1.0, float(value)))
    return int(v * _FP_I16_MAX)


def _position(x, y, screen_width, screen_height):
    """位置结构：x(4) y(4) 屏宽(2) 屏高(2)。"""
    return struct.pack(">IIHH", int(x) & 0xFFFFFFFF, int(y) & 0xFFFFFFFF,
                       int(screen_width) & 0xFFFF, int(screen_height) & 0xFFFF)


def build_touch(action, pointer_id, x, y, screen_width, screen_height,
                pressure=None, action_button=0, buttons=0):
    """触摸事件（32 字节）。

    action: ACTION_DOWN / ACTION_UP / ACTION_MOVE
    pointer_id: 手指编号（0-9），不是浏览器的 pointerId
    x, y: 设备像素坐标
    screen_width/height: 设备屏幕尺寸（设备端按它做坐标换算）

    压力按 scrcpy 的做法：抬起为 0，其余为 1。
    """
    if pressure is None:
        pressure = 0.0 if action == ACTION_UP else 1.0
    return bytes([MSG_INJECT_TOUCH_EVENT, action & 0xFF]) \
        + struct.pack(">Q", int(pointer_id) & 0xFFFFFFFFFFFFFFFF) \
        + _position(x, y, screen_width, screen_height) \
        + struct.pack(">H", _u16fp(pressure)) \
        + struct.pack(">I", int(action_button) & 0xFFFFFFFF) \
        + struct.pack(">I", int(buttons) & 0xFFFFFFFF)


def build_keycode(action, keycode, repeat=0, metastate=0):
    """按键事件（14 字节）。keycode 是 Android keycode，不是浏览器 keyCode。"""
    return bytes([MSG_INJECT_KEYCODE, action & 0xFF]) \
        + struct.pack(">III", int(keycode) & 0xFFFFFFFF,
                      int(repeat) & 0xFFFFFFFF, int(metastate) & 0xFFFFFFFF)


def build_scroll(x, y, screen_width, screen_height, hscroll=0.0, vscroll=0.0, buttons=0):
    """滚轮事件（21 字节）。

    设备端会把 scroll 值先除以 16 再夹到 [-1, 1]，所以这里按「格数」传，
    一般就是 ±1。
    """
    h = _i16fp(max(-16.0, min(16.0, hscroll)) / 16.0)
    v = _i16fp(max(-16.0, min(16.0, vscroll)) / 16.0)
    return bytes([MSG_INJECT_SCROLL_EVENT]) \
        + _position(x, y, screen_width, screen_height) \
        + struct.pack(">hh", h, v) \
        + struct.pack(">I", int(buttons) & 0xFFFFFFFF)


def build_text(text):
    """注入文本（仅 ASCII 才安全，中文请用剪贴板粘贴那条路）。"""
    try:
        raw = text.encode("utf-8")[:300]
    except Exception:
        return b""
    return bytes([MSG_INJECT_TEXT]) + struct.pack(">I", len(raw)) + raw


def build_back_or_screen_on(action):
    """返回键（15 字节以内，2 字节）。"""
    return bytes([MSG_BACK_OR_SCREEN_ON, action & 0xFF])


def build_set_display_power(on):
    """开关屏幕。"""
    return bytes([MSG_SET_DISPLAY_POWER, 1 if on else 0])


def build_rotate_device():
    return bytes([MSG_ROTATE_DEVICE])


def build_collapse_panels():
    return bytes([MSG_COLLAPSE_PANELS])


def build_expand_notification_panel():
    return bytes([MSG_EXPAND_NOTIFICATION_PANEL])


def build_expand_settings_panel():
    return bytes([MSG_EXPAND_SETTINGS_PANEL])


def build_reset_video():
    return bytes([MSG_RESET_VIDEO])


def build_set_clipboard(sequence, text, paste=False):
    """把文本塞进设备剪贴板；paste=True 时顺带触发粘贴。"""
    try:
        raw = text.encode("utf-8")
    except Exception:
        raw = b""
    return bytes([MSG_SET_CLIPBOARD]) + struct.pack(">Q", sequence & 0xFFFFFFFFFFFFFFFF) \
        + bytes([1 if paste else 0]) + struct.pack(">I", len(raw)) + raw


def parse_video_header(header):
    """解析 12 字节头部。

    返回 ("session", width, height, client_resized) 或 ("packet", pts_flags, size)。
    两种包共用这 12 字节，靠第一个字节的最高位区分。
    """
    if len(header) < PACKET_HEADER_SIZE:
        raise ValueError("short header")
    if header[0] & 0x80:
        width = struct.unpack(">I", header[4:8])[0]
        height = struct.unpack(">I", header[8:12])[0]
        client_resized = bool(header[3] & 1)
        return ("session", width, height, client_resized)
    pts_flags = struct.unpack(">Q", header[:8])[0]
    size = struct.unpack(">I", header[8:12])[0]
    return ("packet", pts_flags, size)
