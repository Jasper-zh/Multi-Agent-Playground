<script setup>
import { inject, reactive, watch } from "vue";
import { KeyRound, Link2, Save, Settings2 } from "lucide-vue-next";
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
  openai_api_key: "",
  openai_base_url: "https://api.openai.com/v1",
  openai_model: "gpt-4o-mini",
});

watch(
  () => props.settings,
  (value) => {
    form.openai_api_key = value?.openai_api_key || "";
    form.openai_base_url = value?.openai_base_url || "https://api.openai.com/v1";
    form.openai_model = value?.openai_model || "gpt-4o-mini";
  },
  { immediate: true },
);

function submit() {
  emit("save", {
    openai_api_key: form.openai_api_key,
    openai_base_url: form.openai_base_url,
    openai_model: form.openai_model,
  });
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
            {{ t("settings.modelSection") }}
          </h3>
          <p>{{ t("settings.modelSectionDesc") }}</p>
        </div>
      </div>

      <div class="settings-form-grid">
        <label class="settings-field">
          <span><KeyRound :size="14" /> {{ t("settings.apiKey") }}</span>
          <input
            v-model="form.openai_api_key"
            type="password"
            autocomplete="off"
            :placeholder="t('settings.apiKeyPlaceholder')"
          />
        </label>

        <label class="settings-field">
          <span><Link2 :size="14" /> {{ t("settings.baseUrl") }}</span>
          <input
            v-model="form.openai_base_url"
            type="text"
            :placeholder="t('settings.baseUrlPlaceholder')"
          />
        </label>

        <label class="settings-field">
          <span>{{ t("settings.model") }}</span>
          <input
            v-model="form.openai_model"
            type="text"
            :placeholder="t('settings.modelPlaceholder')"
          />
        </label>
      </div>

      <div class="settings-footnote">
        <strong>{{ t("settings.storageLabel") }}</strong>
        <span>{{ props.settings?.env_path || "-" }}</span>
      </div>

      <div class="settings-actions">
        <button type="button" class="primary-button" :disabled="saving" @click="submit">
          <Save :size="16" />
          {{ saving ? t("settings.saving") : t("settings.save") }}
        </button>
      </div>
    </section>
  </section>
</template>
