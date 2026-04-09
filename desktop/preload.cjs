const { contextBridge } = require("electron");

function resolveArgValue(prefix) {
  const match = process.argv.find((item) => item.startsWith(prefix));
  if (!match) return "";
  return match.slice(prefix.length);
}

contextBridge.exposeInMainWorld("__AGENT_PLAYGROUND_CONFIG__", {
  apiBaseUrl: resolveArgValue("--agent-playground-api-base-url="),
});
