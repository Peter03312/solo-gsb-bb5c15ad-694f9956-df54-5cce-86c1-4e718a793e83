#!/bin/sh
# 容器入口：确保数据目录存在 -> 初始化/升级 SQLite 表（幂等）-> 启动服务
set -eu

: "${LF_DATA_DIR:=/data}"
mkdir -p "$LF_DATA_DIR"

echo "[entrypoint] 数据目录: $LF_DATA_DIR"
python -m lostfound.init_db
echo "[entrypoint] 数据库已就绪"

if [ "${STAFF_PASSWORD:-change-me}" = "change-me" ]; then
  echo "[entrypoint] 警告：未设置 STAFF_PASSWORD，正在使用默认口令 change-me，生产环境请务必修改！" >&2
fi

exec python -m lostfound.app
