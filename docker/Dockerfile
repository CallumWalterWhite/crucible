# Build context is the repository root:
#   docker build -f docker/Dockerfile .

FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
# Dependencies come from uv.lock (pinned, hashed); the wheel itself is installed without deps.
RUN uv build --wheel --out-dir /dist \
 && python -m venv /opt/venv \
 && uv export --frozen --no-dev --extra server --no-emit-project --format requirements-txt > /tmp/req.txt \
 && uv pip install --python /opt/venv/bin/python --require-hashes -r /tmp/req.txt \
 && uv pip install --python /opt/venv/bin/python --no-deps /dist/*.whl


FROM python:3.14-slim AS runtime

# uid 10001 is fixed so that volume ownership can be set from outside the container.
RUN groupadd --system --gid 10001 crucible \
 && useradd --system --uid 10001 --gid crucible --home-dir /data --shell /usr/sbin/nologin crucible

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    CRUCIBLE_HOST=0.0.0.0

WORKDIR /data
RUN chown crucible:crucible /data
USER crucible

EXPOSE 9050 8080

# CRUCIBLE_HOST=0.0.0.0 is required inside the container, because Docker port publishing
# forwards to the container's network address, not to its loopback. That exposes the
# port to every interface of the container, so the host must publish it to 127.0.0.1 only:
#   docker run -p 127.0.0.1:9050:9050 crucible:latest
# The health check accepts either default port (9050 BigQuery, 8080 Snowflake).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request\nfor p in (9050, 8080):\n    try:\n        urllib.request.urlopen(f'http://127.0.0.1:{p}/_crucible/health', timeout=1); sys.exit(0)\n    except OSError:\n        pass\nsys.exit(1)"]

ENTRYPOINT ["crucible"]
CMD ["serve", "--dialect", "bigquery"]
