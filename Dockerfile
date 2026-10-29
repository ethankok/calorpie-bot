FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PTB_TIMEDELTA=1 CALBOT_DB=/data/calbot.db UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY calbot ./calbot
VOLUME /data
CMD ["/opt/venv/bin/python", "-m", "calbot"]
