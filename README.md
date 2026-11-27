# BataCode

Phone-only app. Chat with Qwen on the server (through an SSH tunnel) and use git on repos in `~/batacode-workspace`.

Nothing is installed or hosted on the server except the existing tunnel to Ollama.

## Run (Termux)

    bash start.sh

Open http://localhost:8787 in Chrome. Use "Add to Home screen" to install.

## In the chat

- Clone: type `clone https://github.com/owner/repo.git` (optional folder name after the URL). The repo is cloned and selected.
- Download: every code block has a Download button. The Repos tab has Download repo (all files, no `.git`) and Download changes (only changed and new files, named after the change you type, for example `fix-login.zip`).
- Upload: the Upload button in the chat adds text files from your phone as context. In the Repos tab, Upload files saves files into the repo (optional folder), and zip files can be extracted into it.

## Notes

- Binds to 127.0.0.1 only. Rejects other Host and Origin headers.
- Git uses your normal Termux git credentials. HTTPS URLs are the supported path.
- Repos live in `~/batacode-workspace` (change with `PWA_WORKSPACE`).
- Env vars: `PWA_PORT` (8787), `OLLAMA_URL` (http://localhost:11434).
app push test
