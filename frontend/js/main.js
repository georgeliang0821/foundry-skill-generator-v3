import {
  refreshSkillsCache,
  listSkillGrants,
  addSkillGrant,
  removeSkillGrant,
  setSkillVisibility,
  createSession,
  fetchAuthStatus,
  fetchInspect,
  fetchSessionTopology,
  getSession,
  listSessions,
  listSkills,
  logoutMicrosoft,
  postSSE,
  refreshAcaEnv,
  refreshPeerSkills,
  runSessionTest,
  saveSessionSkill,
  addSessionMaterial,
  deleteSessionMaterial,
  sendToolResult,
  StaleToolCallError,
  updateSessionMaterial,
  startMicrosoftLogin,
  undoSessionPatch,
  updateChecklistItem,
  updateSessionChildren,
  updateSamples,
  updateNeighbors,
  openNeighborEdit,
  proposeNeighborEdit,
  selectNeighborEdit,
  saveNeighborEditApi,
  updateVariables,
  updateSessionDraft,
} from "./api.js";

const hljs = window.hljs;

const mdRenderer = createMarkdownRenderer();

// Missing requirement keys from the most recent PREPARE->DRAFT quality-gate
// rejection, mapped onto the checkpoint cards so the user sees exactly what
// blocks DRAFT (the gate checks more than the four checkboxes).
let lastGateMissing = [];

function gateCheckpointFor(missingKey) {
  const k = String(missingKey || "");
  if (
    k.startsWith("understanding.skill_goal") ||
    k.startsWith("understanding.input_sources") ||
    k.startsWith("understanding.key_capabilities") ||
    k === "verify_checklist.definition_clear"
  ) return "definition_clear";
  if (
    k.startsWith("understanding.differentiation") ||
    k.startsWith("research.") ||
    k.startsWith("differentiation_must_mention") ||
    k === "verify_checklist.routing_uniqueness_confirmed"
  ) return "routing_uniqueness_confirmed";
  if (k === "verify_checklist.variables_ok") return "variables_ok";
  if (
    k.startsWith("children") ||
    k.startsWith("delegation.") ||
    k === "verify_checklist.delegation_ok"
  ) return "delegation_ok";
  return null;
}

const GATE_KEY_LABEL = {
  "understanding.skill_goal": "Skill goal not recorded",
  "understanding.input_sources": "Input / data sources missing",
  "understanding.key_capabilities": "Key capabilities missing",
  "understanding.differentiation": "Differentiation not written",
  "research.adjacent_skills": "No peer / adjacent comparison recorded",
  "research.web_status": "Research not yet considered",
};

function gateIssuesForCheckpoint(key) {
  if (!Array.isArray(lastGateMissing) || !lastGateMissing.length) return [];
  const out = [];
  for (const m of lastGateMissing) {
    if (gateCheckpointFor(m) !== key) continue;
    if (String(m).startsWith("differentiation_must_mention:")) {
      out.push("Differentiation must name peer: " + String(m).split(":")[1]);
    } else {
      out.push(GATE_KEY_LABEL[m] || m);
    }
  }
  return out;
}

function highlightCode(str, lang = "") {
  if (hljs && lang && hljs.getLanguage(lang)) {
    try {
      return `<pre><code class="hljs language-${escapeHtml(lang)}">${hljs.highlight(str, { language: lang, ignoreIllegals: true }).value}</code></pre>`;
    } catch {
      // Fall through to escaped code when highlighting fails.
    }
  }
  return `<pre><code class="hljs">${escapeHtml(str)}</code></pre>`;
}

function createMarkdownRenderer() {
  if (window.markdownit) {
    return window.markdownit({
      html: false,
      linkify: true,
      typographer: true,
      highlight: highlightCode,
    });
  }
  console.warn("[Skill Generator v2] markdown-it was not loaded; using basic Markdown renderer.");
  return { render: renderBasicMarkdown };
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

// Inline Markdown for short UI strings (question titles, answer options). Uses
// markdown-it's inline renderer when available (it escapes raw HTML because
// html:false), falling back to the lightweight inline renderer above.
function renderMdInline(value) {
  const text = String(value ?? "");
  if (mdRenderer && typeof mdRenderer.renderInline === "function") {
    try {
      return mdRenderer.renderInline(text);
    } catch {
      // fall through to the basic inline renderer
    }
  }
  return renderInlineMarkdown(text);
}

function renderBasicMarkdown(markdownText) {
  const lines = String(markdownText || "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let listItems = [];
  let inFence = false;
  let fenceLang = "";
  let fenceLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    html.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
    listItems = [];
  };

  for (const line of lines) {
    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      if (inFence) {
        html.push(highlightCode(fenceLines.join("\n"), fenceLang));
        inFence = false;
        fenceLang = "";
        fenceLines = [];
      } else {
        flushParagraph();
        flushList();
        inFence = true;
        fenceLang = fence[1] || "";
      }
      continue;
    }
    if (inFence) {
      fenceLines.push(line);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      listItems.push(bullet[1]);
      continue;
    }
    paragraph.push(line.trim());
  }
  if (inFence) html.push(highlightCode(fenceLines.join("\n"), fenceLang));
  flushParagraph();
  flushList();
  return html.join("");
}

const STAGE_GROUPS = ["PREPARE", "DRAFT", "REFINE", "TEST"];

const GROUP_ICONS = {
  PREPARE: "i-clipboard-check",
  DRAFT: "i-document-text",
  REFINE: "i-adjustments",
  TEST: "i-beaker",
  DONE: "i-check-circle",
};

function svgIcon(name, extraClass = "") {
  const cls = ("icon " + extraClass).trim();
  return `<svg class="${cls}" aria-hidden="true"><use href="#${name}"/></svg>`;
}

const STAGE_TO_GROUP = {
  // v6 5-stage (lowercase from backend, uppercase from legacy paths).
  PREPARE: "PREPARE",
  DRAFT: "DRAFT",
  REFINE: "REFINE",
  TEST: "TEST",
  DONE: "DONE",
  // Legacy aliases (back-compat for sessions persisted before migration).
  INTAKE: "PREPARE",
  RESEARCH: "PREPARE",
  VERIFY: "PREPARE",
  ITERATE: "REFINE",
};

const SUBSTAGE_LABELS = {
  PREPARE: "Confirming definition, routing, and variables",
  DRAFT: "Generating draft",
  REFINE: "Refining via patches",
  TEST: "Running selection tests",
  DONE: "Finalized",
  // Legacy aliases for sessions created before the five-stage migration.
  INTAKE: "Collecting materials",
  RESEARCH: "Reviewing materials",
  VERIFY: "Confirming assumptions",
  ITERATE: "Fixing from test failures",
};

const groupMeta = {
  PREPARE: {
    title: "Prepare",
    purpose: "Collect materials and align scope.",
    description: "Share requirements, docs, or old skills. The model summarizes and asks you to confirm assumptions before drafting.",
    next: "Paste requirements or attach materials. Answer the confirmation cards when they appear.",
  },
  DRAFT: {
    title: "Draft",
    purpose: "Generate the first reviewable files.",
    description: "The model generates one complete SKILL.md with environment guidance and sample code embedded.",
    next: "Review SKILL.md in the Files tab and accept the draft when ready.",
  },
  REFINE: {
    title: "Refine",
    purpose: "Apply focused patches.",
    description: "Request targeted changes or fix issues from test failures. Each change is shown as a reviewable patch.",
    next: "Describe a change, accept or reject the proposed patch, or move to testing.",
  },
  TEST: {
    title: "Test",
    purpose: "Validate skill selection.",
    description: "Run positive and negative selection tests through the APIM /run endpoint with your Microsoft identity. Selection tests run in route_only mode: the runtime routes and returns the script it would have run, but nothing is executed and nothing is written.",
    next: "Run the proposed test samples and review failures.",
  },
  DONE: {
    title: "Done",
    purpose: "Finalized.",
    description: "The skill has passed the authoring flow. You can still request focused edits.",
    next: "Describe a change to reopen refinement, run tests again, or start a new session.",
  },
};

const CONTEXT_TABS = ["materials", "checklist", "files", "tests", "topology", "inspect", "agent"];

const CONTEXT_TAB_FOR_STAGE = {
  // v6 5-stage names -- these are what currentStage() actually returns.
  PREPARE: "checklist",
  DRAFT: "files",
  REFINE: "files",
  TEST: "tests",
  DONE: "tests",
  // Legacy aliases (sessions persisted before the 5-stage migration).
  INTAKE: "materials",
  RESEARCH: "materials",
  VERIFY: "checklist",
  ITERATE: "files",
};

const modeHelp = {
  new: "Create a skill from scratch from notes, docs, code, or API specs.",
  modify: "Load and revise an existing system skill from the skill store.",
};

const kindHelp = {
  capability: "A directly selectable skill that performs one bounded capability.",
  scenario: "A host-selected workflow that delegates work to child capability skills.",
};

let session = null;
let attachedMaterials = [];
let activeTab = "skill";
let activeContextTab = "materials";
let userPickedContextTab = false;
let isSending = false;
let availableSkills = [];
let skillsLoaded = false;
let savedSessions = [];
let sessionsLoaded = false;
let inspectData = null;
let inspectLoading = false;
let topologyData = null;
let topologyLoading = false;
let agentGraph = null;
let selectedGraphStage = "";
let selectedGraphEdge = "";
let agentGraphFullscreen = false;
let authStatus = null;
let draftSaveTimer = null;
let localEditorDirty = false;
const pendingChoiceAnswers = new Map();
const pendingQuestionCalls = new Map();
const UI_STORAGE_KEY = "skill_generator_v2_ui_state";
const WORKSPACE_SPLIT_KEY = "skill_generator_v2_workspace_split";
const FILES_SPLIT_KEY = "skill_generator_v2_files_split";
const AGENT_GRAPH_SPLIT_KEY = "skill_generator_v2_agent_graph_split";

const el = (id) => document.getElementById(id);

function appLog(message, data = undefined) {
  console.info(`[Skill Generator v2] ${message}`, data || "");
  const node = el("appStatus");
  if (!node) return;
  node.textContent = message;
  node.classList.remove("hidden");
  window.clearTimeout(node.dataset.timerId);
  const timerId = window.setTimeout(() => node.classList.add("hidden"), 5000);
  node.dataset.timerId = String(timerId);
}

// Verbose, structured tracing for the chat/tool-render pipeline. Logs to the
// console only (never the UI toast) so it is safe to keep on while debugging.
// Toggle with `window.SGV2_DEBUG = false` in the devtools console to silence.
window.SGV2_DEBUG = window.SGV2_DEBUG ?? true;
function dbgLog(category, message, data = undefined) {
  if (!window.SGV2_DEBUG) return;
  const ts = new Date().toISOString().slice(11, 23);
  if (data === undefined) {
    console.debug(`[SGV2 ${ts}][${category}] ${message}`);
  } else {
    console.debug(`[SGV2 ${ts}][${category}] ${message}`, data);
  }
}

function bind(id, eventName, handler) {
  const node = el(id);
  if (!node) {
    showFatalError(`Missing DOM element #${id}; frontend version does not match loaded JavaScript.`);
    console.error(`[Skill Generator v2] Missing DOM element #${id}`);
    return null;
  }
  node.addEventListener(eventName, handler);
  return node;
}

function workspaceResizeMetrics() {
  const workspace = document.querySelector(".workspace");
  const resizer = el("workspaceResizer");
  if (!workspace || !resizer || window.matchMedia("(max-width: 1024px)").matches) return null;
  const rect = workspace.getBoundingClientRect();
  const style = getComputedStyle(workspace);
  const paddingLeft = parseFloat(style.paddingLeft) || 0;
  const paddingRight = parseFloat(style.paddingRight) || 0;
  const contentLeft = rect.left + paddingLeft;
  const contentWidth = rect.width - paddingLeft - paddingRight;
  const splitterWidth = resizer.getBoundingClientRect().width || 12;
  const availableWidth = contentWidth - splitterWidth;
  const minLeft = Math.min(440, Math.max(320, availableWidth * 0.42));
  const minRight = Math.min(420, Math.max(320, availableWidth * 0.36));
  if (availableWidth < minLeft + minRight) return null;
  return { workspace, contentLeft, availableWidth, splitterWidth, minLeft, minRight };
}

function clampWorkspaceLeft(leftPx, metrics) {
  return Math.min(Math.max(leftPx, metrics.minLeft), metrics.availableWidth - metrics.minRight);
}

function setWorkspaceSplit(leftPx, shouldPersist = false) {
  const metrics = workspaceResizeMetrics();
  if (!metrics) return;
  const left = clampWorkspaceLeft(leftPx, metrics);
  const right = metrics.availableWidth - left;
  metrics.workspace.style.gridTemplateColumns = `${Math.round(left)}px ${Math.round(metrics.splitterWidth)}px ${Math.round(right)}px`;
  el("workspaceResizer")?.setAttribute("aria-valuenow", String(Math.round((left / metrics.availableWidth) * 100)));
  if (shouldPersist) {
    localStorage.setItem(WORKSPACE_SPLIT_KEY, String(left / metrics.availableWidth));
  }
  window.requestAnimationFrame(() => {
    agentGraph?.resize();
    if (selectedGraphEdge) fitGraphEdge(selectedGraphEdge, 90);
  });
}

function restoreWorkspaceSplit() {
  const metrics = workspaceResizeMetrics();
  if (!metrics) {
    if (window.matchMedia("(max-width: 1024px)").matches) {
      const workspace = document.querySelector(".workspace");
      if (workspace) workspace.style.gridTemplateColumns = "";
    }
    return;
  }
  const storedRatio = Number(localStorage.getItem(WORKSPACE_SPLIT_KEY));
  const ratio = Number.isFinite(storedRatio) && storedRatio > 0 ? storedRatio : 0.52;
  setWorkspaceSplit(metrics.availableWidth * ratio, false);
}

function initWorkspaceResizer() {
  const resizer = el("workspaceResizer");
  if (!resizer) return;
  restoreWorkspaceSplit();
  let dragging = false;

  resizer.addEventListener("pointerdown", (event) => {
    const metrics = workspaceResizeMetrics();
    if (!metrics) return;
    dragging = true;
    metrics.workspace.classList.add("resizing");
    resizer.setPointerCapture?.(event.pointerId);
    setWorkspaceSplit(event.clientX - metrics.contentLeft, false);
    event.preventDefault();
  });

  resizer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const metrics = workspaceResizeMetrics();
    if (!metrics) return;
    setWorkspaceSplit(event.clientX - metrics.contentLeft, false);
  });

  const finishDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    const metrics = workspaceResizeMetrics();
    if (metrics) {
      setWorkspaceSplit(event.clientX - metrics.contentLeft, true);
      metrics.workspace.classList.remove("resizing");
    } else {
      document.querySelector(".workspace")?.classList.remove("resizing");
    }
  };
  resizer.addEventListener("pointerup", finishDrag);
  resizer.addEventListener("pointercancel", finishDrag);

  resizer.addEventListener("keydown", (event) => {
    const metrics = workspaceResizeMetrics();
    if (!metrics) return;
    const columns = getComputedStyle(metrics.workspace).gridTemplateColumns.split(" ");
    const currentLeft = parseFloat(columns[0]) || metrics.availableWidth * 0.52;
    let nextLeft = currentLeft;
    if (event.key === "ArrowLeft") nextLeft -= event.shiftKey ? 80 : 32;
    if (event.key === "ArrowRight") nextLeft += event.shiftKey ? 80 : 32;
    if (event.key === "Home") nextLeft = metrics.minLeft;
    if (event.key === "End") nextLeft = metrics.availableWidth - metrics.minRight;
    if (nextLeft === currentLeft) return;
    event.preventDefault();
    setWorkspaceSplit(nextLeft, true);
  });

  window.addEventListener("resize", restoreWorkspaceSplit);
}

function filesResizeMetrics() {
  const container = document.querySelector(".skill-md-workspace");
  const resizer = el("filesResizer");
  if (!container || !resizer || window.matchMedia("(max-width: 720px)").matches) return null;
  const rect = container.getBoundingClientRect();
  const splitterWidth = resizer.getBoundingClientRect().width || 12;
  const availableWidth = rect.width - splitterWidth;
  const minLeft = Math.min(260, Math.max(220, availableWidth * 0.34));
  const minRight = Math.min(260, Math.max(220, availableWidth * 0.34));
  if (availableWidth < minLeft + minRight) return null;
  return { container, contentLeft: rect.left, availableWidth, splitterWidth, minLeft, minRight };
}

function clampFilesLeft(leftPx, metrics) {
  return Math.min(Math.max(leftPx, metrics.minLeft), metrics.availableWidth - metrics.minRight);
}

function setFilesSplit(leftPx, shouldPersist = false) {
  const metrics = filesResizeMetrics();
  if (!metrics) return;
  const left = clampFilesLeft(leftPx, metrics);
  const right = metrics.availableWidth - left;
  metrics.container.style.gridTemplateColumns = `${Math.round(left)}px ${Math.round(metrics.splitterWidth)}px ${Math.round(right)}px`;
  el("filesResizer")?.setAttribute("aria-valuenow", String(Math.round((left / metrics.availableWidth) * 100)));
  if (shouldPersist) localStorage.setItem(FILES_SPLIT_KEY, String(left / metrics.availableWidth));
}

function restoreFilesSplit() {
  const metrics = filesResizeMetrics();
  if (!metrics) {
    if (window.matchMedia("(max-width: 720px)").matches) {
      const container = document.querySelector(".skill-md-workspace");
      if (container) container.style.gridTemplateColumns = "";
    }
    return;
  }
  const storedRatio = Number(localStorage.getItem(FILES_SPLIT_KEY));
  const ratio = Number.isFinite(storedRatio) && storedRatio > 0 ? storedRatio : 0.5;
  setFilesSplit(metrics.availableWidth * ratio, false);
}

function initFilesResizer() {
  const resizer = el("filesResizer");
  if (!resizer) return;
  restoreFilesSplit();
  let dragging = false;

  resizer.addEventListener("pointerdown", (event) => {
    const metrics = filesResizeMetrics();
    if (!metrics) return;
    dragging = true;
    metrics.container.classList.add("resizing");
    resizer.setPointerCapture?.(event.pointerId);
    setFilesSplit(event.clientX - metrics.contentLeft, false);
    event.preventDefault();
  });

  resizer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const metrics = filesResizeMetrics();
    if (!metrics) return;
    setFilesSplit(event.clientX - metrics.contentLeft, false);
  });

  const finishDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    const metrics = filesResizeMetrics();
    if (metrics) {
      setFilesSplit(event.clientX - metrics.contentLeft, true);
      metrics.container.classList.remove("resizing");
    } else {
      document.querySelector(".skill-md-workspace")?.classList.remove("resizing");
    }
  };
  resizer.addEventListener("pointerup", finishDrag);
  resizer.addEventListener("pointercancel", finishDrag);

  resizer.addEventListener("keydown", (event) => {
    const metrics = filesResizeMetrics();
    if (!metrics) return;
    const columns = getComputedStyle(metrics.container).gridTemplateColumns.split(" ");
    const currentLeft = parseFloat(columns[0]) || metrics.availableWidth * 0.5;
    let nextLeft = currentLeft;
    if (event.key === "ArrowLeft") nextLeft -= event.shiftKey ? 80 : 32;
    if (event.key === "ArrowRight") nextLeft += event.shiftKey ? 80 : 32;
    if (event.key === "Home") nextLeft = metrics.minLeft;
    if (event.key === "End") nextLeft = metrics.availableWidth - metrics.minRight;
    if (nextLeft === currentLeft) return;
    event.preventDefault();
    setFilesSplit(nextLeft, true);
  });

  window.addEventListener("resize", restoreFilesSplit);
}

function agentGraphResizeMetrics() {
  const container = el("agentGraphContent");
  const resizer = el("agentGraphResizer");
  if (!container || !resizer || window.matchMedia("(max-width: 720px)").matches) return null;
  const rect = container.getBoundingClientRect();
  const splitterWidth = resizer.getBoundingClientRect().width || 12;
  const availableWidth = rect.width - splitterWidth;
  const minLeft = Math.min(280, Math.max(220, availableWidth * 0.28));
  const minRight = Math.min(280, Math.max(240, availableWidth * 0.30));
  if (availableWidth < minLeft + minRight) return null;
  return { container, contentLeft: rect.left, availableWidth, splitterWidth, minLeft, minRight };
}

function clampAgentGraphLeft(leftPx, metrics) {
  return Math.min(Math.max(leftPx, metrics.minLeft), metrics.availableWidth - metrics.minRight);
}

function setAgentGraphSplit(leftPx, shouldPersist = false) {
  const metrics = agentGraphResizeMetrics();
  if (!metrics) return;
  const left = clampAgentGraphLeft(leftPx, metrics);
  const right = metrics.availableWidth - left;
  metrics.container.style.gridTemplateColumns = `${Math.round(left)}px ${Math.round(metrics.splitterWidth)}px ${Math.round(right)}px`;
  el("agentGraphResizer")?.setAttribute("aria-valuenow", String(Math.round((left / metrics.availableWidth) * 100)));
  if (shouldPersist) localStorage.setItem(AGENT_GRAPH_SPLIT_KEY, String(left / metrics.availableWidth));
  window.requestAnimationFrame(() => agentGraph?.resize());
}

function restoreAgentGraphSplit() {
  const metrics = agentGraphResizeMetrics();
  if (!metrics) {
    if (window.matchMedia("(max-width: 720px)").matches) {
      const container = el("agentGraphContent");
      if (container) container.style.gridTemplateColumns = "";
    }
    return;
  }
  const storedRatio = Number(localStorage.getItem(AGENT_GRAPH_SPLIT_KEY));
  const ratio = Number.isFinite(storedRatio) && storedRatio > 0 ? storedRatio : 0.62;
  setAgentGraphSplit(metrics.availableWidth * ratio, false);
}

function initAgentGraphResizer() {
  const resizer = el("agentGraphResizer");
  if (!resizer) return;
  restoreAgentGraphSplit();
  let dragging = false;

  resizer.addEventListener("pointerdown", (event) => {
    const metrics = agentGraphResizeMetrics();
    if (!metrics) return;
    dragging = true;
    metrics.container.classList.add("resizing");
    resizer.setPointerCapture?.(event.pointerId);
    setAgentGraphSplit(event.clientX - metrics.contentLeft, false);
    event.preventDefault();
  });

  resizer.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const metrics = agentGraphResizeMetrics();
    if (!metrics) return;
    setAgentGraphSplit(event.clientX - metrics.contentLeft, false);
  });

  const finishDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    const metrics = agentGraphResizeMetrics();
    if (metrics) {
      setAgentGraphSplit(event.clientX - metrics.contentLeft, true);
      metrics.container.classList.remove("resizing");
      agentGraph?.fit(undefined, agentGraphFullscreen ? 70 : 40);
    } else {
      el("agentGraphContent")?.classList.remove("resizing");
    }
  };
  resizer.addEventListener("pointerup", finishDrag);
  resizer.addEventListener("pointercancel", finishDrag);

  resizer.addEventListener("keydown", (event) => {
    const metrics = agentGraphResizeMetrics();
    if (!metrics) return;
    const columns = getComputedStyle(metrics.container).gridTemplateColumns.split(" ");
    const currentLeft = parseFloat(columns[0]) || metrics.availableWidth * 0.62;
    let nextLeft = currentLeft;
    if (event.key === "ArrowLeft") nextLeft -= event.shiftKey ? 80 : 32;
    if (event.key === "ArrowRight") nextLeft += event.shiftKey ? 80 : 32;
    if (event.key === "Home") nextLeft = metrics.minLeft;
    if (event.key === "End") nextLeft = metrics.availableWidth - metrics.minRight;
    if (nextLeft === currentLeft) return;
    event.preventDefault();
    setAgentGraphSplit(nextLeft, true);
  });

  window.addEventListener("resize", restoreAgentGraphSplit);
}

function persistSessionState() {
  sessionStorage.setItem(
    UI_STORAGE_KEY,
    JSON.stringify({
      activeTab,
      activeContextTab,
      userPickedContextTab,
      attachedMaterials,
      messageInput: el("messageInput")?.value || "",
      mode: el("modeSelect")?.value || "new",
      targetSkill: el("targetSkill")?.value || "",
    }),
  );
}

function restoreUiState() {
  const raw = sessionStorage.getItem(UI_STORAGE_KEY);
  if (!raw) return;
  try {
    const state = JSON.parse(raw);
    if (state.mode && el("modeSelect")) el("modeSelect").value = state.mode;
    if (state.targetSkill && el("targetSkill")) el("targetSkill").value = state.targetSkill;
    if (state.messageInput && el("messageInput")) el("messageInput").value = state.messageInput;
    if (Array.isArray(state.attachedMaterials)) attachedMaterials = state.attachedMaterials;
    activeTab = "skill";
    if (state.activeContextTab) activeContextTab = state.activeContextTab;
    userPickedContextTab = Boolean(state.userPickedContextTab);
  } catch (err) {
    console.warn("[Skill Generator v2] Could not restore UI state", err);
  }
}

function renderLoadedSessionContent(statusText = "") {
  el("chatStream").innerHTML = "";
  el("toolCalls").innerHTML = "";
  pendingQuestionCalls.clear();
  pendingChoiceAnswers.clear();
  if (statusText) appendConversationStatus(statusText);
  const messages = session?.conversation || [];
  messages.forEach((message) => {
    if (message.role === "user" || message.role === "assistant") {
      if (message.role === "user" && message.metadata?.auto_continue) {
        appendConversationStatus("System auto-continued to the next stage.");
        return;
      }
      appendMessage(message.role, message.content);
    }
  });
  const restoredTools = session?.pending_tool_calls || [];
  dbgLog("restore", `replaying session ${session?.id}: ${messages.length} messages, ${restoredTools.length} pending tool calls`);
  restoredTools.forEach((call) => renderToolCall(call));
  resetActivityGroup();
  if (!messages.length && !restoredTools.length) {
    el("chatStream").innerHTML = `<div class="empty-state chat-empty">${svgIcon("i-chat")}<strong>Saved session loaded</strong><em>Continue by sending the next message.</em></div>`;
  }
}

appLog("Frontend loaded");

// Test/debug hook: expose minimal renderers so e2e specs can drive UI without a backend round-trip.
if (typeof window !== "undefined") {
  window.__sgv2 = {
    get session() { return session; },
    set session(value) { session = value; },
    get savedSessions() { return savedSessions; },
    set savedSessions(value) { savedSessions = value; sessionsLoaded = true; },
    get attachedMaterials() { return attachedMaterials; },
    set attachedMaterials(value) { attachedMaterials = value; },
    renderSession,
    renderChecklist,
    renderSkillBindingStatus,
    renderStages,
    renderLoadedSessionContent,
    renderMaterials,
    renderSessionSelector,
  };
}

window.addEventListener("error", (event) => {
  console.error("Frontend error:", event.error || event.message, event);
  const location = event.filename ? ` (${event.filename}:${event.lineno}:${event.colno})` : "";
  showFatalError(`${event.message || "Unexpected frontend error."}${location}`);
});

window.addEventListener("unhandledrejection", (event) => {
  console.error("Unhandled rejection:", event.reason);
  showFatalError(event.reason?.message || String(event.reason || "Unhandled frontend error."));
});

function showFatalError(message) {
  const node = el("fatalError");
  if (!node) return;
  node.textContent = `Frontend error: ${message}`;
  node.classList.remove("hidden");
}

function currentStage() {
  const raw = session?.current_stage || "PREPARE";
  return String(raw).toUpperCase();
}

function normalizeStage(value) {
  return String(value || "PREPARE").toUpperCase();
}

function currentGroup() {
  return STAGE_TO_GROUP[currentStage()] || "PREPARE";
}

function renderStages() {
  const stage = currentStage();
  const group = currentGroup();
  const isDone = stage === "DONE";
  const currentIndex = STAGE_GROUPS.indexOf(group);
  el("stageList").innerHTML = STAGE_GROUPS.map((key, index) => {
    const isActive = !isDone && key === group;
    const isPast = isDone || index < currentIndex;
    const subhint = isActive ? SUBSTAGE_LABELS[stage] || "" : "";
    const stateLabel = isPast ? "completed" : isActive ? "in progress" : "upcoming";
    const marker = isPast
      ? svgIcon("i-check")
      : isActive
        ? svgIcon(GROUP_ICONS[key] || "i-sparkles")
        : `${index + 1}`;
    return `<div class="stage-item ${isActive ? "active" : ""} ${isPast ? "done" : ""}" role="listitem" aria-current="${isActive ? "step" : "false"}" data-phase="${key}">
      <span class="stage-number" aria-label="Step ${index + 1} ${stateLabel}">${marker}</span>
      <span class="stage-copy">
        <strong>${groupMeta[key].title}</strong>
        <small>${escapeHtml(subhint || groupMeta[key].purpose)}</small>
      </span>
    </div>`;
  }).join("");
  el("doneBadge").classList.toggle("hidden", !isDone);
}

function renderSession() {
  el("sessionId").textContent = session ? session.id : "No session";
  if (session?.skill_kind) el("skillKindSelect").value = session.skill_kind;
  el("skillKindSelect").disabled = el("modeSelect").value === "modify";
  el("modeHelp").textContent = modeHelp[el("modeSelect").value] || "";
  el("kindHelp").textContent = kindHelp[el("skillKindSelect").value] || "";
  renderAuthStatus();
  renderSkillBindingStatus();
  renderSkillSelector();
  renderSessionSelector();
  renderStages();
  renderGuide();
  renderMaterials();
  renderChecklist();
  renderEditor();
  renderTests();
  renderTestSampleEditor();
  renderQuestionQueueStatus();
  updateActionButtons();
  applyContextTabDefault();
}

function skillBindingState() {
  const remoteName = session?.remote_skill_id || session?.target_skill_id || "";
  const remoteVersion = session?.remote_version_hash || "";
  const localVersion = session?.current_skill?.version_hash || "";
  const hasFiles = Boolean(session?.current_skill?.skill_md?.trim());
  const hasRemote = Boolean(remoteName);
  const unsaved = Boolean(hasRemote && (localEditorDirty || (remoteVersion && localVersion && remoteVersion !== localVersion)));
  const unknown = Boolean(hasRemote && !remoteVersion);
  return {
    remoteName,
    remoteVersion,
    localVersion,
    hasFiles,
    hasRemote,
    unsaved,
    unknown,
    label: hasRemote ? remoteName : "NEW",
    state: !hasRemote ? "new" : unknown ? "unknown" : unsaved ? "unsaved" : "saved",
  };
}

function renderSkillBindingStatus() {
  const node = el("skillBindingStatus");
  if (!node) return;
  const binding = skillBindingState();
  node.className = `skill-binding-status ${binding.state}`;
  let stateText;
  let icon;
  if (binding.state === "new") {
    stateText = binding.hasFiles ? "Skill not saved" : "Not started";
    icon = "i-flag";
  } else if (binding.state === "unsaved") {
    stateText = "Skill has unsaved changes";
    icon = "i-warning";
  } else if (binding.state === "unknown") {
    stateText = "Skill version unknown";
    icon = "i-info";
  } else {
    stateText = "Skill saved (latest)";
    icon = "i-check-circle";
  }
  const skillLabel = escapeHtml(binding.label || "NEW");
  const isPublic = binding.hasRemote
    && availableSkills.some((s) => s.name === binding.remoteName && s.is_public);
  const publicBadge = isPublic
    ? `<span class="binding-public" title="Public: every signed-in user can use this skill">public</span>`
    : "";
  node.innerHTML = `${svgIcon(icon)}<span class="binding-state">${escapeHtml(stateText)}</span><span class="binding-skill" title="Skill: ${skillLabel}">Skill: ${skillLabel}</span>${publicBadge}`;
  node.title = binding.hasRemote
    ? `Remote: ${binding.remoteName || "none"}\nRemote version: ${binding.remoteVersion || "unknown"}\nLocal version: ${binding.localVersion || "none"}`
    : "This session is not bound to a Blob skill yet.";
}

function formatProfileValue(value) {
  const text = String(value || "").trim();
  return text || "(not provided)";
}

function renderAuthProfilePanel(user = {}) {
  const panel = el("authProfilePanel");
  if (!panel) return;
  if (!authStatus?.authenticated) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const rows = [
    ["Name", user.name],
    ["Email", user.email],
    ["Username", user.username],
    ["OID", user.oid],
    ["Tenant", user.tenant_id],
  ];
  panel.innerHTML = `<div class="auth-profile-card">
    <div class="auth-profile-card-head">
      <strong>${escapeHtml(formatProfileValue(user.name || user.username || user.email || "Signed in"))}</strong>
      <span>OAuth profile</span>
    </div>
    <dl class="auth-profile-fields">
      ${rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(formatProfileValue(value))}</dd></div>`).join("")}
    </dl>
    <button id="authProfileSignOutBtn" class="auth-profile-signout" type="button">Sign out</button>
  </div>`;
}

function setAuthProfileOpen(open) {
  const panel = el("authProfilePanel");
  const button = el("authBtn");
  if (!panel || !button) return;
  panel.classList.toggle("hidden", !open);
  button.setAttribute("aria-expanded", open ? "true" : "false");
}

function renderAuthStatus() {
  const status = el("authStatus");
  const button = el("authBtn");
  const profile = el("authProfile");
  const label = button?.querySelector(".auth-label");
  if (!status || !button || !profile || !label) return;
  if (!authStatus) {
    status.textContent = "Checking sign-in";
    status.classList.remove("signed-in");
    profile.textContent = "Not signed in";
    label.textContent = "Sign in";
    button.disabled = true;
    setAuthProfileOpen(false);
    renderAuthProfilePanel({});
    return;
  }
  button.disabled = false;
  if (authStatus.authenticated) {
    const user = authStatus.profile || {};
    const display = user.name || user.username || user.email || "Signed in";
    status.textContent = "Microsoft signed in";
    status.classList.add("signed-in");
    profile.textContent = display;
    profile.title = [user.email, user.username, user.oid].filter(Boolean).join("\n");
    label.textContent = "Profile";
    renderAuthProfilePanel(user);
  } else {
    status.textContent = "Sign in for OBO tests";
    status.classList.remove("signed-in");
    profile.textContent = "Not signed in";
    profile.title = "";
    label.textContent = "Sign in";
    setAuthProfileOpen(false);
    renderAuthProfilePanel({});
  }
}

function renderSkillSelector() {
  const isModify = el("modeSelect").value === "modify";
  const selector = el("targetSkill");
  // Toggle the whole group, not just the <select>, so the refresh button hides too.
  const group = selector?.closest(".target-skill-group");
  if (group) group.style.display = isModify ? "" : "none";
  selector.disabled = !isModify || isSending;
  selector.classList.toggle("hidden", !isModify);
  if (!isModify) return;
  const currentSkill = session?.remote_skill_id || session?.target_skill_id || selector.value || "";
  if (!skillsLoaded) {
    selector.innerHTML = currentSkill
      ? `<option value="${escapeHtml(currentSkill)}">${escapeHtml(currentSkill)} - loading skill list...</option>`
      : `<option value="">Loading skills...</option>`;
    if (currentSkill) selector.value = currentSkill;
    return;
  }
  if (!availableSkills.length) {
    selector.innerHTML = currentSkill
      ? `<option value="${escapeHtml(currentSkill)}">${escapeHtml(currentSkill)} - current session</option>`
      : `<option value="">No skills you have access to - ask an owner to grant you</option>`;
    if (currentSkill) selector.value = currentSkill;
    return;
  }
  const current = currentSkill || selector.value;
  const currentInList = availableSkills.some((skill) => skill.name === current);
  selector.innerHTML = [
    `<option value="">Select an existing skill</option>`,
    current && !currentInList ? `<option value="${escapeHtml(current)}">${escapeHtml(current)} - current session</option>` : "",
    ...availableSkills.map((skill) => {
      const badge = skill.is_public ? " [public]" : "";
      const internalBadge = skill.is_internal ? " [internal]" : "";
      const desc = skill.description ? ` - ${escapeHtml(skill.description.slice(0, 60))}` : "";
      return `<option value="${escapeHtml(skill.name)}">${escapeHtml(skill.name)}${internalBadge}${badge}${desc}</option>`;
    }),
  ].join("");
  if (current) selector.value = current;
}

function visibleSessionsForSelector() {
  // In modify mode with a chosen skill, only show prior sessions that target
  // that same skill -- avoids dumping every session into the picker.
  const isModify = el("modeSelect")?.value === "modify";
  const targetSkill = (el("targetSkill")?.value || "").trim();
  if (isModify && targetSkill) {
    return savedSessions.filter((item) => (item.target_skill_id || "") === targetSkill);
  }
  return savedSessions;
}

function renderSessionSelector() {
  const selector = el("sessionList");
  if (!selector) return;
  selector.disabled = isSending || !sessionsLoaded;
  if (!sessionsLoaded) {
    selector.innerHTML = `<option value="">Loading sessions...</option>`;
    return;
  }
  const visible = visibleSessionsForSelector();
  if (!visible.length) {
    const isModify = el("modeSelect")?.value === "modify";
    const targetSkill = (el("targetSkill")?.value || "").trim();
    const emptyLabel = isModify && targetSkill
      ? `No prior sessions for ${targetSkill}`
      : "No saved sessions";
    selector.innerHTML = `<option value="">${escapeHtml(emptyLabel)}</option>`;
    return;
  }
  selector.innerHTML = [
    `<option value="">Resume session...</option>`,
    ...visible.map((item) => formatSessionOption(item)),
  ].join("");
  if (session?.id) selector.value = session.id;
}

function formatSessionOption(item) {
  const rawTitle = String(item.title || item.target_skill_id || "Untitled session").trim();
  const title = rawTitle.length > 48 ? `${rawTitle.slice(0, 48)}...` : rawTitle;
  const mode = String(item.mode || "").toLowerCase() === "modify" ? "MODIFY" : "NEW";
  const kind = String(item.skill_kind || "capability").toUpperCase();
  const stage = normalizeStage(item.current_stage);
  const msgs = Number(item.message_count) || 0;
  const mats = Number(item.material_count) || 0;
  const meta = [];
  if (msgs) meta.push(`${msgs} msg${msgs === 1 ? "" : "s"}`);
  if (mats) meta.push(`${mats} material${mats === 1 ? "" : "s"}`);
  const parts = [`[${mode}]`, `[${kind}]`, title, stage];
  if (meta.length) parts.push(meta.join(", "));
  parts.push(formatSessionTime(item.updated_at));
  return `<option value="${escapeHtml(item.id)}" data-mode="${escapeHtml(mode)}" title="${escapeHtml(rawTitle)}">${escapeHtml(parts.join(" • "))}</option>`;
}

function formatSessionTime(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function renderGuide() {
  const stage = currentStage();
  const group = currentGroup();
  const meta = groupMeta[group];
  const subhint = SUBSTAGE_LABELS[stage] && stage !== group ? ` - ${SUBSTAGE_LABELS[stage]}` : "";
  const stageTitleEl = el("stageTitle");
  if (stageTitleEl) stageTitleEl.textContent = `${meta.title}${subhint}`;
  const stageDescriptionEl = el("stageDescription");
  if (stageDescriptionEl) stageDescriptionEl.textContent = meta.description;
  const nextActionEl = el("nextAction");
  if (nextActionEl) nextActionEl.textContent = deriveNextAction(stage, meta.next);
  el("messageInput").placeholder = messagePlaceholder(stage);
}

function deriveNextAction(stage, fallback) {
  if (!session) return fallback;
  const group = STAGE_TO_GROUP[stage] || "PREPARE";
  const hasDraft = Boolean(session.current_skill?.skill_md);
  const choices = choiceProgress();
  if (choices.total) {
    if (choices.remaining) return `Answer confirmation questions: ${choices.answered}/${choices.total} answered, ${choices.remaining} remaining.`;
    return `All ${choices.total} confirmation questions answered. Sending them now.`;
  }
  if (group === "PREPARE" && stage === "INTAKE" && attachedMaterials.length) return "Send your message to include the attached materials.";
  if (stage === "DRAFT" && hasDraft) return "Review files in the Files tab. Accept the draft when ready.";
  if (group === "REFINE" && hasDraft) return "Request a focused change or run selection tests.";
  if (group === "TEST" && session.test_runs?.length) return "Review failures. Iterate if hit rates are not good enough.";
  return fallback;
}

function choiceProgress() {
  const pending = currentPendingQuestionCalls();
  const answered = [...pendingChoiceAnswers.keys()].filter((callId) => pending.some((call) => call.call_id === callId)).length;
  return {
    total: pending.length,
    answered,
    remaining: Math.max(0, pending.length - answered),
  };
}

function currentPendingQuestionCalls() {
  const fromDom = [...pendingQuestionCalls.values()].filter((call) => {
    const node = document.querySelector(`[data-question-id="${call.call_id}"]`);
    return node && !node.classList.contains("resolved");
  });
  if (fromDom.length) return fromDom;
  return (session?.pending_tool_calls || []).filter((call) => call.tool === "ask_user_input");
}

function choiceProgressText(progress = choiceProgress()) {
  if (!progress.total) return "";
  return `${progress.answered}/${progress.total} answered · ${progress.remaining} remaining`;
}

function renderQuestionQueueStatus() {
  const node = el("questionQueueStatus");
  if (!node) return;
  const progress = choiceProgress();
  if (!progress.total) {
    node.classList.add("hidden");
    node.innerHTML = "";
    return;
  }
  node.classList.remove("hidden");
  node.innerHTML = `<strong>Confirmation queue</strong><span>${escapeHtml(choiceProgressText(progress))}</span>`;
}

function updateChoiceCardMeta() {
  const progress = choiceProgress();
  document.querySelectorAll(".choice-card[data-question-block]:not(.resolved) .choice-progress").forEach((meta) => {
    meta.textContent = choiceProgressText(progress);
  });
  renderQuestionQueueStatus();
  renderGuide();
}

function setLlmStatus(data) {
  const status = el("llmStatus");
  const text = el("llmStatusText");
  if (!status || !text) return;
  if (!data || data.status === "completed") {
    status.classList.add("hidden");
    text.textContent = "Thinking";
    return;
  }
  if (data.status === "failed") {
    status.classList.remove("hidden");
    status.classList.add("failed-status");
    text.textContent = `Model failed: ${data.model || "unknown"}`;
    window.setTimeout(() => {
      status.classList.add("hidden");
      status.classList.remove("failed-status");
    }, 5000);
    return;
  }
  status.classList.remove("hidden", "failed-status");
  text.textContent = `Thinking with ${data.model || "model"} (${data.stage || "?"})`;
}

function setTestStatus(data) {
  const status = el("llmStatus");
  const text = el("llmStatusText");
  if (!status || !text) return;
  if (!data || data.status === "completed") {
    status.classList.add("hidden");
    text.textContent = "Calling model";
    return;
  }
  if (data.status === "failed") {
    status.classList.remove("hidden");
    status.classList.add("failed-status");
    text.textContent = "Skill-selection test failed";
    window.setTimeout(() => {
      status.classList.add("hidden");
      status.classList.remove("failed-status");
    }, 5000);
    return;
  }
  status.classList.remove("hidden", "failed-status");
  text.textContent = "Running skill-selection tests";
}

// C5: turn references to SKILL.md / context tabs / checklist steps inside agent
// chat into clickable links that jump to that tab/step.
const CHAT_JUMP_TEST = /(SKILL\.md|Agent Graph|Routing & uniqueness|Skill definition|Variables verified|\bMaterials\b|\bChecklist\b|\bTests\b)/;
const CHAT_JUMP_PATTERN = new RegExp(CHAT_JUMP_TEST.source, "g");
function jumpTargetFor(text) {
  switch (text) {
    case "SKILL.md": return { tab: "files" };
    case "Agent Graph": return { tab: "agent" };
    case "Materials": return { tab: "materials" };
    case "Tests": return { tab: "tests" };
    case "Checklist": return { tab: "checklist" };
    case "Skill definition": return { tab: "checklist", checkpoint: "definition_clear" };
    case "Routing & uniqueness": return { tab: "checklist", checkpoint: "routing_uniqueness_confirmed" };
    case "Variables verified": return { tab: "checklist", checkpoint: "variables_ok" };
    default: return { tab: "checklist" };
  }
}
function enhanceChatJumpLinks(root) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !CHAT_JUMP_TEST.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
      let p = node.parentElement;
      while (p && p !== root) {
        const t = p.tagName;
        if (t === "A" || t === "CODE" || t === "PRE") return NodeFilter.FILTER_REJECT;
        p = p.parentElement;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let cur;
  while ((cur = walker.nextNode())) nodes.push(cur);
  nodes.forEach((node) => {
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    CHAT_JUMP_PATTERN.lastIndex = 0;
    while ((m = CHAT_JUMP_PATTERN.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const target = jumpTargetFor(m[0]);
      const a = document.createElement("a");
      a.className = "chat-jump";
      a.href = "#";
      a.dataset.jump = target.tab + (target.checkpoint ? "#" + target.checkpoint : "");
      a.textContent = m[0];
      frag.appendChild(a);
      last = m.index + m[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

function appendMessage(role, text) {
  document.querySelector(".chat-empty")?.remove();
  const cleaned = sanitizeChatText(text);
  if (!cleaned) return;
  const node = document.createElement("div");
  node.className = `message ${role}`;
  if (role === "assistant") {
    const label = document.createElement("span");
    label.className = "message-role";
    label.textContent = "assistant";
    const body = document.createElement("div");
    body.className = "md-body";
    body.innerHTML = mdRenderer.render(cleaned);
    enhanceChatJumpLinks(body);
    node.appendChild(label);
    node.appendChild(body);
  } else {
    node.textContent = `${role}: ${cleaned}`;
  }
  el("chatStream").appendChild(node);
  resetActivityGroup();
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
}

function sanitizeChatText(text) {
  const value = String(text || "").trim();
  if (!value) return "";
  // Only suppress a chat bubble when the text really is a full SKILL.md draft
  // (frontmatter + the two required H2 sections, or the model literally named the tool).
  const hasFrontmatter = /^---\s*\n[\s\S]*?\nname:\s*/m.test(value);
  const hasSkillSections =
    value.includes("## When to Use This Skill") &&
    value.includes("## API Reference / Key Patterns");
  const looksLikeDraft =
    value.includes("propose_skill_draft") ||
    (hasFrontmatter && hasSkillSections);
  if (!looksLikeDraft) return value;
  const hasStoredDraft = Boolean(session?.current_skill?.skill_md?.trim());
  if (hasStoredDraft) {
    return "SKILL.md is ready. Open the Files tab to review it.";
  }
  // Draft hasn't been stored yet (e.g. model streamed markdown without calling
  // propose_skill_draft). Surface the content so the user is not left blank.
  return value;
}

function messagePlaceholder(stage) {
  const group = STAGE_TO_GROUP[stage] || "PREPARE";
  if (stage === "VERIFY") return "Answer a confirmation card, or describe what should be corrected";
  if (group === "PREPARE") return "Describe the skill goal, paste API docs, old skill content, sample code, or rough notes";
  if (group === "REFINE") return "Describe the focused change you want, or type test to enter testing";
  if (group === "TEST") return "Type test to request another test run, or describe what failed";
  return "Send feedback or additional context";
}

const MATERIAL_KIND_OPTIONS = ["text", "code", "api_spec", "url", "file", "existing_skill"];
const editingMaterialIds = new Set();
let creatingMaterial = false;
let creatingMaterialDraft = { kind: "text", content: "" };

function materialAddBarHtml() {
  if (creatingMaterial) {
    const draftKind = creatingMaterialDraft.kind || "text";
    const options = MATERIAL_KIND_OPTIONS
      .map((k) => `<option value="${k}"${k === draftKind ? " selected" : ""}>${k}</option>`)
      .join("");
    return `<div class="material-new-editor" data-material-new>
      <div class="material-edit">
        <div class="material-edit-head"><strong>New material</strong>
          <select data-new-kind data-testid="material-kind" aria-label="Material kind">${options}</select>
        </div>
        <textarea data-new-content data-testid="material-input" rows="6" placeholder="Paste docs, code, an old skill, or a rough note">${escapeHtml(creatingMaterialDraft.content || "")}</textarea>
        <div class="material-actions">
          <button type="button" class="icon-button primary" data-action="save-new" data-testid="attach-material-button">Save</button>
          <button type="button" class="icon-button" data-action="cancel-new">Cancel</button>
        </div>
      </div>
    </div>`;
  }
  return `<button type="button" class="icon-button material-add-btn" data-action="new-material" data-testid="add-material-row-button">${svgIcon("i-paper-clip")}<span>+ Add material</span></button>`;
}

function renderMaterials() {
  const saved = (session && Array.isArray(session.materials)) ? session.materials : [];
  const pending = Array.isArray(attachedMaterials) ? attachedMaterials : [];
  const list = el("materialList");
  if (!saved.length && !pending.length) {
    list.innerHTML = `<div class="material-add-bar">${materialAddBarHtml()}</div><div class="empty-state">${svgIcon("i-paper-clip")}<strong>No materials in this session</strong><em>Press + Add material to attach docs, code, or a sample.</em></div>`;
    return;
  }
  const previewText = (content) => {
    const text = String(content || "").replace(/\s+/g, " ").trim();
    return text.length > 140 ? text.slice(0, 140) + "\u2026" : (text || "(empty)");
  };
  const renderSavedRow = (m, i) => {
    const id = m.id || "";
    if (id && editingMaterialIds.has(id)) {
      const options = MATERIAL_KIND_OPTIONS
        .map((k) => `<option value="${k}"${k === (m.kind || "text") ? " selected" : ""}>${k}</option>`)
        .join("");
      return `<tr class="material-item material-item-editing" data-source="saved" data-material-id="${escapeHtml(id)}" data-testid="saved-material-item">
        <td colspan="4">
          <div class="material-edit">
            <div class="material-edit-head"><strong>${i + 1}. Editing material</strong>
              <select data-edit-kind aria-label="Material kind">${options}</select>
            </div>
            <textarea data-edit-content rows="6">${escapeHtml(m.content || "")}</textarea>
            <div class="material-actions">
              <button type="button" class="icon-button primary" data-action="save" data-material-id="${escapeHtml(id)}" data-testid="save-material-button">Save</button>
              <button type="button" class="icon-button" data-action="cancel" data-material-id="${escapeHtml(id)}" data-testid="cancel-material-button">Cancel</button>
            </div>
          </div>
        </td>
      </tr>`;
    }
    const editAttrs = id ? `data-action="edit" data-material-id="${escapeHtml(id)}"` : "data-action=\"edit\" disabled";
    const deleteAttrs = id ? `data-action="delete" data-material-id="${escapeHtml(id)}"` : "data-action=\"delete\" disabled";
    const expandAttrs = id ? `data-action="expand" data-material-id="${escapeHtml(id)}"` : "data-action=\"expand\" disabled";
    return `<tr class="material-item" data-source="saved" data-material-id="${escapeHtml(id)}" data-testid="saved-material-item">
      <td class="material-cell-index">${i + 1}</td>
      <td class="material-cell-kind"><span class="material-kind-tag">${escapeHtml(m.kind || "text")}</span></td>
      <td class="material-cell-content"><span class="material-preview">${escapeHtml(previewText(m.content))}</span></td>
      <td class="material-cell-actions">
        <button type="button" class="icon-only-button" title="Enlarge / view full content" ${expandAttrs}>${svgIcon("i-expand")}</button>
        <button type="button" class="icon-only-button" title="Edit material" ${editAttrs} data-testid="edit-material-button">${svgIcon("i-adjustments")}</button>
        <button type="button" class="icon-only-button danger" title="Delete material" ${deleteAttrs} data-testid="delete-material-button">${svgIcon("i-x")}</button>
      </td>
    </tr>`;
  };
  const renderPendingRow = (m, i) => `<tr class="material-item material-item-pending" data-source="pending" data-pending-index="${i}">
      <td class="material-cell-index">${i + 1}</td>
      <td class="material-cell-kind"><span class="material-kind-tag">${escapeHtml(m.kind || "text")}</span></td>
      <td class="material-cell-content"><span class="material-preview">${escapeHtml(previewText(m.content))}</span></td>
      <td class="material-cell-actions">
        <button type="button" class="icon-only-button" title="Enlarge / view full content" data-action="expand" data-pending-index="${i}">${svgIcon("i-expand")}</button>
      </td>
    </tr>`;
  const materialTable = (rowsHtml) => `<table class="material-table"><thead><tr><th class="material-cell-index">#</th><th class="material-cell-kind">Kind</th><th>Content</th><th class="material-cell-actions">Actions</th></tr></thead><tbody>${rowsHtml}</tbody></table>`;
  const sections = [];
  if (saved.length) {
    sections.push(`<div class="material-section" data-section="saved"><h3 class="material-section-title">${svgIcon("i-paper-clip")} Saved in this session <span class="material-count" data-testid="saved-material-count">${saved.length}</span></h3>${materialTable(saved.map((m, i) => renderSavedRow(m, i)).join(""))}</div>`);
  }
  if (pending.length) {
    sections.push(`<div class="material-section" data-section="pending"><h3 class="material-section-title">${svgIcon("i-paper-clip")} Will send next <span class="material-count" data-testid="pending-material-count">${pending.length}</span></h3>${materialTable(pending.map((m, i) => renderPendingRow(m, i)).join(""))}</div>`);
  }
  list.innerHTML = `<div class="material-add-bar">${materialAddBarHtml()}</div>` + sections.join("");
}

// Four merged checkpoints, in the top-to-bottom confirmation order used in PREPARE.
const PREPARE_CHECKPOINT_LABELS = {
  definition_clear:     { title: "Skill definition",     hint: "Goal, input/data sources, key capabilities, and neighbor skills (asked one at a time)." },
  routing_uniqueness_confirmed: { title: "Routing & uniqueness", hint: "Neighbor skills, description contrast, mutual-exclusion check, When NOT to Use, and routing samples." },
  variables_ok:         { title: "Variables verified",   hint: "ACA environment variables, OBO token scopes, per-query runtime inputs, and the verified actor identity." },
  delegation_ok:        { title: "Delegation confirmed", hint: "Child skills, host capabilities, credentials payloads, and handshakes." },
};

function prepareCheckpointKeys() {
  return session?.skill_kind === "scenario"
    ? ["definition_clear", "routing_uniqueness_confirmed", "delegation_ok"]
    : ["definition_clear", "routing_uniqueness_confirmed", "variables_ok"];
}

function prepareCheckpointEvidence(key) {
  const brief = session?.prepare_brief || {};
  const stored = brief.verify_evidence?.[key];
  if (stored && String(stored).trim()) return String(stored).trim();
  const u = brief.understanding || {};
  const r = brief.research || {};
  if (key === "definition_clear") {
    const bits = [];
    if (u.skill_goal) bits.push(`Skill goal: ${u.skill_goal}`);
    if (Array.isArray(u.input_sources) && u.input_sources.length) bits.push(`Data sources: ${u.input_sources.join("; ")}`);
    if (Array.isArray(u.key_capabilities) && u.key_capabilities.length) bits.push(`Capabilities: ${u.key_capabilities.join("; ")}`);
    return bits.join("\n");
  }
  if (key === "routing_uniqueness_confirmed") {
    const bits = [];
    if (u.differentiation) bits.push(`Differentiation: ${u.differentiation}`);
    if (Array.isArray(u.out_of_scope) && u.out_of_scope.length) bits.push(`Out of scope: ${u.out_of_scope.join("; ")}`);
    const overlaps = Array.isArray(r.existing_skills_overlap) ? r.existing_skills_overlap : [];
    if (overlaps.length) bits.push(`Compared: ${overlaps.slice(0, 5).map((o) => `${o.name} (sim ${(o.similarity || 0).toFixed(2)})`).join(", ")}`);
    else bits.push("No similar existing skill detected.");
    return bits.join("\n");
  }
  if (key === "variables_ok") {
    const aca = session?.aca_env_result;
    if (aca && Array.isArray(aca.variables)) return `${aca.variables.length} ACA variable(s) reviewed`;
  }
  if (key === "delegation_ok") {
    const children = Array.isArray(session?.children) ? session.children : [];
    return children.length ? `Children: ${children.join(", ")}` : "No child skills selected.";
  }
  return "";
}

// Concise, human-readable summary of what the agent currently understands for a
// checkpoint, derived from the Prepare Brief (NOT the raw stored evidence text).
// Users only need to see the agent's cognition of each field, not a transcript.
function prepareCheckpointCognition(key) {
  const brief = session?.prepare_brief || {};
  const u = brief.understanding || {};
  if (key === "routing_uniqueness_confirmed") {
    const bits = [];
    if (u.differentiation) bits.push(`<li><strong>Differentiation:</strong> ${escapeHtml(u.differentiation)}</li>`);
    if (Array.isArray(u.out_of_scope) && u.out_of_scope.length) bits.push(`<li><strong>Out of scope:</strong> ${escapeHtml(u.out_of_scope.join("; "))}</li>`);
    if (Array.isArray(u.neighbor_skills) && u.neighbor_skills.length) bits.push(`<li><strong>Neighbors:</strong> ${escapeHtml(u.neighbor_skills.map((n) => n.skill).filter(Boolean).join(", "))}</li>`);
    return bits.length ? `<ul class="cognition-list">${bits.join("")}</ul>` : "";
  }
  return "";
}

function renderDefinitionFieldsPanel(activeSub = null) {
  const u = session?.prepare_brief?.understanding || {};
  const fields = [
    { key: "skill_goal", label: "Skill goal", hint: "what this skill does (read by a coding agent)", value: u.skill_goal, list: false },
    { key: "input_sources", label: "Input / data sources", hint: "static data sources the skill reads (not per-query values)", value: u.input_sources, list: true },
    { key: "key_capabilities", label: "Key capabilities", hint: "key_capabilities", value: u.key_capabilities, list: true },
  ];
  const rows = fields.map((f) => {
    const filled = f.list ? (Array.isArray(f.value) && f.value.length) : Boolean(String(f.value || "").trim());
    let body;
    if (!filled) {
      body = `<span class="definition-field-empty">Not confirmed yet</span>`;
    } else if (f.list) {
      body = `<ul class="definition-field-list">${f.value.map((v) => `<li>${escapeHtml(String(v))}</li>`).join("")}</ul>`;
    } else {
      body = `<span class="definition-field-value">${escapeHtml(String(f.value))}</span>`;
    }
    return `<div class="definition-field ${filled ? "filled" : "empty"}${activeSub === f.key ? " active-sub" : ""}" data-field="${escapeHtml(f.key)}">
      <div class="definition-field-head"><strong>${escapeHtml(f.label)}</strong><code>${escapeHtml(f.hint)}</code></div>
      ${body}
    </div>`;
  }).join("");
  return `<div class="definition-fields">${rows}</div>`;
}

function briefNeighbors() {
  const u = session?.prepare_brief?.understanding || {};
  return Array.isArray(u.neighbor_skills) ? u.neighbor_skills : [];
}

function neighborRowHtml(n = {}) {
  return `<div class="nb-row">
      <input class="nb-input nb-skill" type="text" value="${escapeHtml(String(n.skill || ""))}" placeholder="neighbor-skill-name" />
      <input class="nb-input nb-axis" type="text" value="${escapeHtml(String(n.axis || ""))}" placeholder="distinguishing axis (feeds description contrast)" />
      <input class="nb-input nb-scenario" type="text" value="${escapeHtml(String(n.scenario || ""))}" placeholder="neighbor scenario (feeds When NOT to Use + negative sample)" />
      <button type="button" class="icon-button spl-del" data-nb-del title="Remove">\u00d7</button>
    </div>`;
}

function renderNeighborSkillsPanel() {
  const neighbors = briefNeighbors();
  const rows = (neighbors.length ? neighbors : [{}]).map((n) => neighborRowHtml(n)).join("");
  return `<div class="neighbors-editor" data-neighbors-editor>
    <div class="nb-row col-head-row">
      <span class="nb-input col-head">Skill name</span>
      <span class="nb-input col-head">Distinguishing axis (vs. this skill)</span>
      <span class="nb-input col-head">Scenario (a query that would wrongly hit this skill)</span>
      <span class="col-head-del"></span>
    </div>
    <div class="nb-rows" data-nb-rows>${rows}</div>
    <button type="button" class="spl-add-btn" data-nb-add>+ Add neighbor</button>
    <div class="spl-actions">
      <button type="button" class="icon-button primary" data-nb-save>Save neighbors</button>
      <span class="spl-save-status" data-nb-status></span>
    </div>
  </div>`;
}

function collectNeighborsFromUI() {
  const editor = document.querySelector("[data-neighbors-editor]");
  if (!editor) return null;
  return [...editor.querySelectorAll(".nb-row")]
    .map((row) => ({
      skill: row.querySelector(".nb-skill")?.value.trim() || "",
      axis: row.querySelector(".nb-axis")?.value.trim() || "",
      scenario: row.querySelector(".nb-scenario")?.value.trim() || "",
    }))
    .filter((n) => n.skill);
}

// Diff the neighbor list (keyed by skill name) so we can tell the agent exactly
// what the user added / removed / changed -- the neighbor list drives the
// description contrast, the When NOT to Use lines, and the negative samples.
function diffNeighbors(before, after) {
  const byName = (arr) => new Map((Array.isArray(arr) ? arr : [])
    .map((n) => [String(n?.skill || "").trim(), n])
    .filter(([k]) => k));
  const b = byName(before);
  const a = byName(after);
  const added = [];
  const modified = [];
  const removed = [];
  a.forEach((n, name) => {
    const o = b.get(name);
    if (!o) { added.push(name); return; }
    if (String(o.axis || "") !== String(n.axis || "") || String(o.scenario || "") !== String(n.scenario || "")) {
      modified.push(name);
    }
  });
  b.forEach((_o, name) => { if (!a.has(name)) removed.push(name); });
  const parts = [];
  if (added.length) parts.push(`added ${added.join(", ")}`);
  if (removed.length) parts.push(`removed ${removed.join(", ")}`);
  if (modified.length) parts.push(`changed ${modified.join(", ")}`);
  return { added, removed, modified, changed: Boolean(parts.length), summary: parts.join("; ") };
}

async function saveBriefNeighbors() {
  if (!session) return;
  const neighbor_skills = collectNeighborsFromUI();
  if (neighbor_skills == null) return;
  const before = briefNeighbors();
  // Auto-detect what changed BEFORE the save so we can tell the agent exactly.
  const diff = diffNeighbors(before, neighbor_skills);
  const status = document.querySelector("[data-nb-status]");
  if (status) status.textContent = "Saving\u2026";
  try {
    session = await updateNeighbors(session.id, { neighbor_skills, revisit: diff.changed });
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appLog("save neighbors failed: " + err);
    return;
  }
  // The neighbor list is the single source for the routing block and everything
  // downstream depends on it. When it actually changes, the backend re-opens
  // routing + the dependent blocks (now pending) and we hand control to the
  // agent to re-review them step by step.
  if (diff.changed && !isSending) {
    appendConversationStatus(`Neighbor skills updated (${diff.summary}). Asking the agent to re-review.`);
    await sendChatPayload(
      `I changed the neighbor skills list (${diff.summary}). Because the routing block and the blocks after it depend on the neighbors, please RE-REVIEW starting from routing: re-derive the description contrast, the When NOT to Use routing lines, and the negative samples, proactively summarize and propose the neighbor backlink edits, and then walk me through re-confirming routing and every dependent downstream block (variables) STEP BY STEP -- one checkpoint at a time, waiting for my confirmation on each.`,
      [],
    );
    persistSessionState();
  }
}
function sessionNeighborEdits() {
  return Array.isArray(session?.neighbor_edits) ? session.neighbor_edits : [];
}

// All neighbor skill names that can be edited: the confirmed neighbor_skills
// single source, plus any that already have an edit card.
function editableNeighborNames() {
  const names = [];
  const u = session?.prepare_brief?.understanding || {};
  (Array.isArray(u.neighbor_skills) ? u.neighbor_skills : []).forEach((n) => {
    const s = String(n?.skill || "").trim();
    if (s && !names.includes(s)) names.push(s);
  });
  sessionNeighborEdits().forEach((ne) => {
    const s = String(ne?.skill_name || "").trim();
    if (s && !names.includes(s)) names.push(s);
  });
  return names;
}

function neighborEditCardHtml(skill, ne) {
  if (!ne) {
    // No edit started yet -- offer to open the editor (loads current SKILL.md).
    return `<div class="nbedit-card" data-nbedit="${escapeHtml(skill)}">
      <div class="nbedit-head"><strong>${escapeHtml(skill)}</strong><span class="nbedit-status">not opened</span></div>
      <div class="nbedit-controls">
        <button type="button" class="icon-button" data-nbedit-open="${escapeHtml(skill)}">Edit this skill</button>
        <span class="spl-save-status" data-nbedit-statustext="${escapeHtml(skill)}"></span>
      </div>
    </div>`;
  }
  const versions = Array.isArray(ne.versions) ? ne.versions : [];
  const selected = versions.find((v) => v.version_id === ne.selected_version_id) || versions[versions.length - 1] || {};
  const dirty = ne.selected_version_id !== ne.saved_version_id;
  const options = versions.map((v) => {
    const tag = v.version_id === ne.saved_version_id ? " (saved)" : "";
    const when = v.created_at ? ` \u00b7 ${formatSessionTime(v.created_at)}` : "";
    return `<option value="${escapeHtml(v.version_id)}"${v.version_id === ne.selected_version_id ? " selected" : ""}>${escapeHtml(v.label || v.origin)}${escapeHtml(when)}${tag}</option>`;
  }).join("");
  const selectedMd = String(selected.skill_md || "");
  return `<div class="nbedit-card" data-nbedit="${escapeHtml(skill)}">
      <div class="nbedit-head">
        <strong>${escapeHtml(skill)}</strong>
        <span class="nbedit-status ${dirty ? "dirty" : "saved"}">${dirty ? "unsaved" : "synced"}</span>
      </div>
      <div class="nbedit-controls">
        <label>Version <select class="nbedit-select" data-nbedit-select="${escapeHtml(skill)}">${options}</select></label>
        <button type="button" class="icon-button primary" data-nbedit-save="${escapeHtml(skill)}"${dirty ? "" : " disabled"}>Save skill</button>
        <span class="spl-save-status" data-nbedit-statustext="${escapeHtml(skill)}"></span>
      </div>
      <label class="nbedit-editor-label">Edit the SKILL.md on the left; a live preview (YAML front matter highlighted) renders on the right. Drag the middle divider to resize the panes, or drag the textarea corner to grow its height. Save as a new version when done.</label>
      <div class="nbedit-split" data-nbedit-split="${escapeHtml(skill)}">
        <div class="nbedit-editor-pane">
          <textarea class="nbedit-textarea" data-nbedit-textarea="${escapeHtml(skill)}" spellcheck="false">${escapeHtml(selectedMd)}</textarea>
        </div>
        <div class="nbedit-resizer" data-nbedit-resizer="${escapeHtml(skill)}" title="Drag to resize"></div>
        <div class="nbedit-preview md-body" data-nbedit-preview="${escapeHtml(skill)}">${neighborPreviewHtml(selectedMd)}</div>
      </div>
      <div class="nbedit-controls">
        <button type="button" class="icon-button" data-nbedit-savever="${escapeHtml(skill)}">Save as new version</button>
      </div>
    </div>`;
}

function renderNeighborEditsPanel() {
  const names = editableNeighborNames();
  if (!names.length) return "";
  const editByName = new Map(sessionNeighborEdits().map((ne) => [ne.skill_name, ne]));
  const cards = names.map((skill) => neighborEditCardHtml(skill, editByName.get(skill))).join("");
  return `<div class="neighbor-edits" data-neighbor-edits>
    ${cards}
  </div>`;
}

async function openNeighborEditor(skill) {
  if (!session) return;
  const status = document.querySelector(`[data-nbedit-statustext="${CSS.escape(skill)}"]`);
  if (status) status.textContent = "Loading\u2026";
  try {
    session = await openNeighborEdit(session.id, skill);
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Could not load skill";
    appLog("open neighbor edit failed: " + err);
  }
}

async function saveNeighborVersion(skill) {
  if (!session) return;
  const ta = document.querySelector(`[data-nbedit-textarea="${CSS.escape(skill)}"]`);
  if (!ta) return;
  const skill_md = ta.value;
  const status = document.querySelector(`[data-nbedit-statustext="${CSS.escape(skill)}"]`);
  if (status) status.textContent = "Saving version\u2026";
  try {
    session = await proposeNeighborEdit(session.id, skill, { skill_md, origin: "user_edited", label: "Manual edit" });
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appLog("save neighbor version failed: " + err);
  }
}

async function selectNeighborVersion(skill, versionId) {
  if (!session) return;
  try {
    session = await selectNeighborEdit(session.id, skill, { version_id: versionId });
    persistSessionState();
    renderSession();
  } catch (err) {
    appLog("select neighbor version failed: " + err);
  }
}

// Render a neighbor SKILL.md into preview HTML: YAML front matter is shown as a
// highlighted code block (same style as the main draft preview) and the body is
// rendered as markdown. Reuses splitFrontmatter so the two previews stay in sync.
function neighborPreviewHtml(markdownText) {
  if (!String(markdownText || "").trim()) {
    return `<div class="empty-state">${svgIcon("i-document")}<strong>No SKILL.md</strong><em>Type on the left to preview.</em></div>`;
  }
  const { frontmatter, body } = splitFrontmatter(markdownText);
  const frontmatterHtml = frontmatter
    ? `<section class="markdown-frontmatter"><strong>YAML front matter</strong><pre><code class="hljs language-yaml">${hljs ? hljs.highlight(frontmatter, { language: "yaml", ignoreIllegals: true }).value : escapeHtml(frontmatter)}</code></pre></section>`
    : "";
  const bodyHtml = mdRenderer ? mdRenderer.render(body || "") : `<pre>${escapeHtml(body || "")}</pre>`;
  return `${frontmatterHtml}${bodyHtml}`;
}

// Live-update the right-hand preview as the user types in the editor textarea.
function refreshNeighborPreview(skill) {
  const card = document.querySelector(`.nbedit-card[data-nbedit="${CSS.escape(skill)}"]`);
  if (!card) return;
  const ta = card.querySelector("[data-nbedit-textarea]");
  const pv = card.querySelector("[data-nbedit-preview]");
  if (ta && pv) pv.innerHTML = neighborPreviewHtml(ta.value);
}

async function saveNeighborEdit(skill) {
  if (!session) return;
  const status = document.querySelector(`[data-nbedit-statustext="${CSS.escape(skill)}"]`);
  if (status) status.textContent = "Saving\u2026";
  try {
    session = await saveNeighborEditApi(session.id, skill);
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appLog("save neighbor edit failed: " + err);
  }
}

function renderPeerSkillsPanel() {
  const research = session?.prepare_brief?.research || {};
  const peers = Array.isArray(research.peer_skills) ? research.peer_skills : [];
  const status = research.peer_skills_status || "skipped";
  const error = String(research.peer_skills_error || "").trim();
  const refreshBtn = `<button type="button" class="peer-skills-refresh-btn" data-refresh-peer-skills>Refresh peer skills</button>`;
  if (!peers.length && (error || status === "skipped" || status === "failed")) {
    if (error) {
      return `<details class="env-details peer-skills-panel" open><summary>Peer skills <span class="env-section-count">!</span></summary><div class="sample-error"><strong>Could not load your accessible skills.</strong><br />${escapeHtml(error)}</div>${refreshBtn}</details>`;
    }
    if (status === "failed") {
      return `<details class="env-details peer-skills-panel" open><summary>Peer skills <span class="env-section-count">!</span></summary><div class="sample-error">Could not load your accessible skills.</div>${refreshBtn}</details>`;
    }
    return `<details class="env-details peer-skills-panel"><summary>Peer skills <span class="env-section-count">0</span></summary><div class="empty-state">${svgIcon("i-info")}<strong>Peer skills not loaded yet</strong><em>Refresh after sign-in or credential changes.</em></div>${refreshBtn}</details>`;
  }
  const rows = peers.map((peer) => {
    const name = escapeHtml(String(peer.name || "(unnamed)"));
    const desc = escapeHtml(String(peer.description || "")) || "<em>(no description)</em>";
    // Keep the boundary text available on hover without bloating the table.
    const wtu = String(peer.when_to_use || "").trim();
    const wnot = String(peer.when_not_to_use || "").trim();
    const tip = [wtu ? `When to use: ${wtu}` : "", wnot ? `When NOT to use: ${wnot}` : ""]
      .filter(Boolean)
      .join("\n\n");
    const title = tip ? ` title="${escapeHtml(tip)}"` : "";
    return `<tr${title}><td class="peer-skill-name"><code>${name}</code></td><td class="peer-skill-desc">${desc}</td></tr>`;
  }).join("");
  const table = peers.length
    ? `<table class="peer-skills-table"><thead><tr><th>Skill name</th><th>Description</th></tr></thead><tbody>${rows}</tbody></table>`
    : `<div class="empty-state">${svgIcon("i-info")}<strong>No accessible skills found</strong><em>You have no granted skills to compare against yet.</em></div>`;
  const note = peers.length
    ? "Skills you can already route to. Differentiation and the new skill's When NOT to Use router are built from these. Hover a row for its boundaries."
    : "No other accessible skills found.";
  const warning = error
    ? `<div class="sample-error"><strong>This list is incomplete.</strong><br />${escapeHtml(error)}</div>`
    : "";
  return `<details class="env-details peer-skills-panel"${error ? " open" : ""}>
    <summary>Peer skills (your accessible skills) <span class="env-section-count">${peers.length}</span></summary>
    <p class="env-hint">${escapeHtml(note)}</p>
    ${warning}
    ${refreshBtn}
    ${table}
  </details>`;
}

function renderAcaEnvPanel() {
  const aca = session?.aca_env_result || null;
  const error = session?.aca_env_error || "";
  const variables = Array.isArray(aca?.variables) ? aca.variables : [];
  const obo = aca?.architectural_config?.OBO_SCOPE_REGISTRY || {};
  const revision = aca?.revision ? ` \u00b7 ${escapeHtml(String(aca.revision))}` : "";
  const statusText = error
    ? "Lookup failed"
    : variables.length
      ? `${variables.length} variable(s) found${revision}`
      : "Not loaded yet";
  const body = error
    ? `<div class="sample-error">${escapeHtml(error)}</div>`
    : variables.length
      ? `<div class="env-chip-list">${variables.map((name) => `<code>${escapeHtml(String(name))}</code>`).join("")}</div>`
      : `<div class="empty-state">${svgIcon("i-info")}<strong>ACA variables not loaded</strong><em>They appear after the MCP lookup completes.</em></div>`;
  const oboBlock = Object.keys(obo).length
    ? `<details class="env-details"><summary>OBO token variables <span class="env-section-count">${Object.keys(obo).length}</span></summary>${renderObotokenTable(obo)}</details>`
    : "";
  return `<details class="env-details aca-env-panel">
    <summary>Fetched ACA environment <span class="env-section-count">${error ? "!" : variables.length}</span></summary>
    <p class="env-hint">${escapeHtml(statusText)} \u2014 reference list of what we read from ACA. Reuse these names when they match; flag any missing one as needs-to-be-added.</p>
    ${body}
    ${oboBlock}
  </details>`;
}

// Read the routing samples from the Prepare Brief (the source the agent writes),
// falling back to the legacy flat verify_checklist samples for older sessions.
function briefSamples() {
  const brief = session?.prepare_brief || {};
  const positive = Array.isArray(brief.positive_samples) ? brief.positive_samples : [];
  const negative = Array.isArray(brief.negative_samples) ? brief.negative_samples : [];
  if (!positive.length && !negative.length) {
    const legacy = currentTestSamples();
    return {
      positive: Array.isArray(legacy.positive) ? legacy.positive : [],
      negative: (Array.isArray(legacy.negative) ? legacy.negative : []).map((q) => ({ query: String(q), route_to_peer: "", why_not_this: "" })),
    };
  }
  return { positive, negative };
}

function positiveRowHtml(value = "") {
  return `<div class="spl-row spl-pos-row">
      <input class="spl-input spl-pos-query" type="text" value="${escapeHtml(String(value))}" placeholder="A real query that SHOULD route to this skill" />
      <button type="button" class="icon-button spl-del" data-spl-del title="Remove">\u00d7</button>
    </div>`;
}

function negativeRowHtml(n = {}) {
  return `<div class="spl-row spl-neg-row">
      <input class="spl-input spl-neg-query" type="text" value="${escapeHtml(String(n.query || ""))}" placeholder="Similar query that should route ELSEWHERE" />
      <input class="spl-input spl-neg-peer" type="text" value="${escapeHtml(String(n.route_to_peer || ""))}" placeholder="Route to peer skill" />
      <input class="spl-input spl-neg-why" type="text" value="${escapeHtml(String(n.why_not_this || ""))}" placeholder="Why NOT this skill" />
      <button type="button" class="icon-button spl-del" data-spl-del title="Remove">\u00d7</button>
    </div>`;
}

function renderSamplesPanel() {
  const { positive, negative } = briefSamples();
  const pos = positive.length ? positive : [""];
  const neg = negative.length ? negative : [{}];
  const posRows = pos.map((q) => positiveRowHtml(q)).join("");
  const negRows = neg.map((n) => negativeRowHtml(n)).join("");
  return `<div class="samples-editor" data-samples-editor>
    <div class="spl-group spl-positive">
      <div class="spl-group-head"><strong>Positive \u2014 should route HERE</strong></div>
      <p class="spl-hint">Fill in your own real queries that should select this skill. Cover different real intents \u2014 the more the better.</p>
      <div class="spl-row col-head-row">
        <span class="spl-input col-head">A query that should select THIS skill</span>
        <span class="col-head-del"></span>
      </div>
      <div class="spl-rows" data-spl-rows="positive">${posRows}</div>
      <button type="button" class="spl-add-btn" data-spl-add="positive">+ Add positive</button>
    </div>
    <div class="spl-group spl-negative">
      <div class="spl-group-head"><strong>Negative \u2014 should route to a PEER</strong></div>
      <p class="spl-hint">Agent-suggested similar queries that must route to a different skill. Edit, add, or remove freely.</p>
      <div class="spl-row col-head-row">
        <span class="spl-input spl-neg-query col-head">Query</span>
        <span class="spl-input spl-neg-peer col-head">Route to which peer</span>
        <span class="spl-input spl-neg-why col-head">Why NOT this skill</span>
        <span class="col-head-del"></span>
      </div>
      <div class="spl-rows" data-spl-rows="negative">${negRows}</div>
      <button type="button" class="spl-add-btn" data-spl-add="negative">+ Add negative</button>
    </div>
    <div class="spl-actions">
      <button type="button" class="icon-button primary" data-spl-save>Save samples</button>
      <span class="spl-save-status" data-spl-status></span>
    </div>
  </div>`;
}

// Four buckets by kind. aca_env and obo_token each carry an in_aca checkbox
// (whether it already exists / is registered) -- a deployment status that never
// changes the skill text. runtime is a per-query caller input. platform_identity
// is injected by the platform, so it has neither a deployment status nor a
// name the user may choose.
const VERIFIED_UPN_VAR = "EAA_VERIFIED_USER_UPN";
const VAR_BUCKETS = [
  { key: "aca_env", kind: "aca_env", label: "ACA environment variable", hint: "Read from the ACA environment at deploy time. Tick \u201cexists\u201d if it is already in ACA. Missing at runtime is a deployment error.", inAcaLabel: "exists in ACA" },
  { key: "obo_token", kind: "obo_token", label: "OBO token", hint: "Injected by the OBO exchange. Tick \u201cregistered\u201d if it is already in the OBO token registry. Missing means the OBO chain is broken.", inAcaLabel: "registered" },
  { key: "runtime", kind: "runtime", label: "Runtime (per-query)", hint: "A value that differs on every user query (resource id, file name, URL). Becomes a Required Input; when missing the skill emits [NEEDS_INFO] and asks the user.", inAcaLabel: "" },
  { key: "platform_identity", kind: "platform_identity", label: "Verified actor identity", hint: "Add this only when a downstream system needs the caller's UPN/email/alias as a STRING field. If it instead accepts an OBO token and derives the caller from it, use an OBO token \u2014 the token already is the identity. The platform injects this after the OBO exchange; nobody else may set it, and its absence aborts the run.", inAcaLabel: "" },
];

function briefVariables() {
  const brief = session?.prepare_brief || {};
  return Array.isArray(brief.variables) ? brief.variables : [];
}

function variableRowHtml(kind, v = null) {
  const required = v ? (v.required !== false) : true;
  if (kind === "runtime") {
    return `<div class="var-row var-row-runtime" data-var-kind="runtime">
        <input class="var-input var-name" type="text" value="${escapeHtml(String(v?.name || ""))}" placeholder="parameter_name" />
        <input class="var-input var-desc" type="text" value="${escapeHtml(String(v?.description || ""))}" placeholder="What it is / how to ask the user for it" />
        <input class="var-input var-example" type="text" value="${escapeHtml(String(v?.example || ""))}" placeholder="example value" />
        <label class="var-required"><input type="checkbox" class="var-req"${required ? " checked" : ""} /> required</label>
        <button type="button" class="icon-button spl-del" data-var-del title="Remove">\u00d7</button>
      </div>`;
  }
  if (kind === "platform_identity") {
    return `<div class="var-row var-row-identity" data-var-kind="platform_identity">
        <input class="var-input var-name" type="text" value="${VERIFIED_UPN_VAR}" readonly title="Fixed by the platform; this is the only identity variable a skill may read." />
        <input class="var-input var-desc" type="text" value="${escapeHtml(String(v?.description || ""))}" placeholder="Which downstream field needs the actor as a string?" />
        <button type="button" class="icon-button spl-del" data-var-del title="Remove">\u00d7</button>
      </div>`;
  }
  const bucket = VAR_BUCKETS.find((b) => b.kind === kind) || {};
  const inAca = v ? Boolean(v.in_aca) : false;
  return `<div class="var-row var-row-env" data-var-kind="${escapeHtml(kind)}">
      <input class="var-input var-name" type="text" value="${escapeHtml(String(v?.name || ""))}" placeholder="${kind === "obo_token" ? "OBO_TOKEN_VAR" : "ENV_VAR_NAME"}" />
      <input class="var-input var-desc" type="text" value="${escapeHtml(String(v?.description || ""))}" placeholder="${kind === "obo_token" ? "Scope / purpose" : "What it is used for"}" />
      <label class="var-inaca" title="Deployment status only; does not change the skill text."><input type="checkbox" class="var-in-aca" data-var-inaca${inAca ? " checked" : ""} /> ${escapeHtml(bucket.inAcaLabel || "exists")}</label>
      <label class="var-required"><input type="checkbox" class="var-req"${required ? " checked" : ""} /> required</label>
      <button type="button" class="icon-button spl-del" data-var-del title="Remove">\u00d7</button>
    </div>`;
}

function variablesForBucket(bucket) {
  const vars = briefVariables();
  return vars.filter((v) => (v.kind || "runtime") === bucket.kind);
}

function variableHeaderHtml(bucket) {
  if (bucket.kind === "runtime") {
    return `<div class="var-row col-head-row">
      <span class="var-input var-name col-head">Variable name</span>
      <span class="var-input var-desc col-head">Description (how to ask the user)</span>
      <span class="var-input var-example col-head">Example value</span>
      <span class="var-required col-head">Required</span>
      <span class="col-head-del"></span>
    </div>`;
  }
  if (bucket.kind === "platform_identity") {
    return `<div class="var-row col-head-row">
      <span class="var-input var-name col-head">Variable name (fixed)</span>
      <span class="var-input var-desc col-head">Why the downstream needs the actor string</span>
      <span class="col-head-del"></span>
    </div>`;
  }
  const existsLabel = bucket.kind === "obo_token" ? "Registered?" : "Exists?";
  return `<div class="var-row col-head-row">
    <span class="var-input var-name col-head">Variable name</span>
    <span class="var-input var-desc col-head">Description</span>
    <span class="var-inaca col-head">${escapeHtml(existsLabel)}</span>
    <span class="var-required col-head">Required</span>
    <span class="col-head-del"></span>
  </div>`;
}

function renderVariablesPanel(activeSub = null) {
  const sections = VAR_BUCKETS.map((b) => {
    const rows = variablesForBucket(b);
    // Every identity row already carries the fixed name, so an empty placeholder
    // would declare the variable for a skill that never asked for it.
    const seeded = rows.length ? rows : (b.kind === "platform_identity" ? [] : [null]);
    const rowsHtml = seeded.map((v) => variableRowHtml(b.kind, v)).join("");
    return `<div class="var-group${activeSub === b.key ? " active-sub" : ""}" data-var-group="${b.key}">
      <div class="var-group-head"><strong>${escapeHtml(b.label)}</strong></div>
      <p class="spl-hint">${escapeHtml(b.hint)}</p>
      ${variableHeaderHtml(b)}
      <div class="var-rows" data-var-rows="${b.kind}">${rowsHtml}</div>
      <button type="button" class="spl-add-btn" data-var-add="${b.kind}">+ Add</button>
    </div>`;
  }).join("");
  return `<div class="variables-editor" data-variables-editor>
    ${sections}
    <div class="spl-actions">
      <button type="button" class="icon-button primary" data-var-save>Save variables</button>
      <span class="spl-save-status" data-var-status></span>
    </div>
    ${renderAcaEnvPanel()}
  </div>`;
}

function delegationRowHtml(value = "") {
  const selected = String(value || "");
  const options = [
    `<option value="">Select a child skill</option>`,
    ...availableSkills.map((skill) => {
      const badge = skill.is_internal ? " [internal]" : "";
      return `<option value="${escapeHtml(skill.name)}"${skill.name === selected ? " selected" : ""}>${escapeHtml(skill.name)}${badge}</option>`;
    }),
  ].join("");
  return `<div class="delegation-row" data-testid="delegation-row">
    <select class="delegation-name">${options}</select>
    <button type="button" class="icon-only-button danger" data-delegation-del title="Remove child" aria-label="Remove child">${svgIcon("i-x")}</button>
  </div>`;
}

function renderDelegationPanel() {
  const children = Array.isArray(session?.children) ? session.children : [];
  const rows = (children.length ? children : [""]).map((child) => delegationRowHtml(child)).join("");
  const delegation = Array.isArray(session?.prepare_brief?.delegation)
    ? session.prepare_brief.delegation
    : [];
  const contracts = delegation.length
    ? `<dl class="delegation-contracts">${delegation.map((item) => {
        const key = item.credentials_key || "No credentials key recorded";
        const sections = Array.isArray(item.sections) ? item.sections.filter(Boolean) : [];
        const pointer = sections.length
          ? `<div class="delegation-sections">Fetches: ${sections.map((s) => `<code>${escapeHtml(s)}</code>`).join(", ")}</div>`
          : `<div class="delegation-sections">No section pointer recorded yet.</div>`;
        const operations = Array.isArray(item.operations) ? item.operations.filter(Boolean) : [];
        const ops = operations.length
          ? `<div class="delegation-sections">Drives: ${operations.map((o) => `<code>${escapeHtml(o)}</code>`).join(", ")}</div>`
          : "";
        return `<div><dt><code>${escapeHtml(item.child_skill || "(unnamed)")}</code></dt><dd>${escapeHtml(key)}${pointer}${ops}</dd></div>`;
      }).join("")}</dl>`
    : `<p class="spl-hint">The agent records credentials keys, section pointers, host capabilities, and handshakes after children are selected.</p>`;
  const delegated = new Set(delegation.map((item) => item.child_skill).filter(Boolean));
  const plain = children.filter((child) => child && !delegated.has(child));
  const internalNote = delegation.length
    ? `<p class="spl-hint">Saving marks only ${[...delegated].map((c) => `<code>${escapeHtml(c)}</code>`).join(", ")} as internal (hidden from the host catalog).${plain.length ? ` ${plain.map((c) => `<code>${escapeHtml(c)}</code>`).join(", ")} stay visible to everyone.` : ""}</p>`
    : "";
  return `<div class="delegation-editor" data-testid="delegation-panel">
    <div class="delegation-rows" data-delegation-rows>${rows}</div>
    <button type="button" class="spl-add-btn" data-delegation-add data-testid="add-child-button">+ Add skill</button>
    ${contracts}
    ${internalNote}
    <div class="spl-actions">
      <button type="button" class="icon-button primary" data-delegation-save data-testid="save-children-button">Save children</button>
      <span class="spl-save-status" data-delegation-status></span>
    </div>
  </div>`;
}

function collectChildrenFromUI() {
  return [...document.querySelectorAll("[data-delegation-rows] .delegation-name")]
    .map((node) => node.value.trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
}

async function saveBriefChildren() {
  if (!session) return;
  const status = document.querySelector("[data-delegation-status]");
  if (status) status.textContent = "Saving...";
  try {
    session = await updateSessionChildren(session.id, collectChildrenFromUI());
    topologyData = null;
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appendConversationStatus(`Could not save children: ${err.message}`, { failed: true });
  }
}

function collectSamplesFromUI() {
  const editor = document.querySelector("[data-samples-editor]");
  if (!editor) return null;
  const positive = [...editor.querySelectorAll('[data-spl-rows="positive"] .spl-pos-query')]
    .map((i) => i.value.trim())
    .filter(Boolean);
  const negative = [...editor.querySelectorAll('[data-spl-rows="negative"] .spl-neg-row')]
    .map((row) => ({
      query: row.querySelector(".spl-neg-query")?.value.trim() || "",
      route_to_peer: row.querySelector(".spl-neg-peer")?.value.trim() || "",
      why_not_this: row.querySelector(".spl-neg-why")?.value.trim() || "",
    }))
    .filter((n) => n.query);
  return { positive, negative };
}

function collectVariablesFromUI() {
  const editor = document.querySelector("[data-variables-editor]");
  if (!editor) return null;
  const out = [];
  editor.querySelectorAll("[data-var-rows]").forEach((rows) => {
    const kind = rows.getAttribute("data-var-rows");
    rows.querySelectorAll(".var-row").forEach((row) => {
      const name = row.querySelector(".var-name")?.value.trim() || "";
      if (!name) return;
      const required = row.querySelector(".var-req")?.checked ?? true;
      if (kind === "runtime") {
        out.push({
          name,
          kind: "runtime",
          in_aca: false,
          description: row.querySelector(".var-desc")?.value.trim() || "",
          example: row.querySelector(".var-example")?.value.trim() || "",
          required,
        });
      } else {
        out.push({
          name,
          kind,
          in_aca: row.querySelector(".var-in-aca")?.checked ?? false,
          description: row.querySelector(".var-desc")?.value.trim() || "",
          example: "",
          required,
        });
      }
    });
  });
  return out;
}

async function saveBriefSamples({ revisit = false } = {}) {
  if (!session) return;
  const payload = collectSamplesFromUI();
  if (!payload) return;
  const status = document.querySelector("[data-spl-status]");
  if (status) status.textContent = "Saving\u2026";
  try {
    session = await updateSamples(session.id, { ...payload, revisit });
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appLog("save samples failed: " + err);
    return;
  }
  // "Ask the agent to review" hands control back so it can validate positives,
  // derive negatives from the neighbor skills if needed, and tell the user
  // whether the description / checklist should change. "Just update" does not.
  if (revisit && !isSending) {
    const count = Array.isArray(payload.positive) ? payload.positive.length : 0;
    appendConversationStatus("Routing samples updated. Asking the agent to review.");
    await sendChatPayload(
      count
        ? `I've saved ${count} positive routing sample(s) plus my negatives. Please validate the positives against the key capabilities and, if the negative samples are still empty, derive them from the neighbor skills (don't overwrite negatives I've already edited), then tell me if the description or checklist should change.`
        : "I've revised my routing samples. Please review them, re-derive anything downstream if needed, and tell me if the description or checklist should change.",
      [],
    );
    persistSessionState();
  }
}

// Signature of the fields that actually change the SKILL.md (name/kind/desc/
// example/required) -- IGNORING in_aca, which is a deploy-only flag that never
// alters the skill text. Mirrors the backend `_variable_content_signature`.
function variableContentSig(v) {
  return JSON.stringify([
    String(v?.name || "").trim(),
    String(v?.kind || "runtime"),
    String(v?.description || "").trim(),
    String(v?.example || "").trim(),
    v?.required !== false,
  ]);
}

function diffVariables(before, after) {
  const byName = (arr) => new Map((Array.isArray(arr) ? arr : [])
    .map((v) => [String(v?.name || "").trim(), v])
    .filter(([k]) => k));
  const b = byName(before);
  const a = byName(after);
  const added = [];
  const modified = [];
  const removed = [];
  a.forEach((v, name) => {
    const o = b.get(name);
    if (!o) { added.push(name); return; }
    if (variableContentSig(o) !== variableContentSig(v)) modified.push(name);
  });
  b.forEach((_o, name) => { if (!a.has(name)) removed.push(name); });
  const parts = [];
  if (added.length) parts.push(`added ${added.join(", ")}`);
  if (modified.length) parts.push(`changed ${modified.join(", ")}`);
  if (removed.length) parts.push(`removed ${removed.join(", ")}`);
  return { changed: parts.length > 0, summary: parts.join("; ") || "no content change" };
}

async function saveBriefVariables() {
  if (!session) return;
  const variables = collectVariablesFromUI();
  if (variables == null) return;
  // Diff BEFORE the save so we can tell the agent exactly what changed. A pure
  // in_aca flip is ignored (deploy-only, no skill-text impact).
  const before = briefVariables();
  const diff = diffVariables(before, variables);
  const status = document.querySelector("[data-var-status]");
  if (status) status.textContent = "Saving\u2026";
  try {
    session = await updateVariables(session.id, { variables });
    persistSessionState();
    renderSession();
  } catch (err) {
    if (status) status.textContent = "Save failed";
    appLog("save variables failed: " + err);
    return;
  }
  // Variables feed the draft's '## Required Inputs' / '## Environment Variables'
  // / '## OBO Token Scopes' sections, so a real content change must flow into
  // the SKILL.md. When a draft already exists we hand control to the agent to
  // patch it -- mirroring the neighbors/samples flows. (in_aca-only edits stay
  // silent, matching the backend.)
  const hasDraft = !!String(session?.current_skill?.skill_md || "").trim();
  if (diff.changed && hasDraft && !isSending) {
    appendConversationStatus(`Variables updated (${diff.summary}). Asking the agent to sync the draft.`);
    await sendChatPayload(
      `I edited the skill variables (${diff.summary}). Please propose ONE narrow patch to sync the '## Required Inputs' (runtime variables), '## Environment Variables' (aca_env), '## OBO Token Scopes' (obo_token), and '## Skill 身分使用規範' (present only when a platform_identity variable exists) sections of the SKILL.md draft with my changes -- add/rename/remove lines and adjust required/description as needed, and don't rewrite unrelated parts.`,
      [],
    );
    persistSessionState();
  }
}

// Collapsed/expanded state for Verify-checklist sections and routing sub-panels,
// kept across re-renders (a re-render rebuilds the HTML, so <details open> alone
// would reset every time the user saves something).
const collapsedSections = new Set();
function isSectionOpen(id) { return !collapsedSections.has(id); }

// One collapsible, clearly-titled card for a routing sub-panel. Returns "" when
// the panel has no content so we never render an empty collapsible.
function routingSubpanel(id, title, hint, bodyHtml, activeSub = null) {
  if (!String(bodyHtml || "").trim()) return "";
  const cid = `routing:${id}`;
  const isActive = activeSub === cid;
  return `<details class="routing-subpanel${isActive ? " active-sub" : ""}" data-collapse-id="${escapeHtml(cid)}"${isSectionOpen(cid) || isActive ? " open" : ""}>
    <summary class="rsp-summary"><span class="rsp-title"><strong>${escapeHtml(title)}</strong>${hint ? `<span class="rsp-hint">${escapeHtml(hint)}</span>` : ""}</span></summary>
    <div class="rsp-body">${bodyHtml}</div>
  </details>`;
}

// C4: figure out which checkpoint the AGENT is currently working on, taken from
// what the chat says ("Step 2/3", or the checkpoint key in the question), so the
// "In progress" highlight follows the conversation -- not just the first pending
// item. Falls back to the first not-yet-confirmed checkpoint.
function prepareStepKey(step) {
  if (step === "1") return "definition_clear";
  if (step === "2") return "routing_uniqueness_confirmed";
  if (step === "3") return session?.skill_kind === "scenario" ? "delegation_ok" : "variables_ok";
  return null;
}
function detectCheckpointInText(text) {
  const s = String(text || "");
  for (const k of prepareCheckpointKeys()) {
    if (s.includes(k)) return k;
  }
  const m = s.match(/Step\s*([123])\s*\/\s*3/i);
  if (m) return prepareStepKey(m[1]);
  return null;
}
// A pending in-chat positive-samples card means the user is on the routing
// SAMPLES sub-step right now -- the strongest, most current signal.
function pendingPositiveSamplesCard() {
  try {
    for (const call of pendingQuestionCalls.values()) {
      if (call?.tool === "request_positive_samples") return true;
    }
  } catch (_e) { /* empty */ }
  return false;
}

function activeCheckpointKey(checks) {
  const keys = prepareCheckpointKeys();
  // 0) a pending positive-samples card is unambiguously the routing block.
  if (pendingPositiveSamplesCard()) return "routing_uniqueness_confirmed";
  // 1) the current pending question card is the strongest signal.
  try {
    for (const call of pendingQuestionCalls.values()) {
      const k = detectCheckpointInText(call?.args?.question || call?.args?.prompt);
      if (k) return k;
    }
  } catch (_e) { /* pendingQuestionCalls may be empty */ }
  // 2) the most recent assistant message that names a step / checkpoint.
  const conv = Array.isArray(session?.conversation) ? session.conversation : [];
  for (let i = conv.length - 1; i >= 0; i--) {
    if (String(conv[i]?.role || "").toLowerCase() !== "assistant") continue;
    const k = detectCheckpointInText(conv[i]?.content);
    if (k) return k;
  }
  // 3) fallback: first not-yet-confirmed checkpoint.
  return keys.find((k) => checks[k] !== true) || null;
}

// C4 (sub-level): the chat text the agent is currently acting on.
function activeChatText() {
  try {
    for (const call of pendingQuestionCalls.values()) {
      const q = call?.args?.question || call?.args?.prompt;
      if (q) return String(q);
    }
  } catch (_e) { /* empty */ }
  const conv = Array.isArray(session?.conversation) ? session.conversation : [];
  for (let i = conv.length - 1; i >= 0; i--) {
    if (String(conv[i]?.role || "").toLowerCase() === "assistant") return String(conv[i]?.content || "");
  }
  return "";
}

// Within the active checkpoint, which sub-item is the chat about? Used to
// highlight a single field / sub-panel, not just the whole checkpoint.
const SUBKEY_PATTERNS = {
  definition_clear: [
    { sub: "skill_goal", re: /skill[_ ]?goal/i },
    { sub: "input_sources", re: /input[_ ]?sources|data sources|data\/?source/i },
    { sub: "key_capabilities", re: /key[_ ]?capabilit/i },
  ],
  routing_uniqueness_confirmed: [
    { sub: "routing:nb", re: /neighbou?r/i },
    { sub: "routing:samples", re: /sample|positive|negative/i },
    { sub: "routing:nbedit", re: /backlink|when not to use|neighbou?r edit/i },
    { sub: "routing:peers", re: /peer skill/i },
  ],
  variables_ok: [
    { sub: "platform_identity", re: /platform[_ ]?identity|verified[_ ]?user[_ ]?upn|身分使用規範/i },
    { sub: "aca_env", re: /aca[_ ]?env|environment variable/i },
    { sub: "obo_token", re: /obo[_ ]?token/i },
    { sub: "runtime", re: /runtime|per[_ -]?query/i },
  ],
  delegation_ok: [
    { sub: "children", re: /child|delegat/i },
  ],
};
function activeSubKey(activeKey) {
  const patterns = SUBKEY_PATTERNS[activeKey];
  if (!patterns) return null;
  // A pending positive-samples card pins the highlight to the routing samples
  // sub-step (the user is filling samples right now).
  if (activeKey === "routing_uniqueness_confirmed" && pendingPositiveSamplesCard()) return "routing:samples";
  const text = activeChatText();
  // Pick the sub-step whose keyword appears LATEST in the chat text -- the most
  // recent topic wins, so "now fill positive samples" beats an earlier mention
  // of "neighbor". (A fixed first-match order made it lag on neighbor skills.)
  let best = null;
  let bestPos = -1;
  for (const p of patterns) {
    const re = new RegExp(p.re.source, "gi");
    let m;
    let last = -1;
    while ((m = re.exec(text)) !== null) {
      last = m.index;
      if (re.lastIndex === m.index) re.lastIndex += 1;
    }
    if (last > bestPos) { bestPos = last; best = p.sub; }
  }
  return best;
}

function renderPrepareChecklist() {
  // Do not wipe an in-progress edit (samples/variables inputs) on a background
  // re-render triggered by an agent state_update.
  const clNode = el("checklist");
  if (clNode && clNode.contains(document.activeElement) &&
      document.activeElement.matches && document.activeElement.matches("input, textarea, select")) {
    return;
  }
  const brief = session?.prepare_brief || {};
  const checks = brief.verify_checklist || {};
  const keys = prepareCheckpointKeys();
  // C4: the active step follows what the chat is currently asking (Step N/3 /
  // checkpoint key), falling back to the first not-yet-confirmed checkpoint.
  const activeKey = activeCheckpointKey(checks);
  const activeSub = activeSubKey(activeKey);
  const confirmedCount = keys.filter((k) => checks[k] === true).length;
  el("checklistSummary").textContent = `${confirmedCount}/${keys.length} confirmed - PREPARE checkpoint`;
  // Children sit above the checkpoints because they must be declared before the
  // routing step, which is what excludes them from the neighbor comparison.
  const childrenMissing = session?.skill_kind === "scenario"
    && !(Array.isArray(session?.children) && session.children.length);
  const childrenBlock = session?.skill_kind === "scenario"
    ? `<details class="check-item${childrenMissing ? " pending" : ""}" data-testid="scenario-children-panel" open>
        <summary class="check-item-summary">
          <span class="check-summary-main"><strong>Dependency whitelist (metadata.children)</strong></span>
          ${childrenMissing ? `<span class="check-status pending">${svgIcon("i-warning")} Required first</span>` : ""}
        </summary>
        <div class="check-body">
          <p class="spl-hint">Every skill this scenario's flow uses, not just the ones it delegates a payload to. The host treats this as a whitelist: a skill left out is simply never available, with no error message -- when in doubt, list it. Declare them before the routing step, since a declared skill is excluded from the neighbor comparison instead of competing with this one.</p>
          ${renderDelegationPanel()}
        </div>
      </details>`
    : "";
  el("checklist").innerHTML = childrenBlock + keys.map((key) => {
    const isConfirmed = checks[key] === true;
    const label = PREPARE_CHECKPOINT_LABELS[key];
    const evidence = prepareCheckpointEvidence(key);
    const updatedAt = brief.verify_updated_at?.[key] || "";
    const statusBadge = isConfirmed
      ? `<span class="check-status confirmed">${svgIcon("i-check-circle")} Confirmed</span>`
      : `<span class="check-status pending">${svgIcon("i-warning")} Pending</span>`;
    const cognition = prepareCheckpointCognition(key);
    const evidenceBlock = key === "definition_clear"
      ? renderDefinitionFieldsPanel(activeKey === "definition_clear" ? activeSub : null)
      : (cognition
          ? `<div class="check-cognition"><strong>What the agent understands</strong>${cognition}</div>`
          : "");
    const tsBlock = updatedAt ? `<span class="check-timestamp">${escapeHtml(updatedAt)}</span>` : "";
    const cid = `check:${key}`;
    const reviseBtn = isConfirmed
      ? `<button type="button" class="check-revise" data-revise-checkpoint="${escapeHtml(key)}" title="Mark this checkpoint for re-confirmation; downstream checkpoints become pending too.">Revise this checkpoint</button>`
      : "";
    const blockedByChildren = key === "routing_uniqueness_confirmed" && childrenMissing;
    const blockedBlock = blockedByChildren
      ? `<div class="check-gate-missing" data-testid="routing-blocked-note">${svgIcon("i-warning")}<div><strong>Declare the dependency whitelist first</strong><ul><li>The peer list this checkpoint compares against excludes every declared child, so confirming it now would compare this skill against the ones it delegates to. Confirmation is refused until at least one child is declared.</li></ul></div></div>`
      : "";
    const gateBlock = gateIssuesForCheckpoint(key).length
      ? `<div class="check-gate-missing">${svgIcon("i-warning")}<div><strong>Blocking DRAFT</strong><ul>${gateIssuesForCheckpoint(key).map((g) => `<li>${escapeHtml(g)}</li>`).join("")}</ul></div></div>`
      : "";
    const rsub = activeKey === "routing_uniqueness_confirmed" ? activeSub : null;
    const routingPanels = key === "routing_uniqueness_confirmed"
      ? `<div class="routing-subpanels">
          ${routingSubpanel("nb", "Neighbor skills (single source)", "1-3 similar existing skills. Each feeds the description contrast, the When NOT to Use routing line, and a derived negative sample.", renderNeighborSkillsPanel(), rsub)}
          ${routingSubpanel("samples", "Routing samples (positive / negative)", "Positive queries should select THIS skill; negatives must route to a peer.", renderSamplesPanel(), rsub)}
          ${routingSubpanel("nbedit", "Neighbor skill edits", "Optionally tighten a neighbor's description or add a backlink. Versioned; nothing is written until Save to Blob.", renderNeighborEditsPanel(), rsub)}
          ${renderPeerSkillsPanel()}
        </div>`
      : "";
    const variablesPanel = key === "variables_ok" ? renderVariablesPanel(activeKey === "variables_ok" ? activeSub : null) : "";
    return `<details class="check-item ${isConfirmed ? "confirmed" : "pending"}${key === activeKey ? " active" : ""}" data-checkpoint="${escapeHtml(key)}" data-collapse-id="${escapeHtml(cid)}"${isSectionOpen(cid) ? " open" : ""}>
      <summary class="check-item-summary">
        <span class="check-summary-main">
          <strong>${escapeHtml(label.title)}</strong>
        </span>
        ${statusBadge}
      </summary>
      <div class="check-body">
        ${reviseBtn}
        ${evidenceBlock}
        ${blockedBlock}
        ${gateBlock}
        ${routingPanels}
        ${variablesPanel}
        ${tsBlock}
      </div>
    </details>`;
  }).join("");

}

function renderChecklist() {
  // v6: prefer the PREPARE-stage checkpoint view (matches what the agent actually updates).
  if (session?.prepare_brief) {
    renderPrepareChecklist();
    return;
  }
  const checklist = session?.verify_checklist || {};
  const entries = Object.entries(checklist);
  const confirmed = entries.filter(([, item]) => item.status === "confirmed").length;
  el("checklistSummary").textContent = entries.length ? `${confirmed}/${entries.length} confirmed` : "Waiting for verify stage.";
  if (!entries.length) {
    el("checklist").innerHTML = `<div class="empty-state">${svgIcon("i-check-badge")}<strong>Checklist will appear here</strong><em>It populates after the model reviews your materials.</em></div>`;
    return;
  }
  el("checklist").innerHTML = entries
    .map(([key, item]) => {
      const statusLabel = item.status === "confirmed" ? "confirmed" : item.status === "pending" ? "awaiting review" : item.status || "unknown";
      const statusIcon = item.status === "confirmed" ? "i-check-circle" : "i-warning";
      const body = key === "env_vars"
        ? renderEnvChecklist(item)
        : renderChecklistFields(item.content);
      return `<article class="check-item ${item.status}">
        <header class="check-row">
          <strong>${escapeHtml(labelize(key))}</strong>
          <span class="check-status">${svgIcon(statusIcon)}${escapeHtml(statusLabel)}</span>
        </header>
        ${body}
      </article>`;
    })
    .join("");
}

function renderChecklistFields(content) {
  if (content == null || content === "") {
    return `<p class="check-empty">No content yet.</p>`;
  }
  if (typeof content === "string" || typeof content === "number" || typeof content === "boolean") {
    return `<p class="check-summary">${escapeHtml(String(content))}</p>`;
  }
  if (Array.isArray(content)) {
    if (!content.length) return `<p class="check-empty">Empty list.</p>`;
    return `<ul class="check-list-inline">${content
      .map((v) => `<li>${typeof v === "object" ? renderChecklistFields(v) : escapeHtml(String(v))}</li>`)
      .join("")}</ul>`;
  }
  if (typeof content === "object") {
    const entries = Object.entries(content).filter(([k]) => k !== "source");
    if (!entries.length) return `<p class="check-empty">No fields recorded.</p>`;
    return `<dl class="check-fields">${entries
      .map(([k, v]) => {
        const isComplex = v !== null && typeof v === "object";
        const valueHtml = isComplex
          ? renderChecklistFields(v)
          : `<span>${escapeHtml(String(v ?? ""))}</span>`;
        return `<div class="check-field"><dt>${escapeHtml(labelize(k))}</dt><dd>${valueHtml}</dd></div>`;
      })
      .join("")}</dl>`;
  }
  return `<p class="check-summary">${escapeHtml(String(content))}</p>`;
}

function renderObotokenTable(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return `<p class="check-empty">No OBO tokens recorded.</p>`;
  return `<dl class="env-token-table">${entries
    .map(
      ([name, scope]) =>
        `<div class="env-token-row"><dt><code>${escapeHtml(name)}</code></dt><dd>${escapeHtml(String(scope ?? ""))}</dd></div>`
    )
    .join("")}</dl>`;
}

function renderEnvReconciliation(content) {
  if (content == null || (typeof content === "object" && !Array.isArray(content) && !Object.keys(content).length)) {
    return `<p class="check-empty">No reconciliation recorded yet.</p>`;
  }
  if (typeof content !== "object" || Array.isArray(content)) {
    return renderChecklistFields(content);
  }
  const sections = [];
  const KNOWN = new Set(["matched_existing", "existing_obo_tokens", "needs_to_be_added", "user_warning"]);

  const matched = Array.isArray(content.matched_existing) ? content.matched_existing : [];
  if (matched.length) {
    sections.push(`<section class="env-section env-section-matched">
      <h4 class="env-section-title">${svgIcon("i-check-circle")} Reused from existing ACA <span class="env-section-count">${matched.length}</span></h4>
      <ul class="env-card-list">${matched
        .map((row) => {
          const aca = row?.aca_variable ? `<code class="env-var-name">${escapeHtml(String(row.aca_variable))}</code>` : "";
          const required = row?.required ? `<div class="env-card-required">${escapeHtml(String(row.required))}</div>` : "";
          const reason = row?.reason ? `<div class="env-card-reason">${escapeHtml(String(row.reason))}</div>` : "";
          return `<li class="env-card">${aca}${required}${reason}</li>`;
        })
        .join("")}</ul>
    </section>`);
  }

  if (content.existing_obo_tokens && Object.keys(content.existing_obo_tokens).length) {
    sections.push(`<section class="env-section env-section-obo">
      <h4 class="env-section-title">${svgIcon("i-share")} Existing OBO tokens <span class="env-section-count">${Object.keys(content.existing_obo_tokens).length}</span></h4>
      ${renderObotokenTable(content.existing_obo_tokens)}
    </section>`);
  }

  const needs = Array.isArray(content.needs_to_be_added) ? content.needs_to_be_added : [];
  if (needs.length) {
    sections.push(`<section class="env-section env-section-needs">
      <h4 class="env-section-title">${svgIcon("i-warning")} Needs to be added to ACA <span class="env-section-count">${needs.length}</span></h4>
      <ul class="env-card-list">${needs
        .map((row) => {
          const name = row?.variable ? `<code class="env-var-name env-var-missing">${escapeHtml(String(row.variable))}</code>` : "";
          const reason = row?.reason ? `<div class="env-card-reason">${escapeHtml(String(row.reason))}</div>` : "";
          return `<li class="env-card env-card-needs">${name}${reason}</li>`;
        })
        .join("")}</ul>
    </section>`);
  }

  if (content.user_warning) {
    sections.push(`<aside class="env-warning">${svgIcon("i-warning")}<span>${escapeHtml(String(content.user_warning))}</span></aside>`);
  }

  const extras = Object.entries(content).filter(([k]) => !KNOWN.has(k));
  if (extras.length) {
    const extrasObj = Object.fromEntries(extras);
    sections.push(`<section class="env-section"><h4 class="env-section-title">${svgIcon("i-info")} Other details</h4>${renderChecklistFields(extrasObj)}</section>`);
  }

  if (!sections.length) return renderChecklistFields(content);
  return `<div class="env-reconciliation">${sections.join("")}</div>`;
}

function renderEnvChecklist(item) {
  const aca = session?.aca_env_result || null;
  const error = session?.aca_env_error || "";
  const variables = Array.isArray(aca?.variables) ? aca.variables : [];
  const obo = aca?.architectural_config?.OBO_SCOPE_REGISTRY || {};
  const content = item?.content || {};
  return `<div class="env-checklist">
    <div class="env-source ${error ? "failed" : variables.length ? "ready" : ""}">
      <strong>ACA existing environment</strong>
      <span>${error ? "Lookup failed" : variables.length ? `${variables.length} variable(s) found` : "Not loaded yet"}</span>
    </div>
    ${error ? `<div class="sample-error">${escapeHtml(error)}</div>` : ""}
    ${variables.length ? `<div class="env-chip-list">${variables.map((name) => `<code>${escapeHtml(name)}</code>`).join("")}</div>` : `<div class="empty-state">${svgIcon("i-info")}<strong>ACA variables not loaded</strong><em>They appear after the MCP lookup completes.</em></div>`}
    ${Object.keys(obo).length ? `<details class="env-details"><summary>OBO token variables <span class="env-section-count">${Object.keys(obo).length}</span></summary>${renderObotokenTable(obo)}</details>` : ""}
    <details class="env-details" open>
      <summary>Model env reconciliation</summary>
      ${renderEnvReconciliation(content)}
    </details>
    <p class="env-hint">Drafting should reuse existing ACA names when they match. If a required variable is missing, the skill should warn that it must be added to ACA.</p>
  </div>`;
}

function renderEditor() {
  if (!session) return;
  const draft = session.current_skill || {};
  activeTab = "skill";
  const value = draft.skill_md || "";
  el("editor").value = value;
  el("editor").placeholder = "SKILL.md will appear here after Draft.";
  renderMarkdownPreview(value);
}

function splitFrontmatter(markdownText) {
  const match = String(markdownText || "").match(/^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
  if (!match) return { frontmatter: "", body: markdownText || "" };
  return {
    frontmatter: match[1],
    body: String(markdownText || "").slice(match[0].length),
  };
}

function renderMarkdownPreview(markdownText) {
  const node = el("markdownPreview");
  if (!node) return;
  if (!String(markdownText || "").trim()) {
    node.innerHTML = `<div class="empty-state">${svgIcon("i-document")}<strong>No SKILL.md yet</strong><em>Preview appears here after the first draft.</em></div>`;
    return;
  }
  const { frontmatter, body } = splitFrontmatter(markdownText);
  const frontmatterHtml = frontmatter
    ? `<section class="markdown-frontmatter"><strong>YAML front matter</strong><pre><code class="hljs language-yaml">${hljs ? hljs.highlight(frontmatter, { language: "yaml", ignoreIllegals: true }).value : escapeHtml(frontmatter)}</code></pre></section>`
    : "";
  const bodyHtml = mdRenderer.render(body || "");
  node.innerHTML = `${frontmatterHtml}${bodyHtml}`;
}

function renderTests() {
  const runs = [...(session?.test_runs || [])].sort((a, b) => new Date(b.ran_at || 0) - new Date(a.ran_at || 0));
  if (!runs.length) {
    el("testResults").innerHTML = `<div class="empty-state">${svgIcon("i-beaker")}<strong>No test run yet</strong><em>Generate a draft, then run skill-selection samples.</em></div>`;
    return;
  }
  const totalRuns = runs.length;
  el("testResults").innerHTML = runs
    .map((run, index) => {
      const isScenario = Array.isArray(run.scenario_layers) && run.scenario_layers.length > 0;
      return `<details class="test-item" data-test-run-id="${escapeHtml(run.run_id)}" ${index === 0 ? "open" : ""}>
      <summary>
        <span class="test-run-title">Test run ${totalRuns - index}${index === 0 ? " (latest)" : ""}</span>
        <span class="test-run-time">${escapeHtml(formatSessionTime(run.ran_at))}</span>
        ${testRunScoreHtml(run)}
      </summary>
      ${renderTestRunNotes(run)}
      ${isScenario ? renderScenarioLayers(run) : `${renderResultList("Positive", run.positive_results)}${renderResultList("Negative", run.negative_results)}`}
    </details>`;
    })
    .join("");
}

function currentTestSamples() {
  // Read the AUTHORITATIVE Prepare Brief samples so the Tests tab and the
  // Checklist editor always show the same data. Negatives are structured
  // {query, route_to_peer, why_not_this} just like the Checklist editor.
  const brief = session?.prepare_brief || {};
  const positive = Array.isArray(brief.positive_samples)
    ? brief.positive_samples.map((s) => String(s || "")).filter((s) => s.trim())
    : [];
  const negative = Array.isArray(brief.negative_samples)
    ? brief.negative_samples
        .map((n) => ({
          query: String(n?.query || ""),
          route_to_peer: String(n?.route_to_peer || ""),
          why_not_this: String(n?.why_not_this || ""),
        }))
        .filter((n) => n.query.trim())
    : [];
  return { positive, negative };
}

function renderTestSampleEditor() {
  const positiveNode = el("positiveSamplesInput");
  const negativeNode = el("negativeSamplesInput");
  if (!positiveNode || !negativeNode) return;
  const editor = document.querySelector("[data-test-samples-editor]");
  if (editor && editor.contains(document.activeElement)) return;
  const samples = currentTestSamples();
  // Hidden textareas keep query-only strings for draft continuity; the rows are
  // the real editor and the Prepare Brief is the source of truth on save.
  positiveNode.value = samples.positive.join("\n");
  negativeNode.value = samples.negative.map((n) => n.query).join("\n");
  renderTestSamplesTables(samples);
}

function renderTestSamplesTables(samples = currentTestSamples()) {
  const posRows = document.querySelector(`[data-test-rows="positive"]`);
  if (posRows) {
    const list = samples.positive.length ? samples.positive : [""];
    posRows.innerHTML =
      `<div class="sample-row col-head-row"><span class="sample-input col-head">A query that should select THIS skill</span><span class="col-head-del"></span></div>` +
      list.map((v) => sampleRowHtml(v, "positive")).join("");
  }
  const negRows = document.querySelector(`[data-test-rows="negative"]`);
  if (negRows) {
    const list = samples.negative.length ? samples.negative : [{}];
    negRows.innerHTML =
      `<div class="sample-row col-head-row"><span class="sample-input col-head">Query</span><span class="sample-input col-head">Route to which peer</span><span class="sample-input col-head">Why NOT this skill</span><span class="col-head-del"></span></div>` +
      list.map((n) => testNegativeRowHtml(n)).join("");
  }
}

function testNegativeRowHtml(n = {}) {
  return `<div class="sample-row sample-neg-row" data-kind="negative">
    <input type="text" class="sample-input test-neg-query" value="${escapeHtml(String(n.query || ""))}" placeholder="A query that should NOT hit this skill">
    <input type="text" class="sample-input test-neg-peer" value="${escapeHtml(String(n.route_to_peer || ""))}" placeholder="Route to which peer">
    <input type="text" class="sample-input test-neg-why" value="${escapeHtml(String(n.why_not_this || ""))}" placeholder="Why NOT this skill">
    <button type="button" class="sample-del" data-del-sample title="Remove this row">\u2715</button>
  </div>`;
}

function collectTestSamples(kind) {
  return [...document.querySelectorAll(`[data-test-rows="${kind}"] .sample-row:not(.col-head-row):not(.sample-neg-row) .sample-input`)]
    .map((input) => input.value.trim())
    .filter(Boolean);
}

function collectTestNegatives() {
  return [...document.querySelectorAll(`[data-test-rows="negative"] .sample-neg-row`)]
    .map((row) => ({
      query: row.querySelector(".test-neg-query")?.value.trim() || "",
      route_to_peer: row.querySelector(".test-neg-peer")?.value.trim() || "",
      why_not_this: row.querySelector(".test-neg-why")?.value.trim() || "",
    }))
    .filter((n) => n.query);
}

function syncTestSamplesTextareas() {
  const positiveNode = el("positiveSamplesInput");
  const negativeNode = el("negativeSamplesInput");
  if (positiveNode) positiveNode.value = collectTestSamples("positive").join("\n");
  if (negativeNode) negativeNode.value = collectTestNegatives().map((n) => n.query).join("\n");
  // Trigger the existing persistence path bound to the textareas.
  positiveNode?.dispatchEvent(new Event("input", { bubbles: true }));
}

function parseSampleLines(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

// Has the user actually started PREPARE yet? An empty just-loaded session (no
// materials, no brief data, no messages) should sit on Materials; once there is
// any input we move to the Checklist where the confirmation flow happens.
function prepareHasStarted() {
  if (!session) return false;
  if (Array.isArray(session.materials) && session.materials.length) return true;
  const u = session.prepare_brief?.understanding || {};
  if (
    (u.skill_goal && String(u.skill_goal).trim()) ||
    (Array.isArray(u.key_capabilities) && u.key_capabilities.length) ||
    (Array.isArray(u.input_sources) && u.input_sources.length) ||
    (Array.isArray(u.neighbor_skills) && u.neighbor_skills.length)
  ) return true;
  if (Object.keys(session.prepare_brief?.verify_updated_at || {}).length) return true;
  const conv = Array.isArray(session.conversation) ? session.conversation : [];
  if (conv.some((m) => String(m.role || "").toLowerCase() === "user")) return true;
  return false;
}

// The tab this stage wants the user on. PREPARE is special: stay on Materials
// until the user has given something, then steer to the Checklist.
function suggestedTabForStage() {
  const stage = currentStage();
  if (stage === "PREPARE") return prepareHasStarted() ? "checklist" : "materials";
  return CONTEXT_TAB_FOR_STAGE[stage] || "materials";
}

function applyContextTabDefault() {
  const suggested = suggestedTabForStage();
  if (!userPickedContextTab) {
    setActiveContextTab(suggested);
  } else {
    setActiveContextTab(activeContextTab);
  }
}

function setActiveContextTab(name) {
  if (!CONTEXT_TABS.includes(name)) name = "materials";
  activeContextTab = name;
  document.querySelectorAll(".context-tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.context === name);
  });
  document.querySelectorAll("[data-context-pane]").forEach((pane) => {
    pane.classList.toggle("hidden", pane.dataset.contextPane !== name);
  });
  if (name === "inspect" || name === "agent") loadInspect();
  if (name === "topology") loadTopology();
  if (name === "agent") setTimeout(() => {
    restoreAgentGraphSplit();
    agentGraph?.resize().fit(undefined, 40);
  }, 0);
  updateTabAttention();
}

// C3: when the agent is steering the user to the panel this stage cares about,
// pulse the corresponding tab with a "needs attention" dot until they open it.
function updateTabAttention() {
  const suggested = suggestedTabForStage();
  document.querySelectorAll(".context-tabs button").forEach((button) => {
    const isSuggested = button.dataset.context === suggested;
    button.classList.toggle("needs-attention", isSuggested && activeContextTab !== suggested);
  });
}

async function loadInspect() {
  if (inspectData || inspectLoading) {
    renderInspectViews();
    return;
  }
  inspectLoading = true;
  renderInspectViews();
  try {
    inspectData = await fetchInspect();
  } catch (err) {
    renderInspectError(err);
    inspectLoading = false;
    return;
  }
  inspectLoading = false;
  renderInspectViews();
}

async function loadTopology(force = false) {
  const node = el("topologyResults");
  if (!node) return;
  if (!session?.id) {
    node.innerHTML = `<div class="empty-state">Start or resume a session to inspect topology.</div>`;
    return;
  }
  if (topologyLoading) return;
  if (topologyData && !force) {
    renderTopologyPanel();
    return;
  }
  topologyLoading = true;
  node.innerHTML = `<div class="empty-state">Validating current SKILL.md...</div>`;
  try {
    topologyData = await fetchSessionTopology(session.id);
    renderTopologyPanel();
  } catch (err) {
    node.innerHTML = `<div class="empty-state">${svgIcon("i-warning")}<strong>Validation unavailable</strong><em>${escapeHtml(err.message)}</em></div>`;
  } finally {
    topologyLoading = false;
  }
}

function renderTopologyPanel() {
  const node = el("topologyResults");
  if (!node) return;
  const results = Array.isArray(topologyData?.results) ? topologyData.results : [];
  if (!results.length) {
    node.innerHTML = `<div class="empty-state">No topology results yet.</div>`;
    return;
  }
  const rules = results.map((result) => {
    const warnings = (result.issues || []).filter((issue) => issue.severity === "warning");
    const state = result.passed ? (warnings.length ? "warning" : "passed") : "failed";
    const label = result.passed ? (warnings.length ? "WARN" : "PASS") : "FAIL";
    const icon = result.passed ? (warnings.length ? "i-warning" : "i-check-circle") : "i-x";
    const details = (result.issues || []).length
      ? `<ul>${result.issues.map((issue) => `<li>${escapeHtml(issue.message)}</li>`).join("")}</ul>`
      : "";
    return `<details class="topology-rule ${state}" data-topology-rule="${escapeHtml(result.rule)}">
      <summary><span class="topology-status">${svgIcon(icon)} ${label}</span><strong>${escapeHtml(result.rule)}</strong><span>${escapeHtml(result.description)}</span></summary>
      ${details}
    </details>`;
  }).join("");
  node.innerHTML = rules + renderFidelitySection() + renderLintSection();
}

// Content defects the topology rules cannot see. Only A1 blocks (at save time);
// everything here is shown for the author to judge.
function renderLintSection() {
  const issues = Array.isArray(topologyData?.lint) ? topologyData.lint : [];
  const body = issues.length
    ? issues.map((issue) => {
        const state = issue.severity === "error" ? "failed" : issue.severity === "warning" ? "warning" : "passed";
        const label = issue.severity === "error" ? "FAIL" : issue.severity === "warning" ? "WARN" : "INFO";
        const icon = issue.severity === "error" ? "i-x" : issue.severity === "warning" ? "i-warning" : "i-check-circle";
        return `<details class="topology-rule lint-rule ${state}" data-lint-rule="${escapeHtml(issue.rule)}">
          <summary><span class="topology-status">${svgIcon(icon)} ${label}</span><span><strong>${escapeHtml(issue.rule)}</strong> ${escapeHtml(issue.message)}</span></summary>
        </details>`;
      }).join("")
    : `<div class="empty-state">No content-lint findings.</div>`;
  return `<h4 class="topology-section-title">Content lint</h4>${body}`;
}

// Advisory only: drift from the user's materials is a judgement call, so it is
// shown next to the topology rules rather than blocking anything.
function renderFidelitySection() {
  const issues = Array.isArray(topologyData?.fidelity) ? topologyData.fidelity : [];
  const body = issues.length
    ? issues.map((issue) => {
        const state = issue.severity === "warning" ? "warning" : "passed";
        const label = issue.severity === "warning" ? "WARN" : "INFO";
        const icon = issue.severity === "warning" ? "i-warning" : "i-check-circle";
        return `<details class="topology-rule fidelity-rule ${state}" data-fidelity-rule="${escapeHtml(issue.rule)}">
          <summary><span class="topology-status">${svgIcon(icon)} ${label}</span><span><strong>${escapeHtml(issue.rule)}</strong> ${escapeHtml(issue.message)}</span></summary>
        </details>`;
      }).join("")
    : `<div class="empty-state">No fidelity warnings.</div>`;
  return `<h4 class="topology-section-title">Material fidelity</h4>${body}`;
}

function renderInspectViews() {
  renderInspect();
  renderAgentGraphView();
}

function renderInspectError(err) {
  const html = `<div class="empty-state">${svgIcon("i-warning")}<strong>Could not load inspect data</strong><em>${escapeHtml(err.message)}</em></div>`;
  const inspectNode = el("inspectContent");
  const graphNode = el("agentGraphDetails");
  if (inspectNode) inspectNode.innerHTML = html;
  if (graphNode) graphNode.innerHTML = html;
}

function renderInspectPlaceholder(node, loadingText, emptyText) {
  if (!node) return true;
  if (inspectLoading && !inspectData) {
    node.innerHTML = `<div class="empty-state">${escapeHtml(loadingText)}</div>`;
    return true;
  }
  if (!inspectData) {
    node.innerHTML = `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    return true;
  }
  return false;
}

function renderInspect() {
  const node = el("inspectContent");
  if (renderInspectPlaceholder(node, "Loading agent mechanism...", "Open this tab to load the agent mechanism.")) return;
  node.innerHTML = [
    renderInspectSessionState(),
    renderCurrentStageDetail(),
    renderInspectTools(inspectData.tools || []),
    renderInspectOutput(inspectData.output_shape, inspectData.output_rules || []),
  ].join("");
}

function renderInspectSection(title, subtitle, bodyHtml) {
  return `<section class="inspect-section">
    <header class="inspect-section-head">
      <h3>${escapeHtml(title)}</h3>
      ${subtitle ? `<p class="panel-subtitle">${escapeHtml(subtitle)}</p>` : ""}
    </header>
    ${bodyHtml}
  </section>`;
}

function renderAgentRuntimeMap() {
  const nodes = [
    ["User input", "Chat text, materials, card answers, or file edits."],
    ["Session state", "Current stage, checklist, draft files, test samples, and pending tool calls."],
    ["Prompt bundle", "Base instructions plus stage-specific prompt and injected session facts."],
    ["Agent response", "Structured JSON with assistant text and deterministic tool calls."],
    ["UI effects", "Cards, draft files, patches, checklist updates, tests, and stage movement."],
  ];
  const body = `<div class="agent-runtime-map">
    ${nodes.map(([title, description], index) => `<div class="agent-runtime-node">
      <span class="stage-number">${index + 1}</span>
      <strong>${escapeHtml(title)}</strong>
      <p>${escapeHtml(description)}</p>
    </div>${index < nodes.length - 1 ? `<span class="agent-runtime-arrow">-></span>` : ""}`).join("")}
  </div>`;
  return renderInspectSection("Runtime Loop", "What happens during one user message before the UI updates.", body);
}

function renderCurrentStageDetail() {
  const stage = currentStage();
  const group = currentGroup();
  const meta = groupMeta[group] || groupMeta.PREPARE;
  const transitions = inspectData?.transitions || [];
  const node = transitions.find((item) => normalizeStage(item.from) === stage);
  const promptMatches = promptsForStage(stage, inspectData?.prompts || []);
  const body = `<div class="current-stage-detail">
    <div class="current-stage-card">
      <span class="eyebrow">Current node</span>
      <strong>${escapeHtml(stage)}</strong>
      <p>${escapeHtml(SUBSTAGE_LABELS[stage] || meta.purpose || "")}</p>
      <small>${escapeHtml(meta.description || "")}</small>
    </div>
    <div class="current-stage-card">
      <span class="eyebrow">Visible phase</span>
      <strong>${escapeHtml(meta.title || group)}</strong>
      <p>${escapeHtml(meta.next || "")}</p>
    </div>
    <div class="current-stage-card">
      <span class="eyebrow">Next transitions</span>
      ${(node?.edges || []).length
        ? `<ul>${node.edges.map((edge) => `<li><code>${escapeHtml(normalizeStage(edge.to))}</code> ${escapeHtml(edge.reason || "")}</li>`).join("")}</ul>`
        : "<p>No outgoing transition registered.</p>"}
    </div>
    <div class="current-stage-card">
      <span class="eyebrow">Prompt bundle</span>
      ${promptMatches.length ? promptMatches.map((p) => `<code>${escapeHtml(p.filename || p.title || "prompt")}</code>`).join("") : "<p>No prompt match found.</p>"}
    </div>
  </div>`;
  return renderInspectSection("Current Stage Detail", "Where the agent is now and what can happen next.", body);
}

function renderInspectStageGroups(groups) {
  const activeGroup = currentGroup();
  const body = `<div class="stage-flow-map">
    ${groups.map((g, index) => `<div class="inspect-group-card ${g.key === activeGroup ? "active" : ""}">
      <div class="inspect-group-head">
        <span class="stage-number">${index + 1}</span>
        <strong>${escapeHtml(g.title)}</strong>
        ${g.key === activeGroup ? `<em>Current</em>` : ""}
      </div>
      <p>${escapeHtml(g.purpose)}</p>
      <small>Internal: ${(g.internal_stages || []).map((s) => `<code>${escapeHtml(s)}</code>`).join(" -> ")}</small>
    </div>${index < groups.length - 1 ? `<span class="stage-flow-arrow">-></span>` : ""}`).join("")}
  </div>`;
  return renderInspectSection("Stage Flow (5 stages)", "The inspect view follows the current PREPARE → DRAFT → REFINE → TEST → DONE state machine.", body);
}

function renderInspectTransitions(transitions) {
  if (!transitions.length) return "";
  const groupOf = (stage) => STAGE_TO_GROUP[normalizeStage(stage)] || "PREPARE";
  const rows = transitions.map((node) => {
    const from = normalizeStage(node.from);
    const fromGroup = groupOf(from);
    const edges = (node.edges || []).map((edge) => {
      const to = normalizeStage(edge.to);
      const toGroup = groupOf(to);
      const crossGroup = fromGroup !== toGroup;
      return `<li class="inspect-transition-edge ${crossGroup ? "cross-group" : "same-group"}">
        <span class="edge-arrow">-></span>
        <code class="edge-target">${escapeHtml(to)}</code>
        <span class="edge-reason">${escapeHtml(edge.reason || "")}</span>
      </li>`;
    }).join("");
    return `<div class="inspect-transition-row">
      <div class="inspect-transition-from">
        <code>${escapeHtml(from)}</code>
        <small>${escapeHtml((groupMeta[fromGroup] || {}).title || "")}</small>
      </div>
      <ul class="inspect-transition-edges">${edges || "<li><em>Terminal stage - no transitions out.</em></li>"}</ul>
    </div>`;
  }).join("");
  const body = `<div class="inspect-transition-list">${rows}</div>
    <p class="panel-subtitle inspect-transition-legend">
      Every edge comes directly from the backend five-stage transition table. Forward edges advance the workflow; backward edges reopen planning or refinement.
      Cross-stage data remains in the session and the current stage prompt receives the relevant checklist, draft, and latest test information.
    </p>`;
  return renderInspectSection("Stage Transitions", "Every edge has a reason. The agent picks the edge whose reason matches the situation.", body);
}

const PROMPT_FILE_FOR_STAGE = {
  PREPARE: "01_prepare.md",
  DRAFT: "02_draft.md",
  REFINE: "03_refine.md",
  TEST: "04_test.md",
  DONE: "05_done.md",
};

const BASE_PROMPT_FILES = new Set(["00_global_system.md", "09_best_practices.md", "10_format_spec.md"]);

function renderPromptMatrix(prompts) {
  const labels = [
    ["PREPARE", "Confirm definition, routing boundaries, examples, and variables"],
    ["DRAFT", "Create the initial complete SKILL.md"],
    ["REFINE", "Apply focused, reviewable patches"],
    ["TEST", "Validate positive and negative routing samples"],
    ["DONE", "Finalize or reopen the skill"],
  ];
  const promptRows = labels.map(([stage, description]) => {
    const filename = PROMPT_FILE_FOR_STAGE[stage];
    const matches = prompts.filter((prompt) => prompt.filename === filename);
    return `<div class="prompt-matrix-row ${stage === currentStage() ? "active" : ""}">
      <div class="prompt-matrix-stage">
        <strong>${escapeHtml((groupMeta[stage] || {}).title || stage)}</strong>
        <span>${escapeHtml(description)}</span>
      </div>
      <div class="prompt-matrix-prompts">
        ${matches.length ? matches.map((p) => `<code>${escapeHtml(p.filename || p.title || "prompt")}</code>`).join("") : "<em>No dedicated prompt file detected.</em>"}
      </div>
    </div>`;
  }).join("");
  return renderInspectSection("Prompt Routing", "The dedicated prompt file used by each current state-machine stage.", `<div class="prompt-matrix">${promptRows}</div>`);
}

function promptsForStage(stage, prompts) {
  const normalized = normalizeStage(stage);
  const currentStageName = STAGE_TO_GROUP[normalized] || normalized;
  const dedicatedFilename = PROMPT_FILE_FOR_STAGE[currentStageName];
  return prompts.filter((prompt) => BASE_PROMPT_FILES.has(prompt.filename) || prompt.filename === dedicatedFilename);
}

const CARRIED_CONTEXT_FOR_STAGE = {
  PREPARE: [
    ["User request and materials", "The conversation starts here; on re-entry, the Prepare Brief and materials are preserved while the draft is cleared."],
  ],
  DRAFT: [
    ["Confirmed Prepare Brief", "Definition, research, routing samples, peer boundaries, and variables confirmed in PREPARE."],
  ],
  REFINE: [
    ["Current SKILL.md", "The accepted draft from DRAFT, or the latest patched version."],
    ["Test findings", "When returning from TEST, the latest results and reflection guide the next focused patch."],
  ],
  TEST: [
    ["Skill and routing samples", "The current SKILL.md and positive/negative samples prepared before testing."],
  ],
  DONE: [
    ["Final skill history", "The current SKILL.md plus available patch and test history remain available for reopening."],
  ],
};

function allowedTransitionsPromptDescription(stage) {
  const selected = normalizeStage(stage);
  const node = (inspectData?.transitions || []).find((item) => normalizeStage(item.from) === selected);
  const exits = (node?.edges || []).map((edge) => {
    const target = normalizeStage(edge.to);
    return `${target} — ${edge.reason || "No reason provided."}`;
  });
  if (!exits.length) return `The system prompt states that ${selected} has no outgoing transition.`;
  return `Outgoing only: ${exits.join(" | ")} Incoming edges shown under Transition Logic are not injected while ${selected} is active.`;
}

function dynamicPromptContextForStage(stage) {
  const selected = normalizeStage(stage);
  const brief = session?.prepare_brief || {};
  const research = brief.research || {};
  const understanding = brief.understanding || {};
  const hasPrepareBrief = Boolean(
    understanding.skill_goal || understanding.key_capabilities?.length || understanding.differentiation ||
    research.web_status && research.web_status !== "skipped" || research.summary ||
    research.adjacent_skills?.length || research.existing_skills_overlap?.length || brief.revisit
  );
  const items = [
    ["Runtime State", "Always", `Mode and selected stage (${selected.toLowerCase()}).`],
    ["Blob Skill Binding", "Always", "Saved skill name and local/remote version status."],
  ];
  const add = (name, source, description, condition = true) => {
    if (condition) items.push([name, source, description]);
  };

  add("DONE Re-entry Guidance", "DONE", "Classifies reopen requests into refine, prepare, or test.", selected === "DONE");
  add("Mode Addendum", "Mode", "Additional import or modify guidance.", ["import", "modify"].includes(String(session?.mode || "")));
  add("Existing Skills Index", "Skill store", "Candidate skills used for overlap awareness.", Boolean(session?.existing_skills_index?.length));
  add("Materials Count", "User", "Number of attached source materials.", Boolean(session?.materials?.length));
  add("Prepare Brief", "PREPARE", "Confirmed definition, research, routing samples, variables, and checklist.", hasPrepareBrief);
  add("Peer Skills", "PREPARE", "Accessible peer-skill boundaries used for differentiation.", ["PREPARE", "DRAFT", "REFINE"].includes(selected) && Boolean(research.peer_skills?.length));
  add("Selected Neighbor Skills", "PREPARE", "Full selected peer skills available for proposed edits.", ["PREPARE", "DRAFT", "REFINE"].includes(selected) && Boolean(Object.keys(session?.neighbor_full_md || {}).length));
  add("Iteration Log", "REFINE / TEST", "Accepted patches and recorded test reflections.", Boolean(session?.patch_history?.length || session?.iteration_reflections?.length));
  add("Research Summary", "PREPARE", "Additional persisted research summary.", Boolean(session?.research_summary));
  add("ACA Environment", "MCP / ACA", "Existing environment variables and OBO token registry.", Boolean(session?.aca_env_result || session?.aca_env_error));
  add("Latest Test Run", "TEST", "Latest routing outcomes supplied to refinement or completion.", ["REFINE", "TEST", "DONE"].includes(selected) && Boolean(session?.test_runs?.length));
  add("Current Draft", "DRAFT / REFINE", "Current full SKILL.md and version hash.", selected !== "PREPARE" && Boolean(session?.current_skill?.skill_md));
  return items;
}

function promptFileContent(filename) {
  const prompt = (inspectData?.prompts || []).find((item) => item.filename === filename);
  return prompt?.content || `Prompt file not available: ${filename}`;
}

function dynamicContextPromptPreview(stage) {
  const items = dynamicPromptContextForStage(stage);
  if (!items.length) return "No eligible dynamic context for this session.";
  return items.map(([name, source, description]) => `## ${name}\n\nSource: ${source}\n${description}`).join("\n\n");
}

function allowedTransitionsPromptPreview(stage) {
  const selected = normalizeStage(stage);
  const node = (inspectData?.transitions || []).find((item) => normalizeStage(item.from) === selected);
  const edges = node?.edges || [];
  if (!edges.length) return "## Allowed Stage Transitions\n\nNo further transitions; this is a terminal stage.";
  const lines = [
    "## Allowed Stage Transitions",
    "",
    `From \`${selected.toLowerCase()}\` you may emit \`request_stage_transition\` to:`,
  ];
  edges.forEach((edge) => lines.push(`- \`${normalizeStage(edge.to).toLowerCase()}\` -- ${edge.reason || ""}`));
  lines.push("", "Pick the transition whose reason matches the actual situation; do not invent new edges.");
  return lines.join("\n");
}

function currentSessionSnapshotPreview() {
  if (!session) return "No session has been started.";
  const snapshot = JSON.parse(JSON.stringify(session));
  if (Array.isArray(snapshot.conversation)) snapshot.conversation = snapshot.conversation.slice(-12);
  return JSON.stringify(snapshot, null, 2);
}

function latestUserMessagePreview() {
  const messages = Array.isArray(session?.conversation) ? session.conversation : [];
  const latest = [...messages].reverse().find((message) => String(message?.role || "").toLowerCase() === "user");
  return latest?.content || "No user message is currently recorded in the session.";
}

function renderTurnPromptAssembly(stage) {
  const selected = normalizeStage(stage);
  const stagePrompt = PROMPT_FILE_FOR_STAGE[selected] || "(none)";
  const dynamicNames = dynamicPromptContextForStage(selected).map(([name]) => name);
  const dynamicSummary = dynamicNames.length ? dynamicNames.join(" → ") : "No eligible session context";
  const systemSteps = [
    ["Global System", "00_global_system.md", promptFileContent("00_global_system.md")],
    ["Current Stage Prompt", stagePrompt, promptFileContent(stagePrompt)],
    ["Writing Best Practices", "09_best_practices.md", promptFileContent("09_best_practices.md")],
    ["Skill Format Spec", "10_format_spec.md", promptFileContent("10_format_spec.md")],
    ["Eligible Dynamic Context", dynamicSummary, dynamicContextPromptPreview(selected)],
    ["Outgoing Allowed Stage Transitions", allowedTransitionsPromptDescription(selected), allowedTransitionsPromptPreview(selected)],
  ];
  const outputRules = inspectData?.output_rules || [];
  const userSteps = [
    ["Turn instruction", "UI-driver instruction and JSON-only requirement", `You are driving the Skill Generator v2 UI.\n\n${outputRules[0] || "Return ONLY valid JSON."}`],
    ["JSON output shape", "Required text and tool_calls response structure", inspectData?.output_shape || "Output shape unavailable."],
    ["Available tools", "Current tool schemas", JSON.stringify(inspectData?.tools || [], null, 2)],
    ["Output and orchestration rules", "11_output_rules.md", outputRules.slice(1).map((rule) => `- ${rule}`).join("\n") || "No additional rules."],
    ["Current session snapshot", "Session data with the latest 12 conversation messages", currentSessionSnapshotPreview()],
    ["Latest user message", "The request that triggered this turn", latestUserMessagePreview()],
  ];
  const stepsHtml = (steps) => steps.map(([name, detail, content]) => `<li>
    <details class="graph-assembly-step">
      <summary><strong>${escapeHtml(name)}</strong><span>${escapeHtml(detail)}</span></summary>
      <pre>${escapeHtml(content || "No content available.")}</pre>
    </details>
  </li>`).join("");

  return `<section class="graph-detail-section graph-turn-assembly">
      <h4>Per-turn Message Assembly</h4>
      <div class="graph-message-flow">
        <div class="graph-message-head"><code>1 · role=&quot;system&quot;</code></div>
        <ol class="graph-assembly-steps">${stepsHtml(systemSteps)}</ol>
      </div>
      <div class="graph-message-arrow" aria-hidden="true">↓</div>
      <div class="graph-message-flow">
        <div class="graph-message-head"><code>2 · role=&quot;user&quot;</code></div>
        <ol class="graph-assembly-steps">${stepsHtml(userSteps)}</ol>
      </div>
    </section>`;
}

function renderDataLineage(stage) {
  const selected = normalizeStage(stage);
  const carriedItems = CARRIED_CONTEXT_FOR_STAGE[selected] || [];
  const carriedRows = carriedItems.map(([name, description]) => `<li>
    <strong>${escapeHtml(name)}</strong>
    <p>${escapeHtml(description)}</p>
  </li>`).join("");
  return `<details class="graph-detail-section graph-prompt-details graph-context-explanatory">
      <summary>
        <span><strong>Data Lineage</strong><small>Where this stage gets its data</small></span>
        <em>Explanation only</em>
      </summary>
      <div class="graph-detail-collapsible-body">
        <p class="graph-context-note">These explanations help people read the flow; they are not sent to the Agent.</p>
        <ul class="graph-carry-list">${carriedRows}</ul>
      </div>
    </details>`;
}
function stagePurpose(stage) {
  const group = STAGE_TO_GROUP[stage] || "PREPARE";
  const meta = groupMeta[group] || {};
  return `${SUBSTAGE_LABELS[stage] || ""}${SUBSTAGE_LABELS[stage] ? " - " : ""}${meta.purpose || ""}`;
}

function graphStageAbbrev(stage) {
  return {
    PREPARE: "PR",
    DRAFT: "DR",
    REFINE: "RF",
    TEST: "TS",
    DONE: "DN",
  }[normalizeStage(stage)] || String(stage || "").slice(0, 2).toUpperCase();
}

function graphEdgeKey(from, to, index = 0) {
  const duplicateSuffix = index > 0 ? `.${index + 1}` : "";
  return `T.${graphStageAbbrev(from)}.${graphStageAbbrev(to)}${duplicateSuffix}`;
}

function graphEdgeId(from, to, index = 0) {
  return `${from}-${to}-${index}`;
}

function renderAgentGraphView() {
  const details = el("agentGraphDetails");
  const graphNode = el("agentGraph");
  if (!details || !graphNode) return;
  if (inspectLoading && !inspectData) {
    details.innerHTML = `<div class="empty-state">${svgIcon("i-arrow-path")}<strong>Loading agent graph</strong></div>`;
    return;
  }
  if (!inspectData) {
    details.innerHTML = `<div class="empty-state">${svgIcon("i-share")}<strong>Click to load the agent graph</strong><em>You will see all stages and how the model moves between them.</em></div>`;
    return;
  }
  if (!window.cytoscape) {
    const message = `<div class="empty-state">${svgIcon("i-warning")}<strong>Agent Graph library is unavailable</strong><em>Run npm ci, then restart the application so /vendor/cytoscape can be loaded.</em></div>`;
    graphNode.innerHTML = message;
    details.innerHTML = message;
    return;
  }
  const stages = ["PREPARE", "DRAFT", "REFINE", "TEST", "DONE"];
  if (!stages.includes(selectedGraphStage)) selectedGraphStage = STAGE_TO_GROUP[currentStage()] || currentStage();
  renderAgentGraphDetails(selectedGraphStage);
  renderAgentGraph();
}

function renderAgentGraph() {
  if (!window.cytoscape || !inspectData) return;
  const graphNode = el("agentGraph");
  if (!graphNode) return;
  const stages = ["PREPARE", "DRAFT", "REFINE", "TEST", "DONE"];
  const stageElements = stages.map((stage) => ({
    data: {
      id: stage,
      label: stage,
      subtitle: SUBSTAGE_LABELS[stage] || "",
      group: STAGE_TO_GROUP[stage] || "PREPARE",
    },
    classes: stage === currentStage() ? "current" : "",
  }));
  const edgeElements = (inspectData.transitions || []).flatMap((item) => {
    const fromNorm = normalizeStage(item.from);
    return (item.edges || []).map((edge, index) => {
      const toNorm = normalizeStage(edge.to);
      const edgeId = graphEdgeId(fromNorm, toNorm, index);
      const edgeKey = graphEdgeKey(fromNorm, toNorm, index);
      return {
        data: {
          id: edgeId,
          key: edgeKey,
          source: fromNorm,
          target: toNorm,
          reason: edge.reason || "",
          label: edgeKey,
        },
      };
    });
  });
  const elements = [...stageElements, ...edgeElements];
  const layoutOptions = {
    name: "preset",
    fit: true,
    padding: 70,
    positions: {
      PREPARE: { x: 100, y: 190 },
      DRAFT: { x: 350, y: 80 },
      REFINE: { x: 600, y: 190 },
      TEST: { x: 850, y: 80 },
      DONE: { x: 1100, y: 190 },
    },
  };
  if (!agentGraph) {
    agentGraph = window.cytoscape({
      container: graphNode,
      elements,
      wheelSensitivity: 0.18,
      minZoom: 0.25,
      maxZoom: 2.5,
      layout: layoutOptions,
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#ffffff",
            "border-width": 2,
            "border-color": "#d6c4ae",
            "label": "data(label)",
            "text-valign": "center",
            "text-halign": "center",
            "font-size": 12,
            "font-weight": 700,
            "color": "#1f1410",
            "width": 112,
            "height": 64,
            "shape": "round-rectangle",
          },
        },
        {
          selector: "node[group = 'PREPARE']",
          style: { "border-color": "#c4663a", "background-color": "#fbe8d5" },
        },
        {
          selector: "node[group = 'DRAFT']",
          style: { "border-color": "#8b6cb8", "background-color": "#f1e9fb" },
        },
        {
          selector: "node[group = 'REFINE']",
          style: { "border-color": "#b85c00", "background-color": "#fdf0db" },
        },
        {
          selector: "node[group = 'TEST']",
          style: { "border-color": "#2f7a3d", "background-color": "#e7f1e3" },
        },
        {
          selector: "node[group = 'DONE']",
          style: { "border-color": "#287a67", "background-color": "#e2f3ee" },
        },
        {
          selector: "node.current",
          style: { "border-width": 4, "border-color": "#7d3d20", "box-shadow": "0 6px 20px rgba(125, 61, 32, 0.22)" },
        },
        {
          selector: "node.focused-node",
          style: { "border-width": 5, "border-color": "#7d3d20", "background-color": "#fbe8d5", "z-index": 20 },
        },
        {
          selector: "node.neighbor-node",
          style: { "border-width": 3, "border-style": "dashed", "z-index": 12 },
        },
        {
          selector: "edge",
          style: {
            "width": 2,
            "line-color": "#cbb59a",
            "target-arrow-color": "#cbb59a",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            "label": "data(label)",
            "font-size": 9,
            "font-weight": 800,
            "text-background-color": "#fdfbf8",
            "text-background-opacity": 1,
            "text-background-padding": 3,
            "text-border-color": "#ecdfd1",
            "text-border-width": 1,
            "text-border-opacity": 1,
            "color": "#5a4a3e",
            "text-wrap": "none",
            "text-max-width": 60,
            "text-margin-y": -14,
            "control-point-step-size": 120,
            "source-endpoint": "outside-to-node",
            "target-endpoint": "outside-to-node",
          },
        },
        {
          selector: ".dimmed",
          style: { "opacity": 0.2 },
        },
        {
          selector: "edge.related-outgoing",
          style: {
            "opacity": 1,
            "width": 4,
            "line-color": "#b85c00",
            "target-arrow-color": "#b85c00",
            "color": "#7d3d20",
            "text-background-color": "#fdf0db",
            "text-border-color": "#efb079",
            "z-index": 14,
          },
        },
        {
          selector: "edge.related-incoming",
          style: {
            "opacity": 1,
            "width": 4,
            "line-color": "#8b6cb8",
            "target-arrow-color": "#8b6cb8",
            "color": "#4a3470",
            "text-background-color": "#f1e9fb",
            "text-border-color": "#b9a5d8",
            "z-index": 14,
          },
        },
        {
          selector: "edge.selected-edge",
          style: {
            "opacity": 1,
            "width": 5,
            "line-color": "#c4663a",
            "target-arrow-color": "#c4663a",
            "color": "#7d3d20",
            "text-background-color": "#fbe8d5",
            "text-border-color": "#c4663a",
            "z-index": 24,
          },
        },
        {
          selector: "edge:selected",
          style: {
            "width": 4,
            "line-color": "#c4663a",
            "target-arrow-color": "#c4663a",
            "text-background-color": "#fbe8d5",
            "text-border-color": "#c4663a",
          },
        },
        {
          selector: "edge[source = target]",
          style: {
            "curve-style": "bezier",
            "loop-direction": "45deg",
            "loop-sweep": "70deg",
          },
        },
        {
          selector: "node:selected",
          style: { "border-color": "#7d3d20", "border-width": 4 },
        },
      ],
    });
    agentGraph.on("tap", "node", (event) => {
      focusGraphNode(event.target.id(), true);
    });
    agentGraph.on("tap", "edge", (event) => {
      focusGraphEdge(event.target.id(), true);
    });
  } else {
    agentGraph.json({ elements });
    agentGraph.layout(layoutOptions).run();
  }
  applyGraphFocus();
  setTimeout(() => {
    agentGraph?.resize();
    if (selectedGraphEdge) {
      fitGraphEdge(selectedGraphEdge, 90);
      return;
    }
    agentGraph?.fit(undefined, 40);
  }, 0);
}

function graphEdgesForStage(stage) {
  const selected = normalizeStage(stage);
  const rows = [];
  (inspectData?.transitions || []).forEach((item) => {
    const from = normalizeStage(item.from);
    (item.edges || []).forEach((edge, index) => {
      const to = normalizeStage(edge.to);
      if (from !== selected && to !== selected) return;
      rows.push({
        direction: from === selected ? "outgoing" : "incoming",
        from,
        to,
        index,
        reason: edge.reason || "",
      });
    });
  });
  return rows;
}

function graphEdgeRowsForStage(stage, selectedEdgeId = "") {
  const rows = graphEdgesForStage(stage);
  if (!rows.length) return "";
  return rows.map((edge) => {
    const edgeId = graphEdgeId(edge.from, edge.to, edge.index);
    const key = graphEdgeKey(edge.from, edge.to, edge.index);
    const directionText = edge.direction === "outgoing" ? "Out" : "In";
    return `<li class="${edge.direction} ${edgeId === selectedEdgeId ? "selected" : ""}" data-graph-edge-id="${escapeHtml(edgeId)}" role="button" tabindex="0">
      <code>${escapeHtml(key)}</code>
      <span><b>${directionText}</b> <code>${escapeHtml(edge.from)}</code> -> <code>${escapeHtml(edge.to)}</code></span>
      <p>${escapeHtml(edge.reason)}</p>
    </li>`;
  }).join("");
}

function fitGraphEdge(edgeId, padding = 80) {
  if (!agentGraph || !edgeId) return;
  const edge = agentGraph.getElementById(edgeId);
  if (!edge?.length) return;
  agentGraph.fit(edge.union(edge.connectedNodes()), padding);
}

function fitGraphNode(stage, padding = 80) {
  if (!agentGraph || !stage) return;
  const node = agentGraph.getElementById(stage);
  if (!node?.length) return;
  agentGraph.fit(node.closedNeighborhood(), padding);
}

function applyGraphFocus() {
  if (!agentGraph) return;
  const classNames = "dimmed focused-node neighbor-node related-outgoing related-incoming selected-edge";
  agentGraph.elements().removeClass(classNames).unselect();

  const stage = selectedGraphStage;
  if (!stage) return;
  const node = agentGraph.getElementById(stage);
  if (!node?.length) return;

  agentGraph.elements().addClass("dimmed");
  node.removeClass("dimmed").addClass("focused-node").select();

  const outgoing = node.outgoers("edge");
  const incoming = node.incomers("edge");
  const relatedEdges = outgoing.union(incoming);
  const relatedNodes = relatedEdges.connectedNodes();

  relatedNodes.removeClass("dimmed").addClass("neighbor-node");
  outgoing.removeClass("dimmed").addClass("related-outgoing");
  incoming.removeClass("dimmed").addClass("related-incoming");

  if (!selectedGraphEdge) return;
  const edge = agentGraph.getElementById(selectedGraphEdge);
  if (!edge?.length) return;
  edge.removeClass("dimmed related-outgoing related-incoming").addClass("selected-edge").select();
  edge.connectedNodes().removeClass("dimmed").addClass("neighbor-node");
}

function focusGraphNode(stage, shouldFit = false) {
  selectedGraphStage = stage;
  selectedGraphEdge = "";
  renderAgentGraphDetails(selectedGraphStage);
  applyGraphFocus();
  if (shouldFit) fitGraphNode(stage, 80);
}

function focusGraphEdge(edgeId, shouldFit = false) {
  if (!agentGraph || !edgeId) return;
  const edge = agentGraph.getElementById(edgeId);
  if (!edge?.length) return;
  selectedGraphEdge = edgeId;
  selectedGraphStage = edge.data("source");
  renderAgentGraphDetails(selectedGraphStage, selectedGraphEdge);
  applyGraphFocus();
  if (shouldFit) fitGraphEdge(edgeId, 90);
}

function renderAgentGraphDetails(stage, selectedEdgeId = selectedGraphEdge) {
  const details = el("agentGraphDetails");
  if (!details || !inspectData) return;
  const transitionRows = graphEdgesForStage(stage);
  const isCurrentStage = normalizeStage(stage) === currentStage();
  details.innerHTML = `<div class="graph-detail-head">
      <span class="eyebrow">${isCurrentStage ? "Current stage" : "Stage preview"}</span>
      <h3>${escapeHtml(stage)}</h3>
      <p>${escapeHtml(stagePurpose(stage))}</p>
    </div>
    ${renderTurnPromptAssembly(stage)}
    ${renderDataLineage(stage)}
    <section class="graph-detail-section">
      <h4>Transition Logic</h4>
      <p class="graph-transition-prompt-note">The backend injects this stage's orange <strong>Out</strong> edges at the end of <code>role=&quot;system&quot;</code>. Blue <strong>In</strong> edges are shown only to explain how other stages return here.</p>
      ${transitionRows.length
        ? `<p class="graph-hint">Select a row to focus its edge.</p><ul class="graph-transition-list">${graphEdgeRowsForStage(stage, selectedEdgeId)}</ul>`
        : "<p>This stage has no transition in the state table.</p>"}
    </section>`;
}

function renderInspectTools(tools) {
  const body = `<div class="inspect-tool-list">
    ${tools.map((tool) => {
      const params = (tool.parameters?.properties || {});
      const required = new Set(tool.parameters?.required || []);
      const paramRows = Object.entries(params).map(([key, schema]) => {
        const isReq = required.has(key);
        const type = schema?.type || (schema?.properties ? "object" : "any");
        return `<li><code>${escapeHtml(key)}</code><span class="tool-type">${escapeHtml(type)}${isReq ? " - required" : ""}</span></li>`;
      }).join("");
      return `<details class="inspect-tool">
        <summary><code>${escapeHtml(tool.name)}</code><span>${escapeHtml(tool.description || "")}</span></summary>
        <ul class="inspect-tool-params">${paramRows || "<li><em>No parameters</em></li>"}</ul>
      </details>`;
    }).join("")}
  </div>`;
  return renderInspectSection("Tools", `${tools.length} structured tool${tools.length === 1 ? "" : "s"} the agent can call.`, body);
}

function renderInspectOutput(shape, rules) {
  const ruleItems = rules.map((rule) => `<li>${escapeHtml(rule)}</li>`).join("");
  const body = `<details class="inspect-output" open>
    <summary>JSON output shape</summary>
    <pre class="inspect-code">${escapeHtml(shape || "")}</pre>
  </details>
  <ol class="inspect-rule-list">${ruleItems}</ol>`;
  return renderInspectSection("Output Rules", "Every agent turn must satisfy these rules.", body);
}

function renderInspectPrompts(prompts) {
  const body = `<div class="inspect-prompt-list">
    ${prompts.map((p) => `<details class="inspect-prompt">
      <summary>
        <strong>${escapeHtml(p.title || p.filename || "Prompt")}</strong>
        <span><code>${escapeHtml(p.filename || "")}</code> - ${escapeHtml(p.scope || "")}</span>
      </summary>
      <pre class="inspect-code">${escapeHtml(p.content || "")}</pre>
    </details>`).join("")}
  </div>`;
  return renderInspectSection("Prompts", "System prompts assembled per stage. Click to expand any prompt.", body);
}

function renderScenarioLayers(run) {
  const layers = Array.isArray(run?.scenario_layers) ? run.scenario_layers : [];
  if (!layers.length) return "";
  const body = layers
    .map((layer) => {
      const state = layer.passed === true ? "passed" : layer.passed === false ? "failed" : "unknown";
      const stateText = layer.passed === true ? "PASS" : layer.passed === false ? "FAIL" : "NOT ESTABLISHED";
      const details = (Array.isArray(layer.details) ? layer.details : [])
        .map((d) => `<li>${escapeHtml(String(d))}</li>`)
        .join("");
      const diagnosis = layer.diagnosis
        ? `<p class="test-layer-diagnosis">${escapeHtml(String(layer.diagnosis))}</p>`
        : "";
      const results = (Array.isArray(layer.results) ? layer.results : [])
        .map((r, index) => renderSampleResult(r, index, layer.layer === "L2" ? "negative" : "positive"))
        .join("");
      return `<details class="sample-result sample-${state}" ${layer.passed === false ? "open" : ""}>
        <summary>
          <span class="${state}">${stateText}</span>
          <span class="sample-query">${escapeHtml(layer.layer)} - ${escapeHtml(layer.title || "")}</span>
        </summary>
        ${details ? `<ul class="test-layer-details">${details}</ul>` : ""}
        ${diagnosis}
        ${results}
      </details>`;
    })
    .join("");
  return `<div class="test-result-group"><strong>Scenario layers</strong> <span class="test-group-hint">cheapest first; a layer only runs when the one above it passed</span>${body}</div>`;
}

function renderTestRunNotes(run) {
  const notes = Array.isArray(run?.notes) ? run.notes.filter(Boolean) : [];
  if (!notes.length) return "";
  return `<div class="test-run-notes">${notes
    .map((n) => `<p><strong>Not verified by this run.</strong> ${escapeHtml(String(n))}</p>`)
    .join("")}</div>`;
}

function testRunScoreHtml(run) {
  const layers = Array.isArray(run?.scenario_layers) ? run.scenario_layers : [];
  if (layers.length) {
    const parts = layers.map((l) => `${escapeHtml(l.layer)} ${l.passed === true ? "PASS" : l.passed === false ? "FAIL" : "-"}`);
    return `<span class="test-run-score">${parts.join(" · ")}</span>`;
  }
  return `<span class="test-run-score">Positive: ${(run.positive_hit_rate * 100).toFixed(0)}% · Negative: ${(run.negative_correct_reject_rate * 100).toFixed(0)}%</span>`;
}

function renderResultList(label, results) {
  const kind = String(label).toLowerCase().startsWith("pos") ? "positive" : "negative";
  const groupHint = kind === "positive" ? "should select this skill" : "should NOT select this skill";
  return `<div class="test-result-group"><strong>${label}</strong> <span class="test-group-hint">${groupHint}</span>${(results || [])
    .map((r, index) => renderSampleResult(r, index, kind))
    .join("")}</div>`;
}

// Measured on real route_only runs: 14.6K chars for a full script response,
// 5.7K for a negative sample. The cap is headroom over that, not a guess.
const RESPONSE_PREVIEW_CHARS = 20000;
const longTextStore = new Map();
let longTextSeq = 0;

function renderLongText(label, text, { open = false } = {}) {
  const full = String(text || "");
  if (!full) return "";
  const truncated = full.length > RESPONSE_PREVIEW_CHARS;
  const key = `lt${++longTextSeq}`;
  if (truncated) {
    longTextStore.set(key, full);
    while (longTextStore.size > 40) longTextStore.delete(longTextStore.keys().next().value);
  }
  const size = `${full.length.toLocaleString()} chars`;
  return `<details class="sample-block" ${open && !truncated ? "open" : ""}>
    <summary>${escapeHtml(label)} <span class="test-group-hint">${escapeHtml(size)}</span></summary>
    <pre>${escapeHtml(truncated ? full.slice(0, RESPONSE_PREVIEW_CHARS) : full)}</pre>
    ${truncated ? `<div class="tool-actions"><span class="test-group-hint">Truncated for display.</span><button type="button" data-download-text="${key}" data-download-name="${escapeHtml(label)}">Download full text</button></div>` : ""}
  </details>`;
}

function modeBadgeHtml(result) {
  const mode = String(result?.apim_raw_response?.mode || "");
  if (!mode) return "";
  const routeOnly = mode === "route_only";
  const note = routeOnly ? "execution not verified" : "the skill really ran";
  return `<span class="sample-mode ${routeOnly ? "mode-route-only" : "mode-execute"}">${escapeHtml(mode)} - ${escapeHtml(note)}</span>`;
}

function renderSampleResult(result, index, kind = "") {
  const state = result.passed === true ? "passed" : result.passed === false ? "failed" : "unknown";
  const stateText = result.passed === true ? "PASS" : result.passed === false ? "FAIL" : "ERROR";
  const expectedSkill = result.expected_skill || "";
  const actual = result.actual_skill || "none";
  const referencedList = Array.isArray(result.skills_referenced) ? result.skills_referenced.filter(Boolean) : [];
  const loadedResources = Array.isArray(result.loaded_resources) ? result.loaded_resources.filter(Boolean) : [];
  const isNegative = kind === "negative" || (!expectedSkill && kind !== "positive");
  const routedTo = referencedList.length ? referencedList.join(", ") : (actual && actual !== "none" ? actual : "no skill");
  const expectationText = isNegative ? "should NOT select this skill" : `should select ${expectedSkill || "this skill"}`;
  const raw = result.apim_raw_response && Object.keys(result.apim_raw_response).length
    ? JSON.stringify(result.apim_raw_response, null, 2)
    : "";
  return `<details class="sample-result sample-${state}">
    <summary>
      <span class="${state}">${stateText}</span>
      <span class="sample-query">${escapeHtml(index + 1)}. ${escapeHtml(result.query || "")}</span>
      <span class="sample-skill">routed: ${escapeHtml(routedTo)}</span>
    </summary>
    <div class="sample-expectation">This sample expects it <strong>${escapeHtml(expectationText)}</strong>.</div>
    <div class="sample-meta">
      <span>Expected: <code>${escapeHtml(isNegative ? "not this skill" : (expectedSkill || "this skill"))}</code></span>
      <span>Routed to: <code>${escapeHtml(routedTo)}</code></span>
      ${modeBadgeHtml(result)}
      ${loadedResources.length ? `<span>Loaded resources: <code>${escapeHtml(loadedResources.join(", "))}</code></span>` : ""}
      ${result.apim_status ? `<span>Status: <code>${escapeHtml(result.apim_status)}</code></span>` : ""}
      ${result.apim_session_id ? `<span>APIM session: <code>${escapeHtml(result.apim_session_id)}</code></span>` : ""}
      ${result.duration_ms ? `<span>${escapeHtml(result.duration_ms)}ms</span>` : ""}
    </div>
    ${result.error ? `<div class="sample-error">${escapeHtml(result.error)}</div>` : ""}
    ${renderLongText("APIM response", result.apim_response, { open: result.passed === false })}
    ${renderLongText("APIM raw JSON", raw)}
    ${result.apim_uploads?.length ? `<div class="sample-block"><strong>Uploads</strong><pre>${escapeHtml(result.apim_uploads.join("\n"))}</pre></div>` : ""}
    <details class="sample-request">
      <summary>Request sent</summary>
      <pre>${escapeHtml(result.request_sent || result.query || "")}</pre>
    </details>
  </details>`;
}

function latestTestRun() {
  const runs = session?.test_runs || [];
  return runs.length ? runs[runs.length - 1] : null;
}

function appendTestRunCard(run, title = "Test run") {
  if (!run || document.querySelector(`[data-test-run-card="${CSS.escape(run.run_id)}"]`)) return;
  document.querySelector(".chat-empty")?.remove();
  const card = document.createElement("div");
  card.className = "tool-card test-card conversation-tool resolved";
  card.dataset.testRunCard = run.run_id;
  const positives = run.positive_results || [];
  const negatives = run.negative_results || [];
  const all = [...positives, ...negatives];
  const failures = all.filter((item) => item.passed !== true).length;
  card.innerHTML = `<div class="test-card-head">
      <div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(formatTestRunScore(run))}</p>
      </div>
      <span class="${failures ? "failed" : "passed"}">${failures ? `${failures} issue(s)` : "Passed"}</span>
    </div>
    ${renderTestCardGroup("Positive samples", positives)}
    ${renderTestCardGroup("Negative samples", negatives)}
    <div class="tool-actions">
      <button type="button" data-test-details>Details</button>
    </div>`;
  card.querySelector("[data-test-details]")?.addEventListener("click", () => openTestRunDetails(run.run_id));
  el("chatStream").appendChild(card);
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
}

function formatTestRunScore(run) {
  return `Positive ${Math.round((run.positive_hit_rate || 0) * 100)}% - Negative ${Math.round((run.negative_correct_reject_rate || 0) * 100)}%`;
}

function renderTestCardGroup(label, results) {
  if (!results?.length) return "";
  return `<div class="test-card-group">
    <strong>${escapeHtml(label)}</strong>
    ${results.map((result, index) => {
      const state = result.passed === true ? "PASS" : result.passed === false ? "FAIL" : "ERROR";
      const stateClass = result.passed === true ? "passed" : result.passed === false ? "failed" : "sample-unknown";
      // The chat card is a summary; the full response lives in the Tests panel.
      const response = result.reasoning || result.error || "";
      return `<div class="test-card-sample">
        <div><span class="${stateClass}">${state}</span><p>${escapeHtml(index + 1)}. ${escapeHtml(result.query || "")}</p></div>
        <pre>${escapeHtml(response)}</pre>
      </div>`;
    }).join("")}
  </div>`;
}

function openTestRunDetails(runId) {
  setActiveContextTab("tests");
  window.setTimeout(() => {
    const node = document.querySelector(`[data-test-run-id="${CSS.escape(runId)}"]`);
    if (!node) return;
    node.open = true;
    node.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 50);
}

let activeActivityGroup = null;

const ACTIVITY_TOOL_META = {
  record_research: { icon: "i-bolt", title: "Research recorded" },
  record_understanding: { icon: "i-bolt", title: "Understanding updated" },
  record_reflection: { icon: "i-bolt", title: "Reflection recorded" },
  request_stage_transition: { icon: "i-bolt", title: "Stage transition requested" },
};

function resetActivityGroup() {
  if (activeActivityGroup) {
    const count = activeActivityGroup.querySelector(".activity-list")?.children.length ?? 0;
    dbgLog("activity", `group closed (${count} action${count === 1 ? "" : "s"})`);
  }
  activeActivityGroup = null;
}

function appendActivityRow(call) {
  const args = call.args || {};
  const meta = ACTIVITY_TOOL_META[call.tool] || { icon: "i-bolt", title: labelize(call.tool || "Tool update") };
  const detail = summarizeToolArgs(args);
  const showDetail = detail && detail !== "No extra action required." && detail !== "Updated.";
  const startedNewGroup = !activeActivityGroup || !activeActivityGroup.isConnected;
  if (startedNewGroup) {
    document.querySelector(".chat-empty")?.remove();
    const group = document.createElement("details");
    group.className = "tool-card activity-card conversation-tool";
    group.dataset.testid = "activity-card";
    group.innerHTML = `<summary><span class="activity-head">${svgIcon("i-bolt")}<span class="activity-title">Agent actions</span><span class="activity-count" data-testid="activity-count"></span></span></summary><div class="activity-list"></div>`;
    el("chatStream").appendChild(group);
    activeActivityGroup = group;
  }
  const list = activeActivityGroup.querySelector(".activity-list");
  const row = document.createElement("div");
  row.className = "activity-row";
  row.dataset.callId = call.call_id || "";
  row.innerHTML = `<strong>${svgIcon(meta.icon)} ${escapeHtml(meta.title)}</strong>${showDetail ? `<p>${escapeHtml(detail)}</p>` : ""}`;
  list.appendChild(row);
  activeActivityGroup.querySelector(".activity-count").textContent = String(list.children.length);
  dbgLog("activity", `${startedNewGroup ? "new group + " : ""}row #${list.children.length}: ${call.tool}`, { call_id: call.call_id, title: meta.title, detail: showDetail ? detail : null });
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
}

// D1: in-chat editable POSITIVE routing samples card. Submitting writes them to
// the Prepare Brief (so the right-hand Routing samples panel syncs) and hands
// control back to the agent to continue with the negatives (E.2).
function positiveCardRowHtml(value = "") {
  return `<div class="psc-row"><input type="text" class="psc-input" value="${escapeHtml(String(value || ""))}" placeholder="A real query that SHOULD route to this skill"><button type="button" class="sample-del" data-psc-del title="Remove">\u2715</button></div>`;
}

function renderPositiveSamplesCard(call) {
  const args = call.args || {};
  const existing = Array.isArray(session?.prepare_brief?.positive_samples) ? session.prepare_brief.positive_samples : [];
  const seed = existing;
  const rows = (seed.length ? seed : ["", ""]).map((q) => positiveCardRowHtml(q)).join("");
  const card = document.createElement("div");
  card.className = "tool-card conversation-tool positive-samples-card";
  card.dataset.callId = call.call_id;
  card.dataset.testid = "positive-samples-card";
  card.innerHTML = `<strong>${svgIcon("i-beaker")} Positive routing samples</strong>
    <p>${escapeHtml(args.prompt || "Add real queries that SHOULD route to this skill. Cover different real intents \u2014 the more the better.")}</p>
    <div class="psc-rows" data-psc-rows>${rows}</div>
    <button type="button" class="spl-add-btn" data-psc-add>+ Add query</button>
    <div class="tool-actions"></div>`;
  card.querySelector("[data-psc-add]").addEventListener("click", () => {
    const rowsNode = card.querySelector("[data-psc-rows]");
    rowsNode.insertAdjacentHTML("beforeend", positiveCardRowHtml(""));
    rowsNode.lastElementChild?.querySelector("input")?.focus();
  });
  card.querySelector("[data-psc-rows]").addEventListener("click", (event) => {
    const del = event.target.closest("[data-psc-del]");
    if (del) del.closest(".psc-row")?.remove();
  });
  card.querySelector(".tool-actions").appendChild(
    actionButton("Submit positives", async () => submitPositiveSamplesCard(call, card)),
  );
  appendConversationTool(card);
  resetActivityGroup();
}

async function submitPositiveSamplesCard(call, card) {
  const positive = [...card.querySelectorAll(".psc-input")].map((i) => i.value.trim()).filter(Boolean);
  card.querySelectorAll("input, button").forEach((node) => { node.disabled = true; });
  card.classList.add("resolved");
  try {
    session = await sendToolResult(session.id, call.call_id, { positive });
    pendingQuestionCalls.delete(call.call_id);
    persistSessionState();
    renderSession();
    await refreshSessions();
    await sendChatPayload(
      `I've filled ${positive.length} positive routing sample(s) in the table. Please validate them against the key capabilities and, if the negative samples are still empty, derive them from the neighbor skills (E.2) without overwriting any negatives I've already edited.`,
      [],
    );
  } catch (err) {
    appendConversationStatus(`Could not submit positive samples: ${err.message}`, { failed: true });
    card.classList.remove("resolved");
    card.querySelectorAll("input, button").forEach((node) => { node.disabled = false; });
  }
}

function renderToolCall(call) {
  dbgLog("render", `renderToolCall: ${call.tool}`, { call_id: call.call_id, args: call.args || {} });
  if (renderPassiveToolCall(call)) {
    dbgLog("render", `-> passive status only: ${call.tool}`);
    return;
  }

  const args = call.args || {};

  if (call.tool === "ask_user_input") {
    dbgLog("render", "-> question block", { call_id: call.call_id });
    pendingQuestionCalls.set(call.call_id, call);
    renderQuestionBlock();
    resetActivityGroup();
    return;
  }

  if (call.tool === "request_positive_samples") {
    dbgLog("render", "-> positive samples card", { call_id: call.call_id });
    pendingQuestionCalls.set(call.call_id, call);
    renderPositiveSamplesCard(call);
    resetActivityGroup();
    return;
  }

  // Lightweight, informational tool calls fold into a single inline "Agent
  // actions" card so they read in chronological order under the assistant reply.
  if (!["propose_skill_draft", "propose_patch", "rename_skill", "request_test_run", "propose_neighbor_edit"].includes(call.tool)) {
    dbgLog("render", `-> folded into activity card: ${call.tool}`);
    appendActivityRow(call);
    return;
  }
  dbgLog("render", `-> inline actionable card: ${call.tool}`);

  const card = document.createElement("div");
  card.className = "tool-card conversation-tool";
  card.dataset.callId = call.call_id;

  if (call.tool === "propose_skill_draft") {
    el("toolCalls").innerHTML = "";
    card.dataset.testid = "draft-card";
    card.innerHTML = `<strong>${svgIcon("i-document-text")} SKILL.md ready</strong><p>Review SKILL.md in the Files tab. Accepting stores this draft and moves you into refinement.</p>
      <label class="draft-name">Skill name
        <input type="text" data-testid="draft-name-input" value="${escapeHtml(normalizeSkillName(inferCurrentName(args.skill_md)))}" placeholder="my-skill-name" maxlength="64" spellcheck="false" autocomplete="off">
      </label>
      <div class="draft-public">
        <label for="draftPublicToggle">
          <input id="draftPublicToggle" type="checkbox" data-testid="draft-public-toggle">
          Public - everyone can use this skill
        </label>
        <p class="draft-public-hint">Leave this off to keep the skill private; you can change it later from Skill access.</p>
      </div>
      <p class="patch-note">Lowercase letters, digits and hyphens only, no leading or trailing hyphen, 64 characters max. Renaming after this point moves the Blob folder, the SQL row and every grant.</p>
      <p class="patch-note card-error" hidden></p>
      <div class="tool-actions"></div>`;
    const nameInput = card.querySelector("[data-testid='draft-name-input']");
    const nameError = card.querySelector(".card-error");
    nameInput.addEventListener("input", () => {
      const caret = nameInput.selectionStart;
      const coerced = coerceSkillNameInput(nameInput.value);
      if (coerced !== nameInput.value) {
        nameInput.value = coerced;
        nameInput.setSelectionRange(caret, caret);
      }
      nameError.hidden = true;
    });
    card.querySelector(".tool-actions").appendChild(
      actionButton("Accept SKILL.md", async () => {
        const name = normalizeSkillName(nameInput.value);
        if (!SKILL_NAME_RE.test(name)) {
          nameError.textContent = "Enter a name using lowercase letters, digits and hyphens, with no leading or trailing hyphen.";
          nameError.hidden = false;
          nameInput.focus();
          return;
        }
        nameError.hidden = true;
        const makePublic = draftPublicChoice();
        if (makePublic === null) return;
        await acceptTool(call, { action: "accept", name });
        if (makePublic) await publishAfterDraftAccept(name);
      }),
    );
  } else if (call.tool === "propose_patch") {
    card.classList.add("patch-card");
    card.dataset.testid = "patch-card";
    card.innerHTML = `<div class="patch-summary"><strong>${svgIcon("i-adjustments")} Patch proposed</strong><span>${escapeHtml(args.target_file || "SKILL.md")}</span></div>
      <p>${escapeHtml(args.reason || "Review this change before applying it.")}</p>
      <details class="patch-diff">
        <summary>Review diff</summary>
        ${renderPatchDiff(args.patch || "")}
      </details>
      <div class="tool-actions"></div>`;
    const actions = card.querySelector(".tool-actions");
    actions.appendChild(actionButton("Accept patch", async () => acceptTool(call, { action: "accept" })));
    actions.appendChild(actionButton("Reject", async () => acceptTool(call, { action: "reject" })));
  } else if (call.tool === "rename_skill") {
    card.classList.add("patch-card");
    card.dataset.testid = "rename-card";
    const oldName = escapeHtml(session?.remote_skill_id || session?.target_skill_id || inferCurrentName());
    const newName = escapeHtml(String(args.new_name || ""));
    card.innerHTML = `<div class="patch-summary"><strong>${svgIcon("i-adjustments")} Rename proposed</strong><span>${oldName} &rarr; ${newName}</span></div>
      <p>${escapeHtml(args.reason || "Rename this skill.")}</p>
      <p class="patch-note">Accepting moves the Blob folder, the SQL row and every grant to <code>${newName}</code> and deletes <code>${oldName}</code>. Other skills that still reference <code>${oldName}</code> are not updated.</p>
      <div class="tool-actions"></div>`;
    const actions = card.querySelector(".tool-actions");
    actions.appendChild(actionButton("Accept rename", async () => acceptTool(call, { action: "accept" })));
    actions.appendChild(actionButton("Reject", async () => acceptTool(call, { action: "reject" })));
  } else if (call.tool === "request_test_run") {
    card.innerHTML = `<strong>${svgIcon("i-beaker")} Run skill-selection test</strong><p>Run the proposed positive and negative samples against the current skill.</p>`;
    card.appendChild(actionButton("Run", async () => acceptTool(call, { action: "run" })));
  } else if (call.tool === "propose_neighbor_edit") {
    card.classList.add("patch-card");
    card.dataset.testid = "neighbor-edit-card";
    const skill = escapeHtml(String(args.skill_name || "neighbor skill"));
    let body;
    if (args.patch) {
      body = `<details class="patch-diff" open><summary>Review diff</summary>${renderPatchDiff(args.patch)}</details>`;
    } else {
      body = `<details class="patch-diff"><summary>Review change</summary><pre>${escapeHtml(String(args.skill_md || "").slice(0, 2000))}</pre></details>`;
    }
    card.innerHTML = `<div class="patch-summary"><strong>${svgIcon("i-adjustments")} Neighbor skill edit proposed</strong><span>${skill}</span></div>
      <p>${escapeHtml(args.label || "Update this neighbor's description / When NOT to Use to stay mutually exclusive.")}</p>
      ${body}
      <p class="patch-note">Accepting adds this as a new version on the neighbor's edit card (right panel). Nothing is written to Blob until you press Save to Blob there.</p>
      <div class="tool-actions"></div>`;
    const actions = card.querySelector(".tool-actions");
    actions.appendChild(actionButton("Accept edit", async () => acceptTool(call, { action: "accept" })));
    actions.appendChild(actionButton("Reject", async () => acceptTool(call, { action: "reject" })));
  }

  appendConversationTool(card);
  resetActivityGroup();
  dbgLog("render", `actionable card mounted: ${call.tool}`, { call_id: call.call_id });
}

function renderQuestionBlock() {
  document.querySelector(".chat-empty")?.remove();
  const calls = [...pendingQuestionCalls.values()].filter((call) => !document.querySelector(`[data-question-id="${call.call_id}"].resolved`));
  if (!calls.length) {
    renderQuestionQueueStatus();
    return;
  }

  let block = document.querySelector(".choice-card[data-question-block]:not(.resolved)");
  if (!block) {
    block = document.createElement("div");
    block.className = "tool-card choice-card conversation-tool";
    block.dataset.questionBlock = "true";
    el("chatStream").appendChild(block);
  }
  const title = calls.length === 1 ? "Decision needed" : `${calls.length} decisions needed`;
    block.dataset.testid = "question-card";
    block.innerHTML = `<div class="choice-head"><strong>${svgIcon("i-chat")} ${escapeHtml(title)}</strong><span class="choice-progress" data-testid="question-progress">${escapeHtml(choiceProgressText())}</span></div>
    <div class="question-list">
      ${calls.map((call, index) => renderQuestionItem(call, index)).join("")}
    </div>
    <div class="tool-actions">
      <button class="primary" type="button" data-submit-questions data-testid="submit-questions-button">Submit answers</button>
    </div>`;

  calls.forEach((call) => hydrateQuestionItem(block, call));
  block.querySelector("[data-submit-questions]")?.addEventListener("click", submitChoiceAnswers);
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
  updateChoiceCardMeta();
}

function isSamplesQuestion(call) {
  const q = String(call?.args?.question || "").toLowerCase();
  return q.includes("samples_confirmed") || q.includes("routing sample");
}

const NEGATIVE_SECTION_RE = /(negative|should\s*not|out[-\s]?of[-\s]?scope|\u53cd\u5411|\u8ca0\u5411|\u4e0d\u61c9|\u4e0d\u8a72|\u4e0d\u5c6c\u65bc)/i;
const POSITIVE_SECTION_RE = /(positive|should\s*(route|select|match|hit)|\u6b63\u5411|\u61c9\u547d\u4e2d|\u61c9\u8a72|\u5019\u9078\u6b63\u5411)/i;

// Split the agent's question into proposed positive / negative candidate
// queries. Numbered lines are routed to the section last announced by a header;
// before any header they default to positive.
function parseCandidateSamples(question) {
  const positive = [];
  const negative = [];
  let bucket = positive;
  for (const raw of String(question || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const numbered = line.match(/^\d+\s*[.)\u3001]\s*(.+?)\s*$/);
    if (numbered && numbered[1]) {
      bucket.push(numbered[1]);
      continue;
    }
    // Header/section line: decide which bucket the following items belong to.
    if (NEGATIVE_SECTION_RE.test(line)) bucket = negative;
    else if (POSITIVE_SECTION_RE.test(line)) bucket = positive;
  }
  return { positive, negative };
}

function sampleRowHtml(value, kind) {
  const ph = kind === "positive" ? "A query that SHOULD hit this skill" : "A query that should NOT hit this skill";
  const hint = kind === "positive" ? "Expected: routes to THIS skill" : "Expected: routes to a DIFFERENT skill";
  return `<div class="sample-row sample-row-captioned" data-kind="${kind}">
    <div class="sample-input-wrap">
      <input type="text" class="sample-input" data-kind="${kind}" value="${escapeHtml(value || "")}" placeholder="${ph}">
      <span class="sample-expect-hint">${hint}</span>
    </div>
    <button type="button" class="sample-del" data-del-sample title="Remove this row">\u2715</button>
  </div>`;
}

function renderSamplesTable(call) {
  const saved = pendingChoiceAnswers.get(call.call_id);
  const data = saved?.samples || parseCandidateSamples(call.args?.question);
  const positive = data.positive.length ? data.positive : [""];
  const negative = data.negative.length ? data.negative : [""];
  return `<div class="samples-editor" data-samples-editor>
    <div class="samples-group">
      <div class="samples-group-head"><strong>Positive samples — should hit</strong><button type="button" class="sample-add" data-add-sample="positive">+ Add row</button></div>
      <div class="samples-rows" data-rows="positive">${positive.map((v) => sampleRowHtml(v, "positive")).join("")}</div>
    </div>
    <div class="samples-group">
      <div class="samples-group-head"><strong>Negative samples — should not hit</strong><button type="button" class="sample-add" data-add-sample="negative">+ Add row</button></div>
      <div class="samples-rows" data-rows="negative">${negative.map((v) => sampleRowHtml(v, "negative")).join("")}</div>
    </div>
    <label class="custom-answer">
      <span>Notes (optional)</span>
      <textarea rows="2" data-samples-notes placeholder="e.g. wrong direction, want to fix the skill definition first"></textarea>
    </label>
  </div>`;
}

function renderQuestionItem(call, index) {
  const args = call.args || {};
  if (isSamplesQuestion(call)) {
    return `<section class="question-item" data-question-id="${escapeHtml(call.call_id)}" data-samples="true" data-testid="question-item">
    <div class="question-title"><span>${index + 1}</span><p class="md-inline">${renderMdInline(args.question || "Please confirm the routing samples")}</p></div>
    ${renderSamplesTable(call)}
  </section>`;
  }
  const options = Array.isArray(args.options) ? args.options : [];
  return `<section class="question-item" data-question-id="${escapeHtml(call.call_id)}" data-testid="question-item">
    <div class="question-title"><span>${index + 1}</span><p class="md-inline">${renderMdInline(args.question || "Please choose the next step")}</p></div>
    <div class="choice-actions">
      ${options.map((option, optionIndex) => `<label class="choice-option ${optionIndex === 0 ? "recommended" : ""}">
        <input type="radio" name="choice-${escapeHtml(call.call_id)}" value="${escapeHtml(option)}">
        <span class="md-inline">${renderMdInline(option)}</span>
      </label>`).join("")}
    </div>
    <label class="custom-answer">
      <span>Custom answer</span>
      <textarea rows="2" data-custom-answer data-testid="custom-answer" placeholder="Type your own answer for this question"></textarea>
    </label>
  </section>`;
}

function hydrateQuestionItem(block, call) {
  const item = block.querySelector(`[data-question-id="${CSS.escape(call.call_id)}"]`);
  if (!item) return;
  if (isSamplesQuestion(call)) {
    hydrateSamplesItem(item, call);
    return;
  }
  const saved = pendingChoiceAnswers.get(call.call_id);
  if (saved?.custom) item.querySelector("[data-custom-answer]").value = saved.custom;
  if (saved?.selected) {
    const radio = [...item.querySelectorAll("input[type='radio']")].find((node) => node.value === saved.selected);
    if (radio) radio.checked = true;
  }
  item.querySelectorAll("input[type='radio']").forEach((radio) => {
    radio.addEventListener("change", () => recordQuestionAnswer(call));
  });
  item.querySelector("[data-custom-answer]")?.addEventListener("input", () => recordQuestionAnswer(call));
}

function appendConversationTool(card) {
  document.querySelector(".chat-empty")?.remove();
  el("chatStream").appendChild(card);
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
  renderQuestionQueueStatus();
}

function renderInspectSessionState() {
  const pending = session?.pending_tool_calls || [];
  const choices = pending.filter((call) => call.tool === "ask_user_input");
  const progress = choiceProgress();
  const checklist = session?.verify_checklist || {};
  const checklistEntries = Object.entries(checklist);
  const confirmed = checklistEntries.filter(([, item]) => item.status === "confirmed").length;
  const binding = skillBindingState();
  const rows = [
    ["Session", session?.id || "none"],
    ["Stage", currentStage()],
    ["Blob skill", binding.hasRemote ? binding.remoteName : "NEW"],
    ["Remote version", binding.remoteVersion || "none"],
    ["Local version", binding.localVersion || "none"],
    ["Local vs remote", binding.state === "unsaved" ? "UNSAVED" : binding.state],
    ["Confirmation questions", progress.total ? choiceProgressText(progress) : "none"],
    ["Pending tool calls", String(pending.length)],
    ["Pending choice IDs", choices.map((call) => call.call_id).join(", ") || "none"],
    ["Checklist", checklistEntries.length ? `${confirmed}/${checklistEntries.length} confirmed` : "not populated"],
    ["ACA env", session?.aca_env_error ? `failed: ${session.aca_env_error}` : session?.aca_env_result ? "loaded" : "not loaded"],
  ];
  const body = `<div class="inspect-state-grid">
    ${rows.map(([label, value]) => `<div class="inspect-state-row"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span></div>`).join("")}
  </div>`;
  return renderInspectSection("Current Session", "Use this to check whether the UI is waiting on unanswered confirmation cards.", body);
}

function renderPassiveToolCall(call) {
  const args = call.args || {};
  if (call.tool === "stage_transition") {
    appendConversationStatus(formatStageTransitionMessage(args));
    return true;
  }
  if (call.tool === "update_verify_checklist") {
    appendConversationStatus(`Checklist updated: ${args.item || "item"} - ${args.status || "updated"}`);
    return true;
  }
  if (call.tool === "update_prepare_checklist") {
    const label = (PREPARE_CHECKPOINT_LABELS[args.item] || {}).title || args.item || "checkpoint";
    const verb = args.confirmed ? "Confirmed" : "Updated";
    const evidence = (args.evidence || "").trim();
    appendConversationStatus(`${verb}: ${label}${evidence ? ` - ${evidence}` : ""}`);
    return true;
  }
  if (call.tool === "request_materials") {
    appendConversationStatus(args.message || "Please add more material before sending again.");
    return true;
  }
  if (call.tool === "show_test_results") {
    appendConversationStatus("Test results updated.");
    return true;
  }
  return false;
}

function appendStatus(text) {
  appendConversationStatus(text);
}

function formatStageTransitionMessage(args) {
  const target = args.target;
  if (!target) return `Continuing${args.summary ? `: ${args.summary}` : ""}`;
  const fromStage = currentStage();
  const fromGroup = STAGE_TO_GROUP[fromStage];
  const toGroup = STAGE_TO_GROUP[target];
  const summary = args.summary ? ` - ${args.summary}` : "";
  if (target === "DONE") return `-> Done${summary}`;
  if (toGroup && fromGroup === toGroup) {
    return `Now: ${SUBSTAGE_LABELS[target] || target}${summary}`;
  }
  const groupTitle = (groupMeta[toGroup] || {}).title || target;
  return `-> ${groupTitle}${summary}`;
}

function appendConversationStatus(text, options = {}) {
  document.querySelector(".chat-empty")?.remove();
  resetActivityGroup();
  const node = document.createElement("div");
  node.className = `status-line conversation-status${options.running ? " running" : ""}${options.failed ? " failed-status" : ""}`;
  if (options.ephemeral) node.classList.add("ephemeral");
  node.innerHTML = options.running
    ? `<span class="mini-spinner"></span><span>${escapeHtml(text)}</span>`
    : escapeHtml(text);
  el("chatStream").appendChild(node);
  el("chatStream").scrollTop = el("chatStream").scrollHeight;
  return node;
}

function labelize(value) {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function compactValue(value) {
  if (value === null || value === undefined || value === "") return "Waiting for confirmation.";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function summarizeToolArgs(args) {
  if (!args || Object.keys(args).length === 0) return "No extra action required.";
  if (args.message) return args.message;
  if (args.summary) return args.summary;
  if (args.reason) return args.reason;
  return "Updated.";
}

function actionButton(text, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = text;
  button.addEventListener("click", handler);
  return button;
}

// Returns the chosen visibility, or null when the user backs out of the
// confirmation and the whole accept must be abandoned.
function draftPublicChoice() {
  const toggle = document.getElementById("draftPublicToggle");
  if (!toggle || !toggle.checked) return false;
  const ok = window.confirm(
    "Make this skill public?\n\n"
    + "Every signed-in user in the tenant will see it and can run it, and no grant is required. "
    + "Any internal child skills are published with it.\n\n"
    + "You can change this later from Skill access.",
  );
  return ok ? true : null;
}

// Saving is a content action, so publishing is a second, explicit call that is
// only made once the draft is really committed under this name.
async function publishAfterDraftAccept(name) {
  if (session?.remote_skill_id !== name) return;
  try {
    await setSkillVisibility(name, true);
    try { await refreshSkills(); } catch {}
    renderSession();
    appendConversationStatus(`${name} is now public. Every signed-in user can use it.`);
  } catch (err) {
    appendConversationStatus(
      `${name} was saved but is still private: ${err.message}. Retry from Skill access.`,
      { failed: true },
    );
  }
}

async function acceptTool(call, result) {
  dbgLog("action", `acceptTool: ${call.tool} -> ${result.action || "done"}`, { call_id: call.call_id, result });
  let runningNode = null;
  if (call.tool === "request_test_run" && result.action !== "reject") {
    const source = await prepareRunTest();
    if (!source) return;
    if (source === "remote" && localEditorDirty) await persistCurrentDraftNow();
    result = { ...result, source: source === "current_saved" ? "current" : source };
  }
  setToolCardBusy(call.call_id, true);
  try {
    if (call.tool === "request_test_run") {
      setTestStatus({ status: "started" });
      runningNode = appendConversationStatus("Running skill-selection tests through APIM /run in route_only mode (routing only, nothing is executed)...", { running: true });
    } else if (call.tool === "propose_skill_draft" && result.action === "accept") {
      runningNode = appendConversationStatus("Saving draft to Blob + SQL...", { running: true });
    } else if (call.tool === "propose_patch" && result.action === "accept") {
      runningNode = appendConversationStatus("Applying patch and syncing to Blob + SQL...", { running: true });
    } else if (call.tool === "rename_skill" && result.action === "accept") {
      runningNode = appendConversationStatus("Renaming and syncing to Blob + SQL...", { running: true });
    }
    try {
      session = await sendToolResult(session.id, call.call_id, result);
    } catch (innerErr) {
      const orphanAction = chooseOrphanAction(innerErr);
      if (orphanAction) {
        session = await sendToolResult(session.id, call.call_id, { ...result, orphan_action: orphanAction });
      } else if (isOrphanDecisionError(innerErr)) {
        removeConversationStatus(runningNode);
        setToolCardBusy(call.call_id, false);
        appendConversationStatus("Save cancelled. No child metadata was changed.");
        return;
      } else
      if (innerErr && innerErr.stale) {
        appendConversationStatus("This action was already processed on the server. Refreshing session state.");
        try {
          session = await getSession(session.id);
          persistSessionState();
        } catch (refreshErr) {
          appendConversationStatus(`Could not refresh session: ${refreshErr.message}`, { failed: true });
        }
        resolveToolCard(call.call_id, "stale", { tool: call.tool });
        renderSession();
        await refreshSessions();
        return;
      }
      throw innerErr;
    }
    if (runningNode && call.tool !== "request_test_run") {
      markConversationStatus(runningNode, "Saved to Blob + SQL.");
      window.setTimeout(() => removeConversationStatus(runningNode), 1400);
      runningNode = null;
    }
    if (call.tool === "propose_skill_draft" && result.action === "accept") localEditorDirty = false;
    dbgLog("action", `tool result applied: ${call.tool}`, { call_id: call.call_id, stage: session?.current_stage });
    resolveToolCard(call.call_id, result.action || "done", { tool: call.tool });
    renderSession();
    await refreshSessions();
    if (call.tool === "propose_skill_draft" && result.action === "accept") {
      // The public badge reads availableSkills, so a stale list hides the state.
      try { await refreshSkills(); } catch {}
    }
    if (call.tool === "request_test_run") {
      setTestStatus({ status: "completed" });
      markConversationStatus(runningNode, "Test completed. Reviewing results...");
      appendTestRunCard(latestTestRun(), "Agent requested test run");
    }
    if (call.tool === "request_test_run" && result.action !== "reject") {
      appendStatus("Test completed. Asking the model to review the result and propose the next step.");
      await sendChatPayload(testAnalysisPrompt(), []);
    }
    if (call.tool === "propose_patch" && result.action === "accept") {
      appendStatus("Patch applied. Requesting the next skill-selection test run.");
      await sendChatPayload(nextTestPrompt(), []);
    }
  } catch (err) {
    setToolCardBusy(call.call_id, false);
    if (call.tool === "request_test_run") setTestStatus({ status: "failed" });
    const detail = parseApiErrorDetail(err);
    if (call.tool === "propose_patch" && detail?.recoverable) {
      appendConversationStatus(`Patch was not applied: ${detail.guidance || detail.error}`, { failed: true });
      await sendChatPayload(detail.retry_prompt || patchRetryPrompt(detail), []);
      return;
    }
    if (call.tool === "propose_skill_draft" && detail?.recoverable && ["invalid_skill_name", "skill_name_conflict"].includes(detail.kind)) {
      appendConversationStatus(`Draft was not saved: ${detail.message}`, { failed: true });
      return;
    }
    if (call.tool === "rename_skill" && detail?.recoverable) {
      const why = detail.message || detail.guidance || detail.error;
      appendConversationStatus(`Rename was not applied: ${why}`, { failed: true });
      await sendChatPayload(
        `The rename to "${call.args?.new_name || ""}" failed: ${why} The skill still has its original name. Ask the user for a different name; do not retry the same one.`,
        [],
      );
      return;
    }
    if (call.tool === "propose_neighbor_edit" && detail?.recoverable) {
      appendConversationStatus(`Neighbor edit was not applied: ${detail.guidance || detail.error}`, { failed: true });
      await sendChatPayload(
        `The neighbor edit patch failed: ${detail.error}. ${detail.guidance || "Regenerate a wider V4A patch (unique anchor) or send the full skill_md."}`,
        [],
      );
      return;
    }
    markConversationStatus(runningNode, `Test failed: ${err.message}`, true);
    appendMessage("assistant", `Error: ${err.message}`);
    throw err;
  }
}

function parseApiErrorDetail(err) {
  if (err?.detail && typeof err.detail === "object") return err.detail;
  const message = err?.message || "";
  const jsonStart = message.indexOf("{");
  if (jsonStart < 0) return null;
  const body = message.slice(jsonStart).trim();
  try {
    const parsed = JSON.parse(body);
    return parsed.detail || parsed;
  } catch {
    return null;
  }
}

function isOrphanDecisionError(err) {
  return err?.status === 409 && err?.detail?.kind === "orphaned_children";
}

function chooseOrphanAction(err) {
  if (!isOrphanDecisionError(err)) return null;
  const children = (err.detail.children || []).join(", ");
  if (window.confirm(`These skills are currently marked INTERNAL (hidden from the host catalog) and are no longer listed by this scenario: ${children}.\n\nMake them visible standalone capability skills?`)) {
    return "make_capability";
  }
  if (window.confirm("Keep them hidden from the host catalog even though this scenario no longer lists them?")) {
    return "keep_internal";
  }
  return null;
}

function patchRetryPrompt(detail) {
  return [
    `The previous patch failed: ${detail?.error || "patch apply failed"}`,
    detail?.guidance || "Regenerate the patch with a wider unique context.",
    "Please propose a corrected V4A patch. Include the nearest unique Markdown section heading and enough unchanged context so the old block appears exactly once. Do not repeat the same patch.",
  ].join("\n");
}

function setToolCardBusy(callId, busy) {
  const card = document.querySelector(`[data-call-id="${callId}"]`);
  if (!card) return;
  card.classList.toggle("is-busy", busy);
  card.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
  const actions = card.querySelector(".tool-actions");
  if (!actions) return;
  let badge = actions.querySelector(".tool-busy");
  if (busy && !badge) {
    badge = document.createElement("span");
    badge.className = "tool-busy";
    badge.textContent = "Processing...";
    actions.appendChild(badge);
  } else if (!busy) {
    badge?.remove();
  }
}

function resolveToolCard(callId, action, context = {}) {
  const card = document.querySelector(`[data-call-id="${callId}"]`);
  if (!card) return;
  setToolCardBusy(callId, false);
  if (!card.classList.contains("conversation-tool")) {
    card.remove();
    return;
  }
  card.classList.add("resolved");
  card.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
    if (
      (action === "accept" && button.textContent.toLowerCase().includes("accept")) ||
      (action === "reject" && button.textContent.toLowerCase().includes("reject"))
    ) {
      button.classList.add(action === "accept" ? "accepted" : "rejected");
    }
  });
  card.querySelector(".tool-resolution")?.remove();
  const badge = document.createElement("span");
  badge.className = `tool-resolution ${action === "accept" ? "accepted" : action === "reject" ? "rejected" : ""}`;
  badge.textContent = action === "accept" ? "Accepted" : action === "reject" ? "Rejected" : "Completed";
  const actions = card.querySelector(".tool-actions");
  actions?.appendChild(badge);
  // No undo affordance for rename_skill: undo only rewinds session content, so
  // it would leave the draft on the old name while Blob/SQL sit on the new one.
  if (context.tool === "propose_patch" && action === "accept") {
    const latest = latestPatchRecord();
    if (latest) {
      card.dataset.patchId = latest.id;
      addUndoButtonIfLatest(card);
    }
  }
}

function latestPatchRecord() {
  const history = session?.patch_history || [];
  return history.length ? history[history.length - 1] : null;
}

function addUndoButtonIfLatest(card) {
  const latest = latestPatchRecord();
  updatePatchUndoButtons();
  if (!latest || card.dataset.patchId !== latest.id || card.querySelector("[data-undo-patch]")) return;
  const button = actionButton("Undo", async () => undoLatestPatch(card, latest.id));
  button.dataset.undoPatch = "true";
  card.querySelector(".tool-actions")?.appendChild(button);
}

function updatePatchUndoButtons() {
  const latest = latestPatchRecord();
  document.querySelectorAll("[data-undo-patch]").forEach((button) => {
    const card = button.closest("[data-patch-id]");
    if (!latest || card?.dataset.patchId !== latest.id) button.remove();
  });
}

async function undoLatestPatch(card, patchId) {
  setInputBusy(true);
  try {
    session = await undoSessionPatch(session.id, patchId);
    persistSessionState();
    renderSession();
    await refreshSessions();
    updatePatchUndoButtons();
    card.querySelector("[data-undo-patch]")?.remove();
    card.querySelector(".tool-resolution")?.remove();
    const badge = document.createElement("span");
    badge.className = "tool-resolution rejected";
    badge.textContent = "Undone";
    card.querySelector(".tool-actions")?.appendChild(badge);
    appendConversationStatus("Latest patch undone.");
  } catch (err) {
    appendConversationStatus(`Undo failed: ${err.message}`, { failed: true });
  } finally {
    setInputBusy(false);
  }
}

function markConversationStatus(node, text, failed = false, running = false) {
  if (!node) return;
  node.classList.toggle("running", running);
  node.classList.toggle("failed-status", failed);
  node.innerHTML = running
    ? `<span class="mini-spinner"></span><span>${escapeHtml(text)}</span>`
    : escapeHtml(text);
}

function removeConversationStatus(node) {
  if (!node) return;
  node.remove();
}

function recordQuestionAnswer(call) {
  if (isSamplesQuestion(call)) {
    recordSamplesAnswer(call);
    return;
  }
  const item = document.querySelector(`[data-question-id="${CSS.escape(call.call_id)}"]`);
  if (!item) return;
  const selected = item.querySelector("input[type='radio']:checked")?.value || "";
  const custom = item.querySelector("[data-custom-answer]")?.value.trim() || "";
  const answer = buildQuestionAnswer(selected, custom);
  if (!answer) {
    pendingChoiceAnswers.delete(call.call_id);
    updateChoiceCardMeta();
    return;
  }
  pendingChoiceAnswers.set(call.call_id, {
    call,
    result: { value: answer },
    question: call.args?.question || "Question",
    answer,
    selected,
    custom,
  });
  updateChoiceCardMeta();
}

function buildQuestionAnswer(selected, custom) {
  if (selected && custom) {
    return `Selected option: ${selected}\nAdditional input: ${custom}`;
  }
  return custom || selected;
}

function hydrateSamplesItem(item, call) {
  const saved = pendingChoiceAnswers.get(call.call_id);
  if (saved?.notes) {
    const notes = item.querySelector("[data-samples-notes]");
    if (notes) notes.value = saved.notes;
  }
  const editor = item.querySelector("[data-samples-editor]");
  if (!editor) return;
  editor.addEventListener("input", () => recordSamplesAnswer(call));
  editor.addEventListener("click", (event) => {
    const add = event.target.closest("[data-add-sample]");
    if (add) {
      event.preventDefault();
      addSampleRow(item, add.dataset.addSample);
      recordSamplesAnswer(call);
      return;
    }
    const del = event.target.closest("[data-del-sample]");
    if (del) {
      event.preventDefault();
      del.closest(".sample-row")?.remove();
      recordSamplesAnswer(call);
    }
  });
  recordSamplesAnswer(call);
}

function addSampleRow(item, kind) {
  const rows = item.querySelector(`[data-rows="${kind}"]`);
  if (!rows) return;
  rows.insertAdjacentHTML("beforeend", sampleRowHtml("", kind));
  rows.lastElementChild?.querySelector("input")?.focus();
}

function recordSamplesAnswer(call) {
  const item = document.querySelector(`[data-question-id="${CSS.escape(call.call_id)}"]`);
  if (!item) return;
  const collect = (kind) => [...item.querySelectorAll(`.sample-input[data-kind="${kind}"]`)]
    .map((input) => input.value.trim())
    .filter(Boolean);
  const positive = collect("positive");
  const negative = collect("negative");
  const notes = item.querySelector("[data-samples-notes]")?.value.trim() || "";
  if (!positive.length && !negative.length && !notes) {
    pendingChoiceAnswers.delete(call.call_id);
    updateChoiceCardMeta();
    return;
  }
  const parts = [];
  if (positive.length) parts.push("Positive samples (should route to this skill):\n" + positive.map((q, i) => `${i + 1}. ${q}`).join("\n"));
  if (negative.length) parts.push("Negative samples (should NOT route to this skill):\n" + negative.map((q, i) => `${i + 1}. ${q}`).join("\n"));
  if (notes) parts.push("Notes: " + notes);
  const answer = parts.join("\n\n");
  pendingChoiceAnswers.set(call.call_id, {
    call,
    result: { value: answer },
    question: call.args?.question || "Routing samples",
    answer,
    samples: { positive, negative },
    notes,
  });
  updateChoiceCardMeta();
}

async function submitChoiceAnswers(options = {}) {
  const { continueAgent = true, source = "form" } = options;
  const calls = currentPendingQuestionCalls();
  if (!calls.length) return;
  calls.forEach((call) => recordQuestionAnswer(call));
  const answers = buildQuestionAnswers(calls);
  pendingChoiceAnswers.clear();
  const acceptedAnswers = [];
  const staleAnswers = [];
  for (const item of answers) {
    try {
      session = await sendToolResult(session.id, item.call.call_id, item.result);
      acceptedAnswers.push(item);
    } catch (err) {
      if (err && err.stale) {
        staleAnswers.push(item);
        appLog(`Question already resolved on server: ${item.call.call_id}`);
      } else {
        throw err;
      }
    }
    document.querySelector(`[data-question-id="${CSS.escape(item.call.call_id)}"]`)?.classList.add("resolved");
    pendingQuestionCalls.delete(item.call.call_id);
  }
  resolveQuestionBlock([...acceptedAnswers, ...staleAnswers], source);
  renderQuestionQueueStatus();

  if (staleAnswers.length) {
    const noun = staleAnswers.length === 1 ? "This question was" : `${staleAnswers.length} questions were`;
    appendConversationStatus(`${noun} already resolved on the server. Refreshing session state.`);
    try {
      session = await getSession(session.id);
      persistSessionState();
    } catch (refreshErr) {
      appendConversationStatus(`Could not refresh session: ${refreshErr.message}`, { failed: true });
    }
  }
  renderSession();
  await refreshSessions();
  if (!continueAgent) return;
  if (!acceptedAnswers.length) {
    // Every answer was already consumed on the server, so the agent has moved on. Do not send a stale continuation prompt.
    return;
  }

  const message = [
    "User answered the UI confirmation questions. Continue from these answers and advance the workflow if appropriate.",
    "",
    ...acceptedAnswers.map((item, index) => `${index + 1}. ${item.question}\nAnswer: ${item.answer}`),
  ].join("\n");
  // The answered card itself shows a "Submitted answers" summary, so do NOT also
  // append a duplicate user bubble echoing the same answers. The agent still
  // receives the full context via sendChatPayload below.
  await sendChatPayload(message, []);
}

function buildQuestionAnswers(calls) {
  return calls.map((call) => {
    const saved = pendingChoiceAnswers.get(call.call_id);
    return saved || {
      call,
      result: { value: "(no answer)" },
      question: call.args?.question || "Question",
      answer: "(no answer)",
      selected: "",
      custom: "",
    };
  });
}

function resolveQuestionBlock(answers, source = "form") {
  const block = document.querySelector(".choice-card[data-question-block]:not(.resolved)");
  if (!block) return;
  block.classList.add("resolved");
  block.querySelectorAll("input, textarea, button").forEach((node) => {
    node.disabled = true;
  });
  block.querySelector(".tool-actions")?.remove();
  block.querySelector(".tool-resolution")?.remove();
  const label =
    source === "chat"
      ? "Answered from chat"
      : source === "test-skipped"
        ? "Skipped for test run"
        : source === "test-submitted"
          ? "Submitted before test run"
          : "Submitted answers";
  const summary = document.createElement("div");
  summary.className = "question-answer-summary";
  summary.innerHTML = `<strong>${escapeHtml(label)}</strong>
    <ol>${answers.map((item) => `<li><span>${escapeHtml(item.question)}</span><pre>${escapeHtml(item.answer)}</pre></li>`).join("")}</ol>`;
  block.appendChild(summary);
  const badge = document.createElement("span");
  badge.className = "tool-resolution accepted";
  badge.textContent = source === "chat" ? "Answered" : source === "test-skipped" ? "Skipped" : "Submitted";
  block.querySelector(".choice-progress").textContent = `${answers.length}/${answers.length} answered - 0 remaining`;
  block.querySelector(".choice-head")?.appendChild(badge);
}

async function handlePendingQuestionsBeforeTest() {
  const calls = currentPendingQuestionCalls();
  if (!calls.length) return true;
  calls.forEach((call) => recordQuestionAnswer(call));
  const answers = buildQuestionAnswers(calls);
  const answeredCount = answers.filter((item) => item.answer !== "(no answer)").length;
  const ok = window.confirm(
    `There are pending confirmation questions (${answeredCount}/${answers.length} answered). Run tests anyway?\n\nUnanswered questions will be submitted as (no answer).`,
  );
  if (!ok) {
    appendConversationStatus("Test run paused. Complete the question block or run tests again to skip unanswered questions.");
    return false;
  }
  await submitChoiceAnswers({
    continueAgent: false,
    source: answeredCount ? "test-submitted" : "test-skipped",
  });
  return true;
}

function checkTestEligibility() {
  const binding = skillBindingState();
  if (!binding.hasFiles) {
    appendConversationStatus("No skill files are available to test yet. Generate a draft first.", { failed: true });
    return false;
  }
  if (!binding.hasRemote) {
    appendConversationStatus("This skill has not been saved to Blob yet. Save it before running selection tests.", { failed: true });
    return false;
  }
  if (binding.unknown) {
    appendConversationStatus(`Blob skill '${binding.remoteName}' has no recorded remote version. Reload or save it before testing.`, { failed: true });
    return false;
  }
  return true;
}

async function saveCurrentSkillToBlob() {
  await persistCurrentDraftNow();
  const name = session.target_skill_id || session.remote_skill_id || inferCurrentName();
  try {
    session = await saveSessionSkill(session.id, { name });
  } catch (err) {
    const orphanAction = chooseOrphanAction(err);
    if (!orphanAction) {
      if (isOrphanDecisionError(err)) {
        appendConversationStatus("Save cancelled. No child metadata was changed.");
        return false;
      }
      throw err;
    }
    session = await saveSessionSkill(session.id, { name, orphan_action: orphanAction });
  }
  localEditorDirty = false;
  persistSessionState();
  renderSession();
  await refreshSessions();
  appendConversationStatus(`Saved ${name} to the skill store.`);
  return true;
}

async function handleUnsavedBeforeTest() {
  const binding = skillBindingState();
  if (!binding.unsaved) return "current";
  const answer = window.prompt(
    [
      "You have unsaved file changes.",
      "APIM tests validate the saved Blob/runtime version.",
      "They run in route_only mode: routing only, the skill is not executed.",
      "",
      "Type one option:",
      "save - Save then run",
      "run - Run saved Blob version",
      "cancel - Cancel",
    ].join("\n"),
    "save",
  );
  const normalized = (answer || "").trim().toLowerCase();
  if (!normalized || normalized === "cancel" || normalized === "c") {
    appendConversationStatus("Test run cancelled because local files are UNSAVED.");
    return null;
  }
  if (["save", "s", "save then run"].includes(normalized)) {
    const saved = await saveCurrentSkillToBlob();
    if (!saved) return null;
    return "current_saved";
  }
  if (["run", "r", "run saved version", "run saved blob version"].includes(normalized)) {
    appendConversationStatus("Running tests against the saved Blob version. Local UNSAVED changes are not included.");
    return "remote";
  }
  appendConversationStatus("Test run cancelled. Expected save, run, or cancel.", { failed: true });
  return null;
}

async function prepareRunTest() {
  if (!checkTestEligibility()) return null;
  const canRun = await handlePendingQuestionsBeforeTest();
  if (!canRun) return null;
  return handleUnsavedBeforeTest();
}

// ``mdOverride`` matters for cards rendered from an SSE tool_call: the local
// session is only refreshed by the later state_update event, so reading it
// there would yield the previous draft's name.
function inferCurrentName(mdOverride) {
  const md = String(mdOverride ?? session?.current_skill?.skill_md ?? "");
  const frontmatter = md.match(/^\ufeff?---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)/);
  const match = frontmatter && frontmatter[1].match(/^name:\s*(.+)$/m);
  if (match) return match[1].trim();
  return session?.target_skill_id || session?.remote_skill_id || "";
}

// Mirrors backend _SKILL_NAME_RE / dbo.skills.CK_skill_name_format.
const SKILL_NAME_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

// Forgiving while typing: a trailing hyphen is legal mid-word, so it survives
// here and is only stripped on submit.
function coerceSkillNameInput(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9-]+/g, "-").slice(0, 64);
}

function normalizeSkillName(value) {
  return coerceSkillNameInput(String(value).trim()).replace(/^-+|-+$/g, "");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderPatchDiff(patch) {
  const lines = String(patch || "").split(/\r?\n/);
  const rendered = lines.map((line) => {
    let kind = "diff-context";
    if (line.startsWith("***")) kind = "diff-meta";
    else if (line.startsWith("@@")) kind = "diff-hunk";
    else if (line.startsWith("+")) kind = "diff-add";
    else if (line.startsWith("-")) kind = "diff-del";
    return `<div class="diff-line ${kind}"><span class="diff-marker">${escapeHtml(line.slice(0, 1) || " ")}</span><code>${escapeHtml(line)}</code></div>`;
  });
  return `<div class="diff-view" aria-label="Patch diff">${rendered.join("")}</div>`;
}

function testAnalysisPrompt() {
  return [
    "The skill-selection test run has completed.",
    "Analyze the latest test_runs entry in the session state.",
    "Do not emit propose_patch yet.",
    "If all positive and negative samples pass, summarize the result and ask_user_input whether to finish or continue refining.",
    "If any sample failed, summarize the failure pattern, propose a concise modification direction, and ask_user_input whether the user accepts that direction.",
    "Only after the user accepts the suggested direction should you produce a V4A propose_patch in a later turn.",
  ].join("\n");
}

function nextTestPrompt() {
  return [
    "The user accepted the proposed patch and it has been applied.",
    "Immediately request the next skill-selection test run.",
    "Use request_test_run with the prior positive and negative samples when available; otherwise generate 5 relevant positive and 5 relevant negative samples from the current skill.",
  ].join("\n");
}

async function startSession() {
  appLog("Starting session");
  setInputBusy(true);
  if (el("modeSelect").value === "modify" && !el("targetSkill").value) {
    appendMessage("assistant", "Please select an existing skill before starting a modify session.");
    setInputBusy(false);
    return;
  }
  try {
    session = await createSession({
      mode: el("modeSelect").value,
      skill_kind: el("skillKindSelect").value,
      target_skill_id: el("targetSkill").value || null,
      materials: [],
    });
    localEditorDirty = false;
    topologyData = null;
    appLog(`Session started: ${session.id}`);
    persistSessionState();
    await refreshSessions();
    attachedMaterials = [];
    userPickedContextTab = false;
    el("chatStream").innerHTML = `<div class="empty-state chat-empty">${svgIcon("i-sparkles")}<strong>Tell me what skill to build</strong><em>Paste API specs, sample code, an existing SKILL.md, or a few user examples.</em></div>`;
    el("toolCalls").innerHTML = "";
    pendingQuestionCalls.clear();
    pendingChoiceAnswers.clear();
    renderQuestionQueueStatus();
    renderSession();
    await refreshCurrentAcaEnv();
    if (session.current_skill?.skill_md) {
      appendStatus(`Loaded skill: ${el("targetSkill").value || inferCurrentName()}`);
    }
  } catch (err) {
    appLog(`Start session failed: ${err.message}`);
    appendMessage("assistant", `Could not start session: ${err.message}`);
  } finally {
    setInputBusy(false);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  appLog("Send triggered");
  if (isSending) return;
  if (!session) await startSession();
  if (!session) return;
  const message = el("messageInput").value.trim();
  const progress = choiceProgress();
  if (progress.total && message) {
    await resolvePendingQuestionsWithAnswer(message);
  }
  appendMessage("user", message || "(materials attached)");
  el("messageInput").value = "";
  await sendChatPayload(message, attachedMaterials);
  attachedMaterials = [];
  persistSessionState();
  renderMaterials();
}

// Anchor for "scroll to the first message of a turn" (C2): the chat node that
// existed just before a top-level turn started. After the whole turn (including
// any auto-continue chain) finishes we scroll the first NEW node to the top,
// instead of leaving the user pinned at the very bottom.
let chatBatchAnchor = null;

async function sendChatPayload(message, materials, options = {}) {
  appLog(`Calling chat API: /api/sessions/${session.id}/chat`);
  const isTopLevelTurn = !options.autoContinue;
  if (isTopLevelTurn) chatBatchAnchor = el("chatStream").lastElementChild;
  setInputBusy(true);
  el("toolCalls").innerHTML = "";
  pendingChoiceAnswers.clear();
  lastGateMissing = [];
  let assistantBuffer = "";
  let responseHasInteractiveTool = false;
  let autoContinueTarget = null;
  let responseStarted = false;
  let recoverableRejection = false;
  const thinkingNode = appendConversationStatus(conversationWaitText(options), { running: true, ephemeral: true });
  const flushAssistantMessage = () => {
    const text = assistantBuffer;
    assistantBuffer = "";
    if (text.trim()) {
      dbgLog("stream", `flush assistant bubble (${text.length} chars)`);
      appendMessage("assistant", text);
    }
  };
  try {
    const payload = { message, materials };
    if (options.autoContinue) {
      payload.auto_continue = true;
      payload.auto_depth = options.autoDepth || 1;
    }
    dbgLog("stream", "turn start", { session: session.id, payload });
    let eventSeq = 0;
    await postSSE(`/api/sessions/${session.id}/chat`, payload, (eventName, data) => {
      eventSeq += 1;
      dbgLog("event", `#${eventSeq} ${eventName}`, eventName === "text_delta" ? { chars: (data.delta || "").length } : eventName === "tool_call" ? { tool: data.tool, call_id: data.call_id } : data);
      if (eventName === "llm_status") {
        setLlmStatus(data);
        if (data.status === "started") {
          markConversationStatus(thinkingNode, `Thinking with ${data.model || "model"}...`, false, true);
        }
      } else if (eventName === "text_delta") {
        if (!responseStarted) {
          removeConversationStatus(thinkingNode);
          responseStarted = true;
        }
        assistantBuffer += data.delta || "";
      } else if (eventName === "tool_call") {
        if (!responseStarted) {
          removeConversationStatus(thinkingNode);
          responseStarted = true;
        }
        if (["ask_user_input", "request_positive_samples", "propose_skill_draft", "propose_patch", "rename_skill", "request_test_run", "propose_neighbor_edit"].includes(data.tool)) {
          responseHasInteractiveTool = true;
        }
        if (data.tool === "stage_transition" || data.tool === "request_stage_transition") {
          const target = data.args?.target || data.args?.target_stage || null;
          autoContinueTarget = target ? String(target).toUpperCase() : null;
        }
        // Emit any assistant prose that preceded this tool first, then render the
        // tool inline so the chat reflects the real chronological order.
        flushAssistantMessage();
        renderToolCall(data);
      } else if (eventName === "state_update") {
        session = data;
        persistSessionState();
        renderSession();
      } else if (eventName === "quality_gate_failed") {
        if (!responseStarted) {
          removeConversationStatus(thinkingNode);
          responseStarted = true;
        }
        flushAssistantMessage();
        const missing = Array.isArray(data?.missing) ? data.missing : [];
        lastGateMissing = missing;
        const items = missing.length
          ? missing.map((m) => `- ${m}`).join("\n")
          : "(no items reported)";
        appendMessage(
          "assistant",
          `**Quality gate not yet satisfied.** The following items must be addressed before moving on:\n${items}`,
        );
        if (session?.prepare_brief) renderChecklist();
      } else if (eventName === "tool_effect_rejected") {
        if (!responseStarted) {
          removeConversationStatus(thinkingNode);
          responseStarted = true;
        }
        flushAssistantMessage();
        recoverableRejection = true;
        appendMessage(
          "assistant",
          `**That step was not allowed.** ${data.guidance || data.message || ""}`,
        );
      } else if (eventName === "error") {
        responseStarted = true;
        flushAssistantMessage();
        markConversationStatus(thinkingNode, `Model error: ${data.message}`, true);
        appendMessage("assistant", `Error: ${data.message}`);
      }
    });
  } finally {
    flushAssistantMessage();
    resetActivityGroup();
    dbgLog("stream", `turn end (responseStarted=${responseStarted}, interactive=${responseHasInteractiveTool}, autoContinueTarget=${autoContinueTarget || "none"})`);
    if (!responseStarted) {
      markConversationStatus(thinkingNode, "Model turn completed.", false);
      window.setTimeout(() => removeConversationStatus(thinkingNode), 1400);
    }
    setLlmStatus({ status: "completed" });
    setInputBusy(false);
  }
  await refreshSessions();

  const shouldAutoContinue =
    autoContinueTarget &&
    autoContinueTarget !== "DONE" &&
    !responseHasInteractiveTool &&
    (options.autoDepth || 0) < 4;
  const shouldRecover =
    recoverableRejection &&
    !shouldAutoContinue &&
    !responseHasInteractiveTool &&
    (options.autoDepth || 0) < 4;
  if (shouldRecover) {
    appendConversationStatus("-> recovering");
    await sendChatPayload(
      "The previous action was rejected (see the latest guidance message). Either request a valid stage transition that matches the user's intent, or call ask_user_input to ask the user how to proceed. Do not repeat the rejected action.",
      [],
      { autoDepth: (options.autoDepth || 0) + 1, autoContinue: true },
    );
  } else if (shouldAutoContinue) {
    const targetGroup = STAGE_TO_GROUP[autoContinueTarget];
    const targetLabel =
      STAGE_TO_GROUP[currentStage()] === targetGroup
        ? SUBSTAGE_LABELS[autoContinueTarget] || autoContinueTarget
        : (groupMeta[targetGroup] || {}).title || autoContinueTarget;
    appendConversationStatus(`-> ${targetLabel}`);
    await sendChatPayload(
      `Continue automatically after entering ${autoContinueTarget}. Start the next stage action now. If this stage requires user confirmation, ask the user with ask_user_input instead of waiting for a manual message.`,
      [],
      { autoDepth: (options.autoDepth || 0) + 1, autoContinue: true },
    );
  }

  if (isTopLevelTurn) {
    // Land on the FIRST new message of this turn (top of viewport), not pinned
    // to the very bottom. Works regardless of whether the first new node is an
    // assistant bubble or an interactive card.
    const firstNew = chatBatchAnchor ? chatBatchAnchor.nextElementSibling : el("chatStream").firstElementChild;
    if (firstNew && typeof firstNew.scrollIntoView === "function") firstNew.scrollIntoView({ block: "start" });
    chatBatchAnchor = null;
  }
}

function conversationWaitText(options = {}) {
  if (options.autoDepth) return "Continuing to the next stage...";
  return "Sending to model...";
}

function setInputBusy(busy) {
  isSending = busy;
  el("messageInput").disabled = busy;
  el("sendBtn").disabled = busy;
  el("sendBtn").textContent = busy ? "Sending..." : "Send";
  document.querySelectorAll('[data-action="new-material"], [data-action="save-new"], [data-new-content], [data-new-kind]').forEach((node) => { node.disabled = busy; });
  updateActionButtons();
  renderSkillSelector();
  renderSessionSelector();
}

function updateActionButtons() {
  const hasDraft = Boolean(session?.current_skill?.skill_md);
  const binding = skillBindingState();
  const canRunTests = hasDraft && binding.hasRemote && !binding.unknown;
  if (el("runTestBtn")) {
    el("runTestBtn").disabled = isSending || !canRunTests;
    el("runTestBtn").title = !hasDraft
      ? "Generate files before running tests."
      : !binding.hasRemote
        ? "Save this skill to Blob before running tests."
        : binding.unsaved
          ? "Local files are UNSAVED. Choose whether to save first or test the saved Blob version."
          : binding.unknown
            ? "Remote Blob version is unknown. Reload or save before running tests."
            : "Run tests against the saved Blob skill.";
  }
  if (el("saveDraftBtn")) {
    const upToDate = binding.hasRemote && !binding.unsaved && !binding.unknown && !localEditorDirty;
    el("saveDraftBtn").disabled = isSending || !hasDraft || upToDate;
    el("saveDraftBtn").title = !hasDraft
      ? "Generate a skill draft first."
      : upToDate
        ? "Skill already saved to Blob (latest version)."
        : "Save this skill to Blob + database and sync.";
  }
  if (el("saveTestSamplesBtn")) el("saveTestSamplesBtn").disabled = isSending || !session;
}

async function resolvePendingQuestionsWithAnswer(answer) {
  const calls = currentPendingQuestionCalls();
  if (!calls.length) return;
  setInputBusy(true);
  let staleCount = 0;
  try {
    for (const call of calls) {
      try {
        session = await sendToolResult(session.id, call.call_id, { value: answer || "(no answer)" });
      } catch (err) {
        if (err && err.stale) {
          staleCount += 1;
          appLog(`Question already resolved on server: ${call.call_id}`);
        } else {
          throw err;
        }
      }
      pendingQuestionCalls.delete(call.call_id);
      pendingChoiceAnswers.delete(call.call_id);
    }
    if (staleCount) {
      const noun = staleCount === 1 ? "This question was" : `${staleCount} questions were`;
      appendConversationStatus(`${noun} already resolved on the server. Refreshing session state.`);
      try {
        session = await getSession(session.id);
        persistSessionState();
      } catch (refreshErr) {
        appendConversationStatus(`Could not refresh session: ${refreshErr.message}`, { failed: true });
      }
    }
    resolveQuestionBlock(
      calls.map((call) => ({
        call,
        question: call.args?.question || "Question",
        answer: answer || "(no answer)",
        result: { value: answer || "(no answer)" },
      })),
      "chat",
    );
    renderQuestionQueueStatus();
    renderSession();
    await refreshSessions();
  } finally {
    setInputBusy(false);
  }
}

async function attachMaterial() {
  const card = document.querySelector("[data-material-new]");
  const content = (card?.querySelector("[data-new-content]")?.value || "").trim();
  if (!content) return;
  const kind = card?.querySelector("[data-new-kind]")?.value || "text";
  const payload = { kind, content, metadata: {} };
  if (!session?.id) {
    attachedMaterials.push(payload);
    creatingMaterial = false;
    creatingMaterialDraft = { kind: "text", content: "" };
    renderMaterials();
    renderGuide();
    return;
  }
  setInputBusy(true);
  try {
    session = await addSessionMaterial(session.id, payload);
    persistSessionState();
    creatingMaterial = false;
    creatingMaterialDraft = { kind: "text", content: "" };
    appendConversationStatus(`Saved \"${kind}\" material to this session.`);
    renderSession();
    await refreshSessions();
  } catch (err) {
    appendConversationStatus(`Could not save material: ${err.message}`, { failed: true });
    appLog(`Material save failed: ${err.message}`);
  } finally {
    setInputBusy(false);
  }
}

function openMaterialView(id, pendingIndex) {
  let material = null;
  if (id) {
    const saved = (session && Array.isArray(session.materials)) ? session.materials : [];
    material = saved.find((m) => String(m.id) === String(id)) || null;
  } else if (pendingIndex != null && pendingIndex !== "") {
    material = (Array.isArray(attachedMaterials) ? attachedMaterials : [])[Number(pendingIndex)] || null;
  }
  if (!material) return;
  const modal = document.getElementById("materialViewModal");
  const kindEl = document.getElementById("materialViewKind");
  const contentEl = document.getElementById("materialViewContent");
  if (!modal || !contentEl) return;
  if (kindEl) kindEl.textContent = material.kind || "text";
  contentEl.textContent = String(material.content || "");
  modal.classList.remove("hidden");
}

function closeMaterialView() {
  document.getElementById("materialViewModal")?.classList.add("hidden");
}

async function handleSavedMaterialAction(event) {
  const flowBtn = event.target.closest('button[data-action="new-material"], button[data-action="save-new"], button[data-action="cancel-new"]');
  if (flowBtn) {
    if (flowBtn.disabled) return;
    const act = flowBtn.dataset.action;
    if (act === "new-material") {
      creatingMaterial = true;
      creatingMaterialDraft = { kind: "text", content: "" };
      renderMaterials();
      document.querySelector("[data-new-content]")?.focus();
      return;
    }
    if (act === "cancel-new") {
      creatingMaterial = false;
      creatingMaterialDraft = { kind: "text", content: "" };
      renderMaterials();
      return;
    }
    if (act === "save-new") {
      await attachMaterial();
      return;
    }
  }
  const expandBtn = event.target.closest('button[data-action="expand"]');
  if (expandBtn && !expandBtn.disabled) {
    openMaterialView(expandBtn.dataset.materialId, expandBtn.dataset.pendingIndex);
    return;
  }
  const button = event.target.closest("button[data-material-id][data-action]");
  if (!button || button.disabled) return;
  const id = button.dataset.materialId;
  const action = button.dataset.action;
  if (!id || !session?.id) return;
  if (action === "edit") {
    editingMaterialIds.add(id);
    renderMaterials();
    return;
  }
  if (action === "cancel") {
    editingMaterialIds.delete(id);
    renderMaterials();
    return;
  }
  if (action === "delete") {
    if (!window.confirm("Delete this material from the session?")) return;
    setInputBusy(true);
    try {
      session = await deleteSessionMaterial(session.id, id);
      persistSessionState();
      editingMaterialIds.delete(id);
      appendConversationStatus("Material deleted from this session.");
      renderSession();
      await refreshSessions();
    } catch (err) {
      appendConversationStatus(`Could not delete material: ${err.message}`, { failed: true });
    } finally {
      setInputBusy(false);
    }
    return;
  }
  if (action === "save") {
    const item = button.closest(`.material-item[data-material-id="${CSS.escape(id)}"]`);
    if (!item) return;
    const kind = item.querySelector("[data-edit-kind]")?.value || "text";
    const content = (item.querySelector("[data-edit-content]")?.value || "").trim();
    if (!content) {
      appendConversationStatus("Material content cannot be empty.", { failed: true });
      return;
    }
    setInputBusy(true);
    try {
      session = await updateSessionMaterial(session.id, id, { kind, content, metadata: {} });
      persistSessionState();
      editingMaterialIds.delete(id);
      appendConversationStatus("Material updated.");
      renderSession();
      await refreshSessions();
    } catch (err) {
      appendConversationStatus(`Could not update material: ${err.message}`, { failed: true });
    } finally {
      setInputBusy(false);
    }
  }
}

function updateDraftFromEditor() {
  if (!session) return;
  const value = el("editor").value;
  localEditorDirty = true;
  topologyData = null;
  activeTab = "skill";
  session.current_skill = session.current_skill || { skill_md: "" };
  session.current_skill.skill_md = value;
  renderMarkdownPreview(value);
  scheduleDraftAutosave();
  renderSkillBindingStatus();
  updateActionButtons();
}

function persistTestSampleEditorState() {
  if (!session) return;
  const content = {
    positive: parseSampleLines(el("positiveSamplesInput")?.value || ""),
    negative: parseSampleLines(el("negativeSamplesInput")?.value || ""),
  };
  session.verify_checklist = session.verify_checklist || {};
  session.verify_checklist.test_samples = session.verify_checklist.test_samples || { status: "pending", content: {} };
  session.verify_checklist.test_samples.content = content;
}

function scheduleDraftAutosave() {
  window.clearTimeout(draftSaveTimer);
  draftSaveTimer = window.setTimeout(async () => {
    if (!session?.id || isSending) return;
    try {
      await persistCurrentDraftNow();
      appLog("Draft autosaved to local session.");
    } catch (err) {
      appLog(`Draft autosave failed: ${err.message}`);
    }
  }, 700);
}

async function persistCurrentDraftNow() {
  if (!session?.id || !session.current_skill) return;
  window.clearTimeout(draftSaveTimer);
  session = await updateSessionDraft(session.id, session.current_skill);
  localEditorDirty = false;
  persistSessionState();
  renderSkillBindingStatus();
  updateActionButtons();
  await refreshSessions();
}

document.querySelectorAll(".context-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    userPickedContextTab = true;
    setActiveContextTab(button.dataset.context);
  });
});

initWorkspaceResizer();
initFilesResizer();
initAgentGraphResizer();

bind("fitAgentGraphBtn", "click", () => {
  agentGraph?.resize();
  agentGraph?.fit(undefined, 40);
});

function setAgentGraphFullscreen(enabled) {
  agentGraphFullscreen = enabled;
  document.body.classList.toggle("agent-graph-fullscreen-open", enabled);
  el("agentGraphContent")?.classList.toggle("fullscreen", enabled);
  const button = el("fullscreenAgentGraphBtn");
  if (button) button.textContent = enabled ? "Exit fullscreen" : "Fullscreen";
  setTimeout(() => {
    restoreAgentGraphSplit();
    agentGraph?.resize();
    agentGraph?.fit(undefined, enabled ? 70 : 40);
  }, 50);
}

bind("fullscreenAgentGraphBtn", "click", () => setAgentGraphFullscreen(!agentGraphFullscreen));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && agentGraphFullscreen) setAgentGraphFullscreen(false);
});
bind("agentGraphDetails", "click", (event) => {
  const row = event.target.closest("[data-graph-edge-id]");
  if (!row) return;
  focusGraphEdge(row.dataset.graphEdgeId, true);
});
bind("agentGraphDetails", "keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest("[data-graph-edge-id]");
  if (!row) return;
  event.preventDefault();
  focusGraphEdge(row.dataset.graphEdgeId, true);
});

bind("newSessionBtn", "click", startSession);
bind("sessionList", "change", async () => {
  const sessionId = el("sessionList").value;
  if (sessionId && sessionId !== session?.id) await resumeSession(sessionId);
});
bind("modeSelect", "change", async () => {
  if (el("modeSelect").value === "modify") await refreshSkills();
  renderSession();
  renderSessionSelector();
});
bind("skillKindSelect", "change", () => {
  el("kindHelp").textContent = kindHelp[el("skillKindSelect").value] || "";
});
bind("chatForm", "submit", sendMessage);
bind("editor", "input", updateDraftFromEditor);
bind("positiveSamplesInput", "input", persistTestSampleEditorState);
bind("negativeSamplesInput", "input", persistTestSampleEditorState);
(() => {
  const editor = document.querySelector("[data-test-samples-editor]");
  if (!editor) return;
  editor.addEventListener("input", (event) => {
    if (event.target.classList.contains("sample-input")) syncTestSamplesTextareas();
  });
  editor.addEventListener("click", (event) => {
    const add = event.target.closest("[data-add-test-sample]");
    if (add) {
      event.preventDefault();
      const kind = add.dataset.addTestSample;
      const rows = editor.querySelector(`[data-test-rows="${kind}"]`);
      const rowHtml = kind === "negative" ? testNegativeRowHtml({}) : sampleRowHtml("", kind);
      rows?.insertAdjacentHTML("beforeend", rowHtml);
      rows?.lastElementChild?.querySelector("input")?.focus();
      syncTestSamplesTextareas();
      return;
    }
    const del = event.target.closest("[data-del-sample]");
    if (del) {
      event.preventDefault();
      del.closest(".sample-row")?.remove();
      syncTestSamplesTextareas();
    }
  });
})();
bind("sendBtn", "click", (event) => {
  appLog("Send button clicked");
  const form = el("chatForm");
  if (form?.requestSubmit && document.activeElement !== form) return;
  event.preventDefault();
  sendMessage(event);
});
bind("materialList", "input", (event) => {
  const t = event.target;
  if (t && t.matches && t.matches("[data-new-content]")) creatingMaterialDraft.content = t.value;
  else if (t && t.matches && t.matches("[data-new-kind]")) creatingMaterialDraft.kind = t.value;
});
bind("materialList", "click", handleSavedMaterialAction);
// Revise a confirmed PREPARE checkpoint: flips it to pending and cascades the
// downstream checkpoints to pending too (backend dependency map).
document.addEventListener("click", async (event) => {
  const reviseBtn = event.target.closest("[data-revise-checkpoint]");
  if (!reviseBtn) return;
  if (!session) return;
  const key = reviseBtn.getAttribute("data-revise-checkpoint");
  reviseBtn.disabled = true;
  try {
    session = await updateChecklistItem(session.id, {
      item: key,
      status: "revised",
      reason: "User chose to revise this checkpoint.",
    });
    persistSessionState();
    renderSession();
  } catch (err) {
    appLog("revise checkpoint failed: " + err);
    reviseBtn.disabled = false;
  }
});

// Editable routing samples + variables cards in the PREPARE checklist.
document.addEventListener("click", async (event) => {
  const addSpl = event.target.closest("[data-spl-add]");
  if (addSpl) {
    const kind = addSpl.getAttribute("data-spl-add");
    const rows = document.querySelector(`[data-spl-rows="${kind}"]`);
    if (rows) {
      const wrap = document.createElement("div");
      wrap.innerHTML = (kind === "positive" ? positiveRowHtml() : negativeRowHtml()).trim();
      const node = wrap.firstElementChild;
      rows.appendChild(node);
      node.querySelector("input")?.focus();
    }
    return;
  }
  const addNb = event.target.closest("[data-nb-add]");
  if (addNb) {
    const rows = document.querySelector("[data-nb-rows]");
    if (rows) {
      const wrap = document.createElement("div");
      wrap.innerHTML = neighborRowHtml().trim();
      const node = wrap.firstElementChild;
      rows.appendChild(node);
      node.querySelector("input")?.focus();
    }
    return;
  }
  if (event.target.closest("[data-nb-del]")) {
    event.target.closest(".nb-row")?.remove();
    return;
  }
  if (event.target.closest("[data-delegation-add]")) {
    const rows = document.querySelector("[data-delegation-rows]");
    rows?.insertAdjacentHTML("beforeend", delegationRowHtml());
    rows?.lastElementChild?.querySelector("select")?.focus();
    return;
  }
  if (event.target.closest("[data-delegation-del]")) {
    event.target.closest(".delegation-row")?.remove();
    return;
  }
  if (event.target.closest("[data-delegation-save]")) {
    await saveBriefChildren();
    return;
  }
  if (event.target.closest("[data-nb-save]")) {
    await saveBriefNeighbors();
    return;
  }
  const nbeditOpen = event.target.closest("[data-nbedit-open]");
  if (nbeditOpen) {
    await openNeighborEditor(nbeditOpen.getAttribute("data-nbedit-open"));
    return;
  }
  const nbeditSaveVer = event.target.closest("[data-nbedit-savever]");
  if (nbeditSaveVer) {
    await saveNeighborVersion(nbeditSaveVer.getAttribute("data-nbedit-savever"));
    return;
  }
  const nbeditSave = event.target.closest("[data-nbedit-save]");
  if (nbeditSave) {
    await saveNeighborEdit(nbeditSave.getAttribute("data-nbedit-save"));
    return;
  }
  const addVar = event.target.closest("[data-var-add]");
  if (addVar) {
    const type = addVar.getAttribute("data-var-add");
    const rows = document.querySelector(`[data-var-rows="${type}"]`);
    // There is exactly one verified actor variable, so a second row could only
    // ever be a duplicate of the same fixed name.
    if (rows && type === "platform_identity" && rows.querySelector(".var-row")) return;
    if (rows) {
      const wrap = document.createElement("div");
      wrap.innerHTML = variableRowHtml(type).trim();
      const node = wrap.firstElementChild;
      rows.appendChild(node);
      node.querySelector("input")?.focus();
    }
    return;
  }
  if (event.target.closest("[data-spl-del]")) {
    event.target.closest(".spl-row")?.remove();
    return;
  }
  if (event.target.closest("[data-var-del]")) {
    event.target.closest(".var-row")?.remove();
    return;
  }
  if (event.target.closest("[data-spl-save]")) {
    openSamplesSaveModal((choice) => {
      if (choice !== "cancel") saveBriefSamples({ revisit: choice === "revisit" });
    });
    return;
  }
  if (event.target.closest("[data-var-save]")) {
    await saveBriefVariables();
    return;
  }
});

// Ticking "in ACA" on an env variable flips its deployment status (add -> reuse)
// and the row moves to the Reuse bucket on the next render. This is a
// deployment-only change: it never alters the skill text and never notifies the
// agent (the backend treats an in_aca flip as content-unchanged).
document.addEventListener("change", async (event) => {
  const nbeditSel = event.target.closest("[data-nbedit-select]");
  if (nbeditSel) {
    // Blur so the checklist focus-guard does not skip the re-render that swaps
    // the textarea content over to the newly selected version.
    nbeditSel.blur();
    await selectNeighborVersion(nbeditSel.getAttribute("data-nbedit-select"), nbeditSel.value);
    return;
  }
  const box = event.target.closest("[data-var-inaca]");
  if (!box) return;
  // Blur so the checklist focus-guard does not skip the re-render that moves the
  // row into the Reuse bucket.
  box.blur();
  await saveBriefVariables();
});

// Live markdown preview: re-render the right-hand pane as the user edits the
// neighbor SKILL.md textarea (no persistence until Save as new version).
document.addEventListener("input", (event) => {
  const ta = event.target.closest("[data-nbedit-textarea]");
  if (!ta) return;
  refreshNeighborPreview(ta.getAttribute("data-nbedit-textarea"));
});

// Drag the middle divider to resize the editor / preview panes horizontally.
document.addEventListener("mousedown", (event) => {
  const resizer = event.target.closest("[data-nbedit-resizer]");
  if (!resizer) return;
  event.preventDefault();
  const split = resizer.closest(".nbedit-split");
  const editorPane = split?.querySelector(".nbedit-editor-pane");
  if (!split || !editorPane) return;
  const startX = event.clientX;
  const startWidth = editorPane.getBoundingClientRect().width;
  const totalWidth = split.getBoundingClientRect().width;
  const onMove = (e) => {
    const pct = ((startWidth + (e.clientX - startX)) / totalWidth) * 100;
    editorPane.style.flex = `0 0 ${Math.max(20, Math.min(80, pct))}%`;
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    document.body.style.userSelect = "";
  };
  document.body.style.userSelect = "none";
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
});

// Persist collapse/expand state of checklist sections + routing sub-panels so a
// re-render (e.g. after Save) keeps whatever the user opened/closed. The toggle
// event does not bubble, so listen in the capture phase.
document.addEventListener("toggle", (event) => {
  const d = event.target;
  if (!(d instanceof HTMLDetailsElement)) return;
  const id = d.getAttribute("data-collapse-id");
  if (!id) return;
  if (d.open) collapsedSections.delete(id); else collapsedSections.add(id);
}, true);

// C5: clicking an in-chat jump link switches to that tab and, for a checklist
// step, expands + highlights it.
function jumpToTarget(spec) {
  const [tab, checkpoint] = String(spec || "").split("#");
  if (!tab) return;
  userPickedContextTab = true;
  setActiveContextTab(tab);
  if (!checkpoint) return;
  requestAnimationFrame(() => {
    const node = document.querySelector(`[data-checkpoint="${CSS.escape(checkpoint)}"]`);
    if (!node) return;
    if (node.tagName === "DETAILS") node.open = true;
    node.scrollIntoView({ block: "start" });
    node.classList.add("checkpoint-flash");
    setTimeout(() => node.classList.remove("checkpoint-flash"), 1600);
  });
}
document.addEventListener("click", (event) => {
  const jump = event.target.closest("[data-jump]");
  if (!jump) return;
  event.preventDefault();
  jumpToTarget(jump.dataset.jump);
});

document.addEventListener("click", (event) => {
  const btn = event.target.closest("[data-download-text]");
  if (!btn) return;
  event.preventDefault();
  const text = longTextStore.get(btn.dataset.downloadText);
  if (!text) return;
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${(btn.dataset.downloadName || "response").replace(/[^a-z0-9._-]+/gi, "-")}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
});

document.addEventListener("click", async (event) => {
  if (!event.target.closest("[data-refresh-peer-skills]")) return;
  event.preventDefault();
  await refreshCurrentPeerSkills({ silent: false });
});
bind("authBtn", "click", () => {
  if (authStatus?.authenticated) {
    const panel = el("authProfilePanel");
    setAuthProfileOpen(panel?.classList.contains("hidden") ?? true);
    return;
  }
  persistSessionState();
  startMicrosoftLogin();
});

document.addEventListener("click", async (event) => {
  if (event.target.closest("#authBtn") || event.target.closest("#authProfilePanel")) {
    const signOut = event.target.closest("#authProfileSignOutBtn");
    if (signOut) {
      persistSessionState();
      await logoutMicrosoft();
      authStatus = null;
      setAuthProfileOpen(false);
      await refreshAuthStatus();
    }
    return;
  }
  setAuthProfileOpen(false);
});

bind("saveDraftBtn", "click", async () => {
  if (!session) return;
  await persistCurrentDraftNow();
  const call = (session?.pending_tool_calls || []).find((c) => c.tool === "propose_skill_draft");
  if (call) {
    const name = session.target_skill_id || inferCurrentName();
    const makePublic = draftPublicChoice();
    if (makePublic === null) return;
    await acceptTool(call, { action: "accept", name });
    appendConversationStatus(`Saved ${name} to the skill store.`);
    if (makePublic) await publishAfterDraftAccept(name);
    return;
  }
  setInputBusy(true);
  try {
    const name = session.target_skill_id || inferCurrentName();
    try {
      session = await saveSessionSkill(session.id, { name });
    } catch (err) {
      const orphanAction = chooseOrphanAction(err);
      if (!orphanAction) {
        if (isOrphanDecisionError(err)) {
          appendConversationStatus("Save cancelled. No child metadata was changed.");
          return;
        }
        throw err;
      }
      session = await saveSessionSkill(session.id, { name, orphan_action: orphanAction });
    }
    localEditorDirty = false;
    renderSession();
    await refreshSessions();
    appendConversationStatus(`Saved ${name} to the skill store.`);
  } catch (err) {
    appendConversationStatus(`Save failed: ${err.message}`, { failed: true });
    throw err;
  } finally {
    setInputBusy(false);
  }
});
bind("runTestBtn", "click", async () => {
  if (!session) return;
  const source = await prepareRunTest();
  if (!source) return;
  if (source === "remote" && localEditorDirty) await persistCurrentDraftNow();
  const testSource = source === "current_saved" ? "current" : source;
  persistSessionState();
  setInputBusy(true);
  setTestStatus({ status: "started" });
  const runningNode = appendConversationStatus(
    testSource === "remote"
      ? "Running skill-selection tests against the saved Blob version through APIM /run in route_only mode (routing only, nothing is executed)..."
      : "Running skill-selection tests through APIM /run in route_only mode (routing only, nothing is executed)...",
    { running: true },
  );
  try {
    session = await runSessionTest(session.id, { source: testSource });
    persistSessionState();
    renderSession();
    await refreshSessions();
    appendTestRunCard(latestTestRun(), testSource === "remote" ? "Manual test run - saved Blob version" : "Manual test run");
    setActiveContextTab("tests");
    document.getElementById("resultsSection")?.setAttribute("open", "");
    markConversationStatus(runningNode, "Test completed. Asking the model to review the result...");
    await sendChatPayload(testAnalysisPrompt(), []);
  } catch (err) {
    setTestStatus({ status: "failed" });
    markConversationStatus(runningNode, `Test failed: ${err.message}`, true);
    throw err;
  } finally {
    setLlmStatus({ status: "completed" });
    setInputBusy(false);
  }
});

let pendingSamplesSaveHandler = null;

// Generic 3-choice samples-save modal shared by the Checklist editor and the
// Tests tab. The stored handler receives "only" | "revisit" | "cancel".
function openSamplesSaveModal(handler) {
  pendingSamplesSaveHandler = typeof handler === "function" ? handler : null;
  document.getElementById("saveSamplesModal")?.classList.remove("hidden");
}

function closeSaveSamplesModal() {
  document.getElementById("saveSamplesModal")?.classList.add("hidden");
  pendingSamplesSaveHandler = null;
}

function resolveSamplesSaveModal(choice) {
  const handler = pendingSamplesSaveHandler;
  closeSaveSamplesModal();
  if (handler) handler(choice);
}

bind("saveTestSamplesBtn", "click", () => {
  if (!session) return;
  openSamplesSaveModal((choice) => doSaveTestSamples(choice));
});

async function doSaveTestSamples(choice) {
  if (!session || choice === "cancel") return;
  const revisit = choice === "revisit";
  const positive = collectTestSamples("positive");
  const negative = collectTestNegatives();

  setInputBusy(true);
  try {
    // Write to the AUTHORITATIVE Prepare Brief samples -- the same source the
    // Checklist editor and the test runner use -- so both editors stay in sync.
    session = await updateSamples(session.id, { positive, negative, revisit });
    persistSessionState();
    renderSession();
    await refreshSessions();
    if (revisit && !isSending) {
      appendConversationStatus("Routing samples updated. Asking the agent to review.");
      await sendChatPayload(
        "I revised the routing test samples. Please review whether the skill's description or the verify checklist should change because of these samples, and update them with me if needed. Do NOT regenerate the SKILL.md unless we agree it is necessary.",
        [],
      );
    } else {
      appendConversationStatus("Routing samples updated for testing. Description and checklist kept.");
    }
  } catch (err) {
    appendConversationStatus(`Could not update test samples: ${err.message}`, { failed: true });
    throw err;
  } finally {
    setInputBusy(false);
  }
}

async function refreshSkills() {
  skillsLoaded = false;
  renderSkillSelector();
  try {
    const storeFilter = session && session.blob_store_id ? session.blob_store_id : undefined;
    if (storeFilter) dbgLog("skills.refresh", "store filter", { store: storeFilter });
    availableSkills = await listSkills(storeFilter);
    skillsLoaded = true;
  } catch (err) {
    availableSkills = [];
    skillsLoaded = true;
    appendMessage("assistant", `Could not load existing skills: ${err.message}`);
  }
  renderSkillSelector();
}

async function refreshSessions() {
  sessionsLoaded = false;
  renderSessionSelector();
  try {
    savedSessions = await listSessions();
    sessionsLoaded = true;
  } catch (err) {
    savedSessions = [];
    sessionsLoaded = true;
    appLog(`Could not load sessions: ${err.message}`);
  }
  renderSessionSelector();
}

async function refreshCurrentPeerSkills({ silent = true } = {}) {
  if (!session?.id) return;
  try {
    session = await refreshPeerSkills(session.id);
    persistSessionState();
    renderSession();
    if (!silent) appendConversationStatus("Peer skills refreshed.");
  } catch (err) {
    appLog(`Peer skills refresh failed: ${err.message}`);
    if (!silent) appendConversationStatus(`Peer skills refresh failed: ${err.message}`, { failed: true });
  }
}

async function refreshCurrentAcaEnv({ silent = true } = {}) {
  if (!session?.id) return;
  try {
    session = await refreshAcaEnv(session.id);
    persistSessionState();
    renderSession();
    if (!silent) appendConversationStatus("ACA environment variables refreshed.");
  } catch (err) {
    appLog(`ACA env lookup failed: ${err.message}`);
    if (!silent) appendConversationStatus(`ACA env lookup failed: ${err.message}`, { failed: true });
  }
}

async function resumeSession(sessionId) {
  if (!sessionId) return;
  if (isSending) {
    appendConversationStatus("Session switch paused while the current request is running.", { failed: true });
    renderSessionSelector();
    return;
  }
  setInputBusy(true);
  try {
    session = await getSession(sessionId);
    localEditorDirty = false;
    topologyData = null;
    attachedMaterials = [];
    pendingChoiceAnswers.clear();
    persistSessionState();
    userPickedContextTab = false;
    renderLoadedSessionContent(`Resumed saved session ${sessionId}.`);
    renderSession();
    await refreshCurrentAcaEnv();
    appLog(`Session resumed: ${sessionId}`);
  } catch (err) {
    appendConversationStatus(`Could not resume session: ${err.message}`, { failed: true });
    appLog(`Resume session failed: ${err.message}`);
  } finally {
    setInputBusy(false);
  }
}

async function refreshAuthStatus() {
  try {
    authStatus = await fetchAuthStatus();
  } catch (err) {
    authStatus = { authenticated: false, error: err.message };
  }
  renderAuthStatus();
  // v7 RLS: show/hide login gate based on auth.
  try { window.__sgv7?.v7UpdateLoginGate?.(authStatus); } catch {}
  // v7: auto-refresh skill list once signed in so the modify-mode dropdown
  // populates even if the page loaded before the cookie was valid.
  if (authStatus && authStatus.authenticated) {
    try { await refreshSkills(); } catch {}
  }
}

async function initializePage() {
  restoreUiState();
  el("chatStream").innerHTML = `<div class="empty-state chat-empty">Choose a mode, then start a session. For new/import workflows, paste the requirements or attach materials before sending.</div>`;
  el("toolCalls").innerHTML = "";
  setActiveContextTab(activeContextTab);
  renderSession();
  await refreshAuthStatus();
  await refreshSessions();
  // If signed in but there are no sessions at all, auto-create one so the
  // user lands in a ready-to-use session instead of an empty/error state.
  if (authStatus?.authenticated && savedSessions.length === 0) {
    appLog("No sessions found on boot; auto-creating a new session.");
    await startSession();
  }
}

initializePage();

// ===========================================================================
// v7 RLS helpers: login gate, permission banner, refresh button, grants modal
// ===========================================================================

function showPermissionBanner(message) {
  const banner = document.getElementById("permissionBanner");
  const text = document.getElementById("permissionBannerText");
  if (!banner || !text) return;
  text.textContent = message || "You do not have permission for that action.";
  banner.classList.remove("hidden");
  window.clearTimeout(showPermissionBanner._timer);
  showPermissionBanner._timer = window.setTimeout(() => banner.classList.add("hidden"), 6000);
}

function showLoginGate(show) {
  const gate = document.getElementById("loginGate");
  if (!gate) return;
  gate.classList.toggle("hidden", !show);
}

// Wrap window.fetch so 401/403 anywhere triggers the banner / login gate.
(function installPermissionInterceptor() {
  const _origFetch = window.fetch.bind(window);
  window.fetch = async function(...args) {
    const res = await _origFetch(...args);
    if (res && res.status === 401) {
      showLoginGate(true);
    } else if (res && res.status === 403) {
      let detail = "Permission denied.";
      try {
        const clone = res.clone();
        const body = await clone.json();
        if (body && body.detail) detail = String(body.detail);
      } catch {}
      showPermissionBanner(detail);
    }
    return res;
  };
})();

async function handleRefreshSkillsClick() {
  const btn = document.getElementById("refreshSkillsBtn");
  if (btn) btn.disabled = true;
  try {
    await refreshSkillsCache();
    if (typeof loadSkills === "function") {
      await loadSkills();
    }
  } catch (err) {
    console.warn("[v7] refresh skills failed", err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Grants modal --------------------------------------------------------------

function _currentGrantsSkill() {
  return document.getElementById("grantsModalSkill")?.textContent || "";
}

async function openGrantsModalFor(skillName) {
  if (!skillName) return;
  const modal = document.getElementById("grantsModal");
  const nameEl = document.getElementById("grantsModalSkill");
  const errEl = document.getElementById("grantsError");
  if (!modal || !nameEl) return;
  nameEl.textContent = skillName;
  errEl.textContent = "";
  modal.classList.remove("hidden");
  await renderGrantsList(skillName);
}

function closeGrantsModal() {
  document.getElementById("grantsModal")?.classList.add("hidden");
}

async function renderGrantsList(skillName) {
  const list = document.getElementById("grantsList");
  if (!list) return;
  list.innerHTML = "<li><em>Loading...</em></li>";
  try {
    const data = await listSkillGrants(skillName);
    renderGrantsVisibility(!!data.is_public);
    if (!data.grants || data.grants.length === 0) {
      list.innerHTML = data.is_public
        ? "<li><em>No grants needed - this skill is public</em></li>"
        : "<li><em>No grants yet</em></li>";
      return;
    }
    list.innerHTML = data.grants.map((g) => {
      const upn = (g.user_upn || "").replace(/</g, "&lt;");
      const grantedBy = (g.granted_by || "").replace(/</g, "&lt;");
      const at = (g.granted_at || "").replace(/T/, " ").slice(0, 19);
      return `<li>
        <div>
          <div class="grant-upn">${upn}</div>
          <div class="grant-meta">granted by ${grantedBy} at ${at}</div>
        </div>
        <button class="danger" type="button" data-grant-remove="${encodeURIComponent(g.user_upn)}">Remove</button>
      </li>`;
    }).join("");
  } catch (err) {
    list.innerHTML = `<li><em>Failed to load: ${String(err).slice(0,200)}</em></li>`;
  }
}

function renderGrantsVisibility(isPublic) {
  const toggle = document.getElementById("grantsPublicToggle");
  const hint = document.getElementById("grantsPublicHint");
  const form = document.getElementById("grantsAddForm");
  if (toggle) toggle.checked = isPublic;
  if (form) form.classList.toggle("hidden", isPublic);
  if (hint) {
    hint.textContent = isPublic
      ? "Everyone signed in can use this skill; per-user grants are not required."
      : "Only the users listed below can use this skill.";
  }
}

async function handlePublicToggleChange(ev) {
  const skill = _currentGrantsSkill();
  const errEl = document.getElementById("grantsError");
  const toggle = ev.target;
  if (!skill) return;
  const next = toggle.checked;
  if (next && !confirm(`Make "${skill}" available to everyone?`)) {
    toggle.checked = false;
    return;
  }
  errEl.textContent = "";
  toggle.disabled = true;
  try {
    await setSkillVisibility(skill, next);
    await renderGrantsList(skill);
    await refreshSkills();
  } catch (err) {
    toggle.checked = !next;
    errEl.textContent = String(err).slice(0, 300);
  } finally {
    toggle.disabled = false;
  }
}

async function handleAddGrantSubmit(ev) {
  ev.preventDefault();
  const input = document.getElementById("grantsAddInput");
  const errEl = document.getElementById("grantsError");
  const skill = _currentGrantsSkill();
  if (!input || !skill) return;
  const upn = (input.value || "").trim().toLowerCase();
  if (!upn) return;
  errEl.textContent = "";
  try {
    await addSkillGrant(skill, { user_upn: upn });
    input.value = "";
    await renderGrantsList(skill);
  } catch (err) {
    errEl.textContent = String(err).slice(0, 300);
  }
}

async function handleGrantsListClick(ev) {
  const btn = ev.target.closest("button[data-grant-remove]");
  if (!btn) return;
  const upn = decodeURIComponent(btn.dataset.grantRemove);
  const skill = _currentGrantsSkill();
  const errEl = document.getElementById("grantsError");
  if (!confirm(`Remove access for ${upn}?`)) return;
  errEl.textContent = "";
  try {
    await removeSkillGrant(skill, upn);
    await renderGrantsList(skill);
  } catch (err) {
    errEl.textContent = String(err).slice(0, 300);
  }
}

function wireV7Ui() {
  // Refresh button
  const refreshBtn = document.getElementById("refreshSkillsBtn");
  if (refreshBtn) refreshBtn.addEventListener("click", handleRefreshSkillsClick);
  // Grants gear: opens modal for currently-selected skill
  const grantsBtn = document.getElementById("grantsBtn");
  if (grantsBtn) grantsBtn.addEventListener("click", () => {
    const sel = document.getElementById("targetSkill");
    const value = sel ? sel.value : "";
    if (!value) {
      showPermissionBanner("Select a skill first.");
      return;
    }
    openGrantsModalFor(value);
  });
  // Modal close
  document.querySelectorAll('[data-action="close-grants-modal"]').forEach((el) => {
    el.addEventListener("click", closeGrantsModal);
  });
  document.getElementById("grantsModal")?.addEventListener("click", (ev) => {
    if (ev.target.id === "grantsModal") closeGrantsModal();
  });
  document.querySelectorAll('[data-action="close-material-view"]').forEach((el) => {
    el.addEventListener("click", closeMaterialView);
  });
  document.getElementById("materialViewModal")?.addEventListener("click", (ev) => {
    if (ev.target.id === "materialViewModal") closeMaterialView();
  });
  // Save-samples modal
  document.querySelectorAll('[data-action="close-save-samples-modal"]').forEach((el) => {
    el.addEventListener("click", closeSaveSamplesModal);
  });
  document.getElementById("saveSamplesModal")?.addEventListener("click", (ev) => {
    if (ev.target.id === "saveSamplesModal") closeSaveSamplesModal();
  });
  document.querySelector('[data-action="save-samples-only"]')?.addEventListener("click", () => resolveSamplesSaveModal("only"));
  document.querySelector('[data-action="save-samples-revisit"]')?.addEventListener("click", () => resolveSamplesSaveModal("revisit"));
  document.querySelector('[data-action="save-samples-cancel"]')?.addEventListener("click", () => resolveSamplesSaveModal("cancel"));
  document.getElementById("grantsAddForm")?.addEventListener("submit", handleAddGrantSubmit);
  document.getElementById("grantsPublicToggle")?.addEventListener("change", handlePublicToggleChange);
  document.getElementById("grantsList")?.addEventListener("click", handleGrantsListClick);
  // Show/hide grants gear when targetSkill changes value.
  const sel = document.getElementById("targetSkill");
  if (sel) {
    sel.addEventListener("change", () => {
      const gb = document.getElementById("grantsBtn");
      if (gb) gb.style.display = sel.value ? "" : "none";
      // Re-filter the session list to the chosen skill (modify mode).
      renderSessionSelector();
    });
  }
}

// Show login gate when auth status reports unauthenticated.
function v7UpdateLoginGate(status) {
  showLoginGate(!(status && status.authenticated));
  // Show grants gear only if a target skill is bound + signed in.
  const grantsBtn = document.getElementById("grantsBtn");
  if (grantsBtn) {
    const sel = document.getElementById("targetSkill");
    grantsBtn.style.display = (status && status.authenticated && sel && sel.value) ? "" : "none";
  }
}

// Hook into existing renderAuthStatus by patching it after-the-fact.
(function hookAuthStatusForGate() {
  const orig = window.renderAuthStatus;
  // renderAuthStatus is module-scoped, not on window; so just call v7UpdateLoginGate
  // whenever the global authStatus changes. We'll poll at load + on document events.
  document.addEventListener("DOMContentLoaded", () => {
    wireV7Ui();
  });
  if (document.readyState !== "loading") wireV7Ui();
})();

// Expose for renderSession integration (manual call from main module if desired).
window.__sgv7 = {
  showPermissionBanner,
  showLoginGate,
  openGrantsModalFor,
  v7UpdateLoginGate,
};

