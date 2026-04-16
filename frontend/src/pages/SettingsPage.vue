<script setup>
import { computed, inject, reactive, ref, watch } from "vue";
import {
  CheckCircle2,
  ChevronUp,
  Copy,
  GripVertical,
  KeyRound,
  Link2,
  Pencil,
  Play,
  Plus,
  Save,
  Settings2,
  Trash2,
  X,
} from "lucide-vue-next";
import { I18N_KEY } from "../i18n";

const props = defineProps({
  settings: {
    type: Object,
    default: null,
  },
  saving: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["save"]);
const i18n = inject(I18N_KEY, null);
const t = i18n?.t || ((key) => key);

const form = reactive({
  model_profiles: [],
  active_model_profile_id: "",
  env_vars: [],
});
const editingProfileId = ref("");
const editingEnvId = ref("");
const providerPresets = [
  {
    id: "moonshot",
    label: "月之暗面",
    name: "Kimi",
    base_url: "https://api.moonshot.cn/v1",
    model: "moonshot-v1-8k",
  },
  {
    id: "zhipu",
    label: "智谱",
    name: "Zhipu",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-4.5",
  },
  {
    id: "minimax",
    label: "MiniMax",
    name: "MiniMax",
    base_url: "https://api.minimaxi.com/v1",
    model: "MiniMax-M2.7",
  },
  {
    id: "custom",
    label: "其他",
    name: "",
    base_url: "https://api.openai.com/v1",
    model: "",
  },
];

function makeProfile(index = 0) {
  return {
    id: `profile_${Date.now()}_${index}`,
    provider: "custom",
    name: index === 0 ? "Default" : `Profile ${index + 1}`,
    api_key: "",
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
  };
}

function makeEnvVar() {
  return {
    id: `env_draft_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    key: "",
    value: "",
    isDraft: true,
  };
}

watch(
  () => props.settings,
  (value) => {
    const profiles = Array.isArray(value?.model_profiles) ? value.model_profiles : [];
    form.model_profiles = profiles.length
      ? profiles.map((profile) => ({ provider: profile.provider || "custom", ...profile }))
      : [makeProfile(0)];
    form.active_model_profile_id =
      value?.active_model_profile_id || form.model_profiles[0]?.id || "";
    form.env_vars = Array.isArray(value?.env_vars)
      ? value.env_vars.map((entry, index) => ({
          id: `env_saved_${index}`,
          key: String(entry.key || ""),
          value: String(entry.value || ""),
          isDraft: false,
        }))
      : [];
    if (!form.model_profiles.some((profile) => profile.id === editingProfileId.value)) {
      editingProfileId.value = "";
    }
    if (!form.env_vars.some((entry) => entry.id === editingEnvId.value)) {
      editingEnvId.value = "";
    }
  },
  { immediate: true },
);

const hasProfiles = computed(() => form.model_profiles.length > 0);

function addProfile() {
  const next = makeProfile(form.model_profiles.length);
  form.model_profiles = [...form.model_profiles, next];
  if (!form.active_model_profile_id) {
    form.active_model_profile_id = next.id;
  }
  editingProfileId.value = next.id;
}

function duplicateProfile(profile) {
  const duplicated = {
    ...profile,
    id: `profile_${Date.now()}_${form.model_profiles.length}`,
    name: `${profile.name} Copy`,
  };
  form.model_profiles = [...form.model_profiles, duplicated];
}

function applyProviderPreset(profile, presetId) {
  const preset = providerPresets.find((item) => item.id === presetId);
  if (!preset) return;
  profile.provider = preset.id;
  if (preset.name) {
    profile.name = preset.name;
  }
  profile.base_url = preset.base_url;
  profile.model = preset.model;
}

function removeProfile(profileId) {
  if (props.saving) return;
  if (form.model_profiles.length <= 1) return;
  form.model_profiles = form.model_profiles.filter((profile) => profile.id !== profileId);
  if (form.active_model_profile_id === profileId) {
    form.active_model_profile_id = form.model_profiles[0]?.id || "";
  }
  if (editingProfileId.value === profileId) {
    editingProfileId.value = "";
  }
  submitModelProfiles();
}

function toggleEditProfile(profileId) {
  editingProfileId.value = editingProfileId.value === profileId ? "" : profileId;
}

function addEnvVar() {
  const entry = makeEnvVar();
  form.env_vars = [...form.env_vars, entry];
  editingEnvId.value = entry.id;
}

function removeEnvVar(index) {
  if (props.saving) return;
  const removed = form.env_vars[index];
  form.env_vars = form.env_vars.filter((_, itemIndex) => itemIndex !== index);
  if (removed?.id === editingEnvId.value) {
    editingEnvId.value = "";
  }
  submitEnvVars();
}

function profileInitial(name) {
  const raw = String(name || "").trim();
  return raw ? raw.slice(0, 1).toUpperCase() : "M";
}

function buildPayload() {
  return {
    model_profiles: form.model_profiles.map((profile) => ({ ...profile })),
    active_model_profile_id: form.active_model_profile_id || form.model_profiles[0]?.id || null,
    env_vars: form.env_vars
      .map((entry) => ({
        key: String(entry.key || "").trim(),
        value: String(entry.value || ""),
      }))
      .filter((entry) => entry.key),
  };
}

function buildSavedEnvVars() {
  return Array.isArray(props.settings?.env_vars)
    ? props.settings.env_vars.map((entry) => ({
        key: String(entry.key || "").trim(),
        value: String(entry.value || ""),
      }))
    : [];
}

function buildSavedProfiles() {
  return Array.isArray(props.settings?.model_profiles)
    ? props.settings.model_profiles.map((profile) => ({ ...profile }))
    : [];
}

function submitModelProfiles() {
  if (props.saving) return;
  emit("save", {
    ...buildPayload(),
    env_vars: buildSavedEnvVars(),
  });
}

function submitEnvVars() {
  if (props.saving) return;
  emit("save", {
    model_profiles: buildSavedProfiles(),
    active_model_profile_id:
      props.settings?.active_model_profile_id || props.settings?.model_profiles?.[0]?.id || null,
    env_vars: buildPayload().env_vars,
  });
}

function saveEnvVarOnEnter(event) {
  if (event.isComposing) return;
  if (props.saving) return;
  const envId = String(event?.target?.dataset?.envId || "").trim();
  if (envId) {
    submitEnvEntry(envId);
  }
}

function activateAndSave(profileId) {
  if (props.saving) return;
  form.active_model_profile_id = profileId;
  submitModelProfiles();
}

function editEnvVar(envId) {
  editingEnvId.value = envId;
}

function cancelEnvEdit(envId) {
  const index = form.env_vars.findIndex((entry) => entry.id === envId);
  if (index < 0) return;
  const entry = form.env_vars[index];
  if (entry.isDraft) {
    form.env_vars = form.env_vars.filter((item) => item.id !== envId);
  } else {
    const savedEntries = Array.isArray(props.settings?.env_vars) ? props.settings.env_vars : [];
    const savedIndex = Number(String(envId).replace("env_saved_", ""));
    const savedEntry = savedEntries[savedIndex] || { key: "", value: "" };
    form.env_vars[index] = {
      id: envId,
      key: String(savedEntry.key || ""),
      value: String(savedEntry.value || ""),
      isDraft: false,
    };
  }
  if (editingEnvId.value === envId) {
    editingEnvId.value = "";
  }
}

function submitEnvEntry(envId) {
  const entry = form.env_vars.find((item) => item.id === envId);
  if (!entry || !String(entry.key || "").trim()) return;
  editingEnvId.value = "";
  submitEnvVars();
}

function envValuePreview(value) {
  const raw = String(value || "");
  if (!raw) return t("settings.envNoValue");
  if (raw.length <= 4) return "••••";
  return `••••${raw.slice(-4)}`;
}
</script>

<template>
  <section class="page-stack settings-page">
    <div class="manager-topbar">
      <div>
        <h2>{{ t("settings.title") }}</h2>
        <p>{{ t("settings.desc") }}</p>
      </div>
    </div>

    <section class="glass-panel section-card settings-card">
      <div class="section-header">
        <div>
          <h3>
            <Settings2 :size="18" class="text-slate-400" />
            {{ t("settings.modelProfiles") }}
          </h3>
          <p>{{ t("settings.modelProfilesDesc") }}</p>
        </div>
        <button type="button" class="text-button" @click="addProfile">
          <Plus :size="14" />
          {{ t("settings.addProfile") }}
        </button>
      </div>

      <div v-if="hasProfiles" class="settings-profile-list compact">
        <article
          v-for="profile in form.model_profiles"
          :key="profile.id"
          class="settings-profile-row"
          :class="{
            active: form.active_model_profile_id === profile.id,
            editing: editingProfileId === profile.id,
          }"
        >
          <div class="settings-profile-row-main">
            <div class="settings-profile-reorder">
              <GripVertical :size="18" />
            </div>

            <div class="settings-profile-avatar">
              {{ profileInitial(profile.name) }}
            </div>

            <div class="settings-profile-copy">
              <div class="settings-profile-name-text">{{ profile.name }}</div>
              <div class="settings-profile-url-text">{{ profile.base_url }}</div>
            </div>

            <div class="settings-profile-row-actions">
              <button
                type="button"
                class="settings-profile-activate inline"
                :class="{ active: form.active_model_profile_id === profile.id }"
                @click="activateAndSave(profile.id)"
              >
                <Play v-if="form.active_model_profile_id !== profile.id" :size="14" />
                <CheckCircle2 v-else :size="14" />
                {{ form.active_model_profile_id === profile.id ? t("settings.active") : t("settings.setActive") }}
              </button>

              <button
                type="button"
                class="settings-profile-icon-action"
                @click="toggleEditProfile(profile.id)"
              >
                <Pencil :size="16" />
              </button>

              <button
                type="button"
                class="settings-profile-icon-action"
                @click="duplicateProfile(profile)"
              >
                <Copy :size="16" />
              </button>

              <button
                type="button"
                class="settings-profile-icon-action"
                :disabled="form.model_profiles.length <= 1"
                @click="removeProfile(profile.id)"
              >
                <Trash2 :size="16" />
              </button>
            </div>
          </div>

          <div v-if="editingProfileId === profile.id" class="settings-profile-editor">
            <div class="settings-profile-editor-head">
              <strong>{{ t("settings.editProfile") }}</strong>
              <button type="button" class="settings-profile-collapse" @click="toggleEditProfile(profile.id)">
                <ChevronUp :size="16" />
              </button>
            </div>

            <div class="settings-form-grid">
              <div class="settings-provider-picker">
                <span class="settings-provider-label">{{ t("settings.providerPreset") }}</span>
                <div class="settings-provider-list">
                  <button
                    v-for="preset in providerPresets"
                    :key="preset.id"
                    type="button"
                    class="settings-provider-pill"
                    :class="{ active: profile.provider === preset.id }"
                    @click="applyProviderPreset(profile, preset.id)"
                  >
                    {{ preset.label }}
                  </button>
                </div>
              </div>

              <label class="settings-field">
                <span>{{ t("settings.profileName") }}</span>
                <input
                  v-model="profile.name"
                  type="text"
                  :placeholder="t('settings.profileNamePlaceholder')"
                />
              </label>

              <label class="settings-field">
                <span><KeyRound :size="14" /> {{ t("settings.apiKey") }}</span>
                <input
                  v-model="profile.api_key"
                  type="password"
                  autocomplete="off"
                  :placeholder="t('settings.apiKeyPlaceholder')"
                />
              </label>

              <label class="settings-field">
                <span><Link2 :size="14" /> {{ t("settings.baseUrl") }}</span>
                <input
                  v-model="profile.base_url"
                  type="text"
                  :placeholder="t('settings.baseUrlPlaceholder')"
                />
              </label>

              <label class="settings-field">
                <span>{{ t("settings.model") }}</span>
                <input
                  v-model="profile.model"
                  type="text"
                  :placeholder="t('settings.modelPlaceholder')"
                />
              </label>
            </div>

            <div class="settings-profile-editor-actions">
              <button
                type="button"
                class="primary-button"
                :disabled="saving"
                @click="submitModelProfiles"
              >
                <Save :size="16" />
                {{ saving ? t("settings.saving") : t("settings.saveProfile") }}
              </button>
              <button
                type="button"
                class="ghost-button"
                :disabled="saving"
                @click="toggleEditProfile(profile.id)"
              >
                {{ t("settings.closeEditor") }}
              </button>
            </div>
          </div>

        </article>
      </div>
    </section>

    <section class="glass-panel section-card settings-card">
      <div class="section-header">
        <div>
          <h3>
            <KeyRound :size="18" class="text-slate-400" />
            {{ t("settings.envVars") }}
          </h3>
          <p>{{ t("settings.envVarsDesc") }}</p>
        </div>
        <button type="button" class="text-button" @click="addEnvVar">
          <Plus :size="14" />
          {{ t("settings.addEnvVar") }}
        </button>
      </div>

      <div class="settings-inline-hint">
        {{ t("settings.envVarsHint") }}
      </div>

      <div v-if="form.env_vars.length" class="settings-env-list">
        <article
          v-for="(entry, index) in form.env_vars"
          :key="entry.id"
          class="settings-env-item"
          :class="{ editing: editingEnvId === entry.id }"
        >
          <template v-if="editingEnvId === entry.id">
            <div class="settings-env-editor">
              <div class="settings-env-row">
                <input
                  v-model="entry.key"
                  :data-env-id="entry.id"
                  :placeholder="t('settings.envKeyPlaceholder')"
                  @keydown.enter.prevent="saveEnvVarOnEnter"
                />
                <input
                  v-model="entry.value"
                  :data-env-id="entry.id"
                  :placeholder="t('settings.envValuePlaceholder')"
                  @keydown.enter.prevent="saveEnvVarOnEnter"
                />
              </div>
              <div class="settings-env-editor-actions">
                <button
                  type="button"
                  class="settings-env-save"
                  :disabled="saving || !String(entry.key || '').trim()"
                  @click="submitEnvEntry(entry.id)"
                >
                  <Save :size="14" />
                  {{ saving ? t("settings.saving") : t("settings.saveEnvVar") }}
                </button>
                <button
                  type="button"
                  class="ghost-button"
                  :disabled="saving"
                  @click="cancelEnvEdit(entry.id)"
                >
                  <X :size="14" />
                  {{ t("settings.closeEditor") }}
                </button>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="settings-env-summary">
              <div class="settings-env-copy">
                <div class="settings-env-key">{{ entry.key }}</div>
                <div class="settings-env-value">{{ envValuePreview(entry.value) }}</div>
              </div>
              <div class="settings-env-actions">
                <button type="button" class="settings-profile-icon-action" @click="editEnvVar(entry.id)">
                  <Pencil :size="16" />
                </button>
                <button type="button" class="settings-profile-icon-action" @click="removeEnvVar(index)">
                  <Trash2 :size="16" />
                </button>
              </div>
            </div>
          </template>
        </article>
      </div>
      <div v-else class="agent-empty-meta">{{ t("settings.envEmpty") }}</div>

      <div class="settings-footnote">
        <strong>{{ t("settings.storageLabel") }}</strong>
        <span>{{ props.settings?.env_path || "-" }}</span>
      </div>
    </section>
  </section>
</template>
