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

# Pre-create agent state directories so the home volume, seeded from this
# image, is owned by the user even when read-only skill mounts nest inside.
# /nix seeds the shared amux-nix volume for single-user Nix; without
# /nix/var/nix, Nix would silently use a private store under $HOME instead.
ARG USER_NAME=amux
ARG USER_UID=1000
ARG USER_GID=1000
ARG USER_HOME=/home/amux
RUN (getent group "${USER_GID}" >/dev/null || groupadd -g "${USER_GID}" amux) \
 && useradd -o -u "${USER_UID}" -g "${USER_GID}" -d "${USER_HOME}" -M -s /bin/bash "${USER_NAME}" \
 && mkdir -p "${USER_HOME}/.claude/skills" "${USER_HOME}/.codex/skills" "${USER_HOME}/.cache" \
      "${USER_HOME}/.config" "${USER_HOME}/.local/bin" /nix/var/nix \
 && chown -R "${USER_UID}:${USER_GID}" "${USER_HOME}" /nix

# Static Nix from the host's nixpkgs lock.
COPY nix-bin/ /usr/local/bin/
COPY entry.sh tmux.conf /amux/
COPY profile.sh /etc/profile.d/amux.sh

# Agents change most often, so they are the last layer. amux resolves the
# newest npm versions on the host and passes verified tarball references.
ARG CLAUDE_URL
ARG CLAUDE_INTEGRITY
ARG CODEX_URL
ARG CODEX_INTEGRITY
COPY fetch-agents.py /tmp/fetch-agents.py
RUN python3 /tmp/fetch-agents.py && rm /tmp/fetch-agents.py

ENV LANG=en_US.UTF-8 \
    TERM=xterm-256color \
    COLORTERM=truecolor \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_INSTALLATION_CHECKS=1 \
    USE_BUILTIN_RIPGREP=0 \
    NIX_CONF_DIR=/amux/etc \
    NIX_PATH=nixpkgs=flake:nixpkgs
LABEL dev.envy.amux.image=1
USER ${USER_UID}:${USER_GID}
WORKDIR ${USER_HOME}
ENTRYPOINT ["/amux/entry.sh"]
