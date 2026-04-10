<script setup>
import AgentManager from "../components/AgentManager.vue";

defineProps({
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

defineEmits(["create", "update", "delete", "quick-chat"]);
</script>

<template>
  <AgentManager
    :agents="agents"
    :skills="skills"
    :skill-sync-status="skillSyncStatus"
    @create="$emit('create', $event)"
    @update="$emit('update', $event)"
    @delete="$emit('delete', $event)"
    @quick-chat="$emit('quick-chat', $event)"
  />
</template>
