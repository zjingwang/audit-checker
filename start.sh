#!/usr/bin/env bash
# ============================================================
#  一键启动 PaddleOCR 审计后端
#
#  用法：
#    ./start.sh          # 默认端口 8000（前端 index.html 默认连这个端口）
#    ./start.sh 9000     # 指定端口 9000
#
#  逻辑（自动完成，无需手动配置）：
#    1) 用项目内 .venv（若存在）或系统 python3；
#    2) 缺依赖时自动创建 .venv 并安装 requirements.txt（仅首次）；
#    3) 模型缓存目录固定在项目内 .smoke_home（已 gitignore）：
#       首次识别时自动下载一次，之后每次直接复用，不重复下载。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8000}"

# ---------- 1) 定位 Python ----------
PY=""
if [ -x ".venv/bin/python" ]; then
  PY="$PWD/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "✖ 未找到 python3，请先安装 Python 3.10+"
  exit 1
fi

# ---------- 2) 依赖检查，缺失则自动安装（find_spec 不触发导入副作用）----------
if ! "$PY" -c '
import importlib.util as u, sys
need = ["fastapi", "uvicorn", "openpyxl", "pypdfium2", "requests", "PIL", "paddleocr"]
sys.exit(0 if all(u.find_spec(m) is not None for m in need) else 1)
'; then
  echo "▶ 首次启动：创建项目内 .venv 并安装依赖（仅此一次，请稍候）..."
  command -v python3 >/dev/null 2>&1 || { echo "✖ 需要 python3 来创建虚拟环境"; exit 1; }
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip -q
  ./.venv/bin/python -m pip install -r requirements.txt
  PY="$PWD/.venv/bin/python"
fi

# ---------- 3) 模型缓存放项目内目录（持久复用，不出现在 git 中）----------
export HOME="$PWD/.smoke_home"
mkdir -p "$HOME"

echo "▶ 启动后端：端口 ${PORT}，模型缓存：${HOME}（首次识别时自动下载模型）"
exec "$PY" app.py --port "$PORT"
