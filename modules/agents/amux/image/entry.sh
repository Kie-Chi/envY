#!/bin/sh
# Container main process: hold the agent in a private tmux server and stay
# alive until that server exits, which ends the session and the container.
# An agent that fails on its own keeps its last screen until Enter, so errors
# stay readable even in sessions started detached. Statuses above 128 mean an
# interrupt, such as quitting Codex with Ctrl+C, and close the session.
set -eu
# shellcheck disable=SC2016 # the inner script expands its own arguments
tmux -L amux -f /amux/tmux.conf new-session -d -s main \
  -x "${AMUX_COLS:-120}" -y "${AMUX_ROWS:-40}" -- /bin/sh -c '
# Ctrl+C belongs to the agent; caught here, it still has its default action there.
trap : INT QUIT
status=0
"$@" || status=$?
if [ "$status" -ne 0 ] && [ "$status" -le 128 ]; then
  printf "\namux: %s exited with status %s; press Enter to close" "$1" "$status"
  read -r reply || true
fi
exit "$status"' amux-agent "$@"
exec tmux -L amux wait-for amux-never
