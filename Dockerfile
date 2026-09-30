# 失物认领系统：仅依赖 Python 标准库 + SQLite，无需 pip 安装第三方包。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    LF_DATA_DIR=/data

WORKDIR /app

# 仅拷贝标准库实现的应用代码
COPY lostfound ./lostfound
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app
EXPOSE 8080

# 容器启动时初始化/升级数据库，再启动多线程 HTTP 服务
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
