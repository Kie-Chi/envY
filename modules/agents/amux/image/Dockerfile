# Session image for amux. One container per session runs a coding agent inside
# a private tmux server; the host user identity and home path are reproduced
# so bind-mounted files keep their ownership and absolute paths.
ARG BASE_IMAGE=docker.io/library/debian:trixie-slim
FROM ${BASE_IMAGE}

# The slim image has no CA bundle yet, so APT bootstraps over HTTP. Release
# files are still verified against the Debian archive keyring.
ARG APT_MIRROR=http://deb.debian.org/debian
ARG APT_SECURITY_MIRROR=http://deb.debian.org/debian-security
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian-security$|URIs: ${APT_SECURITY_MIRROR}|" \
      -e "s|^URIs: http://deb.debian.org/debian$|URIs: ${APT_MIRROR}|" \
      /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git less locales openssh-client procps python3 ripgrep tmux \
 && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \
 && printf 'Host *\n  StrictHostKeyChecking accept-new\n' > /etc/ssh/ssh_config.d/amux.conf \
 && rm -rf /var/lib/apt/lists/*

# Agent versions follow the nixpkgs lock; binaries come from the npm registry
# and are verified before installation.
ARG NPM_REGISTRY=https://registry.npmjs.org/
ARG CLAUDE_VERSION
ARG CLAUDE_SHA256_ARM64
ARG CLAUDE_SHA256_AMD64
ARG CODEX_VERSION
COPY fetch-agents.py /tmp/fetch-agents.py
RUN python3 /tmp/fetch-agents.py && rm /tmp/fetch-agents.py

# Pre-create agent state directories so the home volume, seeded from this
# image, is owned by the user even when read-only skill mounts nest inside.
ARG USER_NAME=amux
ARG USER_UID=1000
ARG USER_GID=1000
ARG USER_HOME=/home/amux
RUN (getent group "${USER_GID}" >/dev/null || groupadd -g "${USER_GID}" amux) \
 && useradd -o -u "${USER_UID}" -g "${USER_GID}" -d "${USER_HOME}" -M -s /bin/bash "${USER_NAME}" \
 && mkdir -p "${USER_HOME}/.claude" "${USER_HOME}/.codex" "${USER_HOME}/.cache" "${USER_HOME}/.config" \
 && chown -R "${USER_UID}:${USER_GID}" "${USER_HOME}"

COPY entry.sh tmux.conf /amux/
ENV LANG=en_US.UTF-8 \
    TERM=xterm-256color \
    COLORTERM=truecolor \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_INSTALLATION_CHECKS=1 \
    USE_BUILTIN_RIPGREP=0
LABEL dev.envy.amux.image=1
USER ${USER_UID}:${USER_GID}
WORKDIR ${USER_HOME}
ENTRYPOINT ["/amux/entry.sh"]
