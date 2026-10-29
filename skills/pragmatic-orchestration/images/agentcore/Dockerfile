# Build context: skills/pragmatic-orchestration. No credential/config directories copied.
FROM node:24-bookworm-slim AS node
FROM python:3.14-slim-bookworm
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq chromium \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Official release assets; hashes from GitHub's release-asset digest metadata.
RUN case "$TARGETARCH" in \
      amd64) codex_target=x86_64-unknown-linux-musl; codex_sha=be635d3486a5bd060d0fff582ae6d30160280c31344dd85719707fa920e187f0 ;; \
      arm64) codex_target=aarch64-unknown-linux-musl; codex_sha=6522e056d0be7f531da915c9a81ae05bc70811b782542499cc0a9776d3abf5d3 ;; \
      *) exit 1 ;; \
    esac \
    && curl -fSL "https://github.com/openai/codex/releases/download/rust-v0.165.0/codex-${codex_target}.tar.gz" -o /tmp/codex.tar.gz \
    && printf '%s  /tmp/codex.tar.gz\n' "$codex_sha" | sha256sum -c - \
    && tar -xzf /tmp/codex.tar.gz -C /tmp \
    && mv "/tmp/codex-${codex_target}" /usr/local/bin/codex \
    && chmod 755 /usr/local/bin/codex \
    && rm /tmp/codex.tar.gz
COPY scripts /opt/porch/scripts
COPY prompts /opt/porch/prompts
COPY references /opt/porch/references
COPY config.example.json /opt/porch/config.example.json
COPY SKILL.md /opt/porch/SKILL.md
RUN useradd --create-home --uid 1000 agent && mkdir -p /work /home/agent/.agents/skills \
    && ln -s /opt/porch /home/agent/.agents/skills/pragmatic-orchestration \
    && chown -R agent:agent /work /home/agent/.agents
ENV PORCH_FLEET_CONFIG=/work/fleet.json \
    PORCH_STEER_DIR=/work/porch/steer \
    PORCH_OUTPUT_DIR=/work/porch/artifacts \
    PORCH_CONFIG=/opt/porch/config.example.json \
    CODEX_HOME=/work/codex
USER 1000:1000
WORKDIR /work
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/opt/porch/scripts/lib/agentcore_guest.py", "--bind", "0.0.0.0"]
