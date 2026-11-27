# BataCode

Phone-only app. Chat with Qwen on the server (through an SSH tunnel) and use git on repos in `~/batacode-workspace`.

Nothing is installed or hosted on the server except the existing tunnel to Ollama.

## Run (Termux)

    bash start.sh

Open http://localhost:8787 in Chrome. Use "Add to Home screen" to install.

## Git control centre

The Git tab works on the selected repo.

- Review: see every changed and new file, tap one for its colored diff, or press Full diff.
- Commit: tick the files you want and press Commit selected. Only those files go in the commit.
- Revert: Discard selected throws away changes in the ticked files (asks first). In History, open a commit and press Revert this commit to undo it with a new commit.
- Branches: switch with the menu (remote branches too), or create a new one.
- Sync: Pull, Pull (rebase) for when the remote has new commits, and Push.
- History: the latest 40 commits, tap one to see what it changed.

## Project management

- Repos tab: lists the git repos in the workspace, clones new ones, and creates new projects (Blank, Python, Node or Web page). New projects start with `git init` on `main` and are not committed yet.
- Files tab: browse folders, open and edit files (Save, Delete, New file), and view the project structure as a tree.
- Search tab: search code across all repos or just the current one, with an optional regex. Tap a result to open the file at that line.

## AI coding assistant

- Chat with the Qwen model on your server. The conversation is kept between sessions (last 40 messages) and the context size is set with the ctx menu (default 8k).
- Quick actions fill the prompt: Explain, Fix error, Refactor, Optimise, New file. Attach files or press Add diff to give the model your code.
- Project: on sends the repo file list and git status with every message, which helps with debugging a whole project.
- When the model writes a file it labels it `File: path`, so Save to repo and Download pre-fill the right name.

## Patch management (Patch tab)

The whole change workflow in one place, built for a phone:

1. Choose an existing repo (the repo menu in Chat, or the Repos tab).
2. Describe the change in the Patch tab and press Ask Qwen. The reply streams into Chat as a plan plus complete files.
3. Review: press Apply to repo under the reply (or Review patch). Back in the Patch tab every changed file is listed with `+added -removed` line counts. Tap a file for its colored diff and untick anything that should not be part of the patch.
4. Press Download ZIP and show commands. The ZIP holds only the ticked changed files (paths kept), named after the change: `add-logout` gives `add-logout.zip`. Deleted files cannot be zipped, so they are removed by a `git rm` line instead, and renamed files are handled the same way.
5. The exact commands appear with a Copy button, for example:

        cd ~/batacode-workspace/demo
        unzip -o ~/storage/downloads/add-logout.zip
        git rm -f -q --ignore-unmatch -- old.txt
        git add -- app.py header.js
        git status --short
        git commit -m 'Add logout button'
        git push -u origin main

Under "Where the files are installed" you can set the repo folder and the download folder on the machine where you install (the defaults are `~/batacode-workspace/<repo>` and `~/storage/downloads`; in Termux run `termux-setup-storage` and `pkg install unzip git` once). The commit message defaults to your description. File names are shell-quoted, so unusual names are safe to paste.

Commands only shows the commands without downloading. You can also use the Patch tab without Qwen: edit files yourself, then review and zip them.

## Memory

Lightweight and optional: a single small JSON file, `~/batacode-workspace/.batacode-memory.json` (max 50 notes, 300 characters each). Notes are sent to the model with every message.

- `remember I prefer Python 3.11 and type hints` saves a note.
- `memory` lists notes. `forget 2` removes note 2. `forget all` clears them.

## In the chat

- Clone: type `clone https://github.com/owner/repo.git` (optional folder name after the URL). The repo is cloned and selected.
- Download: every code block has a Download button. The Repos tab has Download repo (all files, no `.git`) and Download changes (only changed and new files, named after the change you type, for example `fix-login.zip`).
- Upload: the Upload button in the chat adds text files from your phone as context. In the Repos tab, Upload files saves files into the repo (optional folder), and zip files can be extracted into it.

## AI development automation (Auto tab)

Pick a repo, optionally describe what you want in the text box, and press one of the seven actions. Each one first collects real facts from the repo on the phone, builds a prompt that fits the ctx size, and sends it to Qwen through the normal chat. The answer streams into the Chat tab, so you can ask follow-up questions.

- Analyze project: languages, file counts, manifests, entry points and key files go to the model, which explains the architecture, how to run it, the weak spots and the top 5 next steps.
- Find and fix bugs: checks the syntax of every Python, JavaScript and JSON file, runs the project's tests, and sends the failures plus the files named in the errors to the model, which returns corrected files. The test run asks first because it executes the project's code. Untick "run tests" to check syntax only. With nothing failing, describe the bug in the text box.
- Build a feature: describe it in the text box. The most relevant files are picked by keyword and sent with the file list. The model replies with a plan and complete files.
- Review code: a pattern scan (hard-coded secrets, eval and exec, shell=True, pickle, SQL built from strings, innerHTML, disabled TLS checks, bare except, mutable defaults, very long functions and more) plus the git diff go to the model, which confirms real issues, dismisses false alarms and adds what the scan cannot see. Add `nosec` to a line to silence the scan for it. Scope: changed files or the whole project.
- Generate tests: unit and integration tests in the framework the project already uses (unittest or node:test if none), written to `tests/`.
- Write docs: `README.md` and `docs/TECHNICAL.md` based on the code. Unknown facts are marked `TODO` instead of guessed.
- Check dependencies: reads `requirements*.txt`, `pyproject.toml` and `package.json`, asks PyPI and npm for the latest versions, and flags outdated and major-behind packages, a latest release that needs a newer Python, packages with no version limit, conflicting pins, and duplicates. The model then sorts them into update now, needs care, and risky. It has no vulnerability database, so use `pip-audit` or `npm audit` for security advisories.

When a reply contains files labelled `File: path`, an Apply bar appears under it. Apply to repo writes all of them at once (after asking, and only inside the repo). Apply and check also runs the syntax check and tests and posts the result in the chat. Everything is a normal working-tree change, so the Git tab can review, commit or discard it.

Big prompts only stay in the model's context until the next Auto action, and only their short summary is saved in the chat history. The amount of code sent per request follows the ctx menu (about 1.6 characters per token of context).

## Tests

    python3 -m unittest discover -s tests -v

Covers the automation tools (version-range logic, scanner rules, checks, file selection, apply safety, dependency analysis with a fake registry) and patch management (change review, ZIP contents, generated commands, hostile input). No network or Ollama needed.

## Notes

- Binds to 127.0.0.1 only. Rejects other Host and Origin headers.
- Git uses your normal Termux git credentials. HTTPS URLs are the supported path.
- Repos live in `~/batacode-workspace` (change with `PWA_WORKSPACE`).
- Env vars: `PWA_PORT` (8787), `OLLAMA_URL` (http://localhost:11434).
- Check dependencies contacts pypi.org and registry.npmjs.org from the phone (package names only). Results are cached for an hour.
- Find and fix bugs and Apply and check run the project's tests, which executes its code. Run them only on repos you trust.
app push test
