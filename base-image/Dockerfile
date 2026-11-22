# The factory base image. Every enrolled repo's .factory/Dockerfile builds
# FROM it and adds its own toolchain. See "Base image" in
# docs/software-factory.md.
#
# Build it from the repo root, so the shared skills are in the context:
#   docker build -f base-image/Dockerfile -t factory-base .
FROM node:22-bookworm

ARG CLAUDE_CODE_VERSION=2.1.330
ARG SANDCASTLE_VERSION=0.14.0
ARG AGENT_BROWSER_VERSION=0.27.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    chromium \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g \
    @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    @ai-hero/sandcastle@${SANDCASTLE_VERSION} \
    agent-browser@${AGENT_BROWSER_VERSION} \
  && npm cache clean --force

# agent-browser drives Debian's Chromium rather than downloading Chrome for
# Testing, which has no Linux ARM64 build, so the image builds on any arch.
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium

# Runs pin the CLI through this image; it never updates itself mid-run.
ENV DISABLE_AUTOUPDATER=1

# Sandcastle's convention: the agent runs as "agent" (the base image's
# "node" user, renamed), with the project at /home/agent/workspace.
RUN usermod -d /home/agent -m -l agent node && groupmod -n agent node

# The shared skills, as the `factory` plugin in a local marketplace, so
# tasks call them as factory:code-review, factory:to-tickets and so on, and
# they never shadow, or get shadowed by, a target repo's own skills.
COPY base-image/marketplace /opt/factory-marketplace
COPY .agents/skills /opt/factory-marketplace/factory/skills

USER agent
WORKDIR /home/agent

RUN claude plugin marketplace add /opt/factory-marketplace \
  && claude plugin install factory@factory

ENTRYPOINT ["sleep", "infinity"]
