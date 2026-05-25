/* Entry point for the paipai frontend bundle.
 *
 * Today this is intentionally minimal — it loads the few modules that
 * have already been pulled out of the legacy `index.html` (Phase 2.3 of
 * the migration) and re-exposes their public functions on `window` so
 * the still-inline JS in `index.html` can keep calling them by name.
 *
 * As more code moves into modules here, the inline `<script>` in
 * `index.html` shrinks. When it's empty, Phase 3 (React) can start. */

// Boot first (side-effect only) so its DOMContentLoaded handler registers
// before any other module's — preserving the original inline boot ordering.
import "./boot.js";
import { registerActions } from "./events.js";

import { renderResearchPlanCheckpoint } from "./research-plan-card.js";
import {
  INTENT_LABELS,
  ASSISTANT_AVATARS,
  AGENT_LABELS,
  WRITING_LABELS,
  FM_CAT_LABELS,
  QUOTES,
} from "./constants.js";
import { mk, esc, js, fmtTime, toast, autoResize } from "./utils.js";
import { apiGet, apiPost, apiPut, apiPatch, apiDelete, ApiError } from "./api.js";
import {
  normalizeMarkdownIndent,
  fixMarkdownBoldSpacing,
  _purify,
  _mdToHtml,
  renderMermaidBlocks,
  renderMarkdownInto,
} from "./markdown.js";
import { renderAssistantAvatar, renderAvatar } from "./avatar.js";
import { connectWS } from "./ws.js";
import {
  setDot, setSend, setGenerating, isGenerating,
  showThinking, removeThinking,
} from "./thinking.js";
import {
  currentProfile,
  loadProfile, renderProfile, pickAvatar, clearAvatar, saveProfile,
  loadLlmConfig, renderLlmConfig, fillDefaultModel,
  saveLlmConfig, testLlmConfig,
} from "./profile.js";
import {
  READING_AGENT_SESSION_PREFIX, READING_AGENT_SPLIT_KEY,
  isReadingAgentSession, renderSessionList, startChat,
  initSessions, createSession, switchSession, deleteSession,
  refreshPanel, newChat,
  getCurrentSid, getChatStarted, setChatStarted,
} from "./session-list.js";
import {
  loadLibraries, renderLibTabs, switchLib, loadLibDocs, removeLibDoc,
  showCreateLib, cancelCreateLib, confirmCreateLib, deleteLib, uploadToLibrary,
  loadPaperManagerDocs, renderPaperManagerTabs, switchPaperManagerLib,
  updatePaperManagerStats, renderPaperManager,
  paperVenueLabel, formatPaperTime,
} from "./library.js";
import {
  updateFound, clearFoundResults, renderFoundTags, setFoundTag, renderFound,
  updateDownloaded, downloadFoundPaper, openFoundPaperReader, openStoredPaper,
  saveFoundPaperToLibrary, addStoredPaperToLibrary, flushPendingLibrarySaves,
  updateLibrary, updateLibraryProgress,
  addRecentViewedPaper, renderRecentViewed,
  paperKey, titleKey, isPaperDownloaded, isPaperSaved, storedIndexForFoundPaper,
  normalizePaperCategories, computeFoundHotTags,
  num, fmtCitations, shortTitle,
} from "./papers.js";
import {
  send, sendText, onMsg, addMsg, renderMessageActions,
  streamAssistantText, renderEvaluation, evalClass,
  updateCompression, updateUsage, clearPanels,
  draftText, draftQuestion, draftLibraryQuestion, handleMessageLinkClick,
  getPendingImage, setPendingImage,
} from "./chat.js";
import {
  openCitationGraph, closeCitationGraph, reloadCitationGraph,
  navigateCitationGraph, goBackCitationGraph,
  updateCitationBackButton,
  openCitationNodeMenu, closeCitationMenu,
  expandCitationMenuNode, openCitationMenuNode, downloadCitationMenuNode,
  loadCitationGraph, renderCitationGraph, setCitationState,
} from "./citation-graph.js";
import {
  figPickPaper, figGeneratePrompt, figAppendPrompt, figClearPrompt,
  figGenerateImage, figSaveMock,
  figLoadHistory, figSaveHistory, figAddHistory, figRenderHistory,
  figRestoreHistory, figDownloadHistory, figDeleteHistory,
} from "./figure.js";
import {
  calendarHolidayRules, formatLocalDate, getCalendarDayType,
  calDateKey, calMonthKey, calDisplayDate,
  loadCalendar, renderCalendar, renderCalDay,
  calSelectDate, calShiftMonth, calGoToday,
  calNewTask, calClearEditor, calEditTask,
  calSaveTask, calToggleDone, calDeleteTask,
} from "./calendar.js";
import {
  openReader, ensurePdfJs, openReaderFallback, closeReader, openReaderInNewWindow,
  renderReaderPdf, renderReaderHighlights, renderReaderAnnotations, readerColorValue,
  handleReaderSelection, hideReaderPop,
  saveReaderSelection, editReaderAnnotation, deleteReaderAnnotation,
  translateReaderSelection, hideReaderTranslation, copyReaderTranslation, saveReaderTranslationAsNote,
  jumpToAnnotation, scrollReaderToPage, readerZoom, currentReaderPage,
  updateReaderPageIndicator, scheduleReaderProgressSave, saveReaderProgress,
} from "./reader.js";
import {
  writingPillGroupHtml, writingDividerHtml, writingRenderAcademicChat,
  renderSimpleWritingView, writingPick, writingVal, writingSettings,
  writingSettingsTag, writingBindInput, writingThread, writingAppendGreeting,
  writingAppendBubble, writingShowTyping, writingToggleKb, writingCallAnthropic,
  renderWritingUploads, removeWritingFile, uploadWritingFiles, writingSubmit,
  writingClear, writingReset, loadWritingHistory, saveWritingHistory,
  activeWritingSession, deriveWritingTitle, ensureWritingActiveSession,
  persistActiveWritingSession, formatWritingTime, renderWritingHistoryList,
  switchWritingSession, deleteWritingSession, newWritingSession,
  applyWritingSidebarCollapsed, toggleWritingHistorySidebar,
} from "./writing.js";
import {
  loadNotes, applyNotesSidebarState, toggleNotesSidebar,
  _tcd, _tcl, _noteDotColor, renderNotesList,
  _nvRefreshProps, _q, _nvRenderTagPills, _nvRmTag, _nvTagKey,
  _nvChange, _nvAutoSave, toggleNotePreview, updateSaveBtnText, _exitPreviewMode,
  selectNote, newNoteDraft, saveSelectedNote, deleteSelectedNote, deleteNoteById,
  embedSelectedNote, exportSelectedNotePdf, _nvToggleProps, toggleNotePin, _nvPin,
  _nvTab, _nvOutline, _nvScrollTo, _nvAI, _nvAIAsk,
  _nfmt, _nfmtLine, _nfmtBlock,
} from "./notes.js";
import {
  openLibraryDoc, openReadingAgent, readingAgentSessionKey,
  clearReadingAgentSession, closeReadingAgent, setReadingAgentFrame,
  setReadingAgentSplit, initReadingAgentSplit, openReadingAgentFile,
  loadReadingAgentHistory, setReadingAgentBusy, appendReadingAgentMessage,
  sendReadingAgentQuestion,
} from "./reading-agent.js";
import { openChunkViewer, renderChunks, closeChunkViewer } from "./chunk-viewer.js";
import { openWebPreview, closeWebPreview, openWebPreviewExternal } from "./web-preview.js";
import {
  openToolModal, closeToolModal,
  openCalculator, normalizeCalcExpression, formatCalcResult, evaluateCalcExpression,
  renderCalculator, calcTryEvaluate, calcPress, calcToggleParen, calcBackspace,
  calcClear, calcRun, calcDraftResult,
  openCalendarTool, shiftCalendarMonth, renderCalendarTool,
  openWordCounter, updateWordCounter,
  fmtMonitorNumber, tokenBar, openTokenMonitorTool, loadTokenMonitor, renderTokenMonitor,
  stopDateTimeTicker,
} from "./tools.js";
import {
  fmFmtSize, fmFmtDate, refreshStorageUsage,
  openFileManager, closeFileManager, fmSwitchTab, loadFileList, fmDelete,
} from "./file-manager.js";
import {
  playWoodfishSound, knockWoodfish, showWoodfishMerit, clampWoodfishPosition,
  setWoodfishVisible, toggleWoodfish, initWoodfish,
} from "./woodfish.js";
import {
  isNativeDesktopShell, enableNativeDesktopShell,
  desktopWindowMinimize, desktopWindowMaximize, desktopWindowClose,
  toggleDesktopSizeMenu, hideDesktopSizeMenu, desktopWindowResize,
  initNativeWindowDrag, initNativeWindowResizeGrip,
} from "./desktop-shell.js";
import {
  uploadFile, uploadImage, uploadImageFile, handleImagePaste,
} from "./upload.js";
import { applyTheme, setTheme, initTheme } from "./theme.js";
import {
  setResearchPanelCollapsed, toggleResearchPanel, initResearchPanelCollapse,
  setLeftNavCollapsed, toggleLeftNav, initLeftNavCollapse,
} from "./panels.js";
import {
  navClick, restoreActiveView, hideMainViews,
  showChatView, showLibraryManager, showFigureView, showWritingView,
  showSettingsView, showNotesView, showCalendarView,
} from "./nav.js";
import {
  confirmDialog, confirmDialogResolve, confirmDialogBackdrop,
} from "./confirm-dialog.js";
import { stopGeneration, compressCurrentSession } from "./session-ops.js";

// Action surface for the delegated event system (events.js). The HTML's
// data-act="name" attributes resolve to these functions. Cross-module calls
// use direct imports (not window), so this is no longer mirrored onto window.
const _api = {
  // utils
  mk, esc, js, fmtTime, toast, autoResize,
  // api client
  apiGet, apiPost, apiPut, apiPatch, apiDelete, ApiError,
  // markdown pipeline
  normalizeMarkdownIndent, fixMarkdownBoldSpacing,
  _purify, _mdToHtml, renderMermaidBlocks, renderMarkdownInto,
  // constants
  INTENT_LABELS, ASSISTANT_AVATARS, AGENT_LABELS,
  WRITING_LABELS, FM_CAT_LABELS, QUOTES,
  // avatars
  renderAssistantAvatar, renderAvatar,
  // websocket
  connectWS,
  // thinking bubble + ws/send/stop UI controls
  setDot, setSend, setGenerating, isGenerating,
  showThinking, removeThinking,
  // profile + LLM config
  currentProfile,
  loadProfile, renderProfile, pickAvatar, clearAvatar, saveProfile,
  loadLlmConfig, renderLlmConfig, fillDefaultModel,
  saveLlmConfig, testLlmConfig,
  // session list + chat lifecycle
  READING_AGENT_SESSION_PREFIX, READING_AGENT_SPLIT_KEY,
  isReadingAgentSession, renderSessionList, startChat,
  initSessions, createSession, switchSession, deleteSession,
  refreshPanel, newChat,
  getCurrentSid, getChatStarted, setChatStarted,
  // library + paper manager
  loadLibraries, renderLibTabs, switchLib, loadLibDocs, removeLibDoc,
  showCreateLib, cancelCreateLib, confirmCreateLib, deleteLib, uploadToLibrary,
  loadPaperManagerDocs, renderPaperManagerTabs, switchPaperManagerLib,
  updatePaperManagerStats, renderPaperManager,
  paperVenueLabel, formatPaperTime,
  // papers (right-sidebar found + downloaded lists, library save flow)
  updateFound, clearFoundResults, renderFoundTags, setFoundTag, renderFound,
  updateDownloaded, downloadFoundPaper, openFoundPaperReader, openStoredPaper,
  saveFoundPaperToLibrary, addStoredPaperToLibrary, flushPendingLibrarySaves,
  updateLibrary, updateLibraryProgress,
  addRecentViewedPaper, renderRecentViewed,
  paperKey, titleKey, isPaperDownloaded, isPaperSaved, storedIndexForFoundPaper,
  normalizePaperCategories, computeFoundHotTags,
  num, fmtCitations, shortTitle,
  // chat (composer + bubbles + WS dispatch)
  send, sendText, onMsg, addMsg, renderMessageActions,
  streamAssistantText, renderEvaluation, evalClass,
  updateCompression, updateUsage, clearPanels,
  draftText, draftQuestion, draftLibraryQuestion, handleMessageLinkClick,
  getPendingImage, setPendingImage,
  // citation graph overlay (ECharts force-directed)
  openCitationGraph, closeCitationGraph, reloadCitationGraph,
  navigateCitationGraph, goBackCitationGraph,
  updateCitationBackButton,
  openCitationNodeMenu, closeCitationMenu,
  expandCitationMenuNode, openCitationMenuNode, downloadCitationMenuNode,
  loadCitationGraph, renderCitationGraph, setCitationState,
  // figure generator panel
  figPickPaper, figGeneratePrompt, figAppendPrompt, figClearPrompt,
  figGenerateImage, figSaveMock,
  figLoadHistory, figSaveHistory, figAddHistory, figRenderHistory,
  figRestoreHistory, figDownloadHistory, figDeleteHistory,
  // calendar
  calendarHolidayRules, formatLocalDate, getCalendarDayType,
  calDateKey, calMonthKey, calDisplayDate,
  loadCalendar, renderCalendar, renderCalDay,
  calSelectDate, calShiftMonth, calGoToday,
  calNewTask, calClearEditor, calEditTask,
  calSaveTask, calToggleDone, calDeleteTask,
  // PDF reader + annotations + translation
  openReader, ensurePdfJs, openReaderFallback, closeReader, openReaderInNewWindow,
  renderReaderPdf, renderReaderHighlights, renderReaderAnnotations, readerColorValue,
  handleReaderSelection, hideReaderPop,
  saveReaderSelection, editReaderAnnotation, deleteReaderAnnotation,
  translateReaderSelection, hideReaderTranslation, copyReaderTranslation, saveReaderTranslationAsNote,
  jumpToAnnotation, scrollReaderToPage, readerZoom, currentReaderPage,
  updateReaderPageIndicator, scheduleReaderProgressSave, saveReaderProgress,
  // academic writing workspace
  writingPillGroupHtml, writingDividerHtml, writingRenderAcademicChat,
  renderSimpleWritingView, writingPick, writingVal, writingSettings,
  writingSettingsTag, writingBindInput, writingThread, writingAppendGreeting,
  writingAppendBubble, writingShowTyping, writingToggleKb, writingCallAnthropic,
  renderWritingUploads, removeWritingFile, uploadWritingFiles, writingSubmit,
  writingClear, writingReset, loadWritingHistory, saveWritingHistory,
  activeWritingSession, deriveWritingTitle, ensureWritingActiveSession,
  persistActiveWritingSession, formatWritingTime, renderWritingHistoryList,
  switchWritingSession, deleteWritingSession, newWritingSession,
  applyWritingSidebarCollapsed, toggleWritingHistorySidebar,
  // notes workspace (笔记 view)
  loadNotes, applyNotesSidebarState, toggleNotesSidebar,
  _tcd, _tcl, _noteDotColor, renderNotesList,
  _nvRefreshProps, _q, _nvRenderTagPills, _nvRmTag, _nvTagKey,
  _nvChange, _nvAutoSave, toggleNotePreview, updateSaveBtnText, _exitPreviewMode,
  selectNote, newNoteDraft, saveSelectedNote, deleteSelectedNote, deleteNoteById,
  embedSelectedNote, exportSelectedNotePdf, _nvToggleProps, toggleNotePin, _nvPin,
  _nvTab, _nvOutline, _nvScrollTo, _nvAI, _nvAIAsk,
  _nfmt, _nfmtLine, _nfmtBlock,
  // single-paper reading agent (文献阅读 workbench)
  openLibraryDoc, openReadingAgent, readingAgentSessionKey,
  clearReadingAgentSession, closeReadingAgent, setReadingAgentFrame,
  setReadingAgentSplit, initReadingAgentSplit, openReadingAgentFile,
  loadReadingAgentHistory, setReadingAgentBusy, appendReadingAgentMessage,
  sendReadingAgentQuestion,
  // chunk inspector + web preview overlays
  openChunkViewer, renderChunks, closeChunkViewer,
  openWebPreview, closeWebPreview, openWebPreviewExternal,
  // local tool modals (calculator / word counter / token monitor / calendar widget)
  openToolModal, closeToolModal,
  openCalculator, normalizeCalcExpression, formatCalcResult, evaluateCalcExpression,
  renderCalculator, calcTryEvaluate, calcPress, calcToggleParen, calcBackspace,
  calcClear, calcRun, calcDraftResult,
  openCalendarTool, shiftCalendarMonth, renderCalendarTool,
  openWordCounter, updateWordCounter,
  fmtMonitorNumber, tokenBar, openTokenMonitorTool, loadTokenMonitor, renderTokenMonitor,
  stopDateTimeTicker,
  // file manager + storage usage
  fmFmtSize, fmFmtDate, refreshStorageUsage,
  openFileManager, closeFileManager, fmSwitchTab, loadFileList, fmDelete,
  // 功德木鱼 widget
  playWoodfishSound, knockWoodfish, showWoodfishMerit, clampWoodfishPosition,
  setWoodfishVisible, toggleWoodfish, initWoodfish,
  // native desktop shell (pywebview window controls)
  isNativeDesktopShell, enableNativeDesktopShell,
  desktopWindowMinimize, desktopWindowMaximize, desktopWindowClose,
  toggleDesktopSizeMenu, hideDesktopSizeMenu, desktopWindowResize,
  initNativeWindowDrag, initNativeWindowResizeGrip,
  // composer file/image uploads
  uploadFile, uploadImage, uploadImageFile, handleImagePaste,
  // theme + collapsible chrome
  applyTheme, setTheme, initTheme,
  setResearchPanelCollapsed, toggleResearchPanel, initResearchPanelCollapse,
  setLeftNavCollapsed, toggleLeftNav, initLeftNavCollapse,
  // nav + main view switching
  navClick, restoreActiveView, hideMainViews,
  showChatView, showLibraryManager, showFigureView, showWritingView,
  showSettingsView, showNotesView, showCalendarView,
  // confirmation modal
  confirmDialog, confirmDialogResolve, confirmDialogBackdrop,
  // session controls (stop / compress)
  stopGeneration, compressCurrentSession,
  // feature modules
  renderResearchPlanCheckpoint,
};

// Register the delegated-action map (events.js). Inline on*= handlers are
// gone — the HTML uses data-act="..." resolved through this registry — so the
// old Object.assign(window, _api) bridge has been removed. Cross-module calls
// now go through imports; cross-module state self-syncs in its owning module.
registerActions(_api);
// doLogout lives in a <head> inline script (global), not a module — register
// a thunk so the converted logout button's data-act resolves.
registerActions({ doLogout: () => window.doLogout?.() });
