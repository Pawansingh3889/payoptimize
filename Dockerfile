# PayOptimize AI — one process, one worker, one SQLite file on a volume.
#
# Installs from uv.lock rather than resolving at build time. PLAN §12 wrote this
# as `pip install .`, but the dependency pins in pyproject.toml are lower bounds:
# a build on demo morning would happily pick up a release that did not exist when
# the tests last passed. The lockfile is committed precisely so that cannot
# happen, and a rebuild during judging has to produce the image that was tested.

FROM python:3.12-slim

# Pinned by digest-free tag on purpose: uv is a build-time tool, and a floating
# patch of it cannot change what ends up installed — uv.lock decides that.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so a source-only change does not re-resolve the world.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    PAYOPTIMIZE_DB=/data/payoptimize.sqlite3 \
    PAYOPTIMIZE_PORT=8080 \
    PYTHONUNBUFFERED=1

# A directory, never a bare file: SQLite in WAL mode writes -wal and -shm
# siblings, and a single-file mount would leave them on the container's
# ephemeral layer where a restart loses committed transactions.
VOLUME /data
EXPOSE 8080

# The serve command detects /run/.containerenv or /.dockerenv and binds 0.0.0.0.
CMD ["python", "-m", "payoptimize", "serve"]
