#!/bin/bash
# Verifier entry point. Harbor runs this as root in the verifier container after the agent's /app has
# been copied in. All logic is in verify.py; the outcome is /logs/verifier/reward.json and reward.txt.
cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && exec python3 verify.py
