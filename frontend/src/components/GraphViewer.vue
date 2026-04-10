<script setup>
import { computed, inject, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { Bot, BrainCircuit, Network, Zap } from "lucide-vue-next";
import { I18N_KEY } from "../i18n";

const props = defineProps({
  graph: {
    type: Object,
    default: null,
  },
  activeNodeId: {
    type: String,
    default: "",
  },
  trace: {
    type: Array,
    default: () => [],
  },
});

const i18n = inject(I18N_KEY, null);
const t = i18n?.t || ((key) => key);

const canvasRef = ref(null);
const canvasSize = ref({ width: 0, height: 0 });
const hoveredNodeId = ref("");
const dragState = reactive({
  active: false,
  nodeId: "",
  startX: 0,
  startY: 0,
  offsetX: 0,
  offsetY: 0,
});
const nodeOffsets = ref({});

let resizeObserver = null;

function refreshCanvasSize() {
  if (!canvasRef.value) return;
  const rect = canvasRef.value.getBoundingClientRect();
  canvasSize.value = {
    width: rect.width,
    height: rect.height,
  };
}

function byId(nodes) {
  const map = new Map();
  nodes.forEach((node) => map.set(node.id, node));
  return map;
}

function placeNode(result, map, nodeId, x, y) {
  const node = map.get(nodeId);
  if (!node) return;
  result.push({ ...node, x, y });
}

function placeRow(result, nodes, y, from, to) {
  if (!nodes.length) return;
  nodes.forEach((node, index) => {
    const x = nodes.length === 1 ? (from + to) / 2 : from + ((to - from) * index) / (nodes.length - 1);
    result.push({ ...node, x, y });
  });
}

function layoutRouter(nodes, width, height) {
  const map = byId(nodes);
  const result = [];
  placeNode(result, map, "start", width * 0.5, height * 0.1);
  placeNode(result, map, "router", width * 0.5, height * 0.3);
  placeRow(
    result,
    nodes.filter((node) => node.kind === "agent"),
    height * 0.56,
    width * 0.16,
    width * 0.84,
  );
  placeNode(result, map, "finalize", width * 0.5, height * 0.8);
  placeNode(result, map, "end", width * 0.5, height * 0.92);
  return result;
}

function layoutPlanner(nodes, width, height) {
  const map = byId(nodes);
  const result = [];
  placeNode(result, map, "start", width * 0.5, height * 0.08);
  placeNode(result, map, "planner_core", width * 0.5, height * 0.24);
  placeNode(result, map, "planner_validator", width * 0.5, height * 0.4);
  placeNode(result, map, "task_dispatcher", width * 0.5, height * 0.56);
  placeRow(
    result,
    nodes.filter((node) => node.kind === "agent"),
    height * 0.74,
    width * 0.14,
    width * 0.86,
  );
  placeNode(result, map, "synthesizer", width * 0.5, height * 0.87);
  placeNode(result, map, "end", width * 0.5, height * 0.95);
  return result;
}

function layoutSupervisor(nodes, width, height) {
  const map = byId(nodes);
  const result = [];
  placeNode(result, map, "start", width * 0.5, height * 0.08);
  placeNode(result, map, "supervisor_intake", width * 0.5, height * 0.24);
  placeNode(result, map, "delegation_policy", width * 0.3, height * 0.42);
  placeNode(result, map, "supervisor_review", width * 0.7, height * 0.42);
  placeRow(
    result,
    nodes.filter((node) => node.kind === "agent"),
    height * 0.68,
    width * 0.14,
    width * 0.86,
  );
  placeNode(result, map, "finalize", width * 0.5, height * 0.86);
  placeNode(result, map, "end", width * 0.5, height * 0.95);
  return result;
}

function layoutFallback(nodes, width, height) {
  const startNode = nodes.find((node) => node.kind === "start");
  const endNode = nodes.find((node) => node.kind === "end");
  const finalNode = nodes.find((node) => node.kind === "final");
  const logicNodes = nodes.filter((node) => node.kind === "logic");
  const agentNodes = nodes.filter((node) => node.kind === "agent");
  const result = [];

  if (startNode) result.push({ ...startNode, x: width / 2, y: height * 0.1 });
  if (logicNodes.length) {
    logicNodes.forEach((node, index) => {
      const x = logicNodes.length === 1
        ? width / 2
        : width * (0.24 + (0.52 * index) / (logicNodes.length - 1));
      result.push({ ...node, x, y: height * 0.28 });
    });
  }
  if (agentNodes.length) {
    agentNodes.forEach((node, index) => {
      const x = agentNodes.length === 1
        ? width / 2
        : width * (0.14 + (0.72 * index) / (agentNodes.length - 1));
      result.push({ ...node, x, y: height * 0.56 });
    });
  }
  if (finalNode) result.push({ ...finalNode, x: width / 2, y: height * 0.79 });
  if (endNode) result.push({ ...endNode, x: width / 2, y: height * 0.92 });
  return result;
}

const baseNodes = computed(() => {
  if (!props.graph?.nodes?.length) return [];
  const width = canvasSize.value.width || 560;
  const height = canvasSize.value.height || 420;
  const nodes = props.graph.nodes;
  const nodeIds = new Set(nodes.map((node) => node.id));

  if (nodeIds.has("planner_core")) return layoutPlanner(nodes, width, height);
  if (nodeIds.has("supervisor_intake")) return layoutSupervisor(nodes, width, height);
  if (nodeIds.has("router")) return layoutRouter(nodes, width, height);
  return layoutFallback(nodes, width, height);
});

const graphNodes = computed(() =>
  baseNodes.value.map((node) => {
    const offset = nodeOffsets.value[node.id] || { x: 0, y: 0 };
    return {
      ...node,
      x: node.x + offset.x,
      y: node.y + offset.y,
    };
  }),
);

const nodeMap = computed(() => {
  const map = new Map();
  graphNodes.value.forEach((node) => map.set(node.id, node));
  return map;
});

const traversedEdgeKeys = computed(() => {
  const keys = new Set();
  let lastEnteredNode = "";
  props.trace.forEach((event) => {
    const from = event?.payload?.node_id || "";
    const to = event?.payload?.next_node_id || "";
    if (from && to) {
      keys.add(`${from}->${to}`);
    }

    if (event?.type === "node_entered" && from) {
      if (lastEnteredNode && lastEnteredNode !== from) {
        keys.add(`${lastEnteredNode}->${from}`);
      }
      lastEnteredNode = from;
    }
  });
  return keys;
});

function connectionPath(fromNode, toNode) {
  const radius = 26;
  const dx = toNode.x - fromNode.x;
  const dy = toNode.y - fromNode.y;
  const distance = Math.max(1, Math.hypot(dx, dy));
  const unitX = dx / distance;
  const unitY = dy / distance;

  const startX = fromNode.x + unitX * radius;
  const startY = fromNode.y + unitY * radius;
  const endX = toNode.x - unitX * radius;
  const endY = toNode.y - unitY * radius;

  const verticalCurve = Math.abs(dy) >= Math.abs(dx)
    ? Math.max(24, Math.abs(dy) * 0.4)
    : 0;
  const horizontalCurve = Math.abs(dx) > Math.abs(dy)
    ? Math.max(24, Math.abs(dx) * 0.22)
    : 0;

  const cp1x = startX + horizontalCurve * Math.sign(dx || 1);
  const cp1y = startY + verticalCurve * Math.sign(dy || 1);
  const cp2x = endX - horizontalCurve * Math.sign(dx || 1);
  const cp2y = endY - verticalCurve * Math.sign(dy || 1);

  return `M ${startX} ${startY} C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${endX} ${endY}`;
}

const graphEdges = computed(() => {
  if (!props.graph?.edges?.length) return [];
  return props.graph.edges
    .map((edge) => {
      const fromNode = nodeMap.value.get(edge.source);
      const toNode = nodeMap.value.get(edge.target);
      if (!fromNode || !toNode) return null;
      const key = `${edge.source}->${edge.target}`;
      return {
        key,
        d: connectionPath(fromNode, toNode),
        active: traversedEdgeKeys.value.has(key),
      };
    })
    .filter(Boolean);
});

function nodeIcon(kind) {
  if (kind === "logic") return BrainCircuit;
  if (kind === "agent") return Bot;
  if (kind === "final") return Zap;
  return null;
}

function nodeVisited(nodeId) {
  if (!props.trace.length) return false;
  return props.trace.some((event) => event?.payload?.node_id === nodeId || event?.payload?.next_node_id === nodeId);
}

function nodeCurrent(nodeId) {
  return props.activeNodeId === nodeId;
}

function resetOffsets() {
  nodeOffsets.value = {};
}

watch(
  () => props.graph?.nodes?.map((node) => node.id).join("|") || "",
  () => resetOffsets(),
);

function onPointerMove(event) {
  if (!dragState.active || !dragState.nodeId) return;
  const rect = canvasRef.value?.getBoundingClientRect();
  if (!rect) return;
  const localX = event.clientX - rect.left;
  const localY = event.clientY - rect.top;
  const deltaX = localX - dragState.startX;
  const deltaY = localY - dragState.startY;
  nodeOffsets.value = {
    ...nodeOffsets.value,
    [dragState.nodeId]: {
      x: dragState.offsetX + deltaX,
      y: dragState.offsetY + deltaY,
    },
  };
}

function onPointerUp() {
  dragState.active = false;
  dragState.nodeId = "";
}

function onNodePointerDown(event, node) {
  event.preventDefault();
  const rect = canvasRef.value?.getBoundingClientRect();
  if (!rect) return;
  const offset = nodeOffsets.value[node.id] || { x: 0, y: 0 };
  dragState.active = true;
  dragState.nodeId = node.id;
  dragState.startX = event.clientX - rect.left;
  dragState.startY = event.clientY - rect.top;
  dragState.offsetX = offset.x;
  dragState.offsetY = offset.y;
}

onMounted(() => {
  refreshCanvasSize();
  if (canvasRef.value) {
    resizeObserver = new ResizeObserver(() => refreshCanvasSize());
    resizeObserver.observe(canvasRef.value);
  }
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
});

onBeforeUnmount(() => {
  if (resizeObserver) resizeObserver.disconnect();
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
});
</script>

<template>
  <section class="glass-panel graph-shell">
    <header class="run-panel-header">
      <h3 class="run-panel-title">
        <Network :size="18" class="text-blue-500" />
        Workflow Graph
      </h3>
      <span class="panel-tag">Visual</span>
    </header>

    <div ref="canvasRef" class="graph-canvas-wrap">
      <div v-if="!graph" class="trace-empty">{{ t("graph.empty") }}</div>
      <template v-else>
        <svg
          class="graph-svg"
          :viewBox="`0 0 ${canvasSize.width || 560} ${canvasSize.height || 420}`"
          preserveAspectRatio="none"
        >
          <defs>
            <marker id="graphArrowBase" viewBox="0 0 10 10" refX="7.8" refY="5" markerWidth="6.2" markerHeight="6.2" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#cbd5e1" />
            </marker>
            <marker id="graphArrowActive" viewBox="0 0 10 10" refX="7.8" refY="5" markerWidth="6.2" markerHeight="6.2" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#3b82f6" />
            </marker>
          </defs>

          <path
            v-for="edge in graphEdges"
            :key="`base_${edge.key}`"
            :d="edge.d"
            fill="none"
            stroke="#cbd5e1"
            stroke-width="2"
            stroke-dasharray="4 4"
            marker-end="url(#graphArrowBase)"
          />
          <path
            v-for="edge in graphEdges.filter((item) => item.active)"
            :key="`active_${edge.key}`"
            :d="edge.d"
            fill="none"
            stroke="#3b82f6"
            stroke-width="2.8"
            marker-end="url(#graphArrowActive)"
          />
        </svg>

        <div
          v-for="node in graphNodes"
          :key="node.id"
          class="graph-node"
          :class="[
            `kind-${node.kind}`,
            {
              visited: nodeVisited(node.id),
              active: nodeCurrent(node.id),
            },
          ]"
          :style="{ left: `${node.x}px`, top: `${node.y}px` }"
          @pointerdown="onNodePointerDown($event, node)"
          @mouseenter="hoveredNodeId = node.id"
          @mouseleave="hoveredNodeId = ''"
        >
          <div
            v-if="hoveredNodeId === node.id"
            class="graph-node-tooltip"
          >
            {{ node.label }}
          </div>

          <component v-if="nodeIcon(node.kind)" :is="nodeIcon(node.kind)" :size="15" />
          <span v-else class="terminal-dot"></span>
          <span v-if="nodeCurrent(node.id)" class="graph-node-ring"></span>
        </div>
      </template>
    </div>

    <footer class="graph-terminal">
      <span v-if="activeNodeId">&gt; {{ t("graph.activeNode") }}: {{ activeNodeId }}</span>
      <span v-else>&gt; {{ t("graph.waiting") }}</span>
    </footer>
  </section>
</template>
