<script setup>
import { computed, onMounted, provide, ref, watch } from "vue";
import {
  Activity,
  BrainCircuit,
  GitBranch,
  LayoutDashboard,
  Play,
  Settings2,
  Users,
} from "lucide-vue-next";

import {
  createAgent,
  createConversation,
  createWorkflow,
  deleteAgent,
  deleteWorkflow,
  fetchAppSettings,
  fetchAgents,
  fetchConversation,
  fetchSkills,
  fetchTemplates,
  fetchWorkflowGraph,
  fetchWorkflows,
  updateAgent,
  updateAppSettings,
  updateWorkflow,
  runWorkflowStream,
  runWorkflow,
} from "./api";
import AgentsPage from "./pages/AgentsPage.vue";
import { I18N_KEY, createUiI18n } from "./i18n";
import OverviewPage from "./pages/OverviewPage.vue";
import PlaygroundPage from "./pages/PlaygroundPage.vue";
import SettingsPage from "./pages/SettingsPage.vue";
import WorkflowsPage from "./pages/WorkflowsPage.vue";

const templates = ref([]);
const skills = ref([]);
const agents = ref([]);
const workflows = ref([]);
const selectedWorkflowId = ref("");
const selectedGraph = ref(null);
const lastRun = ref(null);
const loading = ref(false);
const errorMessage = ref("");
const currentPage = ref("overview");
const chatMessages = ref([]);
const currentConversationId = ref("");
const displayedTrace = ref([]);
const replayNodeId = ref("");
const replayingTrace = ref(false);
const replayToken = ref(0);
const activeRunController = ref(null);
const skillSyncStatus = ref("");
const appSettings = ref(null);
const savingSettings = ref(false);

const i18n = createUiI18n();
provide(I18N_KEY, i18n);
const { locale, setLocale, t } = i18n;

const navItems = computed(() => [
  { id: "overview", label: t("nav.overview"), icon: LayoutDashboard },
  { id: "agents", label: t("nav.agents"), icon: Users },
  { id: "workflows", label: t("nav.workflows"), icon: GitBranch },
  { id: "playground", label: t("nav.playground"), icon: Play },
  { id: "settings", label: t("nav.settings"), icon: Settings2 },
]);

const activeNodeId = computed(() => {
  if (replayingTrace.value) return replayNodeId.value;
  const trace = lastRun.value?.trace || [];
  for (let index = trace.length - 1; index >= 0; index -= 1) {
    if (trace[index].payload?.node_id) return trace[index].payload.node_id;
  }
  return "";
});

const traceForView = computed(() => (replayingTrace.value ? displayedTrace.value : (lastRun.value?.trace || [])));

const selectedWorkflow = computed(() =>
  workflows.value.find((workflow) => workflow.id === selectedWorkflowId.value) || null,
);

async function loadInitialData() {
  [templates.value, skills.value, agents.value, workflows.value, appSettings.value] = await Promise.all([
    fetchTemplates(),
    fetchSkills(),
    fetchAgents(),
    fetchWorkflows(),
    fetchAppSettings(),
  ]);

  if (!selectedWorkflowId.value && workflows.value.length) {
    selectedWorkflowId.value = workflows.value[0].id;
  }
}

async function loadGraph(workflowId) {
  if (!workflowId) {
    selectedGraph.value = null;
    return;
  }
  selectedGraph.value = await fetchWorkflowGraph(workflowId);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function replayTrace(traceEvents) {
  const token = replayToken.value + 1;
  replayToken.value = token;
  displayedTrace.value = [];
  replayNodeId.value = "start";
  replayingTrace.value = true;

  if (!traceEvents?.length) {
    replayingTrace.value = false;
    return true;
  }

  const stepDelay = traceEvents.length > 24 ? 85 : traceEvents.length > 14 ? 120 : 160;
  for (const event of traceEvents) {
    if (token !== replayToken.value) return false;
    displayedTrace.value = [...displayedTrace.value, event];
    const nextNode =
      event?.payload?.node_id ||
      event?.payload?.next_node_id ||
      "";
    if (nextNode) replayNodeId.value = nextNode;
    await sleep(stepDelay);
  }

  if (token === replayToken.value) {
    replayingTrace.value = false;
    return true;
  }
  return false;
}

async function handleCreateAgent(payload) {
  errorMessage.value = "";
  try {
    await createAgent(payload);
    agents.value = await fetchAgents();
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleUpdateAgent(payload) {
  errorMessage.value = "";
  try {
    await updateAgent(payload.id, payload.data);
    agents.value = await fetchAgents();
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleDeleteAgent(agentId) {
  errorMessage.value = "";
  try {
    await deleteAgent(agentId);
    agents.value = await fetchAgents();
    workflows.value = await fetchWorkflows();
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

function findSingleAgentChatWorkflow(agentId) {
  return (
    workflows.value.find(
      (workflow) =>
        workflow.type === "single_agent_chat" &&
        Array.isArray(workflow.specialist_agent_ids) &&
        workflow.specialist_agent_ids.length === 1 &&
        workflow.specialist_agent_ids[0] === agentId,
    ) || null
  );
}

async function handleQuickChatAgent(agent) {
  if (!agent?.id) return;
  errorMessage.value = "";
  try {
    let targetWorkflow = findSingleAgentChatWorkflow(agent.id);
    if (!targetWorkflow) {
      targetWorkflow = await createWorkflow({
        name: `${agent.name || "Agent"} Chat`,
        type: "single_agent_chat",
        specialist_agent_ids: [agent.id],
        finalizer_enabled: false,
        router_prompt: "Direct single-agent chat workflow.",
      });
      workflows.value = await fetchWorkflows();
      targetWorkflow =
        workflows.value.find((workflow) => workflow.id === targetWorkflow.id) ||
        findSingleAgentChatWorkflow(agent.id) ||
        targetWorkflow;
    }

    currentPage.value = "playground";
    selectedWorkflowId.value = targetWorkflow.id;
    await loadGraph(targetWorkflow.id);
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleCreateWorkflow(payload) {
  errorMessage.value = "";
  try {
    const workflow = await createWorkflow(payload);
    workflows.value = await fetchWorkflows();
    selectedWorkflowId.value = workflow.id;
    currentPage.value = "playground";
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleUpdateWorkflow(payload) {
  errorMessage.value = "";
  try {
    const updated = await updateWorkflow(payload.id, payload.data);
    workflows.value = await fetchWorkflows();
    if (selectedWorkflowId.value === updated.id) {
      await loadGraph(updated.id);
    }
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleDeleteWorkflow(workflowId) {
  errorMessage.value = "";
  try {
    await deleteWorkflow(workflowId);
    workflows.value = await fetchWorkflows();
    if (selectedWorkflowId.value === workflowId) {
      selectedWorkflowId.value = workflows.value[0]?.id || "";
      if (selectedWorkflowId.value) {
        await loadGraph(selectedWorkflowId.value);
      } else {
        selectedGraph.value = null;
      }
    }
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
}

async function handleSaveSettings(payload) {
  errorMessage.value = "";
  if (savingSettings.value) return;
  savingSettings.value = true;
  try {
    appSettings.value = await updateAppSettings(payload);
  } catch (error) {
    errorMessage.value = String(error.message || error);
  } finally {
    savingSettings.value = false;
  }
}

async function handleRun(payload) {
  errorMessage.value = "";
  loading.value = true;
  if (activeRunController.value) {
    activeRunController.value.abort();
    activeRunController.value = null;
  }
  const token = replayToken.value + 1;
  replayToken.value = token;
  displayedTrace.value = [];
  replayNodeId.value = "start";
  replayingTrace.value = true;
  const controller = new AbortController();
  activeRunController.value = controller;

  const userMessage = {
    id: `user_${Date.now()}`,
    role: "user",
    content: payload.user_input,
  };
  chatMessages.value = [...chatMessages.value, userMessage];

  const runPayload = {
    ...payload,
    conversation_id: currentConversationId.value || undefined,
  };

  try {
    let streamResult = null;
    let streamError = "";
    let streamTransportFailed = false;

    try {
      await runWorkflowStream(runPayload, {
        signal: controller.signal,
        onTrace: (event) => {
          if (token !== replayToken.value) return;
          displayedTrace.value = [...displayedTrace.value, event];
          const nextNode =
            event?.payload?.node_id ||
            event?.payload?.next_node_id ||
            "";
          if (nextNode) replayNodeId.value = nextNode;
        },
        onFinal: (result) => {
          if (token !== replayToken.value) return;
          streamResult = result;
        },
        onError: (error) => {
          if (token !== replayToken.value) return;
          streamError = error?.message || String(error || "");
        },
      });
    } catch (error) {
      if (error?.name === "AbortError") return;
      streamResult = null;
      streamTransportFailed = true;
    }

    if (token !== replayToken.value) return;

    if (!streamResult) {
      if (streamError && !streamTransportFailed) {
        errorMessage.value = streamError;
        return;
      }
      const runResult = await runWorkflow(runPayload);
      if (token !== replayToken.value) return;
      lastRun.value = runResult;
      selectedGraph.value = runResult.graph;
      if (runResult.conversation_id) {
        currentConversationId.value = runResult.conversation_id;
      }
      const finished = await replayTrace(runResult.trace || []);
      if (finished && token === replayToken.value) {
        const assistantMessage = {
          id: `assistant_${Date.now()}`,
          role: "assistant",
          agentName: runResult.artifacts?.route_agent_name || t("chat.assistant"),
          content: runResult.assistant_message,
        };
        chatMessages.value = [...chatMessages.value, assistantMessage];
      }
      return;
    }

    if (streamError) {
      errorMessage.value = streamError;
    }

    lastRun.value = streamResult;
    selectedGraph.value = streamResult.graph;
    displayedTrace.value = streamResult.trace || displayedTrace.value;
    if (streamResult.conversation_id) {
      currentConversationId.value = streamResult.conversation_id;
    }
    const assistantMessage = {
      id: `assistant_${Date.now()}`,
      role: "assistant",
      agentName: streamResult.artifacts?.route_agent_name || t("chat.assistant"),
      content: streamResult.assistant_message,
    };
    chatMessages.value = [...chatMessages.value, assistantMessage];
  } catch (error) {
    if (token === replayToken.value) {
      errorMessage.value = String(error.message || error);
    }
  } finally {
    if (activeRunController.value === controller) {
      activeRunController.value = null;
    }
    if (token === replayToken.value) {
      replayingTrace.value = false;
    }
    loading.value = false;
  }
}

function handleClearRun() {
  if (activeRunController.value) {
    activeRunController.value.abort();
    activeRunController.value = null;
  }
  lastRun.value = null;
  chatMessages.value = [];
  currentConversationId.value = "";
  displayedTrace.value = [];
  replayNodeId.value = "";
  replayingTrace.value = false;
  replayToken.value += 1;
}

watch(selectedWorkflowId, async (workflowId) => {
  if (activeRunController.value) {
    activeRunController.value.abort();
    activeRunController.value = null;
  }
  chatMessages.value = [];
  currentConversationId.value = "";
  lastRun.value = null;
  displayedTrace.value = [];
  replayNodeId.value = "";
  replayingTrace.value = false;
  replayToken.value += 1;
  await loadGraph(workflowId);
});

onMounted(async () => {
  try {
    await loadInitialData();
    await loadGraph(selectedWorkflowId.value);
  } catch (error) {
    errorMessage.value = String(error.message || error);
  }
});
</script>

<template>
  <div class="app-frame" :class="{ 'playground-mode': currentPage === 'playground' }">
    <header class="topbar">
      <div class="shell topbar-inner">
        <div class="brand">
          <div class="brand-mark">
            <BrainCircuit :size="22" />
          </div>
          <div>
            <h1>Agent Playground</h1>
            <p>{{ t("brand.subtitle") }}</p>
          </div>
        </div>

        <nav class="topnav">
          <button
            v-for="item in navItems"
            :key="item.id"
            class="topnav-item"
            :class="{ active: currentPage === item.id }"
            @click="currentPage = item.id"
          >
            <component :is="item.icon" :size="16" />
            <span>{{ item.label }}</span>
          </button>
        </nav>

        <div class="topbar-right">
          <div class="lang-switch">
            <button
              class="lang-button"
              :class="{ active: locale === 'zh-CN' }"
              @click="setLocale('zh-CN')"
            >
              {{ t("lang.zh") }}
            </button>
            <button
              class="lang-button"
              :class="{ active: locale === 'en-US' }"
              @click="setLocale('en-US')"
            >
              {{ t("lang.en") }}
            </button>
          </div>
          <div class="topbar-status">
            <span class="chip chip-dark">MVP</span>
            <Activity :size="14" class="status-icon" />
            <span>{{ t("status.ready") }}</span>
          </div>
        </div>
      </div>
    </header>

    <main class="shell page-shell">
      <div v-if="errorMessage" class="error-banner">
        {{ errorMessage }}
      </div>

      <Transition name="page-fade" mode="out-in">
        <div :key="currentPage" class="page-stage" :class="{ 'playground-stage': currentPage === 'playground' }">
          <OverviewPage
            v-if="currentPage === 'overview'"
            :agents="agents"
            :workflows="workflows"
            :templates="templates"
            @navigate="currentPage = $event"
          />

          <AgentsPage
            v-else-if="currentPage === 'agents'"
            :agents="agents"
            :skills="skills"
            :skill-sync-status="skillSyncStatus"
            @create="handleCreateAgent"
            @update="handleUpdateAgent"
            @delete="handleDeleteAgent"
            @quick-chat="handleQuickChatAgent"
          />

          <WorkflowsPage
            v-else-if="currentPage === 'workflows'"
            :templates="templates"
            :agents="agents"
            :workflows="workflows"
            :selected-workflow-id="selectedWorkflowId"
            @create="handleCreateWorkflow"
            @update="handleUpdateWorkflow"
            @delete="handleDeleteWorkflow"
            @select="selectedWorkflowId = $event"
          />

          <PlaygroundPage
            v-else-if="currentPage === 'playground'"
            :workflows="workflows"
            :agents="agents"
            :selected-workflow-id="selectedWorkflowId"
            :selected-workflow="selectedWorkflow"
            :selected-graph="selectedGraph"
            :active-node-id="activeNodeId"
            :loading="loading"
            :trace="traceForView"
            :trace-playing="replayingTrace"
            :chat-messages="chatMessages"
            @run="handleRun"
            @clear="handleClearRun"
            @select-workflow="selectedWorkflowId = $event"
          />

          <SettingsPage
            v-else
            :settings="appSettings"
            :saving="savingSettings"
            @save="handleSaveSettings"
          />
        </div>
      </Transition>
    </main>
  </div>
</template>
