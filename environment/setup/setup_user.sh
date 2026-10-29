#!/bin/sh
# Git identity so commits work non-interactively, and the single non-root task user `agent` (uid 1000).
# ubuntu:24.04 ships an `ubuntu` account at uid 1000, so that one goes first.
set -eu
git config --system user.email agent@task.local
git config --system user.name "Agent"
if id -u 1000 >/dev/null 2>&1; then
    userdel -r "$(id -un 1000)" 2>/dev/null || true
fi
useradd --create-home --shell /bin/bash -u 1000 agent
