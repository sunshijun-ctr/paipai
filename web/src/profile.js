/* User profile + per-agent LLM config.
 *
 * Two related concerns share a module because both render into the
 * same Settings drawer and share auth state:
 *
 *   - currentProfile  : display name + avatar + self-description.
 *                       Used by addMsg() (still inline) for user-side
 *                       avatars — bridged via window for now.
 *   - llmConfig +     : per-agent provider/model overrides surfaced as
 *     llmOptions        the Settings → LLM Config grid.
 *
 * Pattern: state objects are MUTATED IN PLACE (Object.assign) rather
 * than reassigned. That way `window.currentProfile.avatar` stays valid
 * for legacy inline reads regardless of when the latest fetch came in. */

import { esc, js, toast } from "./utils.js";
import { renderAvatar } from "./avatar.js";
import { AGENT_LABELS } from "./constants.js";
import { apiGet, apiPut, apiPost } from "./api.js";
import { act, actChange } from "./events.js";

// ── State (mutated in place — see module docstring) ─────────────────────

export const currentProfile = {
  display_name: "研究者",
  avatar: "",
  self_description: "",
};

const llmConfig = {};
const llmOptions = {
  providers: ["ollama", "openai", "anthropic", "qwen", "doubao", "gemini"],
  defaults: {},
  agents: [],
};

// ── Profile ─────────────────────────────────────────────────────────────

export async function loadProfile() {
  try {
    const d = await apiGet("/api/profile");
    Object.assign(currentProfile, d.profile || {});
    renderProfile();
  } catch (e) {
    console.warn("loadProfile failed", e);
  }
}

export function renderProfile() {
  const name = currentProfile.display_name || "研究者";
  const nameEl = document.getElementById("user-name");
  if (nameEl) nameEl.textContent = name;
  const av = document.getElementById("user-avatar");
  if (av) renderAvatar(av, currentProfile.avatar, name);
  const nameInput = document.getElementById("profile-name-input");
  if (nameInput) nameInput.value = name;
  const descInput = document.getElementById("profile-desc-input");
  if (descInput) descInput.value = currentProfile.self_description || "";
  const preview = document.getElementById("profile-avatar-preview");
  if (preview) renderAvatar(preview, currentProfile.avatar, name);
  document
    .querySelectorAll(".user-msg-av")
    .forEach((el) => renderAvatar(el, currentProfile.avatar, name));
}

export function pickAvatar(input) {
  const file = input.files[0];
  if (!file) return;
  input.value = "";
  if (file.size > 300_000) {
    toast("头像图片不能超过 300KB");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    currentProfile.avatar = String(reader.result || "");
    renderProfile();
  };
  reader.readAsDataURL(file);
}

export function clearAvatar() {
  currentProfile.avatar = "";
  renderProfile();
}

export async function saveProfile() {
  const body = {
    display_name: document.getElementById("profile-name-input").value.trim(),
    avatar: currentProfile.avatar || "",
    self_description: document.getElementById("profile-desc-input").value.trim(),
  };
  try {
    const d = await apiPut("/api/profile", body);
    Object.assign(currentProfile, d.profile || {});
    renderProfile();
    toast("设置已保存");
  } catch (err) {
    toast(`保存失败：${err.body?.detail || err.message || err}`);
  }
}

// ── LLM config grid ─────────────────────────────────────────────────────

export async function loadLlmConfig() {
  try {
    const d = await apiGet("/api/llm-config");
    Object.assign(llmConfig, d.config || {});
    if (d.options) Object.assign(llmOptions, d.options);
    renderLlmConfig();
  } catch (e) {
    console.warn("loadLlmConfig failed", e);
  }
}

export function renderLlmConfig() {
  const grid = document.getElementById("llm-config-grid");
  if (!grid) return;
  const providers = llmOptions.providers || [];
  const agents = llmOptions.agents || Object.keys(llmConfig);
  const rows = agents
    .map((name) => {
      const item = llmConfig[name] || {};
      const env = (llmOptions.env_vars || {})[name] || {};
      const provider = item.provider || providers[0] || "ollama";
      const model = item.model || (llmOptions.defaults || {})[provider] || "";
      return `
      <div class="agent-name">${esc(AGENT_LABELS[name] || name)}</div>
      <select class="form-select" id="llm-provider-${esc(name)}" ${actChange('fillDefaultModel', name)}>
        ${providers
          .map((p) => `<option value="${esc(p)}"${p === provider ? " selected" : ""}>${esc(p)}</option>`)
          .join("")}
      </select>
      <input class="form-input" id="llm-model-${esc(name)}" value="${esc(model)}" placeholder="model name">
      <div class="env-hint">${esc(env.provider || "")}<br>${esc(env.model || "")}<br>${esc(env.api_key || "")}</div>
      <div>
        <button class="sm-btn" id="llm-test-btn-${esc(name)}" ${act('testLlmConfig', name)}>测试</button>
        <div class="test-result" id="llm-test-${esc(name)}"></div>
      </div>`;
    })
    .join("");
  grid.innerHTML = `
    <div class="llm-grid-head">Agent</div>
    <div class="llm-grid-head">Provider</div>
    <div class="llm-grid-head">Model</div>
    <div class="llm-grid-head">.env 配置项</div>
    <div class="llm-grid-head">测试</div>
    ${rows}`;
}

export function fillDefaultModel(agentName) {
  const provider = document.getElementById(`llm-provider-${agentName}`)?.value;
  const modelInput = document.getElementById(`llm-model-${agentName}`);
  if (provider && modelInput && !modelInput.value.trim()) {
    modelInput.value = (llmOptions.defaults || {})[provider] || "";
  }
}

export async function saveLlmConfig() {
  const agents = llmOptions.agents || Object.keys(llmConfig);
  const config = {};
  agents.forEach((name) => {
    config[name] = {
      provider: document.getElementById(`llm-provider-${name}`)?.value || "ollama",
      model: document.getElementById(`llm-model-${name}`)?.value.trim() || "",
    };
  });
  try {
    const d = await apiPut("/api/llm-config", { config });
    // Wipe + repopulate so removed keys don't linger
    for (const k of Object.keys(llmConfig)) delete llmConfig[k];
    Object.assign(llmConfig, d.config || config);
    renderLlmConfig();
    toast("LLM 配置已保存并生效");
  } catch (err) {
    toast(`保存失败：${err.body?.detail || err.message || err}`);
  }
}

export async function testLlmConfig(agentName) {
  const provider = document.getElementById(`llm-provider-${agentName}`)?.value || "";
  const model = document.getElementById(`llm-model-${agentName}`)?.value.trim() || "";
  const out = document.getElementById(`llm-test-${agentName}`);
  const btn = document.getElementById(`llm-test-btn-${agentName}`);
  if (!provider || !model) {
    if (out) { out.className = "test-result err"; out.textContent = "请先填写 provider 和 model"; }
    return;
  }
  if (out) { out.className = "test-result"; out.textContent = "测试中..."; }
  if (btn) btn.disabled = true;
  try {
    const d = await apiPost("/api/llm-config/test", { agent_name: agentName, provider, model });
    if (out) {
      if (d.success) {
        out.className = "test-result ok";
        out.textContent = `成功 ${d.latency_ms}ms · ${d.model}`;
      } else {
        out.className = "test-result err";
        out.textContent = d.error || "测试失败";
      }
    }
  } catch (err) {
    if (out) { out.className = "test-result err"; out.textContent = err.message || String(err); }
  } finally {
    if (btn) btn.disabled = false;
  }
}
