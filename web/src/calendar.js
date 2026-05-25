/* Day-task calendar — month grid + per-day editor.
 *
 * State (module-private, also bridged to window so the still-inline
 * tool-calendar widget that reuses getCalendarDayType keeps working):
 *   calCursor       — first day of the displayed month
 *   calSelectedDate — currently picked date (yyyy-mm-dd)
 *   calTasks        — tasks for the displayed month
 *   calEditingId    — id of the task being edited, or "" for "new"
 *
 * `calendarHolidayRules` is a hand-maintained CN holiday + makeup-day
 * table for 2026. Update at end of each year. The two helper functions
 * `formatLocalDate` / `getCalendarDayType` are exported because the
 * "tool calendar" mini-widget in index.html reuses them via window. */

import { mk, esc, js, toast } from "./utils.js";
import { apiGet, apiPost, apiPut, apiDelete } from "./api.js";
import { act, actChange } from "./events.js";
import { confirmDialog } from "./confirm-dialog.js";
import { showCalendarView } from "./nav.js";

// ── Constants: 2026 CN holiday + makeup-workday calendar ───────────────

export const calendarHolidayRules = {
  rest: new Set([
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
  ]),
  work: new Set([
    "2026-01-04",
    "2026-02-14", "2026-02-28",
    "2026-05-09",
    "2026-09-20", "2026-10-10",
  ]),
};

// ── Helpers (exported — used by inline tool-calendar widget too) ───────

export function formatLocalDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function getCalendarDayType(date) {
  const iso = formatLocalDate(date);
  if (calendarHolidayRules.work.has(iso)) return "workday";
  if (calendarHolidayRules.rest.has(iso)) return "restday";
  const weekday = date.getDay();
  return weekday === 0 || weekday === 6 ? "restday" : "workday";
}

export function calDateKey(date) {
  const d = new Date(date);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function calMonthKey(date) {
  const d = new Date(date);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

export function calDisplayDate(dateKey) {
  const d = new Date(`${dateKey}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateKey;
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const type = getCalendarDayType(d);
  const label = type === "restday"
    ? "假日"
    : calendarHolidayRules.work.has(dateKey)
      ? "调休上班"
      : "工作日";
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日 · ${weekdays[d.getDay()]} · ${label}`;
}

// ── State ──────────────────────────────────────────────────────────────

let calCursor = new Date();
let calSelectedDate = calDateKey(new Date());
let calTasks = [];
let calEditingId = "";

function _sync() {
  window.calCursor = calCursor;
  window.calSelectedDate = calSelectedDate;
  window.calTasks = calTasks;
  window.calEditingId = calEditingId;
}
_sync();

// ── Load + render ──────────────────────────────────────────────────────

export async function loadCalendar() {
  try {
    const month = calMonthKey(calCursor);
    const d = await apiGet(`/api/day-tasks?month=${encodeURIComponent(month)}`);
    calTasks = d.tasks || [];
    _sync();
    renderCalendar();
    renderCalDay();
  } catch (e) {
    toast(`日历加载失败：${e.body?.detail || e.message || e}`);
  }
}

export function renderCalendar() {
  const title = document.getElementById("cal-month-title");
  const meta = document.getElementById("cal-month-meta");
  const grid = document.getElementById("cal-grid");
  if (!title || !grid) return;
  const year = calCursor.getFullYear();
  const month = calCursor.getMonth();
  title.textContent = `${year} 年 ${month + 1} 月`;
  if (meta) {
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    let workDays = 0;
    let restDays = 0;
    for (let d = 1; d <= daysInMonth; d++) {
      const type = getCalendarDayType(new Date(year, month, d));
      if (type === "restday") restDays++;
      else workDays++;
    }
    const reminders = calTasks.filter(
      (t) => t.task_date.startsWith(calMonthKey(calCursor)) && t.remind && !t.completed,
    ).length;
    meta.textContent = `工作日 ${workDays} 天 · 假日 ${restDays} 天 · 待提醒 ${reminders} 项`;
  }
  const first = new Date(year, month, 1);
  const startOffset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - startOffset);
  const todayKey = calDateKey(new Date());
  grid.innerHTML = "";
  for (let i = 0; i < 42; i++) {
    const day = new Date(start);
    day.setDate(start.getDate() + i);
    const key = calDateKey(day);
    const dayTasks = calTasks.filter((t) => t.task_date === key);
    const dayType = getCalendarDayType(day);
    const isManualWork = calendarHolidayRules.work.has(key);
    const tagText = dayType === "restday" ? "休" : "班";
    const tagClass = dayType === "restday" ? "rest" : isManualWork ? "shift" : "work";
    const cell = mk("button", "cal-day");
    cell.type = "button";
    cell.classList.add(dayType);
    if (isManualWork) cell.classList.add("shiftwork");
    if (day.getMonth() !== month) cell.classList.add("out");
    if (key === todayKey) cell.classList.add("today");
    if (key === calSelectedDate) cell.classList.add("selected");
    cell.onclick = () => calSelectDate(key);
    const dots = dayTasks
      .slice(0, 5)
      .map((t) => {
        const cls = t.completed ? "done" : t.remind ? "remind" : "";
        return `<span class="cal-dot ${cls}"></span>`;
      })
      .join("");
    cell.innerHTML = `
      <div class="cal-day-top">
        <div class="cal-day-num">${day.getDate()}</div>
        <span class="cal-day-tag ${tagClass}">${tagText}</span>
      </div>
      <div class="cal-day-summary">${dayTasks.length ? `${dayTasks.length} 项安排` : "无安排"}</div>
      <div class="cal-day-dots">${dots}${dayTasks.length > 5 ? `<span class="cal-more">+${dayTasks.length - 5}</span>` : ""}</div>
    `;
    grid.appendChild(cell);
  }
}

export function renderCalDay() {
  const dateTitle = document.getElementById("cal-selected-title");
  const sub = document.getElementById("cal-selected-sub");
  const list = document.getElementById("cal-task-list");
  if (!dateTitle || !sub || !list) return;
  const tasks = calTasks
    .filter((t) => t.task_date === calSelectedDate)
    .sort((a, b) => `${a.start_time}${a.created_at}`.localeCompare(`${b.start_time}${b.created_at}`));
  dateTitle.textContent = calDisplayDate(calSelectedDate);
  sub.textContent = `${tasks.length} 个时间段，${tasks.filter((t) => t.remind).length} 个需要提醒`;
  if (!tasks.length) {
    list.innerHTML = '<div class="cal-empty">这一天还没有安排。添加一个时间段，让它落地。</div>';
    return;
  }
  list.innerHTML = tasks
    .map(
      (task) => `
    <div class="cal-task ${task.completed ? "done" : ""}">
      <input class="cal-task-check" type="checkbox" ${task.completed ? "checked" : ""} ${actChange('calToggleDone', task.id, '@checked')}>
      <div class="cal-task-main">
        <div class="cal-task-time">${esc(task.start_time)} - ${esc(task.end_time)}</div>
        <div class="cal-task-title">${esc(task.title)}</div>
        ${task.notes ? `<div class="cal-task-notes">${esc(task.notes)}</div>` : ""}
        <div class="cal-task-badges">
          <span class="cal-badge ${task.remind ? "remind" : ""}">${task.remind ? "提醒" : "不提醒"}</span>
        </div>
      </div>
      <div class="cal-task-actions">
        <button ${act('calEditTask', task.id)} title="编辑">编</button>
        <button ${act('calDeleteTask', task.id)} title="删除">删</button>
      </div>
    </div>
  `,
    )
    .join("");
}

// ── Navigation ─────────────────────────────────────────────────────────

export function calSelectDate(dateKey) {
  calSelectedDate = dateKey;
  _sync();
  calClearEditor();
  renderCalendar();
  renderCalDay();
}

export function calShiftMonth(delta) {
  calCursor = new Date(calCursor.getFullYear(), calCursor.getMonth() + delta, 1);
  _sync();
  loadCalendar();
}

export function calGoToday() {
  const today = new Date();
  calCursor = new Date(today.getFullYear(), today.getMonth(), 1);
  calSelectedDate = calDateKey(today);
  _sync();
  loadCalendar();
}

// ── Editor ─────────────────────────────────────────────────────────────

export function calNewTask() {
  if (typeof showCalendarView === "function") {
    showCalendarView(document.getElementById("nav-chat"));
  }
  calClearEditor();
  document.getElementById("cal-title")?.focus();
}

export function calClearEditor() {
  calEditingId = "";
  _sync();
  const now = new Date();
  const hour = String(Math.max(8, now.getHours())).padStart(2, "0");
  const next = String(Math.min(23, Math.max(9, now.getHours() + 1))).padStart(2, "0");
  const startEl = document.getElementById("cal-start");
  const endEl = document.getElementById("cal-end");
  const titleEl = document.getElementById("cal-title");
  const notesEl = document.getElementById("cal-notes");
  const remindEl = document.getElementById("cal-remind");
  if (startEl) startEl.value = `${hour}:00`;
  if (endEl) endEl.value = `${next}:00`;
  if (titleEl) titleEl.value = "";
  if (notesEl) notesEl.value = "";
  if (remindEl) remindEl.checked = false;
}

export function calEditTask(id) {
  const task = calTasks.find((t) => t.id === id);
  if (!task) return;
  calEditingId = id;
  _sync();
  document.getElementById("cal-start").value = task.start_time || "09:00";
  document.getElementById("cal-end").value = task.end_time || "10:00";
  document.getElementById("cal-title").value = task.title || "";
  document.getElementById("cal-notes").value = task.notes || "";
  document.getElementById("cal-remind").checked = !!task.remind;
}

// ── Persistence ────────────────────────────────────────────────────────

export async function calSaveTask() {
  const title = document.getElementById("cal-title").value.trim();
  if (!title) {
    toast("请先写要做的事");
    return;
  }
  const payload = {
    task_date: calSelectedDate,
    start_time: document.getElementById("cal-start").value || "09:00",
    end_time: document.getElementById("cal-end").value || "10:00",
    title,
    notes: document.getElementById("cal-notes").value.trim(),
    remind: document.getElementById("cal-remind").checked,
  };
  try {
    if (calEditingId) {
      await apiPut(`/api/day-tasks/${encodeURIComponent(calEditingId)}`, payload);
    } else {
      await apiPost("/api/day-tasks", payload);
    }
  } catch (e) {
    toast(`保存失败：${e.body?.detail || e.message || e}`);
    return;
  }
  calClearEditor();
  await loadCalendar();
  toast("日程已保存");
}

export async function calToggleDone(id, completed) {
  try {
    await apiPut(`/api/day-tasks/${encodeURIComponent(id)}`, { completed });
  } catch (e) {
    toast("状态更新失败");
  }
  await loadCalendar();
}

export async function calDeleteTask(id) {
  const task = calTasks.find((t) => t.id === id);
  const confirmFn = confirmDialog;
  const msg = `日程「${task?.title || id}」将被删除。`;
  const ok = typeof confirmFn === "function"
    ? await confirmFn(msg, { title: "删除这项日程？", okText: "删除" })
    : window.confirm(msg);
  if (!ok) return;
  try {
    await apiDelete(`/api/day-tasks/${encodeURIComponent(id)}`);
  } catch (e) {
    toast("删除失败");
    return;
  }
  await loadCalendar();
  toast("已删除");
}
