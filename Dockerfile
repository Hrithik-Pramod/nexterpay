FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Source is copied before installing. Installing from pyproject.toml alone
# appears to succeed but installs no code, which then only works by accident
# because the working directory happens to be on sys.path.
COPY pyproject.toml ./
COPY app ./app

RUN pip install --upgrade pip && pip install .

COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

# Non-root. The bot needs no write access to the filesystem.
RUN useradd --create-home --uid 1000 nexterpay && chown -R nexterpay /app
USER nexterpay

# Fails the container if the app cannot be imported, rather than crash-looping
# silently on a typo.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import app.bot.main" || exit 1

CMD ["python", "-m", "app.bot.main"]
