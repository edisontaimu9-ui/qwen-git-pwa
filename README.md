# Qwen Git PWA

Phone-only app. Chat with Qwen on the server (through an SSH tunnel) and use git on repos in `~/qwen-workspace`.

Nothing is installed or hosted on the server except the existing tunnel to Ollama.

## Run (Termux)

    bash start.sh

Open http://localhost:8787 in Chrome. Use "Add to Home screen" to install.

## Notes

- Binds to 127.0.0.1 only. Rejects other Host and Origin headers.
- Git uses your normal Termux git credentials. HTTPS URLs are the supported path.
- Repos live in `~/qwen-workspace` (change with `PWA_WORKSPACE`).
- Env vars: `PWA_PORT` (8787), `OLLAMA_URL` (http://localhost:11434).
app push test
