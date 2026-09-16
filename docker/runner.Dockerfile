FROM node:22-bookworm-slim

RUN (sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
    && (sed -i 's@deb.debian.org@mirrors.tuna.tsinghua.edu.cn@g' /etc/apt/sources.list 2>/dev/null || true)

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    jq \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --registry=https://registry.npmmirror.com @openai/codex @anthropic-ai/claude-code --omit=dev \
    && npm cache clean --force

RUN mkdir -p /workspace /home/node/.codex /home/node/.claude \
    && chown -R 1000:1000 /workspace /home/node

COPY docker/runner-entrypoint.sh /usr/local/bin/runner-entrypoint.sh
RUN chmod +x /usr/local/bin/runner-entrypoint.sh

WORKDIR /workspace
USER node

ENV PATH="/workspace/.venv/bin:/workspace/node_modules/.bin:${PATH}"
ENV HOME="/home/node"
ENV TERM="xterm-256color"

ENTRYPOINT ["/usr/local/bin/runner-entrypoint.sh"]
CMD ["/bin/bash"]
