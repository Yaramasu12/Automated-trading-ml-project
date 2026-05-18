FROM python:3.12-slim

WORKDIR /app

# System deps for smartapi-python websocket client
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gcc \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer-cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY trading_platform/ ./trading_platform/
COPY scripts/ ./scripts/
COPY .env.example .env.example

# Create data directories
RUN mkdir -p data/feature_store backtest_results

# Non-root user for security
RUN useradd -m -u 1000 trader && chown -R trader:trader /app
USER trader

EXPOSE 8000

CMD ["uvicorn", "trading_platform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
