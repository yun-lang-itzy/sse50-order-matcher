#!/bin/zsh
cd "${0:A:h}"
if [[ -n "$SIM_PYTHON" ]]; then
  exec "$SIM_PYTHON" server.py "$@"
elif [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python server.py "$@"
elif [[ -x /opt/anaconda3/bin/python ]]; then
  exec /opt/anaconda3/bin/python server.py "$@"
else
  exec python3 server.py "$@"
fi
