FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS dependencies
ARG UV_VERSION=0.11.23
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
RUN python -m pip install --no-cache-dir --only-binary=:all: uv==${UV_VERSION}
WORKDIR /opt/condor
COPY pyproject.toml uv.lock ./
# http-ece is the upstream locked pure-Python sdist; every other package needs a wheel.
RUN python -c "import subprocess,tomllib; packages=tomllib.load(open('uv.lock','rb'))['package']; subprocess.run(['uv','sync','--frozen','--no-dev','--no-install-project','--python','/usr/local/bin/python']+[arg for pkg in packages if pkg['name'] != 'http-ece' for arg in ('--no-build-package',pkg['name'])],check=True)"

FROM node:22-bookworm-slim AS claude
WORKDIR /opt/claude
COPY docker/claude/package.json docker/claude/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.13-slim-bookworm AS runtime
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Condor" \
      org.opencontainers.image.source="https://github.com/FortPT/condor" \
      org.opencontainers.image.revision=${VCS_REF} \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git libgomp1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin condor \
    && mkdir -p /var/lib/condor && chown condor:condor /var/lib/condor
COPY --from=dependencies /usr/local/bin/uv /usr/local/bin/uv
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=claude /opt/claude /opt/claude
COPY --from=dependencies /opt/condor/.venv /opt/condor/.venv
COPY main.py config_manager.py pyproject.toml uv.lock LICENSE .mcp.json /opt/condor/
COPY condor/ /opt/condor/condor/
COPY handlers/ /opt/condor/handlers/
COPY utils/ /opt/condor/utils/
COPY mcp_servers/ /opt/condor/mcp_servers/
COPY agents/ /opt/condor/agents/
COPY routines/ /opt/condor/routines/
COPY --from=frontend /build/frontend/dist /opt/condor/frontend/dist
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/condor-entrypoint
COPY docker/smoke.py /opt/condor-container-smoke.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_OFFLINE=true UV_NO_SYNC=true \
    HOME=/var/lib/condor PATH=/opt/claude/node_modules/.bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/opt/condor/.venv \
    CONDOR_DATA_DIR=/var/lib/condor/data CONDOR_RUNTIME_ROOT=/var/lib/condor/.condor \
    CONDOR_AGENTS_ROOT=/var/lib/condor/local-agents \
    CONDOR_STOCK_AGENTS_ROOT=/opt/condor/agents \
    CONDOR_REPORTS_DIR=/var/lib/condor/reports \
    CONDOR_TELEMETRY=off CONDOR_SHARING=off
WORKDIR /var/lib/condor
USER 10001:10001
VOLUME ["/var/lib/condor"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD /opt/condor/.venv/bin/python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WEB_PORT','8088')+'/api/v1/auth/mode',timeout=3).read()"
ENTRYPOINT ["condor-entrypoint"]
CMD ["uv", "run", "--offline", "--no-sync", "--project", "/opt/condor", "python", "-m", "main"]
