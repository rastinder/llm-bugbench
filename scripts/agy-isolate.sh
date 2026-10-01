#!/usr/bin/env bash
# Filesystem-isolated wrapper for the Antigravity CLI.
#
# WHY THIS EXISTS: measured, not assumed -- `agy --sandbox` does NOT stop file reads.
#   agy -p "Read ~/zen-proxy/zen_proxy.mjs" --sandbox   ->  returns the contents
# The agent has a file tool and no CLI flag removes it, so on a normal filesystem an
# agentic model can copy the reference fix out of the repo. It did: 9 of 12 answers were
# byte-identical to the historical fix.
#
# This puts the model in a mount namespace where the operator's real projects DO NOT EXIST.
# Only the CLI, its config and its credentials are visible. The NETWORK namespace is
# deliberately shared (Antigravity needs API access); the FILESYSTEM is not.
set -euo pipefail

# The Antigravity CLI keeps its state in ~/.gemini/antigravity-cli (brain/, conversations/,
# cache/, jetski_state.pbtxt) and its OAuth credential is reachable through the desktop
# keyring, whose socket lives in $XDG_RUNTIME_DIR/keyring. Both must be visible or the CLI
# re-prompts for OAuth on every single call ("authentication timed out") and the whole
# agentic lane scores 0 on a transport failure rather than on the model.
STATE="$HOME/.gemini/antigravity-cli"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

args=(
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin
  --ro-bind /etc/alternatives /etc/alternatives
  --ro-bind /etc/ssl /etc/ssl
  --ro-bind /etc/resolv.conf /etc/resolv.conf --ro-bind /etc/hosts /etc/hosts
  --dir /home --tmpfs "$HOME"
  --bind /tmp /tmp
  --proc /proc --dev /dev
  --unshare-user --unshare-pid --unshare-ipc --unshare-uts --die-with-parent
)
for p in "$HOME/.kimi" "$HOME/.local" "$HOME/.cache" \
         "$HOME/.config/Antigravity IDE" "$RUNTIME"; do
  [ -e "$p" ] && args+=(--ro-bind "$p" "$p")
done
# read-WRITE: the CLI writes logs, cache/default_project_id.txt and conversation state
# on every call, and refuses to start conversation without it. This is CLI bookkeeping
# only -- it holds none of the operator's projects, which is what the isolation is for.
for p in "$STATE" "$HOME/.antigravity"; do
  [ -e "$p" ] && args+=(--bind "$p" "$p")
done

exec bwrap "${args[@]}" "$@"
