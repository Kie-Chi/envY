#!/bin/sh
# Container main process: hold the agent in a private tmux server and stay
# alive until that server exits, which ends the session and the container.
set -eu
tmux -L amux -f /amux/tmux.conf new-session -d -s main \
  -x "${AMUX_COLS:-120}" -y "${AMUX_ROWS:-40}" -- "$@"
exec tmux -L amux wait-for amux-never
