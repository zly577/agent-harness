FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app/backend
COPY backend/requirements-lock.txt ./requirements-lock.txt
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements-lock.txt
RUN groupadd --gid 10001 harness \
    && useradd --uid 10001 --gid harness --create-home harness \
    && mkdir -p /app/backend/data /app/workspace /app/logs \
    && chown -R harness:harness /app
COPY --chown=harness:harness backend/app ./app
COPY --chown=harness:harness backend/configs ./configs
COPY --chown=harness:harness backend/scripts ./scripts
COPY --chown=harness:harness backend/alembic ./alembic
COPY --chown=harness:harness backend/alembic.ini ./alembic.ini
USER harness
# 在干净镜像中检查完整启动导入链，不连接数据库或调用模型。
RUN python -c "from app.main import app; assert app is not None"
EXPOSE 8000
# 运行状态/审批缓存与 SQLite 为单进程设计，保持一个 worker。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
