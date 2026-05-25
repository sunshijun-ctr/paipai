/* Local "small tools" modals: scientific calculator, word counter, token /
 * traffic monitor, plus the legacy month-calendar widget and the shared
 * tool-modal shell. All run client-side (the monitor reads /monitor/*).
 *
 * State (calcState, toolCalendarDate) is module-private; only the functions
 * are bridged (main.js) for the tool-menu buttons and the onclick/oninput
 * handlers in the modal HTML these functions generate.
 *
 * Dependencies on still-inline code: showCalendarView (nav switch).
 */

import { esc } from "./utils.js";
import { apiGet } from "./api.js";
import { draftText } from "./chat.js";
import { formatLocalDate, getCalendarDayType } from "./calendar.js";
import { act, actChange, actInput, actKeydown } from "./events.js";
import { showCalendarView } from "./nav.js";

let toolCalendarDate = new Date();

// ── Modal shell ────────────────────────────────────────────────────────

export function openToolModal(title, sub, html) {
  stopDateTimeTicker();
  const modal = document.getElementById('tool-modal');
  document.getElementById('tool-modal-title').textContent = title;
  document.getElementById('tool-modal-sub').textContent = sub || '本地小工具，不占用 Agent 任务';
  document.getElementById('tool-modal-body').innerHTML = html;
  modal.classList.add('on');
}

export function closeToolModal(event) {
  if (event && event.target !== document.getElementById('tool-modal')) return;
  stopDateTimeTicker();
  document.getElementById('tool-modal')?.classList.remove('on');
}

// ── Calculator ─────────────────────────────────────────────────────────

export function openCalculator() {
  openToolModal('计算器', '支持根号、对数、指数、幂和括号', `
    <div class="calc-shell">
      <div class="calc-display">
        <div id="calc-expression" class="calc-expression">等待输入表达式</div>
        <div id="calc-result" class="calc-result-line">0</div>
      </div>
      <div class="calc-grid">
        <button class="calc-key op" ${act('calcPress', 'sin(')}>sin</button>
        <button class="calc-key op" ${act('calcPress', 'cos(')}>cos</button>
        <button class="calc-key op" ${act('calcPress', 'tan(')}>tan</button>
        <button class="calc-key op" ${act('calcToggleParen')}>()</button>
        <button class="calc-key op" ${act('calcPress', 'sqrt(')}>√x</button>
        <button class="calc-key op" ${act('calcPress', 'ln(')}>ln</button>
        <button class="calc-key op" ${act('calcPress', 'log(')}>log</button>
        <button class="calc-key op" ${act('calcPress', '^')}>xⁿ</button>
        <button class="calc-key" ${act('calcPress', '7')}>7</button>
        <button class="calc-key" ${act('calcPress', '8')}>8</button>
        <button class="calc-key" ${act('calcPress', '9')}>9</button>
        <button class="calc-key op" ${act('calcPress', '÷')}>÷</button>
        <button class="calc-key" ${act('calcPress', '4')}>4</button>
        <button class="calc-key" ${act('calcPress', '5')}>5</button>
        <button class="calc-key" ${act('calcPress', '6')}>6</button>
        <button class="calc-key op" ${act('calcPress', '×')}>×</button>
        <button class="calc-key" ${act('calcPress', '1')}>1</button>
        <button class="calc-key" ${act('calcPress', '2')}>2</button>
        <button class="calc-key" ${act('calcPress', '3')}>3</button>
        <button class="calc-key op" ${act('calcPress', '−')}>−</button>
        <button class="calc-key" ${act('calcPress', '0')}>0</button>
        <button class="calc-key" ${act('calcPress', '.')}>.</button>
        <button class="calc-key op" ${act('calcBackspace')}>⌫</button>
        <button class="calc-key op" ${act('calcPress', '+')}>+</button>
        <button class="calc-key op" ${act('calcPress', 'pi')}>π</button>
        <button class="calc-key op" ${act('calcPress', 'e')}>e</button>
        <button class="calc-key danger" ${act('calcClear')}>AC</button>
        <button class="calc-key equal" ${act('calcRun')}>=</button>
      </div>
      <div class="calc-actions">
        <button class="sm-btn" ${act('calcDraftResult')}>放入对话框</button>
      </div>
    </div>
  `);
  calcClear();
}

const calcState = {
  expr: '',
  result: '',
};

export function normalizeCalcExpression(expr) {
  let s = expr
    .toLowerCase()
    .replace(/π/g, 'pi')
    .replace(/√/g, 'sqrt')
    .replace(/×/g, '*')
    .replace(/÷/g, '/')
    .replace(/−/g, '-');
  if (!/^[0-9a-z_+\-*/().,%\s^]+$/.test(s)) throw new Error('unsupported chars');
  const allowed = new Set(['sqrt','log','ln','sin','cos','tan','pi','e']);
  const names = s.match(/[a-z_]+/g) || [];
  for (const name of names) {
    if (!allowed.has(name)) throw new Error(`unsupported function: ${name}`);
  }
  s = s.replace(/\^/g, '**');
  s = s.replace(/\bpi\b/g, 'Math.PI').replace(/\be\b/g, 'Math.E');
  const fnMap = {
    sqrt:'Math.sqrt',
    log:'Math.log10',
    ln:'Math.log',
    sin:'Math.sin',
    cos:'Math.cos',
    tan:'Math.tan',
  };
  return s.replace(/\b(sqrt|log|ln|sin|cos|tan)\s*\(/g, (_, fn) => `${fnMap[fn]}(`);
}

export function formatCalcResult(value) {
  const normalized = Math.abs(value) < 1e-12 ? 0 : value;
  return Number.isInteger(normalized) ? String(normalized) : Number(normalized.toPrecision(12)).toString();
}

export function evaluateCalcExpression(expr) {
  const normalized = normalizeCalcExpression(expr);
  const value = Function(`"use strict"; return (${normalized})`)();
  if (!Number.isFinite(value)) throw new Error('invalid result');
  return formatCalcResult(value);
}

export function renderCalculator(resultText) {
  const expression = document.getElementById('calc-expression');
  const result = document.getElementById('calc-result');
  if (!expression || !result) return;
  expression.textContent = calcState.expr || '等待输入表达式';
  result.textContent = resultText ?? (calcState.result || '0');
}

export function calcTryEvaluate() {
  if (!calcState.expr) {
    calcState.result = '';
    renderCalculator('0');
    return;
  }
  try {
    calcState.result = evaluateCalcExpression(calcState.expr);
    renderCalculator(calcState.result);
  } catch (e) {
    renderCalculator(calcState.result || '...');
  }
}

export function calcPress(value) {
  calcState.expr += value;
  calcTryEvaluate();
}

export function calcToggleParen() {
  const opened = (calcState.expr.match(/\(/g) || []).length;
  const closed = (calcState.expr.match(/\)/g) || []).length;
  calcPress(opened > closed ? ')' : '(');
}

export function calcBackspace() {
  calcState.expr = calcState.expr.slice(0, -1);
  calcTryEvaluate();
}

export function calcClear() {
  calcState.expr = '';
  calcState.result = '';
  renderCalculator('0');
}

export function calcRun() {
  if (!calcState.expr) {
    renderCalculator('0');
    return;
  }
  try {
    calcState.result = evaluateCalcExpression(calcState.expr);
    renderCalculator(calcState.result);
  } catch(e) {
    renderCalculator('表达式错误');
  }
}

export function calcDraftResult() {
  if (!calcState.result && calcState.expr) calcRun();
  draftText(calcState.result || '0');
}

// ── Calendar widget (legacy month grid) ────────────────────────────────

export function openCalendarTool() {
  closeToolModal();
  showCalendarView(document.getElementById('nav-chat'));
}

export function shiftCalendarMonth(delta) {
  toolCalendarDate = new Date(toolCalendarDate.getFullYear(), toolCalendarDate.getMonth() + delta, 1);
  renderCalendarTool();
}

export function renderCalendarTool() {
  const box = document.getElementById('calendar-body');
  if (!box) return;
  const year = toolCalendarDate.getFullYear();
  const month = toolCalendarDate.getMonth();
  const today = new Date();
  const firstDay = new Date(year, month, 1).getDay();
  const days = new Date(year, month + 1, 0).getDate();
  const prevDays = new Date(year, month, 0).getDate();
  const cells = [];
  for (let i = firstDay - 1; i >= 0; i--) cells.push({day: prevDays - i, muted:true});
  for (let d = 1; d <= days; d++) cells.push({day:d, date:new Date(year, month, d)});
  let nextDay = 1;
  while (cells.length % 7) cells.push({day: nextDay++, muted:true});
  const weeks = ['日','一','二','三','四','五','六'];
  box.innerHTML = `
    <div class="calendar-head">
      <button class="sm-btn" ${act('shiftCalendarMonth', -1)}>上月</button>
      <div class="calendar-title">${year} 年 ${month + 1} 月</div>
      <button class="sm-btn" ${act('shiftCalendarMonth', 1)}>下月</button>
    </div>
    <div class="calendar-grid">
      ${weeks.map(w => `<div class="calendar-cell head">${w}</div>`).join('')}
      ${cells.map(c => {
        const isToday = c.date && c.date.toDateString() === today.toDateString();
        const iso = c.date ? formatLocalDate(c.date) : '';
        const dayType = c.date ? getCalendarDayType(c.date) : 'workday';
        const tag = c.date ? (dayType === 'restday' ? '休' : '班') : '';
        return `<button class="calendar-cell${c.muted ? ' muted' : ''}${isToday ? ' today' : ''} ${dayType}" ${c.date ? `${act('draftText', `请帮我安排 ${iso} 的科研计划：`)}` : ''}><span class="calendar-day">${c.day}</span>${tag ? `<span class="calendar-tag">${tag}</span>` : ''}</button>`;
      }).join('')}
    </div>
    <div class="calendar-legend">
      <span><i></i>工作日</span>
      <span class="rest"><i></i>休息日</span>
    </div>
    <div class="calendar-note">2026 年按国务院办公厅节假日安排显示调休；其他年份按周末规则显示。</div>
  `;
}

// ── Word counter ───────────────────────────────────────────────────────

export function openWordCounter() {
  openToolModal('字数统计', '统计字符、中文字数、英文词数和段落', `
    <textarea id="word-counter-input" class="tool-textarea" placeholder="粘贴文本..." ${actInput('updateWordCounter')}></textarea>
    <div class="tool-stat-grid">
      <div class="tool-stat"><strong id="wc-chars">0</strong><span>字符</span></div>
      <div class="tool-stat"><strong id="wc-zh">0</strong><span>中文字符</span></div>
      <div class="tool-stat"><strong id="wc-words">0</strong><span>英文词</span></div>
      <div class="tool-stat"><strong id="wc-lines">0</strong><span>行数</span></div>
      <div class="tool-stat"><strong id="wc-paras">0</strong><span>段落</span></div>
      <div class="tool-stat"><strong id="wc-reading">0</strong><span>分钟阅读</span></div>
    </div>
  `);
  setTimeout(() => document.getElementById('word-counter-input')?.focus(), 0);
}

export function updateWordCounter() {
  const text = document.getElementById('word-counter-input')?.value || '';
  const zh = (text.match(/[一-鿿]/g) || []).length;
  const words = (text.match(/[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*/g) || []).length;
  const lines = text ? text.split(/\n/).length : 0;
  const paras = text.trim() ? text.trim().split(/\n\s*\n/).length : 0;
  const minutes = text.trim() ? Math.max(1, Math.ceil((zh + words) / 450)) : 0;
  document.getElementById('wc-chars').textContent = text.length;
  document.getElementById('wc-zh').textContent = zh;
  document.getElementById('wc-words').textContent = words;
  document.getElementById('wc-lines').textContent = lines;
  document.getElementById('wc-paras').textContent = paras;
  document.getElementById('wc-reading').textContent = minutes;
}

// ── Token / traffic monitor ────────────────────────────────────────────

export function fmtMonitorNumber(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0';
  return n.toLocaleString('zh-CN', {maximumFractionDigits: 4});
}

export function tokenBar(value, max) {
  const pct = max > 0 ? Math.max(4, Math.round(Number(value || 0) / max * 100)) : 0;
  return `<div style="height:7px;background:var(--color-accent-light);border-radius:999px;overflow:hidden;margin-top:5px">
    <div style="height:100%;width:${pct}%;background:var(--color-accent);border-radius:999px"></div>
  </div>`;
}

export async function openTokenMonitorTool() {
  openToolModal('流量监控', 'LLM Token 消耗、费用与调用概览', `
    <div class="tool-row" style="margin-top:0">
      <select id="token-monitor-days" class="tool-input" style="width:140px" ${actChange('loadTokenMonitor')}>
        <option value="1">近 1 天</option>
        <option value="7" selected>近 7 天</option>
        <option value="30">近 30 天</option>
        <option value="90">近 90 天</option>
      </select>
      <input id="token-monitor-agent" class="tool-input" style="flex:1" placeholder="按 agent_name 过滤（可选）" ${actKeydown('__enterRun','loadTokenMonitor','@event')}>
      <button class="sm-btn pri" ${act('loadTokenMonitor')}>刷新</button>
    </div>
    <div id="token-monitor-body" class="tool-result">正在加载监控数据...</div>
  `);
  await loadTokenMonitor();
}

export async function loadTokenMonitor() {
  const body = document.getElementById('token-monitor-body');
  if (!body) return;
  const days = document.getElementById('token-monitor-days')?.value || '7';
  const agent = (document.getElementById('token-monitor-agent')?.value || '').trim();
  const qs = `days=${encodeURIComponent(days)}${agent ? `&agent_name=${encodeURIComponent(agent)}` : ''}`;
  body.textContent = '正在加载监控数据...';
  try {
    const [summary, daily, models, agents, errors] = await Promise.all([
      apiGet(`/monitor/summary?${qs}`),
      apiGet(`/monitor/daily-trend?${qs}`),
      apiGet(`/monitor/model-breakdown?${qs}`),
      apiGet(`/monitor/agents?days=${encodeURIComponent(days)}`),
      apiGet(`/monitor/errors?days=1&limit=5`),
    ]);
    renderTokenMonitor(summary || {}, daily || [], models || [], agents || [], errors || []);
  } catch(err) {
    body.innerHTML = `<div class="paper-empty">监控数据加载失败：${esc(err.body?.detail || err.message || err)}</div>`;
  }
}

export function renderTokenMonitor(summary, daily, models, agents, errors) {
  const body = document.getElementById('token-monitor-body');
  if (!body) return;
  const maxDaily = Math.max(0, ...daily.map(x => Number(x.total_tokens || 0)));
  const maxModel = Math.max(0, ...models.map(x => Number(x.total_tokens || 0)));
  body.innerHTML = `
    <div class="tool-stat-grid">
      <div class="tool-stat"><strong>${fmtMonitorNumber(summary.call_count)}</strong><span>调用</span></div>
      <div class="tool-stat"><strong>${fmtMonitorNumber(summary.total_tokens)}</strong><span>总 tokens</span></div>
      <div class="tool-stat"><strong>¥${fmtMonitorNumber(summary.cost_yuan)}</strong><span>费用</span></div>
      <div class="tool-stat"><strong>${fmtMonitorNumber(summary.prompt_tokens)}</strong><span>输入</span></div>
      <div class="tool-stat"><strong>${fmtMonitorNumber(summary.completion_tokens)}</strong><span>输出</span></div>
      <div class="tool-stat"><strong>${fmtMonitorNumber(summary.avg_latency_ms)}ms</strong><span>平均耗时</span></div>
    </div>
    <div style="margin-top:14px;font-weight:800">每日趋势</div>
    ${(daily || []).length ? daily.slice(0, 7).map(row => `
      <div style="margin-top:8px">
        <div style="display:flex;justify-content:space-between;gap:10px"><span>${esc(row.day)}</span><span>${fmtMonitorNumber(row.total_tokens)} tokens</span></div>
        ${tokenBar(row.total_tokens, maxDaily)}
      </div>
    `).join('') : '<div class="paper-empty">暂无每日趋势数据</div>'}
    <div style="margin-top:14px;font-weight:800">模型分布</div>
    ${(models || []).length ? models.slice(0, 8).map(row => `
      <div style="margin-top:8px">
        <div style="display:flex;justify-content:space-between;gap:10px"><span>${esc(row.provider || '')} / ${esc(row.model || '')}</span><span>${fmtMonitorNumber(row.total_tokens)}</span></div>
        ${tokenBar(row.total_tokens, maxModel)}
        <div class="paper-meta">${fmtMonitorNumber(row.call_count)} 次 · ¥${fmtMonitorNumber(row.cost_yuan)} · 错误 ${fmtMonitorNumber(row.error_count)}</div>
      </div>
    `).join('') : '<div class="paper-empty">暂无模型分布数据</div>'}
    <div style="margin-top:14px;font-weight:800">Agent 汇总</div>
    ${(agents || []).length ? agents.slice(0, 6).map(row => `
      <div class="paper-meta" style="display:flex;justify-content:space-between;gap:10px;margin-top:6px">
        <span>${esc(row.agent_name || 'default')}</span><span>${fmtMonitorNumber(row.total_tokens)} tokens · ¥${fmtMonitorNumber(row.cost_yuan)}</span>
      </div>
    `).join('') : '<div class="paper-empty">暂无 Agent 数据</div>'}
    <div style="margin-top:14px;font-weight:800">最近错误</div>
    ${(errors || []).length ? errors.map(row => `
      <div class="paper-meta" style="margin-top:6px">${esc(row.created_at || '')} · ${esc(row.agent_name || '')} · ${esc(row.error_msg || '')}</div>
    `).join('') : '<div class="paper-empty">暂无错误记录</div>'}
  `;
}

export function stopDateTimeTicker() {}
