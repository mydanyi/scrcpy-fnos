# -*- coding: utf-8 -*-
"""装 APK 的回归契约：包必须从中转目录装，不能直接喂 /sdcard 的路径。

2026-09-23 主人的现场（黑鲨 SHARK PRS-A0，SELinux Enforcing）：
文件管理里点安装 → `avc: denied { read } for scontext=u:r:system_server:s0
tcontext=u:object_r:fuse:s0` → `Error: Can't open file: /sdcard/Download/xxx.apk`。
根因是 `pm install` 这个文件的读者是 **system_server**，而 /sdcard 是 FUSE，
SELinux 不许它读 —— 所以那条路在这类设备上**永远装不了**，
而用户看到的是几十行 Java 栈，完全看不出自己能做什么。

判据全在这层纯逻辑单测里（adb 打桩）：命令怎么拼、中转文件删没删、
失败信息翻没翻成人话。真机那一层只验「装成功 + 设备上没留中转文件」。
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import fileops  # noqa: E402

APK = "/sdcard/Download/MaaMeow-v0.21.4-universal.apk"


class _Shell(object):
    """假的 run_shell：记下每一条命令，按规则回 (stdout, stderr, code)。"""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, serial, cmd, timeout=None):
        self.calls.append(cmd)
        for key, val in self.replies.items():
            if key in cmd:
                return val
        return ("", "", 0)

    def find(self, needle):
        return [c for c in self.calls if needle in c]


def _run(shell, path=APK):
    with mock.patch.object(fileops, "run_shell", shell):
        return fileops.install_apk("S1", path)


class InstallStagingTests(unittest.TestCase):
    def setUp(self):
        self.shell = _Shell()

    def test_stage_dir_is_readable_by_system_server(self):
        """中转目录必须在 /data/local/tmp 下 —— 换成 /sdcard 就是这个 bug 本身。"""
        self.assertTrue(
            fileops.PM_STAGE_DIR.startswith("/data/local/tmp/"),
            "中转目录要在 /data/local/tmp 下（system_server 读得到）",
        )
        self.assertNotIn("/sdcard", fileops.PM_STAGE_DIR)

    def test_pm_reads_the_staged_copy_not_sdcard(self):
        self.assertEqual(_run(self.shell), "安装成功")
        pm = self.shell.find("pm install")
        self.assertEqual(len(pm), 1, "只该调一次 pm install")
        self.assertIn("/data/local/tmp/", pm[0], "pm 要装中转目录里的那份")
        self.assertNotIn("/sdcard", pm[0], "不许把 /sdcard 的路径直接喂给 pm")

    def test_order_is_mkdir_copy_install_remove(self):
        _run(self.shell)
        kinds = []
        for c in self.shell.calls:
            for k in ("mkdir", "cp -f", "pm install", "rm -f"):
                if c.startswith(k) or (" " + k) in c:
                    kinds.append(k)
                    break
        self.assertEqual(kinds, ["mkdir", "cp -f", "pm install", "rm -f"])

    def test_staged_copy_is_removed_after_success(self):
        _run(self.shell)
        rm = self.shell.find("rm -f")
        self.assertEqual(len(rm), 1, "中转的包用完要删（几百 MB）")
        self.assertIn("MaaMeow-v0.21.4-universal.apk", rm[0])

    def test_staged_copy_is_removed_even_when_install_fails(self):
        shell = _Shell({"pm install": ("", "Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]", 1)})
        with self.assertRaises(fileops.FileOpError):
            _run(shell)
        self.assertEqual(len(shell.find("rm -f")), 1, "装不成也要把中转文件删掉")

    def test_already_in_tmp_is_installed_in_place(self):
        """用户本来就是从 /data/local/tmp 装的：别多拷一次、也别删他的东西。"""
        shell = _Shell()
        self.assertEqual(_run(shell, "/data/local/tmp/foo.apk"), "安装成功")
        self.assertEqual(shell.find("cp -f"), [], "不需要再拷一份")
        self.assertEqual(shell.find("rm -f"), [], "原地那份是用户的，不许删")
        self.assertIn("/data/local/tmp/foo.apk", shell.find("pm install")[0])

    def test_copy_failure_says_what_to_do(self):
        shell = _Shell({"cp -f": ("", "cp: write error: No space left on device", 1)})
        with self.assertRaises(fileops.FileOpError) as ctx:
            _run(shell)
        msg = str(ctx.exception)
        self.assertIn("中转", msg)
        self.assertIn("No space left", msg, "把设备给的原话带上，别吞掉")
        self.assertEqual(shell.find("pm install"), [], "搬不过去就别装了")

    def test_weird_name_does_not_break_the_command(self):
        shell = _Shell()
        _run(shell, "/sdcard/Download/it's weird.apk")
        joined = "\n".join(shell.calls)
        self.assertIn("'\\''", joined, "单引号要转义，别让命令被截成两截")


class InstallResultTests(unittest.TestCase):
    def test_success_words(self):
        for out, code in (("Success", 0), ("", 0)):
            shell = _Shell({"pm install": (out, "", code)})
            self.assertEqual(_run(shell), "安装成功")

    def test_failure_reason_is_translated(self):
        shell = _Shell({"pm install": ("", "Failure [INSTALL_FAILED_USER_RESTRICTED]", 1)})
        with self.assertRaises(fileops.FileOpError) as ctx:
            _run(shell)
        self.assertIn("INSTALL_FAILED_USER_RESTRICTED", str(ctx.exception))
        self.assertIn("USB 安装", str(ctx.exception),
                      "最常见的这条要给一句能照着做的话")

    def test_unknown_reason_keeps_the_original_text(self):
        shell = _Shell({"pm install": ("", "Failure [INSTALL_FAILED_SOMETHING_NEW]", 1)})
        with self.assertRaises(fileops.FileOpError) as ctx:
            _run(shell)
        self.assertIn("INSTALL_FAILED_SOMETHING_NEW", str(ctx.exception))

    def test_raw_java_wall_is_trimmed(self):
        """翻不动的时候带上原文，但别把一整墙 Java 栈糊到界面上。"""
        wall = "Exception occurred while executing 'install':\n" + ("\tat com.android.x\n" * 80)
        shell = _Shell({"pm install": ("", wall, 1)})
        with self.assertRaises(fileops.FileOpError) as ctx:
            _run(shell)
        self.assertLessEqual(len(str(ctx.exception)), 400)

    def test_avc_fuse_error_explains_itself(self):
        """真机上那个 SELinux 报错要有一句人话（这条 bug 的原始症状）。"""
        text = ("avc:  denied  { read } for  scontext=u:r:system_server:s0 "
                "tcontext=u:object_r:fuse:s0 tclass=file\n"
                "Error: Can't open file: /sdcard/Download/x.apk")
        msg = fileops._pm_human(text, "/sdcard/Download/x.apk")
        self.assertIn("SELinux", msg)
        self.assertIn("/data/local/tmp", msg)

    def test_path_whitelist_still_applies(self):
        shell = _Shell()
        with self.assertRaises(fileops.FileOpError):
            _run(shell, "/system/app/Foo.apk")
        self.assertEqual(shell.calls, [], "白名单外的路径不该产生任何设备命令")


if __name__ == "__main__":
    unittest.main()
