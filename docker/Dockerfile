# The OpenDot step image. Every model step runs in a container made from it.
#
# Build it with `opendot build-image`, which sends this file on stdin with no
# build context, so nothing from the host folder can be copied into a layer.
# Only public releases are installed. Keep the versions in step with
# src/opendot/container_contract.py.

FROM ghcr.io/astral-sh/uv:0.12.20 AS uv

FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git python3 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

RUN npm install -g @openai/codex@0.159.0 @anthropic-ai/claude-code@2.1.284 \
 && npm cache clean --force

# The built-in browser: the Playwright MCP server (command playwright-mcp) and
# the Chromium build that its own Playwright version expects, with the system
# libraries and fonts Chromium needs. Chromium runs headless inside the step
# container; it has no saved logins and uses the container's network.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN npm install -g @playwright/mcp@0.0.83 \
 && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD= node /usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core/cli.js install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

# A fixed non-root user. At run time the root file system is read-only; HOME
# and /tmp are tmpfs mounts, and /work and /opendot/cli are bind mounts.
RUN groupadd --gid 10001 opendot \
 && useradd --uid 10001 --gid 10001 --home-dir /opendot/home --no-create-home --shell /bin/bash opendot \
 && mkdir -p /work /opendot/cli /opendot/home \
 && chown 10001:10001 /work /opendot/cli /opendot/home

# No setuid or setgid programs: snap Docker cannot use no-new-privileges, so
# nothing in the image may gain rights through one.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +

ENV DISABLE_AUTOUPDATER=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    npm_config_cache=/tmp/npm-cache

USER 10001:10001
WORKDIR /work
