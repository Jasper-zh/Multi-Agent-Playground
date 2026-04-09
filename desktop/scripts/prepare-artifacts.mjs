import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const desktopDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(desktopDir, "..");
const frontendDir = path.join(repoRoot, "frontend");
const backendDir = path.join(repoRoot, "backend");
const artifactsDir = path.join(desktopDir, ".artifacts");
const rendererArtifactsDir = path.join(artifactsDir, "renderer");
const backendArtifactsDir = path.join(artifactsDir, "backend");

function run(command, args, options = {}) {
  const resolvedCommand =
    process.platform === "win32" && command === "npm" ? "npm.cmd" : command;
  const result = spawnSync(resolvedCommand, args, {
    stdio: "inherit",
    cwd: options.cwd || repoRoot,
    env: options.env || process.env,
  });
  if (result.status !== 0) {
    throw new Error(`${resolvedCommand} ${args.join(" ")} failed with exit code ${result.status}`);
  }
}

function resolveBackendPython() {
  const override = process.env.AGENT_PLAYGROUND_PYTHON_BIN;
  if (override && existsSync(override)) return override;

  const candidates = [
    path.join(backendDir, ".venv", "bin", "python"),
    path.join(backendDir, ".venv", "Scripts", "python.exe"),
  ];
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

function removeAndRecreate(dirPath) {
  rmSync(dirPath, { recursive: true, force: true });
  mkdirSync(dirPath, { recursive: true });
}

function copyFrontendArtifacts() {
  run("npm", ["run", "build"], {
    cwd: frontendDir,
    env: {
      ...process.env,
      AGENT_PLAYGROUND_DESKTOP_BUILD: "1",
    },
  });
  const frontendDistDir = path.join(frontendDir, "dist");
  if (!existsSync(frontendDistDir)) {
    throw new Error("Frontend build output not found: frontend/dist");
  }
  cpSync(frontendDistDir, rendererArtifactsDir, { recursive: true });
}

function buildBackendArtifacts() {
  const pythonBin = resolveBackendPython();
  if (!pythonBin) {
    throw new Error(
      "Backend virtualenv python not found. Create backend/.venv first, or set AGENT_PLAYGROUND_PYTHON_BIN.",
    );
  }

  const addDataSeparator = process.platform === "win32" ? ";" : ":";
  const specDir = path.join(artifactsDir, "pyinstaller-spec");
  const buildDir = path.join(artifactsDir, "pyinstaller-build");
  const pyinstallerConfigDir = path.join(artifactsDir, "pyinstaller-config");
  mkdirSync(specDir, { recursive: true });
  mkdirSync(buildDir, { recursive: true });
  mkdirSync(pyinstallerConfigDir, { recursive: true });

  run(
    pythonBin,
    [
      "-m",
      "PyInstaller",
      path.join(backendDir, "desktop_entry.py"),
      "--name",
      "agent-playground-backend",
      "--noconfirm",
      "--clean",
      "--onedir",
      "--paths",
      backendDir,
      "--distpath",
      backendArtifactsDir,
      "--workpath",
      buildDir,
      "--specpath",
      specDir,
      "--hidden-import",
      "uvicorn.logging",
      "--hidden-import",
      "uvicorn.loops.auto",
      "--hidden-import",
      "uvicorn.protocols.http.auto",
      "--hidden-import",
      "uvicorn.protocols.websockets.auto",
      "--hidden-import",
      "uvicorn.lifespan.on",
      "--add-data",
      `${path.join(backendDir, "skills")}${addDataSeparator}backend/skills`,
    ],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYINSTALLER_CONFIG_DIR: pyinstallerConfigDir,
      },
    },
  );
}

removeAndRecreate(artifactsDir);
copyFrontendArtifacts();
buildBackendArtifacts();
