# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web ./
RUN npm run build

FROM --platform=$BUILDPLATFORM python:3.13-slim AS runtime
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILT_AT=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CDT_DATA_DIR=/data \
    CDT_LISTEN_PORT=8080 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=frontend /src/app/static ./app/static

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin cdt \
    && mkdir -p /data \
    && chown -R cdt:cdt /data /app

USER cdt
VOLUME ["/data"]
EXPOSE 8080

LABEL org.opencontainers.image.title="CDT-Management" \
      org.opencontainers.image.description="阿里云 CDT 抢占实例保活与联合换班控制台" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILT_AT}" \
      org.opencontainers.image.source="https://github.com/BeacherZ/cdt-management" \
      cn.cdt-management.version="${VERSION}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "-m", "app.main"]
