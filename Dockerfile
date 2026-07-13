FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-cache

COPY server.py client.py db.py grouping.py dashboard.py ./
COPY static ./static

EXPOSE 8000

CMD ["uv", "run", "--no-sync", "python", "server.py"]
