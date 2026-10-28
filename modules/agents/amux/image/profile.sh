# Login shells, such as Codex's command runner, reset PATH from /etc/profile.
# Keep the session's tool directories ahead of the system ones.
if [ -n "${AMUX_PATH:-}" ]; then
  PATH="$AMUX_PATH:$PATH"
fi
