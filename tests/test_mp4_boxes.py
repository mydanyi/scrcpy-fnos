# -*- coding: utf-8 -*-
"""HEVC 封装层的单元测试：**用真设备的参数集**当夹具，断言解析出来的字段。

为什么非要这一层：hvcC 与 codec string 里的字段全是从 SPS 抄的，而 SPS 的 RBSP
里夹着**防竞争字节**（00 00 03）。按原始字节的索引去读，拼出来的盒子和编码串
「看着像字节流、每个字段都是错的」—— 表现是解码器配置上了却一帧不吐（黑屏），
不报任何错。所以判据必须落在**字段值**上，不能只看拼出来的字节像不像。

夹具是黑鲨 SHARK PRS-A0 的真实 scrcpy config 包（tools/_t129_phone_sps.py 抓的）：
  原始  42 01 | 01 01 60 00 00 [03] 00 b0 00 00 [03] 00 00 [03] 00 96 …
  去转义 42 01 | 01 01 60 00 00  00 00 b0 00 00  00 00  00  00 96 …
                    ↑ 方括号里是防竞争字节，必须拿掉，否则后面整段错位

跑法：python -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHECK_JS = os.path.join(HERE, "mp4_boxes_check.js")
NODE = shutil.which("node")

# 真设备抓到的参数集（hex）
REAL_VPS = "40010c01ffff016000000300b00000030000030096ac09"
REAL_SPS = "420101016000000300b00000030000030096a00220800961cbe5aee4c92ea520a0c0c05da14250"
REAL_PPS = "4401c0e30f09418f6108"


@unittest.skipIf(not NODE, "没有 node，跑不了 mp4.js 的单元测试")
class HevcBoxesTests(unittest.TestCase):
    """一次性把 mp4.js 在 node 里跑一遍，结果缓存给所有断言用。"""

    result = None

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, CHECK_JS], cwd=ROOT,
                              capture_output=True, timeout=60)
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        marker = "RESULT "
        idx = out.find(marker)
        if idx < 0:
            raise AssertionError("驱动脚本没输出 RESULT：\n%s\n%s" % (out[:800], err[:800]))
        cls.result = json.loads(out[idx + len(marker):].strip().splitlines()[0])
        if "error" in cls.result:
            raise AssertionError("mp4.js 抛异常了：\n" + cls.result["error"])

    # ---------- 参数集必须先去防竞争字节 ----------

    def test_fixture_really_has_escape_bytes(self):
        """夹具本身要含防竞争字节，否则这组测试就成了空转。"""
        self.assertEqual(self.result["spsLen"], 39)
        self.assertEqual(
            self.result["rawEscapeByte"], 0x03,
            "夹具的 SPS 第 8 字节本应是防竞争字节 0x03 —— 换了夹具就要重新核对这组断言",
        )

    def test_unescape_drops_the_escape_byte(self):
        """去转义之后，RBSP 应是 01 01 60 00 00 00 b0 00 00 00 00 00 96 a0。

        注意 0x03 是在 **00 00 之后**被拿掉的，所以 60 后面紧跟的是 00，
        下一字节直接就是约束位的第一个 b0 —— 少看一眼就会以为错位。
        """
        self.assertEqual(self.result["unescaped"], "010160000000b0000000000096a0")

    def test_profile_and_level_are_real_not_zero(self):
        f = self.result["fields"]
        self.assertEqual(f["profileSpace"], 0)
        self.assertEqual(f["tierFlag"], 0)
        self.assertEqual(f["profileIdc"], 1, "Main profile")
        self.assertEqual(
            f["levelIdc"], 150,
            "level_idc 必须是 SPS 里的真值 150（L5.0）—— 读成 0 就是错位了",
        )

    def test_compat_and_constraints_are_not_shifted(self):
        f = self.result["fields"]
        self.assertEqual(f["compat"], 0x60000000,
                         "兼容位应是 0x60000000；读到 0x60000003 说明没去掉防竞争字节")
        self.assertEqual(f["constraints"], [0xB0, 0, 0, 0, 0, 0])

    def test_temporal_layers_come_from_sps(self):
        f = self.result["fields"]
        self.assertEqual(f["numTemporalLayers"], 1)
        self.assertEqual(f["temporalIdNested"], 1)

    # ---------- codec string ----------

    def test_codec_string_matches_the_rfc_form(self):
        self.assertEqual(
            self.result["codec"], "hvc1.1.6.L150.B0",
            "这条串正是 Chrome 的 isTypeSupported 返回 true 的写法",
        )

    def test_compat_flags_are_bit_reversed(self):
        self.assertEqual(self.result["reverse60000000"], 6,
                         "RFC 7798 要求兼容位按位反转：0x60000000 → 0x00000006 → '6'")
        self.assertNotIn("60000000", self.result["codec"],
                         "不许再把 32 位整数直译进编码串")

    # ---------- hvcC ----------

    def test_hvcc_fields_copy_from_sps(self):
        self.assertEqual(self.result["hvccLevel"], 150)
        self.assertEqual(self.result["hvccCompat"], "60000000")
        self.assertEqual(self.result["hvccConstraints"], "b00000000000")

    def test_hvcc_temporal_layer_bits(self):
        self.assertEqual(
            self.result["hvccByte21"], 0x0F,
            "第 22 字节 = numTemporalLayers(1) | temporalIdNested(1) | lengthSize=3 → 0x0F",
        )

    def test_hvcc_arrays_cover_vps_sps_pps(self):
        self.assertEqual(self.result["hvccNumArrays"], 3)

    def test_hvcc_head_is_byte_exact(self):
        self.assertEqual(
            self.result["hvccHead"],
            "010160000000b0000000000096f000fcfdf8f800000f03",
        )

    # ---------- 反例：没有防竞争字节的 SPS 不能被改坏 ----------

    def test_parser_is_idempotent_without_escape_bytes(self):
        p = self.result["plainFields"]
        self.assertEqual(p["compat"], 0x60000000)
        self.assertEqual(p["constraints"], [0xB0, 0, 0, 0, 0, 0])
        self.assertEqual(p["levelIdc"], 150)

    # ---------- 导出面 ----------

    def test_module_exports_the_parser(self):
        """单测要能直接断字段，所以解析器必须导出。"""
        with open(os.path.join(ROOT, "app", "frontend", "mp4.js"), encoding="utf-8") as fh:
            src = fh.read()
        for name in ("hevcSpsFields", "hevcUnescape", "reverseBits32"):
            self.assertIn(name + ":", src, "mp4.js 应导出 %s" % name)

    def test_frontend_uses_the_shared_parser(self):
        """buildHvcC 与 hevcCodecString 都该走同一个解析器，别各读一遍。"""
        with open(os.path.join(ROOT, "app", "frontend", "mp4.js"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("function buildHvcC"): src.index("function reverseBits32")]
        self.assertIn("hevcSpsFields", body)
        self.assertNotIn("sps[4]", body, "不许再直接按原始 SPS 字节索引取兼容位")


if __name__ == "__main__":
    unittest.main()
