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
exec bwrap \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
  --ro-bind /etc/alternatives /etc/alternatives \
  --ro-bind /etc/resolv.conf /etc/resolv.conf --ro-bind /etc/hosts /etc/hosts \
  --dir /home --tmpfs "$HOME" \
  --bind "$HOME/.antigravity" "$HOME/.antigravity" \
  --bind "$HOME/.kimi" "$HOME/.kimi" \
  --bind "$HOME/.local" "$HOME/.local" \
  --bind "$HOME/.cache" "$HOME/.cache" \
  --ro-bind "$HOME/.config/Antigravity IDE" "$HOME/.config/Antigravity IDE" \
  --bind /tmp /tmp \
  --proc /proc --dev /dev \
  --unshare-user --unshare-pid --unshare-ipc --unshare-uts --die-with-parent \
  "$@"
