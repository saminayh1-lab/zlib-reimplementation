#!/bin/sh
# Container init, run once per sandbox: anchor the wall-clock budget, then hand over to the command.
set -e
sandbox-timer start || true
[ "$#" -gt 0 ] || set -- tail -f /dev/null
exec "$@"
