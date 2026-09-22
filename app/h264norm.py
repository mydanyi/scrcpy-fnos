#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H.264 包规范化：给「不置标记的编码器」补出 CONFIG / KEY_FRAME 标记。

背景（2026-09-21 实测）：redroid 那个硬编组件 `OMX.redroid.h264.encoder` 吐出来的
H.264 **是能解的**（FFmpeg 能从原始码流里解出完整画面），但它不按 scrcpy 的约定给
输出缓冲置标记 —— 10 秒 494 个视频包的 pts_flags 里 FLAG_CONFIG / FLAG_KEY_FRAME
**全是 0**；而 SPS/PPS 其实是跟着每个 IDR、以 Annex-B 形式混在普通视频包里发出来的。

我们这边两处都只认标记：
  - VideoHub 按标记认关键帧，新订阅者处于 resync 时把没有 KEY_FRAME 的包全滤掉；
  - 前端 H.264 要等 CONFIG 包才 configure 解码器，没配上的帧一律丢掉。
于是画面全黑。

这个模块**不改码流**，只在包进 VideoHub 之前把标记补齐：
  - 从普通视频包里认出 Annex-B 的 SPS(7) / PPS(8) / IDR(5)；
  - 缓存本会话的 SPS/PPS，变了才额外发一个 CONFIG 包（排在依赖它的那帧前面）；
  - 含 IDR 的包补上 FLAG_KEY_FRAME；
  - 原始 pts、已有的有效标记、视频载荷一律原样保留。

⚠️ 只对 H.264 建实例（调用方按 codec 判断）。VP8/VP9/AV1 的载荷自带边界，
不能按 NAL 去扫；音频走的是另一条路。
"""

import re

import scrcpy_proto as P

# Annex-B 起始码：00 00 01，或 00 00 00 01（两种都认）。
# ⚠️ 必须把四字节的那种**整段**吃掉。只拿 `00 00 01` 去搜的话，四字节起始码里的
# 第一个 0 会被算进**上一个** NAL 的尾部 —— 切出来的 SPS 多一个 0 字节，
# 拼进 avcC 就是错的配置（前端 buildAvcC 会拿错位的字节当 profile/level）。
_START_CODE = re.compile(b"\x00\x00(?:\x00)?\x01")

_NAL_IDR = 5
_NAL_SPS = 7
_NAL_PPS = 8


def _nal_spans(payload):
    """按起始码切出 NAL 的 (起, 止) 区间；载荷里没有起始码就返回空。"""
    marks = list(_START_CODE.finditer(payload))
    spans = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(payload)
        if end > start:          # 空 NAL（两个起始码挨着）不算
            spans.append((start, end))
    return spans


def build_config(sps, pps):
    """把 SPS/PPS 拼成一个 Annex-B 的 CONFIG 载荷。

    形状和 scrcpy 正常给的 csd 一致（起始码 + SPS + 起始码 + PPS）——
    前端是拿 `nalUnits()` 去切它的，必须是 Annex-B，不能是 AVCC 的长度前缀。
    """
    return b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps


class H264Normalizer:
    """一次视频会话一个实例 —— 缓存跟着会话走，重连/换编码时天然重来。"""

    def __init__(self, log=None):
        self._log = log or (lambda *a: None)
        self._sps = None
        self._pps = None
        self._sent = None            # 上次发出去的 config 载荷，用来判断「还要不要再发」
        self._native_config = False  # 这个流本来就给 CONFIG 包（正常编码器）→ 不再插手
        self._announced = False

    def reset(self):
        """视频会话重建（新起的流 / 尺寸变了）时清掉缓存。

        尺寸一变 SPS 就变了，旧的那份不能再用 —— 清掉，等码流里再带出来。
        """
        self._sps = None
        self._pps = None
        self._sent = None
        self._native_config = False
        self._announced = False

    def normalize(self, pts_flags, payload):
        """把一个原始视频包翻成「要发出去的若干包」。

        返回 [(pts_flags, payload), ...]。绝大多数情况就是一个原样的包；
        需要补配置时是 [CONFIG, 原包]。
        """
        # 本来就是 CONFIG 包（正常编码器会给）→ 原样放行，并且以后不再插手，
        # 免得多发一份把已经配好的解码器又初始化一遍。
        if pts_flags & P.FLAG_CONFIG:
            self._native_config = True
            return [(pts_flags, payload)]

        idr = False
        sps = pps = None
        for start, end in _nal_spans(payload):
            t = payload[start] & 0x1F
            if t == _NAL_IDR:
                idr = True
            elif t == _NAL_SPS:
                sps = payload[start:end]
            elif t == _NAL_PPS:
                pps = payload[start:end]

        if sps is not None:
            self._sps = sps
        if pps is not None:
            self._pps = pps

        out = []
        if not self._native_config and self._sps and self._pps:
            cfg = build_config(self._sps, self._pps)
            if cfg != self._sent:
                self._sent = cfg
                out.append((P.FLAG_CONFIG, cfg))
                if not self._announced:
                    self._announced = True
                    self._log("这个 H.264 流没有 CONFIG 标记，已按码流里的 SPS/PPS 补出配置包")

        # ⚠️ SPS/PPS 和 IDR 混在同一个包里的情况**不能**改成 CONFIG 就把画面部分丢掉 ——
        # 原包照发（载荷一字不改），配置另发一份（上面那个）。
        if idr:
            out.append((pts_flags | P.FLAG_KEY_FRAME, payload))
        else:
            out.append((pts_flags, payload))
        return out


def normalizer_for(codec, log=None):
    """按编码格式决定要不要建规范化器 —— 只有 H.264 需要，也只有它安全。

    VP8/VP9/AV1 的载荷是自带边界的原始帧，按 NAL 去扫会把一帧切碎、直接把画面搞黑；
    音频根本不走这条路。所以这里咬死 codec == "h264"，其它一律返回 None。
    """
    return H264Normalizer(log=log) if codec == "h264" else None
