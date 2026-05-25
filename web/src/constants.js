/* Static lookup tables shared across the chat UI.
 *
 * These were five separate `const` blocks scattered through the inline
 * scripts in index.html. Co-locating them here makes adding a new
 * intent / agent / writing mode a one-file edit, and gets us a head
 * start on the i18n problem (everything user-facing in one place).
 *
 * NB: backend enum strings (e.g. "literature_search", "paper_writing")
 * MUST stay in sync with the Python side. Search-and-replace order:
 *   1. Python: app/agents/intent/agent.py (output enum)
 *   2. Here: add the new key
 *   3. Add to ASSISTANT_AVATARS if a new intent
 *   4. Backend tests should fail if a new intent is wired but missing
 *      from here — TODO: add a runtime check on app startup. */

export const INTENT_LABELS = {
  literature_search:           "📚 文献搜索",
  web_search:                  "🌐 网页搜索",
  paper_download:              "⬇️ 下载论文",
  research_literature_reading: "📖 阅读文献",
  paper_qa:                    "❓ 论文问答",
  add_to_library:              "📌 加入知识库",
  library_qa:                  "🗄️ 知识库查询",
  clear_temp_rag:              "🗑️ 清除缓存",
  summarize_session:           "📝 生成摘要",
  general_chat:                "💬 对话",
  general_open_task:           "🧭 开放任务",
  paper_writing:               "论文写作",
};

// All intents currently share the "PP" (paipai) avatar. The lookup is
// kept as a separate table so per-intent variation (e.g. different
// avatar accent colors) can be added without touching the labels.
export const ASSISTANT_AVATARS = Object.fromEntries(
  Object.keys(INTENT_LABELS).map((k) => [k, "PP"]),
);

export const AGENT_LABELS = {
  intent_agent:     "意图识别",
  general_agent:    "通用规划",
  literature_agent: "文献检索",
  reading_agent:    "论文阅读",
  note_agent:       "科研笔记",
  summary_agent:    "总结报告",
  chat_agent:       "通用对话",
  writing_agent:    "论文写作",
};

export const WRITING_LABELS = {
  zh: "中文", en: "English",
  academic: "学术", formal: "正式", concise: "简洁", review: "综述",
  short: "短", medium: "中", long: "长",
  polish: "润色", rewrite: "改写", supplement: "补充论述", imitate: "模仿写作",
};

export const FM_CAT_LABELS = {
  upload: "文档",
  image:  "图片",
  figure: "插图",
};

// Researcher quotes shown on the empty/welcome state. Keep them short
// — too long and the layout breaks on narrow screens.
export const QUOTES = [
  { t: '"Science is the pursuit of truth, knowledge is its reward."',                                       by: "— Li Ka Shing" },
  { t: '"Research is seeing what everybody has seen and thinking what nobody has thought."',                by: "— Albert Szent-Györgyi" },
  { t: '"An investment in knowledge pays the best interest."',                                              by: "— Benjamin Franklin" },
  { t: '"The art of research is finding the signal in the noise."',                                         by: "— Anonymous" },
  { t: '"In the middle of difficulty lies opportunity."',                                                   by: "— Albert Einstein" },
  { t: '"A problem well stated is a problem half solved."',                                                 by: "— Charles Kettering" },
  { t: '"Equipped with his five senses, man explores the universe around him and calls the adventure Science."', by: "— Edwin Hubble" },
];
