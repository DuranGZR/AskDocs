# syntax=docker/dockerfile:1

# ==============================================================================
# Stage 1: Rust Builder — text_processor native Python wheel (.whl) üretir
# ==============================================================================
FROM rust:1-slim-trixie AS rust-builder

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /uvx /bin/

ENV UV_CACHE_DIR=/root/.cache/uv \
    CARGO_HOME=/usr/local/cargo

WORKDIR /build/text_processor

# Yalnızca Rust crate dosyaları: src/ içindeki Python değişikliklerinde yeniden derlenmez
COPY text_processor/Cargo.toml text_processor/Cargo.lock text_processor/pyproject.toml text_processor/text_processor.pyi ./
COPY text_processor/src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/text_processor/target \
    uvx --python 3.11 --from "maturin>=1.7,<2.0" maturin build --release --locked --strip --compatibility off --out /wheels


# ==============================================================================
# Stage 2: Python Builder — Bağımlılıkları ve Rust wheel paketini /app/.venv içine kurar
# ==============================================================================
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python3.11

WORKDIR /app

# Önce yalnızca kilit dosyaları: src/ değiştiğinde paketler yeniden indirilmez
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY --from=rust-builder /wheels /wheels

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps /wheels/*.whl \
    && rm -rf /wheels


# ==============================================================================
# Stage 3: Runtime — Yalnızca çalışma zamanı dosyaları ve yetkisiz kullanıcı (derleyicisiz)
# ==============================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app"

# Sabit UID/GID ile yetkisiz kullanıcı: ortak volume izinleri öngörülebilir kalır
RUN groupadd --system --gid 10001 appgroup \
    && useradd --system --uid 10001 --gid appgroup --create-home --home-dir /home/appuser appuser

WORKDIR /app

# Builder aşamasından hazır .venv ortamını sahipliğiyle birlikte kopyala
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv

# Uygulama kodunu ve paylaşılan upload dizinini doğru izinlerle hazırla
COPY --chown=appuser:appgroup src ./src
RUN mkdir -p /app/data/uploads && chown -R appuser:appgroup /app/data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
