#!/bin/sh
# 容器启动脚本：安装依赖 → 初始化自检 → 启动服务
set -e

echo "[entrypoint] 安装/校验依赖 ..."
pip install --no-cache-dir -r requirements.txt

echo "[entrypoint] 执行初始化 ..."
python -m app.bootstrap

echo "[entrypoint] 启动 Web 服务 (0.0.0.0:8000) ..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
