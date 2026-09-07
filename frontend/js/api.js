// --- API base config ------------------------------------------------------
// `window.SG_API_BASE` is set by frontend/js/config.js. It is empty so the
// FastAPI backend serves /api/* on the same origin.
function apiUrl(path) {
  const base = (typeof window !== "undefined" && window.SG_API_BASE) ? String(window.SG_API_BASE).replace(/\/$/, "") : "";
  if (!base) return path;
  return (typeof path === "string" && path.startsWith("/api/")) ? base + path : path;
}
function apiFetch(path, init = {}) {
  const opts = { credentials: "include", ...init };
  return window.fetch(apiUrl(path), opts);
}

export async function createSession(payload) {
  console.info("[Skill Generator v2 API] POST /api/sessions", payload);
  const res = await fetchWithTimeout("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 15000);
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function getSession(id) {
  const res = await apiFetch(`/api/sessions/${id}`);
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function listSessions() {
  const res = await apiFetch("/api/sessions");
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function listSkills(storeId) {
  const qs = storeId ? `?store=${encodeURIComponent(storeId)}` : "";
  const res = await apiFetch(`/api/skills${qs}`);
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function fetchInspect() {
  const res = await apiFetch("/api/inspect");
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function fetchAuthStatus() {
  const res = await apiFetch("/api/auth/status");
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export function startMicrosoftLogin() {
  window.location.href = apiUrl("/api/auth/login");
}

export async function logoutMicrosoft() {
  const res = await apiFetch("/api/auth/logout", { method: "POST" });
  if (!res.ok) throw new Error(await formatFetchError(res));
}

export class StaleToolCallError extends Error {
  constructor(message) {
    super(message);
    this.name = "StaleToolCallError";
    this.stale = true;
  }
}

export class ApiError extends Error {
  constructor(message, status, detail = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function responseError(res) {
  let payload = null;
  try {
    payload = await res.json();
  } catch {
    payload = null;
  }
  const detail = payload?.detail ?? payload;
  const text = typeof detail === "string" ? detail : detail?.message;
  const message = text || `${res.status} ${res.statusText} calling ${res.url}`;
  return new ApiError(message, res.status, detail);
}

export async function sendToolResult(sessionId, toolCallId, result) {
  const res = await apiFetch(`/api/sessions/${sessionId}/tool-result`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool_call_id: toolCallId, result }),
  });
  if (!res.ok) {
    const error = await responseError(res);
    if (res.status === 404) throw new StaleToolCallError(error.message);
    throw error;
  }
  return res.json();
}

export async function saveSessionSkill(sessionId, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw await responseError(res);
  return res.json();
}

export async function updateSessionChildren(sessionId, children) {
  const res = await apiFetch(`/api/sessions/${sessionId}/children`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ children }),
  });
  if (!res.ok) throw await responseError(res);
  return res.json();
}

export async function fetchSessionTopology(sessionId) {
  const res = await apiFetch(`/api/sessions/${sessionId}/topology`);
  if (!res.ok) throw await responseError(res);
  return res.json();
}

export async function updateSessionDraft(sessionId, draft) {
  const res = await apiFetch(`/api/sessions/${sessionId}/draft`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function undoSessionPatch(sessionId, patchId) {
  const res = await apiFetch(`/api/sessions/${sessionId}/patches/${encodeURIComponent(patchId)}/undo`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function refreshAcaEnv(sessionId) {
  const res = await apiFetch(`/api/sessions/${sessionId}/aca-env`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function refreshPeerSkills(sessionId) {
  const res = await apiFetch(`/api/sessions/${sessionId}/peer-skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function runSessionTest(sessionId, payload = {}) {
  const res = await apiFetch(`/api/sessions/${sessionId}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function updateChecklistItem(sessionId, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/checklist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function updateSamples(sessionId, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/samples`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function updateVariables(sessionId, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/variables`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function updateNeighbors(sessionId, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/neighbors`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function openNeighborEdit(sessionId, skill) {
  const res = await apiFetch(`/api/sessions/${sessionId}/neighbor-edits/${encodeURIComponent(skill)}/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function proposeNeighborEdit(sessionId, skill, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/neighbor-edits/${encodeURIComponent(skill)}/propose`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function selectNeighborEdit(sessionId, skill, payload) {
  const res = await apiFetch(`/api/sessions/${sessionId}/neighbor-edits/${encodeURIComponent(skill)}/select`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function saveNeighborEditApi(sessionId, skill) {
  const res = await apiFetch(`/api/sessions/${sessionId}/neighbor-edits/${encodeURIComponent(skill)}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function runSkillTest(name, payload) {
  const res = await apiFetch(`/api/skills/${encodeURIComponent(name)}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function postSSE(url, payload, onEvent) {
  // Backend now returns {"events": [...]} as one JSON document (no SSE).
  // The Function App ASGI runtime cannot reliably stream SSE, so we batch.
  console.info(`[Skill Generator v2 API] POST ${url}`, payload);
  const res = await fetchWithTimeout(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 180000);
  if (!res.ok) throw new Error(await formatFetchError(res));
  const body = await res.json();
  const events = Array.isArray(body?.events) ? body.events : [];
  for (const evt of events) {
    try {
      onEvent(evt.event, evt.data || {});
    } catch (handlerErr) {
      console.error("[Skill Generator v2 API] event handler failed", evt, handlerErr);
    }
  }
}


export async function addSessionMaterial(sessionId, material) {
  const res = await apiFetch(`/api/sessions/${sessionId}/materials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(material),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function updateSessionMaterial(sessionId, materialId, material) {
  const res = await apiFetch(`/api/sessions/${sessionId}/materials/${encodeURIComponent(materialId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(material),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function deleteSessionMaterial(sessionId, materialId) {
  const res = await apiFetch(`/api/sessions/${sessionId}/materials/${encodeURIComponent(materialId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

async function formatFetchError(res) {
  let body = "";
  try {
    body = await res.text();
  } catch {
    body = "";
  }
  return `${res.status} ${res.statusText} calling ${res.url}${body ? `: ${body}` : ""}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await apiFetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s calling ${url}`);
    }
    throw err;
  } finally {
    window.clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// v7 RLS: grants management & skills cache refresh
// ---------------------------------------------------------------------------

export async function refreshSkillsCache() {
  const res = await apiFetch("/api/skills/refresh", { method: "POST" });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function listSkillGrants(name) {
  const res = await apiFetch(`/api/skills/${encodeURIComponent(name)}/grants`);
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function addSkillGrant(name, payload) {
  const res = await apiFetch(`/api/skills/${encodeURIComponent(name)}/grants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function removeSkillGrant(name, userUpn) {
  const res = await apiFetch(
    `/api/skills/${encodeURIComponent(name)}/grants/${encodeURIComponent(userUpn)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}

export async function setSkillVisibility(name, isPublic) {
  const res = await apiFetch(`/api/skills/${encodeURIComponent(name)}/visibility`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_public: !!isPublic }),
  });
  if (!res.ok) throw new Error(await formatFetchError(res));
  return res.json();
}
