<script setup>
import { MessageSquare, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Send } from "lucide-vue-next";
import { inject, nextTick, onMounted, reactive, ref, watch } from "vue";
import { marked } from "marked";
import { I18N_KEY } from "../i18n";

const props = defineProps({
  selectedWorkflowId: {
    type: String,
    default: "",
  },
  selectedWorkflow: {
    type: Object,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  leftVisible: {
    type: Boolean,
    default: true,
  },
  rightVisible: {
    type: Boolean,
    default: true,
  },
  messages: {
    type: Array,
    default: () => [],
  },
});

const emit = defineEmits(["run", "clear", "toggle-left", "toggle-right"]);
const i18n = inject(I18N_KEY, null);
const t = i18n?.t || ((key) => key);

const form = reactive({
  user_input: "",
});
const inputRef = ref(null);
const scrollRef = ref(null);

marked.setOptions({
  breaks: true,
  gfm: true,
});

function resizeInput() {
  const el = inputRef.value;
  if (!el) return;
  el.style.height = "auto";
  const maxHeight = 180;
  const nextHeight = Math.min(el.scrollHeight, maxHeight);
  el.style.height = `${nextHeight}px`;
  el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
}

function handleInput() {
  nextTick(resizeInput);
}

function renderMarkdown(content) {
  return marked.parse(String(content || ""));
}

function scrollToBottom() {
  const el = scrollRef.value;
  if (!el) return;
  el.scrollTop = el.scrollHeight;
}

function handleKeydown(event) {
  if (event.isComposing) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
}

async function submit() {
  if (props.loading || !props.selectedWorkflowId || !form.user_input.trim()) return;
  const nextInput = form.user_input;
  form.user_input = "";
  await nextTick();
  resizeInput();
  await emit("run", {
    workflow_id: props.selectedWorkflowId,
    user_input: nextInput,
  });
}

onMounted(() => {
  resizeInput();
  nextTick(scrollToBottom);
});

watch(
  () => [props.messages.length, props.loading],
  () => {
    nextTick(scrollToBottom);
  },
);
</script>

<template>
  <section class="glass-panel chat-shell">
    <header class="run-panel-header">
      <div class="chat-head-main">
        <button class="chat-panel-button" type="button" @click="$emit('toggle-left')">
          <component :is="props.leftVisible ? PanelLeftClose : PanelLeftOpen" :size="15" />
        </button>
        <div class="chat-icon">
          <MessageSquare :size="16" />
        </div>
        <div>
          <h3 class="run-panel-title">{{ t("chat.title") }}</h3>
          <p class="chat-active-text">{{ t("chat.active") }}: {{ selectedWorkflow?.name || t("chat.noneSelected") }}</p>
        </div>
      </div>
      <div class="chat-header-actions">
        <button class="text-button text-xs" @click="$emit('clear')">
          {{ t("chat.clear") }}
        </button>
        <div class="chat-header-divider"></div>
        <button class="chat-panel-button" type="button" @click="$emit('toggle-right')">
          <component :is="props.rightVisible ? PanelRightClose : PanelRightOpen" :size="15" />
        </button>
      </div>
    </header>

    <div ref="scrollRef" class="chat-scroll">
      <div v-if="!messages.length && !loading" class="chat-empty-state">
        <div class="chat-empty-icon">
          <Send :size="26" />
        </div>
        <div>
          <h4>{{ t("chat.startRun") }}</h4>
          <p>{{ t("chat.startRunDesc") }}</p>
        </div>
      </div>

      <template v-else>
        <div
          v-for="message in messages"
          :key="message.id"
          class="chat-row"
          :class="{ user: message.role === 'user' }"
        >
          <div class="chat-row-inner">
            <span v-if="message.agentName" class="chat-agent-name">{{ message.agentName }}</span>
            <div class="chat-bubble markdown-body" :class="{ user: message.role === 'user' }">
              <div v-if="message.role === 'user'">{{ message.content }}</div>
              <div v-else v-html="renderMarkdown(message.content)"></div>
            </div>
          </div>
        </div>

        <div v-if="loading" class="chat-row">
          <div class="typing-indicator">
            <span></span>
            <span></span>
            <span></span>
            <strong>{{ t("chat.thinking") }}</strong>
          </div>
        </div>
      </template>
    </div>

    <footer class="chat-input-wrap">
      <div class="chat-input-shell">
        <textarea
          ref="inputRef"
          v-model="form.user_input"
          rows="1"
          :placeholder="t('chat.inputPlaceholder')"
          @input="handleInput"
          @keydown="handleKeydown"
        />
        <button class="send-mini-button" :disabled="!selectedWorkflowId || loading" @click="submit">
          <Send :size="14" />
        </button>
      </div>
    </footer>
  </section>
</template>
