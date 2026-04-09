# Multi-Agent Playground

This repo is now self-contained at:

`C:\Users\zhang\MySpace\study\dev\ai\Multi-Agent-Playground`

## Runtime layout

- Python venv: `.\backend\.venv`
- Env file: `.\.env`
- Backend: `.\backend`
- Frontend: `.\frontend`

Backend config is loaded from the project root `.env` only.

## Quick setup (Windows, no PowerShell scripts)

```powershell
cd C:\Users\zhang\MySpace\study\dev\ai\Multi-Agent-Playground
C:\Users\zhang\AppData\Local\Programs\Python\Python312\python.exe -m venv .\backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install --upgrade pip
.\backend\.venv\Scripts\python.exe -m pip install -r .\backend\requirements.txt
```

Then configure:

```text
.\.env
```

At minimum, set `OPENAI_API_KEY`.

## Start services

Backend:

```powershell
cd .\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8011 --reload
```

Frontend:

```powershell
cd .\frontend
npm run dev
```

Frontend proxy default target is `http://127.0.0.1:8011`.

## Desktop Packaging

This repo now supports a desktop distribution path while preserving the normal dev workflow above.

Desktop packaging overview:

1. Build frontend static assets from `frontend/`
2. Build a standalone backend executable from `backend/` via PyInstaller
3. Package both into an Electron app from `desktop/`

Prerequisites for packaging:

- Frontend dependencies installed in `frontend/node_modules`
- Backend venv installed in `backend/.venv`
- Desktop packaging dependencies installed in `desktop/node_modules`
- `pyinstaller` installed in the backend venv:

```bash
cd backend
./.venv/bin/python -m pip install -r requirements-desktop.txt
```

macOS packaging:

```bash
cd desktop
npm install
npm run dist:mac
```

The default macOS packaging command above disables code signing auto-discovery so local test builds do not fail on Apple timestamp service issues.

If you want a signed macOS release build and already have signing configured on this machine:

```bash
cd desktop
npm run dist:mac:signed
```

Windows packaging:

```bash
cd desktop
npm install
npm run dist:win
```

Build artifacts are generated locally and should be uploaded to GitHub Releases manually in the first phase.

## Notes

- Use Python from `.\backend\.venv\Scripts\python.exe`.
- If `.\.env` does not exist, copy from `.\.env.example`.
- Backend dependencies are in `.\backend\requirements.txt`.
- Desktop packaging config lives in `./desktop`.
