#!/usr/bin/env bash
# 组装飞牛 fpk 应用包（在打包机上执行，需已安装 fnpack）
# 用法: bash assemble.sh [输出目录]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-/tmp/scrcpy-fnos-build}"

echo "=== [1/3] 同步骨架到 ${OUT_DIR} ==="
rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"
cp -r "${SRC_DIR}/." "${OUT_DIR}/"
# 打包产物与开发期文件不进包（.gitignore/.gitattributes 与 .git 同类，都是版本控制元数据）
rm -rf "${OUT_DIR}/.git" "${OUT_DIR}/.gitignore" "${OUT_DIR}/.gitattributes" \
       "${OUT_DIR}/release" "${OUT_DIR}/assemble.sh" 2>/dev/null || true
# __pycache__ 也不进包：留着上次编译的 .pyc，跟着源码一起装上去只会让人怀疑
# 「到底跑的是不是新代码」（历史上就这么被坑过一次）。
find "${OUT_DIR}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${OUT_DIR}" -name '*.pyc' -delete 2>/dev/null || true

echo "=== [2/3] 检查必要文件 ==="
for f in manifest app/ui/config cmd/main ICON.PNG ICON_256.PNG; do
    if [ ! -e "${OUT_DIR}/${f}" ]; then
        echo "缺少必要文件：${f}" >&2
        exit 1
    fi
done

echo "=== [3/3] 打 fpk ==="
cd "${OUT_DIR}"
fnpack build
ls -la ./*.fpk
