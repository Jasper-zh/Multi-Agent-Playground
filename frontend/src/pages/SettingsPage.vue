<script setup>
import { computed, inject, reactive, ref, watch } from "vue";
import {
  CheckCircle2,
  ChevronDown,
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
    key: "",
    value: "",
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
      ? value.env_vars.map((entry) => ({ ...entry }))
      : [];
    if (!form.model_profiles.some((profile) => profile.id === editingProfileId.value)) {
      editingProfileId.value = "";
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
  if (form.model_profiles.length <= 1) return;
  form.model_profiles = form.model_profiles.filter((profile) => profile.id !== profileId);
  if (form.active_model_profile_id === profileId) {
    form.active_model_profile_id = form.model_profiles[0]?.id || "";
  }
  if (editingProfileId.value === profileId) {
    editingProfileId.value = "";
  }
}

function toggleEditProfile(profileId) {
  editingProfileId.value = editingProfileId.value === profileId ? "" : profileId;
}

function addEnvVar() {
  form.env_vars = [...form.env_vars, makeEnvVar()];
}

function removeEnvVar(index) {
  form.env_vars = form.env_vars.filter((_, itemIndex) => itemIndex !== index);
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

function submit() {
  emit("save", buildPayload());
}

function activateAndSave(profileId) {
  if (props.saving) return;
  form.active_model_profile_id = profileId;
  emit("save", buildPayload());
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
          </div>

        </article>
      </div>

      <div class="settings-actions">
        <button type="button" class="primary-button" :disabled="saving" @click="submit">
          <Save :size="16" />
          {{ saving ? t("settings.saving") : t("settings.save") }}
        </button>
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

      <div v-if="form.env_vars.length" class="settings-env-list">
        <div
          v-for="(entry, index) in form.env_vars"
          :key="`env_${index}`"
          class="settings-env-row"
        >
          <input v-model="entry.key" :placeholder="t('settings.envKeyPlaceholder')" />
          <input v-model="entry.value" :placeholder="t('settings.envValuePlaceholder')" />
          <button type="button" class="agent-modal-remove-skill" @click="removeEnvVar(index)">
            <Trash2 :size="14" />
          </button>
        </div>
      </div>
      <div v-else class="agent-empty-meta">{{ t("settings.envEmpty") }}</div>

      <div class="settings-footnote">
        <strong>{{ t("settings.storageLabel") }}</strong>
        <span>{{ props.settings?.env_path || "-" }}</span>
      </div>
    </section>
  </section>
</template>
