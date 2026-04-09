<script setup>
import {
  Bot,
  CheckCircle2,
  Code,
  Database,
  FileText,
  FolderOpen,
  FolderSearch,
  Globe,
  Image as ImageIcon,
  Library,
  Mail,
  MessageCircle,
  PencilLine,
  Plus,
  Settings2,
  ShoppingBag,
  Trash2,
  X,
  Zap,
} from "lucide-vue-next";
import { computed, inject, reactive, ref } from "vue";
import { installSkill as installSkillPackage } from "../api";
import { I18N_KEY } from "../i18n";

const props = defineProps({
  agents: {
    type: Array,
    required: true,
  },
  skills: {
    type: Array,
    required: true,
  },
  skillSyncStatus: {
    type: String,
    default: "",
  },
});

const emit = defineEmits(["create", "update", "quick-chat"]);
const i18n = inject(I18N_KEY, null);
const t = i18n?.t || ((key) => key);

const isAdding = ref(false);
const isSavingEdit = ref(false);
const isSavingStore = ref(false);

const addForm = reactive({
  name: "",
  role: "",
  details: "",
});

const editingAgent = ref(null);
const isStoreOpen = ref(false);
const storeAgentId = ref("");

const themeTokens = [
  "agent-theme-blue",
  "agent-theme-violet",
  "agent-theme-green",
  "agent-theme-orange",
  "agent-theme-rose",
  "agent-theme-indigo",
];
const maxVisibleMetaItems = 4;

const skillLibrary = computed(() =>
  props.skills.map((skill) => ({
    ...skill,
    icon: resolveSkillIcon(skill),
  })),
);

const skillMap = computed(() => {
  const map = new Map();
  skillLibrary.value.forEach((skill) => map.set(skill.id, skill));
  return map;
});

const decoratedAgents = computed(() =>
  props.agents.map((agent, index) => {
    const roleLabel = resolveRoleLabel(agent.name, agent.description);
    const boundSkills = (agent.skill_ids || [])
      .map((skillId) => {
        const skill = skillMap.value.get(skillId);
        return {
          id: skillId,
          name: skill?.name || "Unknown Skill",
          icon: skill?.icon || Zap,
        };
      })
      .filter((item) => item.name);
    const builtinCapabilities = builtinCapabilityOptions.filter((option) =>
      (agent.builtin_capabilities || []).includes(option.id),
    );
    return {
      ...agent,
      roleLabel,
      boundSkills: boundSkills.slice(0, maxVisibleMetaItems),
      builtinCapabilities: builtinCapabilities.slice(0, maxVisibleMetaItems),
      theme: themeTokens[index % themeTokens.length],
    };
  }),
);

const storeAgent = computed(
  () => props.agents.find((agent) => agent.id === storeAgentId.value) || null,
);

const builtinCapabilityOptions = [
  {
    id: "fs_list",
    label: "List Directories",
    description: "Allow listing local files and folders.",
    icon: FolderSearch,
  },
  {
    id: "fs_read",
    label: "Read Files",
    description: "Allow reading local text files.",
    icon: FileText,
  },
  {
    id: "fs_write",
    label: "Write Files",
    description: "Allow creating or overwriting local text files.",
    icon: PencilLine,
  },
];

function resolveRoleLabel(name, description) {
  const normalized = String(name || "").trim().toLowerCase();
  const nameMap = {
    "architecture coach": "System Architect",
    "documentation writer": "Technical Writer",
    "learning coach": "Education Specialist",
  };
  if (nameMap[normalized]) return nameMap[normalized];

  const shorten = (text) => {
    const rawText = String(text || "").trim();
    if (!rawText) return "";
    const maxLength = 18;
    if (rawText.length <= maxLength) return rawText;
    return `${rawText.slice(0, maxLength - 3).trim()}...`;
  };

  const raw = String(description || "").trim();
  if (!raw) return "Specialist";
  if (raw.length <= 18) return raw;
  const head = raw.split(/[,.，。；;]/)[0].trim();
  if (head) return shorten(head);
  return shorten(raw);
}

function previewPrompt(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return raw.replace(/\s+/g, " ");
}

function resolveSkillIcon(skill) {
  const text = `${skill.name || ""} ${skill.description || ""} ${skill.instruction || ""}`.toLowerCase();
  if (text.includes("search") || text.includes("web") || text.includes("internet")) return Globe;
  if (text.includes("code") || text.includes("python")) return Code;
  if (text.includes("image") || text.includes("vision")) return ImageIcon;
  if (text.includes("file") || text.includes("document")) return FileText;
  if (text.includes("database") || text.includes("sql")) return Database;
  if (text.includes("mail") || text.includes("email")) return Mail;
  return Zap;
}

function getSkillPreflight(skill) {
  const runtime = skill?.runtime_preflight;
  if (!runtime || typeof runtime !== "object") return null;
  return runtime;
}

function getSkillRuntimeLabel(skill) {
  const runtime = getSkillPreflight(skill);
  if (!runtime) return "Runtime Unknown";
  const status = String(runtime.status || "");
  if (status === "ready") return "Runtime Ready";
  if (status === "prompt_only") return "Prompt Only";
  if (status === "missing_environment") return "Missing Env";
  if (status === "missing_shell_dependencies") return "Missing Deps";
  if (status === "missing_launcher") return "Missing Launcher";
  if (status === "missing_command_target") return "Missing Script";
  if (status === "invalid_local_path") return "Invalid Path";
  if (status === "invalid_tool") return "Invalid Tool";
  return "Runtime Blocked";
}

function getSkillRuntimeClass(skill) {
  const runtime = getSkillPreflight(skill);
  if (!runtime) return "unknown";
  if (runtime.ready) return "ready";
  return "blocked";
}

function getSkillRuntimeDetail(skill) {
  const runtime = getSkillPreflight(skill);
  if (!runtime) return "";
  const missingLaunchers = Array.isArray(runtime.missing_launchers) ? runtime.missing_launchers : [];
  if (missingLaunchers.length) {
    return `Launchers: ${missingLaunchers.join(", ")}`;
  }
  const missingDeps = Array.isArray(runtime.missing_shell_dependencies)
    ? runtime.missing_shell_dependencies
    : [];
  if (missingDeps.length) {
    return `Dependencies: ${missingDeps.join(", ")}`;
  }
  const missingEnv = Array.isArray(runtime.missing_env_vars) ? runtime.missing_env_vars : [];
  if (missingEnv.length) {
    return `Env: ${missingEnv.join(", ")}`;
  }
  if (runtime.node_prepare_required) {
    return "First run may install node dependencies";
  }
  if (runtime.python_prepare_required) {
    return "First run may install python dependencies";
  }
  return String(runtime.message || "");
}

function canCreate() {
  return Boolean(addForm.name.trim() && addForm.role.trim() && addForm.details.trim());
}

function resetCreateForm() {
  isAdding.value = false;
  addForm.name = "";
  addForm.role = "";
  addForm.details = "";
}

function beginCreate() {
  resetCreateForm();
  isAdding.value = true;
}

function beginEdit(agent) {
  isAdding.value = false;
  editingAgent.value = {
    id: agent.id,
    name: agent.name || "",
    role: agent.description || "",
    details: agent.system_prompt || "",
    model: agent.model ?? null,
    skill_ids: [...(agent.skill_ids || [])],
    builtin_capabilities: [...(agent.builtin_capabilities || [])],
  };
}

function closeEdit() {
  editingAgent.value = null;
}

function removeEditingSkill(skillId) {
  if (!editingAgent.value) return;
  editingAgent.value.skill_ids = editingAgent.value.skill_ids.filter((id) => id !== skillId);
}

function toggleBuiltinCapability(capabilityId) {
  if (!editingAgent.value) return;
  const current = new Set(editingAgent.value.builtin_capabilities || []);
  if (current.has(capabilityId)) {
    current.delete(capabilityId);
  } else {
    current.add(capabilityId);
  }
  editingAgent.value.builtin_capabilities = [...current];
}

function openSkillStore(agentId) {
  storeAgentId.value = agentId;
  isStoreOpen.value = true;
}

function startQuickChat(agent) {
  emit("quick-chat", agent);
}

function closeSkillStore() {
  isStoreOpen.value = false;
  storeAgentId.value = "";
}

async function submitCreate() {
  if (!canCreate()) return;
  await emit("create", {
    name: addForm.name,
    description: addForm.role,
    system_prompt: addForm.details,
    skill_ids: [],
    builtin_capabilities: [],
  });
  resetCreateForm();
}

async function submitEdit() {
  if (!editingAgent.value) return;
  if (!editingAgent.value.name.trim() || !editingAgent.value.role.trim() || !editingAgent.value.details.trim()) return;
  if (isSavingEdit.value) return;
  isSavingEdit.value = true;
  try {
    await emit("update", {
      id: editingAgent.value.id,
      data: {
        name: editingAgent.value.name,
        description: editingAgent.value.role,
        system_prompt: editingAgent.value.details,
        model: editingAgent.value.model,
        skill_ids: [...editingAgent.value.skill_ids],
        builtin_capabilities: [...editingAgent.value.builtin_capabilities],
      },
    });
    closeEdit();
  } finally {
    isSavingEdit.value = false;
  }
}

async function toggleStoreSkill(skillId) {
  if (!storeAgent.value || isSavingStore.value) return;
  const currentSkills = [...(storeAgent.value.skill_ids || [])];
  const installing = !currentSkills.includes(skillId);
  const nextSkills = currentSkills.includes(skillId)
    ? currentSkills.filter((id) => id !== skillId)
    : [...currentSkills, skillId];

  isSavingStore.value = true;
  try {
    if (installing) {
      await installSkillPackage(skillId);
    }
    await emit("update", {
      id: storeAgent.value.id,
      data: {
        name: storeAgent.value.name,
        description: storeAgent.value.description,
        system_prompt: storeAgent.value.system_prompt,
        model: storeAgent.value.model ?? null,
        skill_ids: nextSkills,
        builtin_capabilities: [...(storeAgent.value.builtin_capabilities || [])],
      },
    });
    if (editingAgent.value?.id === storeAgent.value.id) {
      editingAgent.value.skill_ids = [...nextSkills];
    }
  } finally {
    isSavingStore.value = false;
  }
}

function isSkillInstalled(skillId) {
  return Boolean(storeAgent.value?.skill_ids?.includes(skillId));
}

function getSkillDisplay(skillId) {
  const skill = skillMap.value.get(skillId);
  return {
    name: skill?.name || "Unknown Skill",
    icon: skill?.icon || Zap,
  };
}

</script>

<template>
  <section class="page-stack">
    <div class="manager-topbar">
      <div>
        <h2>Agents</h2>
        <p>{{ t("page.agentsDesc") }}</p>
      </div>
      <button class="primary-button" @click="beginCreate">
        <Plus :size="16" />
        {{ t("agent.new") }}
      </button>
    </div>

    <div class="agent-grid">
      <article v-if="isAdding" class="glass-panel add-card add-card-blue manager-add-card">
        <div class="page-stack compact-gap">
          <h4>Create Agent</h4>
          <input
            v-model="addForm.name"
            :placeholder="t('agent.name')"
          />
          <input
            v-model="addForm.role"
            :placeholder="t('agent.role')"
          />
          <textarea
            v-model="addForm.details"
            rows="5"
            :placeholder="t('agent.prompt')"
          />
          <div class="inline-actions">
            <button class="accent-button accent-button-blue" :disabled="!canCreate()" @click="submitCreate">
              {{ t("agent.save") }}
            </button>
            <button class="ghost-button" @click="resetCreateForm">
              {{ t("agent.cancel") }}
            </button>
          </div>
        </div>
      </article>

      <article
        v-for="agent in decoratedAgents"
        :key="agent.id"
        class="glass-panel agent-card manager-agent-card"
      >
        <div class="agent-card-shell">
          <div class="agent-card-top">
            <div class="agent-avatar agent-avatar-hero" :class="agent.theme">
              <Bot :size="26" />
            </div>
            <div class="agent-action-row">
              <button
                class="icon-button icon-button-store"
                type="button"
                title="Install Skills"
                @click.stop="openSkillStore(agent.id)"
              >
                <ShoppingBag :size="18" />
              </button>
              <button
                class="icon-button icon-button-chat"
                type="button"
                title="Chat with Agent"
                @click.stop="startQuickChat(agent)"
              >
                <MessageCircle :size="18" />
              </button>
              <button
                class="icon-button"
                type="button"
                title="Edit Agent"
                @click.stop="beginEdit(agent)"
              >
                <Settings2 :size="18" />
              </button>
            </div>
          </div>

          <div class="agent-card-title-block">
            <h4>{{ agent.name }}</h4>
            <p class="workflow-id">agent_{{ agent.id }}</p>
          </div>

          <div class="agent-card-role-wrap">
            <span class="chip role-chip agent-role-pill" :title="agent.description">{{ agent.roleLabel }}</span>
          </div>

          <p class="agent-summary agent-prompt-preview" :title="agent.description || ''">
            {{ previewPrompt(agent.description) || "-" }}
          </p>

          <div class="agent-card-sections">
            <div class="agent-installed-skills">
              <p class="agent-installed-skills-title agent-section-title-skills">
                <Zap :size="10" />
                <span>Installed Skills</span>
              </p>
              <div v-if="agent.boundSkills.length" class="agent-installed-skill-list">
                <div
                  v-for="skill in agent.boundSkills"
                  :key="`${agent.id}_${skill.id}`"
                  class="agent-installed-skill"
                >
                  <span class="agent-installed-skill-icon">
                    <component :is="skill.icon" :size="12" />
                  </span>
                  <span>{{ skill.name }}</span>
                </div>
              </div>
              <div v-else class="agent-empty-meta">No skills installed</div>
            </div>

            <div class="agent-installed-skills">
              <p class="agent-installed-skills-title agent-section-title-capabilities">
                <FolderOpen :size="10" />
                <span>Built-in Capabilities</span>
              </p>
              <div v-if="agent.builtinCapabilities.length" class="agent-installed-skill-list">
                <div
                  v-for="capability in agent.builtinCapabilities"
                  :key="`${agent.id}_${capability.id}`"
                  class="agent-installed-skill capability-pill"
                >
                  <span class="agent-installed-skill-icon">
                    <component :is="capability.icon || FolderOpen" :size="12" />
                  </span>
                  <span>{{ capability.label }}</span>
                </div>
              </div>
              <div v-else class="agent-empty-meta">No capabilities enabled</div>
            </div>
          </div>
        </div>
      </article>
    </div>

    <div v-if="editingAgent" class="agent-modal-overlay" @click="closeEdit">
      <section class="agent-modal-panel" @click.stop>
        <header class="agent-modal-header">
          <h3>Edit Agent</h3>
          <button type="button" class="agent-modal-close" @click="closeEdit">
            <X :size="20" />
          </button>
        </header>
        <div class="agent-modal-body">
          <div class="agent-modal-field">
            <label>Name</label>
            <input v-model="editingAgent.name" />
          </div>
          <div class="agent-modal-field">
            <label>Role</label>
            <input v-model="editingAgent.role" />
          </div>
          <div class="agent-modal-field">
            <label>System Prompt</label>
            <textarea v-model="editingAgent.details" rows="4"></textarea>
          </div>
          <div class="agent-modal-field">
            <label>Skills</label>
            <div v-if="editingAgent.skill_ids.length === 0" class="agent-modal-empty-skill">
              No skills installed.
            </div>
            <div v-else class="agent-modal-installed-list">
              <div
                v-for="skillId in editingAgent.skill_ids"
                :key="`edit_${skillId}`"
                class="agent-modal-installed-item"
              >
                <div class="agent-modal-installed-main">
                  <component :is="getSkillDisplay(skillId).icon" :size="16" />
                  <span>{{ getSkillDisplay(skillId).name }}</span>
                </div>
                <button type="button" class="agent-modal-remove-skill" @click="removeEditingSkill(skillId)">
                  <Trash2 :size="14" />
                </button>
              </div>
            </div>
          </div>
          <div class="agent-modal-field">
            <label>Built-in Capabilities</label>
            <div class="agent-modal-capability-list">
              <button
                v-for="capability in builtinCapabilityOptions"
                :key="capability.id"
                type="button"
                class="agent-modal-capability-item"
                :class="{ active: editingAgent.builtin_capabilities.includes(capability.id) }"
                @click="toggleBuiltinCapability(capability.id)"
              >
                <div class="agent-modal-capability-main">
                  <component :is="capability.icon" :size="16" />
                  <div>
                    <strong>{{ capability.label }}</strong>
                    <p>{{ capability.description }}</p>
                  </div>
                </div>
              </button>
            </div>
          </div>
        </div>
        <footer class="agent-modal-footer">
          <button type="button" class="agent-modal-cancel" @click="closeEdit">
            Cancel
          </button>
          <button type="button" class="agent-modal-save" :disabled="isSavingEdit" @click="submitEdit">
            {{ isSavingEdit ? "Saving..." : "Save" }}
          </button>
        </footer>
      </section>
    </div>

    <div v-if="isStoreOpen" class="skill-store-overlay" @click="closeSkillStore">
      <section class="skill-store-panel" @click.stop>
        <header class="skill-store-header">
          <div class="skill-store-brand">
            <div class="skill-store-brand-icon">
              <Library :size="20" />
            </div>
            <div>
              <h3>Local Skills</h3>
              <p>Workspace Skill Repository</p>
            </div>
          </div>
          <div class="skill-store-header-actions">
            <button type="button" class="skill-store-close" @click="closeSkillStore">
              <X :size="22" />
            </button>
          </div>
        </header>
        <div v-if="skillSyncStatus" class="skill-store-sync-note">
          {{ skillSyncStatus }}
        </div>

        <div class="skill-store-grid">
          <article
            v-for="skill in skillLibrary"
            :key="`store_${skill.id}`"
            class="skill-store-card"
            :class="{ installed: isSkillInstalled(skill.id) }"
          >
            <div class="skill-store-card-top">
              <div class="skill-store-icon" :class="{ installed: isSkillInstalled(skill.id) }">
                <component :is="skill.icon" :size="18" />
              </div>
              <span v-if="isSkillInstalled(skill.id)" class="skill-store-installed-badge">
                <CheckCircle2 :size="12" />
                Installed
              </span>
            </div>
            <h4>{{ skill.name }}</h4>
            <p>{{ skill.description }}</p>
            <div
              class="skill-runtime-chip"
              :class="getSkillRuntimeClass(skill)"
            >
              <strong>{{ getSkillRuntimeLabel(skill) }}</strong>
              <span>{{ getSkillRuntimeDetail(skill) }}</span>
            </div>
            <button
              type="button"
              class="skill-store-action"
              :class="{ uninstall: isSkillInstalled(skill.id) }"
              :disabled="isSavingStore"
              @click="toggleStoreSkill(skill.id)"
            >
              {{ isSkillInstalled(skill.id) ? "Uninstall" : "Install Skill" }}
            </button>
          </article>
        </div>

        <footer class="skill-store-footer">
          <button type="button" class="skill-store-done" @click="closeSkillStore">
            Done
          </button>
        </footer>
      </section>
    </div>
  </section>
</template>
