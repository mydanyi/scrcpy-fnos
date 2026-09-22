import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE_CANDIDATES = [
    os.path.join(ROOT, "cmd", "main"),
    os.path.join(ROOT, "cmd", "main.py"),
    os.path.join(ROOT, "cmd", "main.go"),
    os.path.join(ROOT, "cmd", "main.sh"),
    os.path.join(ROOT, "cmd", "main", "main.go"),
    os.path.join(ROOT, "cmd", "main", "main.py"),
]

# 顶层 stop_process() { ... } 的起始位置。
STOP_PROCESS_START = re.compile(
    r"^stop_process\s*\(\s*\)\s*\{", re.MULTILINE
)

# 下一个顶层 start_process() { ... } 的位置，用于界定 stop_process 区域。
NEXT_TOP_LEVEL_FUNC = re.compile(
    r"^start_process\s*\(\s*\)\s*\{", re.MULTILINE
)

# 只匹配 pgid / PGID（不把函数外的 setsid 计入）。
PGID_PATTERNS = [
    r"\bpgid\b",
    r"\bPGID\b",
]

# 对整组（负 PGID）发信号的写法。
GROUP_KILL_PATTERNS = [
    r"kill\s+--\s+-",       # kill -- -PGID
    r"kill\s+-TERM\s+-",    # kill -TERM -PGID
    r"kill\s+-KILL\s+-",    # kill -KILL -PGID
]


def _read_source():
    for path in SOURCE_CANDIDATES:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return path, fh.read()
    return None, None


def _function_region(source, name):
    """截取顶层 stop_process() { ... } 到下一个顶层 start_process() { 的实现体。"""
    match = STOP_PROCESS_START.search(source)
    if not match:
        return ""
    nxt = NEXT_TOP_LEVEL_FUNC.search(source, match.end())
    end = nxt.start() if nxt else len(source)
    return source[match.start():end]


class StopProcessProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.path, self.source = _read_source()
        if self.source is None:
            self.fail("未找到 cmd/main 源文件，检查以下路径之一：%s" % ", ".join(SOURCE_CANDIDATES))
        self.region = _function_region(self.source, "stop_process")
        if not self.region:
            self.fail("未找到顶层 stop_process() { 函数体")

    def test_stop_process_obtains_pgid(self):
        self.assertTrue(
            any(re.search(p, self.region) for p in PGID_PATTERNS),
            "stop_process 内应获取进程组 ID (PGID)，而不是只使用 PID",
        )

    def test_stop_process_signals_process_group_with_negative_pgid(self):
        self.assertTrue(
            any(re.search(p, self.region) for p in GROUP_KILL_PATTERNS),
            "stop_process 内应对负 PGID（整组）发信号，而不是只 kill 单个 PID",
        )

    def test_stop_process_uses_term_and_kill(self):
        self.assertRegex(self.region, r"kill\s+-TERM\s+-", "stop_process 应先对整组发送 TERM")
        self.assertRegex(self.region, r"kill\s+-KILL\s+-", "stop_process 应最终对整组发送 KILL")


if __name__ == "__main__":
    unittest.main()
