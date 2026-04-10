const { app, BrowserWindow, dialog } = require("electron");
const { spawn, spawnSync } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");

const DESKTOP_BACKEND_PORT = Number(process.env.AGENT_PLAYGROUND_DESKTOP_PORT || 38011);

let backendProcess = null;

function resolveBackendHome() {
  return path.join(app.getPath("userData"), "backend");
}

function resolveBackendPidFile() {
  return path.join(resolveBackendHome(), "backend.pid");
}

function resolveBackendExecutable() {
  const exeName = process.platform === "win32" ? "agent-playground-backend.exe" : "agent-playground-backend";
  const baseDir = app.isPackaged
    ? path.join(process.resourcesPath, "backend", "agent-playground-backend")
    : path.join(__dirname, ".artifacts", "backend", "agent-playground-backend");
  return path.join(baseDir, exeName);
}

function resolveBundledSkillsRoot() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "backend", "agent-playground-backend", "backend", "skills")
    : path.join(__dirname, "..", "backend", "skills");
}

function resolveFrontendEntry() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "renderer", "index.html")
    : path.join(__dirname, ".artifacts", "renderer", "index.html");
}

function waitForBackend(url, timeoutMs = 20000) {
  const startedAt = Date.now();
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => {
        response.resume();
        if (response.statusCode && response.statusCode < 500) {
          resolve();
          return;
        }
        retry(new Error(`health check failed with status ${response.statusCode}`));
      });
      request.on("error", retry);
      request.setTimeout(1500, () => request.destroy(new Error("health check timed out")));
    };

    const retry = (error) => {
      if (Date.now() - startedAt >= timeoutMs) {
        reject(error);
        return;
      }
      setTimeout(attempt, 350);
    };

    attempt();
  });
}

async function startBackend() {
  const executablePath = resolveBackendExecutable();
  const userDataBackendHome = resolveBackendHome();
  fs.mkdirSync(userDataBackendHome, { recursive: true });
  const env = {
    ...process.env,
    AGENT_PLAYGROUND_HOST: "127.0.0.1",
    AGENT_PLAYGROUND_PORT: String(DESKTOP_BACKEND_PORT),
    AGENT_PLAYGROUND_APP_HOME: userDataBackendHome,
    AGENT_PLAYGROUND_ENV_PATH: path.join(app.getPath("userData"), ".env"),
    AGENT_PLAYGROUND_BUNDLED_SKILLS_ROOT: resolveBundledSkillsRoot(),
  };

  await stopBackendProcess();

  backendProcess = spawn(executablePath, [], {
    env,
    stdio: "ignore",
    windowsHide: true,
    detached: process.platform !== "win32",
  });

  try {
    fs.writeFileSync(resolveBackendPidFile(), String(backendProcess.pid || ""), "utf-8");
  } catch {}

  backendProcess.on("exit", () => {
    backendProcess = null;
    try {
      fs.rmSync(resolveBackendPidFile(), { force: true });
    } catch {}
  });

  await waitForBackend(`http://127.0.0.1:${DESKTOP_BACKEND_PORT}/api/health`);
}

async function stopBackendProcess() {
  const pidFile = resolveBackendPidFile();
  let targetPid = backendProcess?.pid || 0;

  if (!targetPid) {
    try {
      if (fs.existsSync(pidFile)) {
        const raw = fs.readFileSync(pidFile, "utf-8").trim();
        targetPid = Number(raw || 0);
      }
    } catch {}
  }

  if (!targetPid) {
    return;
  }

  try {
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/PID", String(targetPid), "/T", "/F"], { stdio: "ignore" });
    } else {
      try {
        process.kill(-targetPid, "SIGTERM");
      } catch {
        process.kill(targetPid, "SIGTERM");
      }
    }
  } catch {}

  backendProcess = null;
  try {
    fs.rmSync(pidFile, { force: true });
  } catch {}
}

async function createWindow() {
  await startBackend();

  const mainWindow = new BrowserWindow({
    width: 1440,
    height: 980,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: "#f8fafc",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
      additionalArguments: [
        `--agent-playground-api-base-url=http://127.0.0.1:${DESKTOP_BACKEND_PORT}`,
      ],
    },
  });

  await mainWindow.loadFile(resolveFrontendEntry());
}

app.whenReady().then(async () => {
  try {
    await createWindow();
  } catch (error) {
    dialog.showErrorBox(
      "Agent Playground failed to start",
      String(error && error.message ? error.message : error),
    );
    app.quit();
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow().catch((error) => {
        dialog.showErrorBox(
          "Agent Playground failed to start",
          String(error && error.message ? error.message : error),
        );
      });
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  void stopBackendProcess();
});

app.on("will-quit", () => {
  void stopBackendProcess();
});
