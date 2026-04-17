FROM python:3.12-slim

# WeasyPrint system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (cache layer)
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy app
COPY . .
RUN uv sync --frozen --no-dev

# Create output dir for preferences/weights
RUN mkdir -p /app/output /app/browser_data

EXPOSE 8080

CMD ["uv", "run", "pa", "dashboard", "--port", "8080"]
