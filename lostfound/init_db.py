"""容器启动时的数据初始化入口：python -m lostfound.init_db"""
from __future__ import annotations

import os

from .db import init_db

if __name__ == "__main__":
    data_dir = os.environ.get("LF_DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "lostfound.db")
    init_db(path)
    print(f"数据库初始化完成: {path}")
