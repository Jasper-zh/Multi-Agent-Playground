import { computed, ref } from "vue";

export const I18N_KEY = Symbol("ui-i18n");

const messages = {
  "en-US": {
    nav: {
      overview: "Overview",
      agents: "Agents",
      workflows: "Orchestration",
      playground: "Run",
      settings: "Settings",
    },
    brand: {
      subtitle: "Multi-agent orchestration and trace system",
    },
    status: {
      ready: "System Ready",
    },
    lang: {
      zh: "中文",
      en: "EN",
    },
    page: {
      agentsTitle: "Agents",
      agentsDesc: "Create role specialists for reuse across different orchestrations.",
      workflowsTitle: "Orchestration",
      workflowsDesc: "Define collaboration patterns and create executable setups.",
    },
    settings: {
      title: "Settings",
      desc: "Configure model access for the desktop app runtime.",
      modelSection: "Model Runtime",
      modelSectionDesc: "These values are written to the app env file and applied immediately.",
      apiKey: "OpenAI API Key",
      apiKeyPlaceholder: "sk-...",
      baseUrl: "Base URL",
      baseUrlPlaceholder: "https://api.openai.com/v1",
      model: "Model",
      modelPlaceholder: "gpt-4o-mini",
      storageLabel: "Env File",
      save: "Save Settings",
      saving: "Saving...",
    },
    overview: {
      chip: "Orchestration-first Playground",
      headline1: "Define pattern first.",
      headline2: "Observe agents in motion.",
      desc: "This UI demonstrates agents, orchestration routing, and runtime trace in one clear experience.",
      flowTitle: "Flow",
      flowDesc: "Follow this order for the clearest walkthrough.",
      step1Title: "Create Agents",
      step1Desc: "Define reusable specialists.",
      step2Title: "Define Pattern",
      step2Desc: "Select a collaboration pattern and create a setup.",
      step3Title: "Run Playground",
      step3Desc: "Send messages and inspect graph plus trace.",
      agents: "Agents",
      workflows: "Setups",
      templates: "Patterns",
    },
    agent: {
      registryTitle: "Agent Registry",
      registryDesc: "Create and reuse specialists across orchestrations.",
      new: "New Agent",
      name: "Agent name",
      role: "Role description",
      prompt: "System prompt",
      save: "Save",
      cancel: "Cancel",
      systemPrompt: "System prompt",
    },
    workflow: {
      catalogTitle: "Orchestration Catalog",
      catalogDesc: "Choose a collaboration pattern and create executable setups.",
      new: "New Setup",
      setup: "Setup Configuration",
      name: "Setup name",
      enableFinalizer: "Enable Finalizer",
      bindAgents: "Bind Agents",
      requiresAtLeast: "This pattern requires at least {count} agents.",
      save: "Save Setup",
      cancel: "Cancel",
      selected: "selected",
      current: "Current setup",
      specialists: "Agents",
      finalizer: "Finalizer",
      on: "On",
      off: "Off",
      selectWorkflow: "Select Setup",
      template_router_specialists: "Router Specialists",
      template_planner_executor: "Planner Executor",
      template_supervisor_dynamic: "Supervisor Dynamic",
      template_single_agent_chat: "Single Agent Chat",
      template_desc_router_specialists:
        "Router selects the best specialist for the user intent, then optionally passes through a finalizer.",
      template_desc_planner_executor:
        "Planner decomposes the request into sub-tasks, delegates each task to workers, then synthesizes a final answer.",
      template_desc_supervisor_dynamic:
        "Supervisor decides delegation at runtime, loops through workers as needed, and composes the final answer.",
      template_desc_single_agent_chat:
        "Direct chat with one selected agent using a minimal start -> agent -> end graph.",
    },
    chat: {
      title: "Run",
      active: "Active setup",
      noneSelected: "No setup selected",
      clear: "Clear",
      presets: "Example Scenarios",
      fill: "Fill",
      presetRouter: "Single intent routing",
      presetPlanner: "Structured multi-part task",
      presetSupervisor: "Dynamic delegation with risks",
      startRun: "Start a run",
      startRunDesc: "Send a prompt and watch routing, graph highlights and trace updates.",
      thinking: "Thinking...",
      inputPlaceholder: "Type a test prompt...",
      send: "Send",
      assistant: "Assistant",
    },
    graph: {
      title: "Graph",
      subtitle: "Runtime highlighted nodes",
      liveGraph: "Live Graph",
      empty: "Select a setup to display its graph.",
      activeNode: "Active node",
      waiting: "Waiting for interaction...",
      live: "Live",
    },
    trace: {
      title: "Trace",
      subtitle: "Runtime events and routing decisions",
      live: "Live",
      playing: "Playing",
      showAll: "All events",
      showKey: "Key only",
      empty: "Run a setup to view events here.",
      payload: "payload",
    },
  },
  "zh-CN": {
    nav: {
      overview: "总览",
      agents: "智能体",
      workflows: "协作编排",
      playground: "运行",
      settings: "设置",
    },
    brand: {
      subtitle: "多智能体编排与执行追踪系统",
    },
    status: {
      ready: "系统就绪",
    },
    lang: {
      zh: "中文",
      en: "EN",
    },
    page: {
      agentsTitle: "智能体",
      agentsDesc: "创建可复用的角色型 Agent，用于不同编排方案。",
      workflowsTitle: "协作编排",
      workflowsDesc: "定义协作模式并创建可执行编排方案。",
    },
    settings: {
      title: "设置",
      desc: "配置桌面应用运行时使用的大模型参数。",
      modelSection: "模型运行配置",
      modelSectionDesc: "这些值会写入应用自己的 env 文件，并立即生效。",
      apiKey: "OpenAI API Key",
      apiKeyPlaceholder: "sk-...",
      baseUrl: "Base URL",
      baseUrlPlaceholder: "https://api.openai.com/v1",
      model: "模型",
      modelPlaceholder: "gpt-4o-mini",
      storageLabel: "配置文件位置",
      save: "保存设置",
      saving: "保存中...",
    },
    overview: {
      chip: "编排优先演示",
      headline1: "先定义模式。",
      headline2: "再观察智能体协作。",
      desc: "该界面用于演示：在一个视图中查看 Agents、编排路由和运行 Trace。",
      flowTitle: "流程",
      flowDesc: "按下面顺序体验最清晰。",
      step1Title: "创建 Agents",
      step1Desc: "定义可复用专家角色。",
      step2Title: "定义模式",
      step2Desc: "选择协作模式并创建编排方案。",
      step3Title: "运行 Playground",
      step3Desc: "发送消息并查看图与追踪。",
      agents: "Agents",
      workflows: "方案",
      templates: "模式",
    },
    agent: {
      registryTitle: "Agent 注册表",
      registryDesc: "创建并复用不同角色专家。",
      new: "新建 Agent",
      name: "Agent 名称",
      role: "角色描述",
      prompt: "系统提示词",
      save: "保存",
      cancel: "取消",
      systemPrompt: "系统提示词",
    },
    workflow: {
      catalogTitle: "编排方案目录",
      catalogDesc: "选择协作模式并创建可执行方案。",
      new: "新建方案",
      setup: "方案配置",
      name: "方案名称",
      enableFinalizer: "启用 Finalizer",
      bindAgents: "绑定 Agents",
      requiresAtLeast: "当前模式至少需要 {count} 个 Agent。",
      save: "保存方案",
      cancel: "取消",
      selected: "已选中",
      current: "当前方案",
      specialists: "Agents",
      finalizer: "Finalizer",
      on: "启用",
      off: "关闭",
      selectWorkflow: "选择方案",
      template_router_specialists: "路由专家",
      template_planner_executor: "规划执行",
      template_supervisor_dynamic: "动态监督",
      template_desc_router_specialists:
        "先由 Router 选择最匹配的专家，再按需经过 Finalizer 统一收口。",
      template_desc_planner_executor:
        "先由 Planner 拆解任务，再分配给 Worker 执行，最后统一合成答复。",
      template_desc_supervisor_dynamic:
        "由 Supervisor 在运行中动态委派，按需循环调度 Worker 并收敛结果。",
    },
    chat: {
      title: "运行",
      active: "当前方案",
      noneSelected: "未选择方案",
      clear: "清空",
      presets: "示例场景",
      fill: "填入",
      presetRouter: "单意图路由",
      presetPlanner: "结构化多段任务",
      presetSupervisor: "含风险的动态委派",
      startRun: "开始一次运行",
      startRunDesc: "发送问题并观察路由、图高亮和 Trace 变化。",
      thinking: "思考中...",
      inputPlaceholder: "输入测试问题...",
      send: "发送",
      assistant: "助手",
    },
    graph: {
      title: "流程图",
      subtitle: "运行节点高亮",
      liveGraph: "实时图",
      empty: "请选择一个方案查看图结构。",
      activeNode: "当前节点",
      waiting: "等待交互...",
      live: "实时",
    },
    trace: {
      title: "追踪",
      subtitle: "运行事件与路由决策",
      live: "实时",
      playing: "回放中",
      showAll: "全部事件",
      showKey: "仅关键",
      empty: "运行一次方案后会在这里显示事件。",
      payload: "载荷",
    },
  },
};

function getByPath(obj, path) {
  return path.split(".").reduce((current, key) => current?.[key], obj);
}

function interpolate(text, vars = {}) {
  return String(text).replace(/\{(\w+)\}/g, (_, key) => vars[key] ?? `{${key}}`);
}

export function createUiI18n() {
  const saved = localStorage.getItem("ui-locale");
  const locale = ref(saved === "zh-CN" || saved === "en-US" ? saved : "zh-CN");
  const dict = computed(() => messages[locale.value] || messages["zh-CN"]);

  function setLocale(nextLocale) {
    if (nextLocale !== "zh-CN" && nextLocale !== "en-US") return;
    locale.value = nextLocale;
    localStorage.setItem("ui-locale", nextLocale);
  }

  function t(path, vars = {}) {
    const found = getByPath(dict.value, path);
    if (typeof found === "string") return interpolate(found, vars);
    return path;
  }

  function workflowTypeLabel(type) {
    const path = `workflow.template_${type}`;
    const value = t(path);
    if (value !== path) return value;

    const fallbackLabels = {
      router_specialists: "Router Specialists",
      planner_executor: "Planner Executor",
      supervisor_dynamic: "Supervisor Dynamic",
      single_agent_chat: "Single Agent Chat",
    };
    return fallbackLabels[type] || type;
  }

  function workflowTypeDesc(type, fallback = "") {
    const value = t(`workflow.template_desc_${type}`);
    if (value !== `workflow.template_desc_${type}`) return value;
    const fallbackDescriptions = {
      router_specialists:
        "Router selects the best specialist for the user intent, then optionally passes through a finalizer.",
      planner_executor:
        "Planner decomposes the request into sub-tasks, delegates each task to workers, then synthesizes a final answer.",
      supervisor_dynamic:
        "Supervisor decides delegation at runtime, loops through workers as needed, and composes the final answer.",
      single_agent_chat:
        "Direct chat with one selected agent using a minimal start -> agent -> end graph.",
    };
    return fallbackDescriptions[type] || fallback;
  }

  return {
    locale,
    setLocale,
    t,
    workflowTypeLabel,
    workflowTypeDesc,
  };
}
