#!/data/data/com.termux/files/usr/bin/bash
# Run in Termux (phone). Starts the SSH tunnel to Ollama if needed, then the app.
cd "$(dirname "$0")" || exit 1
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
if ! curl -s -m 3 http://localhost:11434/api/tags >/dev/null; then
  echo "Starting SSH tunnel to Ollama..."
  ssh -f -N -L 11434:localhost:11434 -i "$HOME/.ssh/server_ssh_key" ubuntu@41.75.113.54
  sleep 2
fi
if curl -s -m 3 http://localhost:11434/api/tags >/dev/null; then
  echo "Ollama reachable."
else
  echo "Ollama NOT reachable. App will still start, chat will fail."
fi
exec python3 server.py
