# xradar engine as a web service (Render, Fly, any Docker host).
# Build context: the python/ directory.   docker build -t xradar-engine python/
# Run:           docker run -p 10000:10000 -e XRADAR_ALLOWED_ORIGINS=https://your-site.example xradar-engine

FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential pkg-config libcairo2-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml ./
COPY engine ./engine
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir ".[radarx]"

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 xradar
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY server.py ./
COPY engine ./engine
COPY modules ./modules
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    XRADAR_WEB=1 \
    PORT=10000 \
    MPLCONFIGDIR=/tmp/matplotlib
USER xradar
EXPOSE 10000
CMD ["python", "server.py", "--web", "--host", "0.0.0.0"]
