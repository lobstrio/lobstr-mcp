FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv pip install --system --no-cache .
COPY src ./src
# The public landing page + brand assets are served from /app/assets
# (server.py resolves it relative to the package root).
COPY assets ./assets
ENV PYTHONPATH=/app/src
EXPOSE 8000
CMD ["uvicorn", "lobstr_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
