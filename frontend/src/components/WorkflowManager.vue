<script setup>
import { CheckCircle2, Pencil, Plus, Trash2, Workflow as WorkflowIcon } from "lucide-vue-next";
import { computed, inject, reactive, ref } from "vue";
import { I18N_KEY } from "../i18n";

const props = defineProps({
  templates: {
    type: Array,
    required: true,
  },
  agents: {
    type: Array,
    required: true,
  },
  workflows: {
    type: Array,
    required: true,
  },
  selectedWorkflowId: {
    type: String,
    default: "",
  },
});

const emit = defineEmits(["create", "update", "delete", "select"]);
const i18n = inject(I18N_KEY, null);
const t = i18n?.t || ((key) => key);
const workflowTypeLabel = i18n?.workflowTypeLabel || ((type) => type);
const workflowTypeDesc = i18n?.workflowTypeDesc || ((_type, fallback) => fallback || _type);
const managedWorkflowTypes = [
  "router_specialists",
  "planner_executor",
  "supervisor_dynamic",
  "peer_handoff",
];

const isAdding = ref(false);
const editingWorkflowId = ref("");
const form = reactive({
  name: "",
  type: "router_specialists",
  description: "",
  specialist_agent_ids: [],
  finalizer_enabled: true,
  router_prompt: "You are an orchestration router. Select the best specialist based on user intent.",
});

const colorTokens = [
  "agent-theme-blue",
  "agent-theme-violet",
  "agent-theme-green",
  "agent-theme-orange",
  "agent-theme-rose",
  "agent-theme-indigo",
];

const requiredAgentCount = computed(() => {
  const found = props.templates.find((template) => template.type === form.type);
  return found?.required_agent_count || 2;
});

const workflowTypeOptions = computed(() =>
  props.templates.filter((template) => managedWorkflowTypes.includes(template.type)),
);

const visibleWorkflows = computed(() =>
  props.workflows.filter((workflow) => managedWorkflowTypes.includes(workflow.type)),
);

const canSubmit = computed(() =>
  Boolean(form.name.trim()) && form.specialist_agent_ids.length >= requiredAgentCount.value,
);

function toggleAgent(agentId) {
  if (form.specialist_agent_ids.includes(agentId)) {
    form.specialist_agent_ids = form.specialist_agent_ids.filter((id) => id !== agentId);
  } else {
    form.specialist_agent_ids = [...form.specialist_agent_ids, agentId];
  }
}

function resolveAgent(agentId) {
  const index = props.agents.findIndex((agent) => agent.id === agentId);
  const agent = props.agents.find((item) => item.id === agentId);
  return {
    name: agent?.name || agentId,
    theme: colorTokens[(index >= 0 ? index : 0) % colorTokens.length],
  };
}

function workflowDescription(workflow) {
  return workflowTypeDesc(workflow.type, "");
}

function beginCreate() {
  editingWorkflowId.value = "";
  isAdding.value = true;
  form.name = "";
  form.type = "router_specialists";
  form.description = "";
  form.specialist_agent_ids = [];
  form.finalizer_enabled = true;
  form.router_prompt = "You are an orchestration router. Select the best specialist based on user intent.";
}

function beginEdit(workflow) {
  if (!managedWorkflowTypes.includes(workflow.type)) return;
  isAdding.value = false;
  editingWorkflowId.value = workflow.id;
  form.name = workflow.name || "";
  form.type = workflow.type || "router_specialists";
  form.description = workflowDescription(workflow);
  form.specialist_agent_ids = [...(workflow.specialist_agent_ids || [])];
  form.finalizer_enabled = Boolean(workflow.finalizer_enabled);
  form.router_prompt = workflow.router_prompt || "You are an orchestration router. Select the best specialist based on user intent.";
}

function cancelForm() {
  isAdding.value = false;
  editingWorkflowId.value = "";
  form.name = "";
  form.type = "router_specialists";
  form.description = "";
  form.specialist_agent_ids = [];
  form.finalizer_enabled = true;
  form.router_prompt = "You are an orchestration router. Select the best specialist based on user intent.";
}

function removeWorkflow(workflowId) {
  emit("delete", workflowId);
  if (editingWorkflowId.value === workflowId) {
    cancelForm();
  }
}

async function submit() {
  if (!canSubmit.value) return;
  const data = {
    name: form.name,
    type: form.type,
    specialist_agent_ids: form.specialist_agent_ids,
    finalizer_enabled: form.finalizer_enabled,
    router_prompt: form.router_prompt,
  };
  if (editingWorkflowId.value) {
    await emit("update", {
      id: editingWorkflowId.value,
      data,
    });
  } else {
    await emit("create", data);
  }
  cancelForm();
}
</script>

<template>
  <section class="page-stack">
    <div class="manager-topbar">
      <div>
        <h2>Workflows</h2>
        <p>{{ t("page.workflowsDesc") }}</p>
      </div>
      <button class="primary-button" @click="beginCreate">
        <Plus :size="16" />
        {{ t("workflow.new") }}
      </button>
    </div>

    <div class="workflow-list">
      <article v-if="isAdding || editingWorkflowId" class="glass-panel add-card add-card-violet workflow-form">
        <div class="workflow-form-grid">
          <div class="page-stack compact-gap">
            <h4>{{ editingWorkflowId ? "Edit Workflow" : "Basic Config" }}</h4>
            <input v-model="form.name" :placeholder="t('workflow.name')" />
            <select v-model="form.type" class="workflow-native-select">
              <option
                v-for="template in workflowTypeOptions"
                :key="template.type"
                :value="template.type"
              >
                {{ workflowTypeLabel(template.type) }}
              </option>
            </select>
            <textarea
              v-model="form.description"
              rows="4"
              placeholder="Collaboration logic description..."
            />
            <label class="check-row">
              <input v-model="form.finalizer_enabled" type="checkbox" />
              <span>{{ t("workflow.enableFinalizer") }}</span>
            </label>
          </div>

          <div class="page-stack compact-gap">
            <h4>
              {{ t("workflow.bindAgents") }} ({{ form.specialist_agent_ids.length }})
            </h4>
            <div class="selection-list workflow-agent-selection">
              <label
                v-for="agent in props.agents"
                :key="agent.id"
                class="selection-item"
                :class="{ selected: form.specialist_agent_ids.includes(agent.id) }"
              >
                <div class="selection-main">
                  <span class="mini-dot" :class="resolveAgent(agent.id).theme"></span>
                  <span>{{ agent.name }}</span>
                </div>
                <input
                  type="checkbox"
                  :checked="form.specialist_agent_ids.includes(agent.id)"
                  @change="toggleAgent(agent.id)"
                />
                <CheckCircle2
                  v-if="form.specialist_agent_ids.includes(agent.id)"
                  :size="16"
                  class="workflow-agent-check"
                />
              </label>
            </div>
            <div class="inline-actions">
              <button class="accent-button accent-button-violet" :disabled="!canSubmit" @click="submit">
                {{ editingWorkflowId ? "Save Changes" : t("workflow.save") }}
              </button>
              <button class="ghost-button" @click="cancelForm">{{ t("workflow.cancel") }}</button>
            </div>
          </div>
        </div>
      </article>

      <article
        v-for="workflow in visibleWorkflows"
        :key="workflow.id"
        class="glass-panel workflow-card workflow-card-rich"
        :class="{ selected: workflow.id === props.selectedWorkflowId }"
        @click="emit('select', workflow.id)"
      >
        <div class="workflow-rich-head">
          <div class="workflow-rich-main">
            <div class="workflow-title-row">
              <h4>{{ workflow.name }}</h4>
              <span class="chip chip-dark">{{ workflowTypeLabel(workflow.type) }}</span>
            </div>
            <p class="workflow-id">workflow_{{ workflow.id }}</p>
            <p class="workflow-rich-desc">{{ workflowDescription(workflow) }}</p>

            <div class="workflow-agent-stack">
              <div class="avatar-stack">
                <div
                  v-for="(agentId, index) in workflow.specialist_agent_ids"
                  :key="agentId"
                  class="stack-avatar"
                  :class="resolveAgent(agentId).theme"
                  :style="{ zIndex: 10 - index }"
                >
                  {{ resolveAgent(agentId).name.charAt(0) }}
                </div>
              </div>
              <span class="workflow-agent-count">
                {{ workflow.specialist_agent_ids.length }} Agents involved
              </span>
            </div>
          </div>

          <div class="workflow-rich-mark" :class="{ selected: workflow.id === props.selectedWorkflowId }">
            <WorkflowIcon :size="24" />
          </div>
        </div>

        <div v-if="workflow.id === props.selectedWorkflowId" class="workflow-selected-foot">
          <span class="workflow-selected-note">
            <CheckCircle2 :size="12" />
            {{ t("workflow.selected") }}
          </span>
          <div class="inline-actions compact-workflow-actions">
            <button
              class="workflow-icon-action"
              type="button"
              title="Edit Workflow"
              @click.stop="beginEdit(workflow)"
            >
              <Pencil :size="14" />
            </button>
            <button
              class="workflow-icon-action workflow-icon-delete"
              type="button"
              title="Delete Workflow"
              @click.stop="removeWorkflow(workflow.id)"
            >
              <Trash2 :size="14" />
            </button>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>
