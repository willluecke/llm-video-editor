const $ = (selector) => document.querySelector(selector);

const editorClientId = (() => {
  const key = "llm-video-editor-client-id";
  const existing = sessionStorage.getItem(key);
  if (existing) return existing;
  const created = crypto.randomUUID();
  sessionStorage.setItem(key, created);
  return created;
})();
const editorPageId = crypto.randomUUID();
const nativeEditor = {
  enabled: new URLSearchParams(window.location.search).get("native") === "1"
    && Boolean(window.webkit?.messageHandlers?.nativeEditor),
  playing: false,
  timelineTimer: null,
  monitorTimer: null,
};
document.documentElement.classList.toggle("native-mode", nativeEditor.enabled);

function nativePost(message) {
  if (nativeEditor.enabled) window.webkit.messageHandlers.nativeEditor.postMessage(message);
}

window.llmNativePlaybackUpdate = (time, playing) => {
  if (!nativeEditor.enabled || !app.state) return;
  nativeEditor.playing = Boolean(playing);
  app.editTime = Math.max(0, Math.min(Number(time) || 0, sequenceDuration()));
  els.play.textContent = nativeEditor.playing ? "Ⅱ" : "▶";
  updatePlayhead();
};
window.llmNativeStatusUpdate = (message, isError) => {
  if (nativeEditor.enabled) toast(String(message || "Native preview update"), Boolean(isError));
};

const els = {
  projectList: $("#project-list"), projectCount: $("#project-count"), search: $("#project-search"),
  projectTitle: $("#project-title"), mediaSelect: $("#media-select"), preview: $("#preview"),
  videoFrame: $("#video-frame"), titleOverlay: $("#title-overlay"), emptyMonitor: $("#empty-monitor"), currentTime: $("#current-time"),
  totalTime: $("#total-time"), play: $("#play-button"), save: $("#save-button"), render: $("#render-button"),
  duplicateEpisode: $("#duplicate-episode-button"), templateContract: $("#template-contract"),
  templateProgress: $("#template-progress"), templateSlots: $("#template-slots"),
  saveState: $("#save-state"), refresh: $("#refresh-button"), timelineScroll: $("#timeline-scroll"),
  timelineContent: $("#timeline-content"), ruler: $("#ruler"), trackLanes: $("#track-lanes"),
  trackControls: $("#track-controls"), playhead: $("#playhead"), zoom: $("#zoom"), split: $("#split-button"),
  undo: $("#undo-button"), redo: $("#redo-button"), delete: $("#delete-button"),
  inspectorEmpty: $("#inspector-empty"), inspectorForm: $("#inspector-form"), clipLabel: $("#clip-label"),
  clipStart: $("#clip-start"), clipEnd: $("#clip-end"), clipVolume: $("#clip-volume"),
  volumeValue: $("#volume-value"), clipDuration: $("#clip-duration"), sequenceDuration: $("#sequence-duration"),
  formatValue: $("#format-value"), fpsValue: $("#fps-value"), sequenceName: $("#sequence-name"),
  jumpBack: $("#jump-back"), jumpForward: $("#jump-forward"), toast: $("#toast"),
  terminalStatusDot: $("#terminal-status-dot"), terminalStatus: $("#terminal-status"), operatorAccessBadge: $("#operator-access-badge"),
  assistantContext: $("#assistant-context"), terminalInterrupt: $("#terminal-interrupt"),
  terminalRestart: $("#terminal-restart"), reviewEdit: $("#review-edit-button"),
  terminalHost: $("#terminal-host"), terminalLoading: $("#terminal-loading"),
  codexTerminal: $("#codex-terminal"),
  assetList: $("#asset-list"), importMedia: $("#import-media-button"), importMediaInput: $("#import-media-input"),
  exportPreset: $("#export-preset"), renderProgress: $("#render-progress"),
  renderProgressBar: $("#render-progress-bar"), renderProgressLabel: $("#render-progress-label"),
  clipTrack: $("#clip-track"), clipSpeed: $("#clip-speed"), clipTimelineStart: $("#clip-timeline-start"),
  unlinkAudio: $("#unlink-audio"), transitionIn: $("#transition-in"), transitionInDuration: $("#transition-in-duration"),
  transitionOut: $("#transition-out"), transitionOutDuration: $("#transition-out-duration"),
  clipScale: $("#clip-scale"), clipX: $("#clip-x"), clipY: $("#clip-y"), clipRotation: $("#clip-rotation"), cropTop: $("#crop-top"),
  cropRight: $("#crop-right"), cropBottom: $("#crop-bottom"), cropLeft: $("#crop-left"),
  colorBrightness: $("#color-brightness"), colorContrast: $("#color-contrast"),
  colorSaturation: $("#color-saturation"), colorTemperature: $("#color-temperature"),
  audioFadeIn: $("#audio-fade-in"), audioFadeOut: $("#audio-fade-out"),
  addKeyframe: $("#add-keyframe"), keyframeCount: $("#keyframe-count"), textControls: $("#text-controls"),
  clipText: $("#clip-text"), fontSize: $("#font-size"), textColor: $("#text-color"),
  slotContext: $("#slot-context"), slotName: $("#slot-name"), slotPrompt: $("#slot-prompt"),
  replaceMedia: $("#replace-media"), replacementMedia: $("#replacement-media"), replaceMediaButton: $("#replace-media-button"),
  addTitle: $("#add-title"), addCaption: $("#add-caption"), addVideoTrack: $("#add-video-track"), addAudioTrack: $("#add-audio-track"),
};

const app = {
  projects: [], project: null, state: null, selectedId: null, selectedGap: null, editTime: 0,
  activeIndex: 0, dirty: false, history: [], future: [], trimSession: null,
};

const terminalState = {
  terminal: null, fitAddon: null, sessionId: null, cursor: 0,
  pollTimer: null, resizeTimer: null, starting: false, alive: false,
  inputBuffer: "", inputTimer: null, writeChain: Promise.resolve(), fullAccess: false,
};

const operatorState = { cursor: 0, revision: 0, projectId: null, pollTimer: null, contextTimer: null };
const PREVIEW_PREROLL_SECONDS = .45;
const previewState = {
  animationFrame: null,
  loadToken: 0,
  pendingBoundary: null,
  playbackPrimed: false,
  proxyAsset: null,
  proxyStale: false,
  usingProxy: false,
  queue: [...document.querySelectorAll(".preview-video")].filter(video => video !== els.preview),
};

function cloneTimeline() { return structuredClone({ segments: app.state?.segments || [], tracks: app.state?.tracks || [] }); }
function enabledSegments() { return (app.state?.segments || []).filter(segment => segment.enabled !== false); }
function durationOf(segment) {
  if (["title", "caption"].includes(segment.kind)) return Math.max(0, Number(segment.duration) || 0);
  return Math.max(0, segment.source_end - segment.source_start) / Math.max(.1, Number(segment.speed) || 1);
}
function sequenceDuration() {
  return enabledSegments().reduce((total, segment) => Math.max(total, Number(segment.timeline_start || 0) + durationOf(segment)), 0);
}
function fps() { return app.state?.fps || 30; }
function assetById(id) { return app.state?.assets?.find(asset => asset.id === id) || null; }
function trackById(id) { return app.state?.tracks?.find(track => track.id === id) || null; }
function episodeTemplate() { return app.state?.episode_template || null; }
function templateSlotById(id) { return episodeTemplate()?.slots?.find(slot => slot.id === id) || null; }
function segmentLocked(segment) { return Boolean(segment?.locked || trackById(segment?.track_id)?.locked); }

function currentTemplateStatus() {
  const template = episodeTemplate();
  if (!template) return null;
  const slots = (template.slots || []).map(slot => {
    const videos = enabledSegments().filter(segment => segment.kind === "video" && segment.slot_id === slot.id);
    const replaced = videos.filter(segment => !segment.template_source).length;
    const status = !videos.length ? "missing" : replaced === videos.length ? "replaced" : replaced ? "mixed" : "original";
    return { ...slot, videos, replaced, status };
  });
  return {
    slots,
    missing: slots.filter(slot => slot.required !== false && slot.status === "missing"),
    replaced: slots.filter(slot => ["replaced", "mixed"].includes(slot.status)),
  };
}

function timecode(seconds, includeFrames = true) {
  const safe = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const secs = Math.floor(safe % 60);
  const frames = Math.floor((safe % 1) * fps());
  const base = [hours, minutes, secs].map(value => String(value).padStart(2, "0")).join(":");
  return includeFrames ? `${base}:${String(frames).padStart(2, "0")}` : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function timelineDurationLabel(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  if (safe < 1) return `${safe.toFixed(2)}s`;
  if (safe < 10) return `${safe.toFixed(1)}s`;
  return timecode(safe, false);
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", isError);
  els.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("show"), 2600);
}

function markDirty() {
  app.dirty = true;
  previewState.proxyStale = true;
  els.saveState.textContent = "Unsaved changes";
  els.saveState.style.color = "#d5cfff";
  scheduleOperatorContext();
  scheduleNativeTimeline();
}

function syncNativeTimeline(revision = operatorState.revision) {
  if (!nativeEditor.enabled || !app.project || !app.state) return;
  clearTimeout(nativeEditor.timelineTimer);
  nativeEditor.timelineTimer = null;
  nativePost({ type: "timeline", project_id: app.project.id, revision, state: app.state, time: app.editTime });
}

function scheduleNativeTimeline() {
  if (!nativeEditor.enabled) return;
  clearTimeout(nativeEditor.timelineTimer);
  nativeEditor.timelineTimer = setTimeout(syncNativeTimeline, 100);
}

function syncNativeMonitorRect() {
  if (!nativeEditor.enabled || !els.videoFrame) return false;
  const rect = els.videoFrame.getBoundingClientRect();
  if (rect.width <= 1 || rect.height <= 1) return false;
  nativePost({ type: "monitor_rect", x: rect.x, y: rect.y, width: rect.width, height: rect.height });
  return true;
}

function ensureNativeMonitorRect(remainingAttempts = 20) {
  if (!nativeEditor.enabled) return;
  clearTimeout(nativeEditor.monitorTimer);
  requestAnimationFrame(() => {
    if (syncNativeMonitorRect() || remainingAttempts <= 0) return;
    nativeEditor.monitorTimer = setTimeout(() => ensureNativeMonitorRect(remainingAttempts - 1), 100);
  });
}

function markSaved() {
  app.dirty = false;
  els.saveState.textContent = "Saved locally";
  els.saveState.style.color = "";
}

function pushHistory() {
  app.history.push(cloneTimeline());
  if (app.history.length > 60) app.history.shift();
  app.future = [];
  updateHistoryButtons();
}

function updateHistoryButtons() {
  els.undo.disabled = app.history.length === 0;
  els.redo.disabled = app.future.length === 0;
}

function restoreTimeline(snapshot) {
  app.state.segments = structuredClone(snapshot.segments);
  app.state.tracks = structuredClone(snapshot.tracks);
  if (!app.state.segments.some(segment => segment.id === app.selectedId)) app.selectedId = null;
  app.editTime = Math.min(app.editTime, sequenceDuration());
  markDirty();
  renderTimeline();
  renderInspector();
}

function undo() {
  if (!app.history.length) return;
  app.future.push(cloneTimeline());
  restoreTimeline(app.history.pop());
  updateHistoryButtons();
}

function redo() {
  if (!app.future.length) return;
  app.history.push(cloneTimeline());
  restoreTimeline(app.future.pop());
  updateHistoryButtons();
}

function segmentOffsets() {
  return enabledSegments().map((segment, index) => {
    const start = Number(segment.timeline_start || 0);
    return { segment, index, start, end: start + durationOf(segment) };
  }).sort((left, right) => left.start - right.start);
}

function videoItemsOnTrack(trackId, exceptId = null) {
  return segmentOffsets().filter(item => (
    item.segment.kind === "video"
    && item.segment.track_id === trackId
    && item.segment.id !== exceptId
  ));
}

function nonOverlappingVideoStart(segment, requestedStart, trackId = segment.track_id) {
  const duration = durationOf(segment);
  const occupied = videoItemsOnTrack(trackId, segment.id);
  const ranges = [];
  let availableFrom = 0;
  occupied.forEach(item => {
    if (item.start - availableFrom >= duration - .001) ranges.push([availableFrom, item.start - duration]);
    availableFrom = Math.max(availableFrom, item.end);
  });
  ranges.push([availableFrom, Number.POSITIVE_INFINITY]);
  const desired = Math.max(0, Number(requestedStart) || 0);
  const candidates = ranges.map(([minimum, maximum]) => Math.max(minimum, Math.min(desired, maximum)));
  return Number(candidates.reduce((best, candidate) => (
    Math.abs(candidate - desired) < Math.abs(best - desired) ? candidate : best
  ), candidates[0]).toFixed(3));
}

function previousVideoEnd(segment, timelineStart = Number(segment.timeline_start || 0)) {
  return videoItemsOnTrack(segment.track_id, segment.id)
    .filter(item => item.end <= timelineStart + .001)
    .reduce((latest, item) => Math.max(latest, item.end), 0);
}

function nextVideoStart(segment, timelineStart = Number(segment.timeline_start || 0)) {
  return videoItemsOnTrack(segment.track_id, segment.id)
    .filter(item => item.start >= timelineStart - .001)
    .reduce((earliest, item) => Math.min(earliest, item.start), Number.POSITIVE_INFINITY);
}

function clampVideoSourceEnd(segment, requestedSourceEnd) {
  const assetDuration = assetById(segment.asset_id)?.duration || app.state.duration;
  let maximum = assetDuration;
  const nextStart = nextVideoStart(segment);
  if (Number.isFinite(nextStart)) {
    const availableTimeline = Math.max(.1, nextStart - Number(segment.timeline_start || 0));
    maximum = Math.min(maximum, segment.source_start + availableTimeline * Math.max(.1, segment.speed || 1));
  }
  return Number(Math.min(maximum, Math.max(segment.source_start + .1, requestedSourceEnd)).toFixed(3));
}

function mediaGapsForTrack(track) {
  const media = segmentOffsets().filter(item => {
    const segment = item.segment;
    if (track.kind === "video") return segment.kind === "video" && segment.track_id === track.id;
    return (segment.kind === "audio" && segment.track_id === track.id)
      || (segment.kind === "video" && segment.audio_linked && segment.audio_track_id === track.id);
  });
  if (!media.length) return [];
  const gaps = [];
  let occupiedUntil = 0;
  media.forEach(item => {
    if (item.start > occupiedUntil + .001) gaps.push({ start: occupiedUntil, end: item.start });
    occupiedUntil = Math.max(occupiedUntil, item.end);
  });
  return gaps;
}

function closeTimelineGap(gap) {
  const duration = Math.max(0, gap.end - gap.start);
  if (duration <= .001) return;
  pushHistory();
  app.state.segments.forEach(segment => {
    const start = Number(segment.timeline_start || 0);
    if (start >= gap.end - .001) segment.timeline_start = Number(Math.max(0, start - duration).toFixed(3));
  });
  app.selectedGap = null;
  app.editTime = gap.start;
  markDirty();
  renderTimeline();
  renderInspector();
  seekEditTime(gap.start);
  toast(`Closed ${duration.toFixed(2)}s timeline gap`);
}

function gapElement(gap, pxPerSecond, track) {
  const element = document.createElement("button");
  element.type = "button";
  const selected = app.selectedGap
    && Math.abs(app.selectedGap.start - gap.start) <= .001
    && Math.abs(app.selectedGap.end - gap.end) <= .001;
  element.className = `timeline-gap${selected ? " selected" : ""}`;
  element.style.left = `${gap.start * pxPerSecond}px`;
  element.style.width = `${Math.max(6, (gap.end - gap.start) * pxPerSecond)}px`;
  element.title = `Select ${(gap.end - gap.start).toFixed(2)}s gap · Delete or Backspace to close`;
  element.setAttribute("aria-label", element.title);
  element.addEventListener("click", event => {
    event.preventDefault();
    event.stopPropagation();
    if (track.locked) return;
    app.selectedGap = { start: gap.start, end: gap.end };
    app.selectedId = null;
    seekEditTime(gap.start);
    renderTimeline();
    renderInspector();
  });
  return element;
}

function trimCaptionsAtVideoEnd(video, previousTimelineEnd) {
  if (!video || video.kind !== "video") return;
  const videoStart = Number(video.timeline_start || 0);
  const videoEnd = videoStart + durationOf(video);
  if (videoEnd >= previousTimelineEnd - .001) return;
  app.state.segments.forEach(caption => {
    if (caption.kind !== "caption" || caption.enabled === false) return;
    const captionStart = Number(caption.timeline_start || 0);
    const captionEnd = captionStart + durationOf(caption);
    const stillIncluded = captionStart >= videoStart - .001 && captionStart < videoEnd - .001;
    const crossedTrimmedEnd = captionEnd > videoEnd + .001 && captionStart < previousTimelineEnd - .001;
    if (stillIncluded && crossedTrimmedEnd) caption.duration = Number(Math.max(.1, videoEnd - captionStart).toFixed(3));
  });
}

function atEditTime(time) {
  const videoTracks = (app.state?.tracks || []).filter(track => track.kind === "video").map(track => track.id);
  const offsets = segmentOffsets().filter(item => item.segment.kind === "video").sort((left, right) => (
    videoTracks.indexOf(right.segment.track_id) - videoTracks.indexOf(left.segment.track_id)
    || right.start - left.start
    || right.index - left.index
  ));
  if (!offsets.length) return null;
  return offsets.find(item => time >= item.start && time < item.end) || offsets.sort((a, b) => a.start - b.start)[offsets.length - 1];
}

function nextVideoItem(item) {
  return followingVideoItems(item, 1)[0] || null;
}

function followingVideoItems(item, limit = previewState.queue.length) {
  return segmentOffsets()
    .filter(candidate => candidate.segment.kind === "video" && candidate.start >= item.end - .01)
    .sort((left, right) => left.start - right.start || left.index - right.index)
    .slice(0, limit);
}

function setPreviewOrientation(asset) {
  const resolution = asset?.resolution || [16, 9];
  els.videoFrame.classList.toggle("portrait", resolution[1] > resolution[0]);
}

function findRenderedPreviewAsset() {
  const total = sequenceDuration();
  const exportedIds = new Set((app.project?.media || []).map(media => media.id));
  return (app.state?.assets || [])
    .filter(asset => exportedIds.has(asset.id) && /\.(mp4|m4v)$/i.test(asset.id))
    .filter(asset => Math.abs(Number(asset.duration || 0) - total) <= Math.max(.12, 2 / fps()))
    .sort((left, right) => /review|preview|render/i.test(right.id) - /review|preview|render/i.test(left.id))[0] || null;
}

function canUseRenderedPreview() {
  return Boolean(previewState.proxyAsset && !previewState.proxyStale);
}

function activateRenderedPreview(time, play = false) {
  const asset = previewState.proxyAsset;
  if (!asset) return false;
  previewState.pendingBoundary = null;
  stopQueuedPreviewPlayback();
  if (!previewState.usingProxy) previewState.queue.forEach(clearPreviewVideo);
  previewState.usingProxy = true;
  const video = els.preview;
  const token = String(++previewState.loadToken);
  const target = Math.max(0, Math.min(Number(time) || 0, sequenceDuration()));
  video.dataset.loadToken = token;
  video.dataset.assetId = asset.id;
  video.dataset.loadedAssetId = asset.id;
  delete video.dataset.segmentId;
  video.playbackRate = 1;
  video.muted = false;
  video.volume = 1;
  video.style.filter = "";
  video.style.transform = "";
  video.style.clipPath = "";
  video.style.opacity = "1";
  const position = () => {
    if (video.dataset.loadToken !== token || video.readyState < HTMLMediaElement.HAVE_METADATA) return;
    if (Math.abs(video.currentTime - target) > .002) video.currentTime = target;
    if (play) video.play().catch(() => {});
  };
  if (video.currentSrc !== new URL(asset.url, window.location.href).href) {
    video.src = asset.url;
    video.load();
  }
  if (video.readyState >= HTMLMediaElement.HAVE_METADATA) position();
  else video.addEventListener("loadedmetadata", position, { once: true });
  setPreviewOrientation(asset);
  els.titleOverlay.hidden = true;
  return true;
}

function configurePreviewVideo(video, item, sourceTime = item?.segment.source_start) {
  const asset = item ? assetById(item.segment.asset_id) : null;
  if (!asset) return null;
  if (video === els.preview) previewState.usingProxy = false;
  const token = String(++previewState.loadToken);
  const target = Math.max(0, Number(sourceTime) || 0);
  video.dataset.loadToken = token;
  video.dataset.assetId = asset.id;
  video.dataset.segmentId = item.segment.id;
  video.dataset.targetTime = String(target);
  video.preload = "auto";
  video.playbackRate = Math.max(.1, Number(item.segment.speed) || 1);

  const positionAtTarget = () => {
    if (video.dataset.loadToken !== token || video.readyState < HTMLMediaElement.HAVE_METADATA) return;
    const upperBound = Number.isFinite(video.duration) ? Math.max(0, video.duration - .001) : target;
    const boundedTarget = Math.min(target, upperBound);
    if (Math.abs(video.currentTime - boundedTarget) > .002) video.currentTime = boundedTarget;
  };

  if (video.dataset.loadedAssetId !== asset.id) {
    video.dataset.loadedAssetId = asset.id;
    video.src = asset.url;
    video.load();
  }
  if (video.readyState >= HTMLMediaElement.HAVE_METADATA) positionAtTarget();
  else video.addEventListener("loadedmetadata", positionAtTarget, { once: true });
  return asset;
}

function preparePreviewQueue(item) {
  const upcoming = item ? followingVideoItems(item) : [];
  previewState.queue.forEach((video, index) => {
    const candidate = upcoming[index];
    if (candidate) {
      const rate = Math.max(.1, Number(candidate.segment.speed) || 1);
      const lead = Math.min(PREVIEW_PREROLL_SECONDS, Math.max(0, Number(candidate.segment.source_start) || 0) / rate);
      video.dataset.prerollLead = String(lead);
      configurePreviewVideo(video, candidate, candidate.segment.source_start - lead * rate);
    }
    else video.dataset.segmentId = "";
  });
}

function stopQueuedPreviewPlayback() {
  previewState.queue.forEach(video => {
    if (!video.paused) video.pause();
    video.muted = false;
    video.volume = 1;
    delete video.dataset.prerollActive;
  });
}

function primePreviewPoolPlayback() {
  if (previewState.playbackPrimed) return;
  previewState.playbackPrimed = true;
  previewState.queue.forEach(video => {
    const target = Math.max(0, Number(video.dataset.targetTime) || 0);
    video.muted = false;
    video.volume = 0;
    video.play().then(() => {
      if (video === els.preview) return;
      video.pause();
      if (video.readyState >= HTMLMediaElement.HAVE_METADATA) video.currentTime = target;
      video.volume = 1;
    }).catch(() => { video.volume = 1; });
  });
}

function maybeStartPreviewPreroll(item, activeVideo) {
  const incoming = previewState.queue[0];
  const next = nextVideoItem(item);
  if (!incoming || !next || incoming.dataset.segmentId !== next.segment.id) return;
  if (incoming.dataset.prerollActive === next.segment.id) return;
  const activeRate = Math.max(.1, Number(item.segment.speed) || 1);
  const remaining = Math.max(0, (Number(item.segment.source_end) - activeVideo.currentTime) / activeRate);
  const lead = Math.max(0, Number(incoming.dataset.prerollLead) || 0);
  if (!lead || remaining > lead || incoming.seeking || incoming.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;

  const nextRate = Math.max(.1, Number(next.segment.speed) || 1);
  const alignedTime = Math.max(0, Number(next.segment.source_start) - remaining * nextRate);
  const startPreroll = () => {
    if (incoming !== previewState.queue[0] || incoming.dataset.segmentId !== next.segment.id) return;
    incoming.muted = false;
    incoming.volume = 0;
    incoming.dataset.prerollActive = next.segment.id;
    incoming.play().catch(() => { delete incoming.dataset.prerollActive; });
  };
  if (Math.abs(incoming.currentTime - alignedTime) > .04) {
    incoming.currentTime = alignedTime;
    incoming.addEventListener("seeked", startPreroll, { once: true });
  } else startPreroll();
}

function activatePreparedPreview(item) {
  const incoming = previewState.queue[0];
  if (!item || !incoming || incoming.dataset.segmentId !== item.segment.id) return false;
  if (incoming.seeking || incoming.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return false;
  const outgoing = els.preview;
  const asset = assetById(item.segment.asset_id);
  const wasPrerolled = incoming.dataset.prerollActive === item.segment.id && !incoming.paused;

  previewState.queue.shift();
  previewState.queue.push(outgoing);
  els.preview = incoming;
  previewState.pendingBoundary = null;
  outgoing.pause();
  incoming.playbackRate = Math.max(.1, Number(item.segment.speed) || 1);
  if (!wasPrerolled && Math.abs(incoming.currentTime - Number(item.segment.source_start || 0)) > .04) {
    incoming.currentTime = Number(item.segment.source_start || 0);
  }
  incoming.muted = false;
  delete incoming.dataset.prerollActive;
  app.activeIndex = item.index;
  app.editTime = item.start;
  setPreviewOrientation(asset);
  applyPreviewEffects(item.segment);
  outgoing.classList.remove("active");
  outgoing.setAttribute("aria-hidden", "true");
  incoming.classList.add("active");
  incoming.removeAttribute("aria-hidden");
  if (incoming.paused) incoming.play().catch(() => {});
  preparePreviewQueue(item);
  return true;
}

function clearPreviewVideo(video) {
  video.pause();
  video.muted = false;
  video.volume = 1;
  video.removeAttribute("src");
  video.removeAttribute("data-asset-id");
  video.removeAttribute("data-loaded-asset-id");
  video.removeAttribute("data-segment-id");
  video.removeAttribute("data-preroll-active");
  video.removeAttribute("data-preroll-lead");
  video.load();
}

function resetPreviewPool() {
  previewState.pendingBoundary = null;
  previewState.usingProxy = false;
  [els.preview, ...previewState.queue].forEach((video, index) => {
    clearPreviewVideo(video);
    video.classList.toggle("active", index === 0);
    if (index === 0) video.removeAttribute("aria-hidden");
    else video.setAttribute("aria-hidden", "true");
  });
}

function seekEditTime(time, play = false) {
  stopQueuedPreviewPlayback();
  previewState.pendingBoundary = null;
  const total = sequenceDuration();
  app.editTime = Math.max(0, Math.min(time, total));
  if (nativeEditor.enabled) {
    const continuePlaying = play || nativeEditor.playing;
    nativePost({ type: "transport", command: "seek", time: app.editTime, play: continuePlaying });
    updatePlayhead();
    return;
  }
  const continuePlaying = play || !els.preview.paused;
  if (canUseRenderedPreview() && activateRenderedPreview(app.editTime, continuePlaying)) {
    updatePlayhead();
    return;
  }
  const item = atEditTime(app.editTime === total ? Math.max(0, total - 0.001) : app.editTime);
  if (item) {
    app.activeIndex = item.index;
    const within = Math.max(0, app.editTime - item.start);
    const playbackRate = Math.max(.1, Number(item.segment.speed) || 1);
    const sourceTime = Math.min(item.segment.source_start + within * playbackRate, item.segment.source_end - 0.001);
    const asset = configurePreviewVideo(els.preview, item, sourceTime);
    setPreviewOrientation(asset);
    applyPreviewEffects(item.segment);
    preparePreviewQueue(item);
    if (continuePlaying) els.preview.play().catch(() => {});
  }
  updatePlayhead();
}

function updatePlayhead() {
  const px = app.editTime * Number(els.zoom.value);
  els.playhead.style.left = `${px}px`;
  els.currentTime.textContent = timecode(app.editTime);
  updatePreviewOverlays();
  updateAssistantContext();
}

function lerp(left, right, amount) { return left + (right - left) * amount; }

function effectiveProperties(segment) {
  const base = { transform: { ...segment.transform }, color: { ...segment.color }, volume: segment.volume };
  const keyframes = [...(segment.keyframes || [])].sort((a, b) => a.time - b.time);
  if (!keyframes.length) return base;
  const local = Math.max(0, app.editTime - Number(segment.timeline_start || 0));
  const afterIndex = keyframes.findIndex(keyframe => keyframe.time >= local);
  const after = afterIndex < 0 ? keyframes[keyframes.length - 1] : keyframes[afterIndex];
  const before = afterIndex <= 0 ? after : keyframes[afterIndex - 1];
  const span = Math.max(.001, after.time - before.time);
  const amount = Math.max(0, Math.min(1, (local - before.time) / span));
  for (const group of ["transform", "color"]) {
    for (const key of Object.keys(base[group])) {
      const left = Number(before[group]?.[key] ?? base[group][key]);
      const right = Number(after[group]?.[key] ?? left);
      base[group][key] = lerp(left, right, amount);
    }
  }
  base.volume = lerp(Number(before.volume ?? base.volume), Number(after.volume ?? before.volume ?? base.volume), amount);
  return base;
}

function applyPreviewEffects(segment) {
  if (!segment || segment.kind !== "video") {
    els.preview.style.filter = "";
    els.preview.style.transform = "";
    els.preview.style.clipPath = "";
    els.preview.style.opacity = "";
    els.preview.volume = 1;
    return;
  }
  const values = effectiveProperties(segment);
  const transform = values.transform;
  const color = values.color;
  const temperature = Number(color.temperature || 0);
  els.preview.style.filter = `brightness(${Math.max(0, 1 + Number(color.brightness || 0))}) contrast(${Number(color.contrast || 1)}) saturate(${Number(color.saturation || 1)}) sepia(${Math.abs(temperature) * .25}) hue-rotate(${temperature * -18}deg)`;
  els.preview.style.transform = `translate(${Number(transform.x || 0)}px, ${Number(transform.y || 0)}px) rotate(${Number(transform.rotation || 0)}deg) scale(${Number(transform.scale || 1)})`;
  els.preview.style.clipPath = `inset(${Number(transform.crop_top || 0)}% ${Number(transform.crop_right || 0)}% ${Number(transform.crop_bottom || 0)}% ${Number(transform.crop_left || 0)}%)`;
  const local = Math.max(0, app.editTime - Number(segment.timeline_start || 0));
  const remaining = Math.max(0, durationOf(segment) - local);
  const fadeIn = Number(segment.transition_in?.duration || 0);
  const fadeOut = Number(segment.transition_out?.duration || 0);
  const inOpacity = segment.transition_in?.type === "none" || !fadeIn ? 1 : Math.min(1, local / fadeIn);
  const outOpacity = segment.transition_out?.type === "none" || !fadeOut ? 1 : Math.min(1, remaining / fadeOut);
  els.preview.style.opacity = String(Math.min(inOpacity, outOpacity));
  els.preview.volume = Math.max(0, Math.min(1, Number(values.volume ?? 1)));
}

function updatePreviewOverlays() {
  if (nativeEditor.enabled) {
    els.titleOverlay.hidden = true;
    return;
  }
  if (previewState.usingProxy) {
    els.titleOverlay.hidden = true;
    return;
  }
  const textClip = segmentOffsets().filter(item => ["title", "caption"].includes(item.segment.kind) && app.editTime >= item.start && app.editTime < item.end).at(-1)?.segment;
  els.titleOverlay.hidden = !textClip;
  if (textClip) {
    els.titleOverlay.textContent = textClip.text || textClip.label;
    els.titleOverlay.className = `title-overlay ${textClip.kind}`;
    els.titleOverlay.style.color = textClip.text_color || "#ffffff";
    const requestedSize = Math.max(12, Number(textClip.font_size) || 54);
    const previewCap = textClip.kind === "caption" ? 48 : 46;
    els.titleOverlay.style.fontSize = `${Math.min(requestedSize, previewCap)}px`;
  }
  const active = atEditTime(app.editTime);
  if (active) applyPreviewEffects(active.segment);
}

function updateAssistantContext() {
  if (!app.project || !app.state) {
    els.assistantContext.textContent = "No edit selected";
    els.reviewEdit.disabled = true;
    return;
  }
  const selected = app.state.segments.find(item => item.id === app.selectedId);
  const slot = templateSlotById(selected?.slot_id);
  els.assistantContext.textContent = `${app.project.name} · ${enabledSegments().length} clips · ${timecode(app.editTime, false)}${slot ? ` · ${slot.label}` : ""}`;
  els.reviewEdit.disabled = !terminalState.alive;
  scheduleOperatorContext();
}

function scheduleOperatorContext() {
  if (operatorState.contextTimer) return;
  operatorState.contextTimer = setTimeout(() => {
    operatorState.contextTimer = null;
    syncOperatorContext();
  }, 250);
}

async function syncOperatorContext() {
  if (!app.project || !app.state) return;
  try {
    const data = await request("/api/operator/context", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project: { id: app.project.id, name: app.project.name },
        media: app.state.media,
        client_id: editorClientId,
        page_id: editorPageId,
        visible: document.visibilityState === "visible",
        focused: document.hasFocus(),
        dirty: app.dirty,
        playhead: app.editTime,
        selected_clip_id: app.selectedId,
        sequence_duration: sequenceDuration(),
        revision: operatorState.revision,
      }),
    });
    reconcileOperatorState(data);
    return data;
  } catch (_error) {
    // The editor remains usable if the Operator bridge is temporarily unavailable.
    return null;
  }
}

function waveformBars(id) {
  let seed = [...id].reduce((value, char) => value + char.charCodeAt(0), 1);
  let html = "";
  for (let index = 0; index < 120; index += 1) {
    seed = (seed * 9301 + 49297) % 233280;
    const height = 10 + (seed / 233280) * 80;
    html += `<i style="height:${height.toFixed(0)}%"></i>`;
  }
  return html;
}

function renderTemplateContract() {
  const status = currentTemplateStatus();
  els.templateContract.hidden = !status;
  els.duplicateEpisode.hidden = !status;
  if (!status) {
    els.templateSlots.innerHTML = "";
    return;
  }
  const complete = status.slots.length - status.missing.length;
  els.templateProgress.textContent = `${complete}/${status.slots.length} beats · ${status.replaced.length} updated`;
  els.templateSlots.innerHTML = "";
  status.slots.forEach((slot, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `template-slot ${slot.status}`;
    const stateLabel = slot.status === "original" ? "reference" : slot.status;
    button.innerHTML = `<span class="template-slot-index">${String(index + 1).padStart(2, "0")}</span><span class="template-slot-label">${escapeHtml(slot.label)}</span><span class="template-slot-state">${stateLabel}</span>`;
    button.title = slot.prompt || slot.label;
    button.addEventListener("click", () => {
      const segment = slot.videos[0] || enabledSegments().find(item => item.slot_id === slot.id);
      if (!segment) {
        seekEditTime(slot.start);
        toast(`${slot.label} is missing`, true);
        return;
      }
      app.selectedId = segment.id;
      seekEditTime(Number(segment.timeline_start || slot.start));
      renderTimeline();
      renderInspector();
    });
    els.templateSlots.appendChild(button);
  });
}

function clipElement(item, type, pxPerSecond, track) {
  const clip = document.createElement("div");
  const slot = templateSlotById(item.segment.slot_id);
  const locked = segmentLocked(item.segment);
  clip.className = `clip ${type}-clip ${item.segment.kind || "video"}${item.segment.id === app.selectedId ? " selected" : ""}${slot && ["video", "audio"].includes(item.segment.kind) ? " template-slot-clip" : ""}${item.segment.template_source ? " template-original" : ""}${locked ? " template-locked" : ""}`;
  clip.style.left = `${item.start * pxPerSecond}px`;
  clip.style.width = `${Math.max(5, durationOf(item.segment) * pxPerSecond)}px`;
  clip.dataset.id = item.segment.id;
  clip.dataset.trackId = track.id;
  clip.dataset.representation = type;
  if (slot) {
    clip.dataset.slotId = slot.id;
    clip.dataset.slotLabel = slot.label;
  }
  const prefix = item.segment.kind === "title" ? "T · " : item.segment.kind === "caption" ? "CC · " : type === "audio" ? "♪ " : "";
  const label = `${prefix}${item.segment.label}`;
  clip.innerHTML = `<span class="trim-handle left" data-edge="left"></span><span class="clip-label">${escapeHtml(label)}</span><span class="clip-time">${timelineDurationLabel(durationOf(item.segment))}</span>${type === "audio" ? `<span class="waveform">${waveformBars(item.segment.id)}</span>` : ""}<span class="trim-handle right" data-edge="right"></span>`;
  clip.draggable = !locked;
  clip.addEventListener("dragstart", event => {
    if (locked) { event.preventDefault(); return; }
    app.selectedGap = null;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-llm-editor-clip", JSON.stringify({ id: item.segment.id, representation: type }));
  });
  clip.addEventListener("pointerdown", event => {
    const edge = event.target.dataset.edge;
    if (edge) beginTrim(event, item.segment.id, edge);
  });
  clip.addEventListener("click", event => {
    if (event.target.dataset.edge) return;
    const selectionChanged = app.selectedId !== item.segment.id || Boolean(app.selectedGap);
    app.selectedGap = null;
    app.selectedId = item.segment.id;
    seekEditTime(item.start);
    if (selectionChanged) renderTimeline();
    renderInspector();
  });
  clip.addEventListener("dblclick", event => {
    if (item.segment.kind !== "video" || item.segment.id !== app.selectedId) return;
    const bounds = clip.getBoundingClientRect();
    const pointerX = Math.max(0, Math.min(bounds.width, event.clientX - bounds.left));
    event.preventDefault();
    event.stopPropagation();
    seekEditTime(pointerX <= bounds.width / 2 ? item.start : item.end);
  });
  return clip;
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

function renderRuler(width, duration, pxPerSecond) {
  els.ruler.innerHTML = "";
  const minor = pxPerSecond >= 100 ? 1 : pxPerSecond >= 45 ? 2 : 5;
  const major = minor * 5;
  for (let second = 0; second <= duration + major; second += minor) {
    const tick = document.createElement("span");
    tick.className = `tick${second % major === 0 ? " major" : ""}`;
    tick.style.left = `${second * pxPerSecond}px`;
    if (second % major === 0) tick.innerHTML = `<label>${timecode(second, false)}</label>`;
    els.ruler.appendChild(tick);
  }
  els.ruler.style.width = `${width}px`;
}

function renderTimeline() {
  const pxPerSecond = Number(els.zoom.value);
  const total = Math.max(sequenceDuration(), 1);
  const available = Math.max(els.timelineScroll.clientWidth || 700, 700);
  const width = Math.max(available, total * pxPerSecond + 120);
  els.timelineContent.style.width = `${width}px`;
  els.trackLanes.innerHTML = "";
  els.trackControls.innerHTML = '<div class="ruler-spacer"></div>';
  const offsets = segmentOffsets();
  if (app.selectedGap) {
    const gapStillExists = (app.state?.tracks || []).some(track => mediaGapsForTrack(track).some(gap => (
      Math.abs(app.selectedGap.start - gap.start) <= .001 && Math.abs(app.selectedGap.end - gap.end) <= .001
    )));
    if (!gapStillExists) app.selectedGap = null;
  }
  (app.state?.tracks || []).forEach(track => {
    const control = document.createElement("div");
    control.className = `track-control ${track.kind}-control${track.template_reserved ? " template-reserved-track" : ""}`;
    const lockTitle = track.template_locked ? "Protected template layer" : track.template_reserved ? "Reserved reusable layer" : "Lock track";
    control.innerHTML = `<button>${escapeHtml(track.name)}</button><span class="track-mute${track.muted ? " active" : ""}" title="Mute track">M</span><span class="track-lock${track.locked ? " active" : ""}" title="${lockTitle}">${track.locked ? "●" : "○"}</span><span class="track-delete" title="Delete track">×</span>`;
    control.querySelector(".track-mute").addEventListener("click", () => { track.muted = !track.muted; markDirty(); renderTimeline(); });
    control.querySelector(".track-lock").addEventListener("click", () => {
      if (track.template_locked) { toast("This reusable template layer is protected"); return; }
      track.locked = !track.locked;
      markDirty();
      renderTimeline();
    });
    control.querySelector(".track-delete").addEventListener("click", () => deleteTrack(track.id));
    els.trackControls.appendChild(control);

    const lane = document.createElement("div");
    lane.className = `track ${track.kind}-track${track.muted ? " muted-track" : ""}`;
    lane.style.width = `${width}px`;
    lane.dataset.trackId = track.id;
    lane.addEventListener("dragover", event => { event.preventDefault(); event.dataTransfer.dropEffect = "move"; });
    lane.addEventListener("drop", event => dropOnTrack(event, track));
    mediaGapsForTrack(track).forEach(gap => lane.appendChild(gapElement(gap, pxPerSecond, track)));
    offsets.forEach(item => {
      const segment = item.segment;
      if (track.kind === "video" && segment.kind !== "audio" && segment.track_id === track.id) {
        lane.appendChild(clipElement(item, "video", pxPerSecond, track));
      }
      if (track.kind === "audio" && ((segment.kind === "audio" && segment.track_id === track.id) || (segment.kind === "video" && segment.audio_linked && segment.audio_track_id === track.id))) {
        lane.appendChild(clipElement(item, "audio", pxPerSecond, track));
      }
    });
    els.trackLanes.appendChild(lane);
  });
  const lanesHeight = Math.max(72, (app.state?.tracks || []).reduce((height, track) => height + (track.kind === "video" ? 140 : 72), 0));
  els.trackLanes.style.height = `${lanesHeight}px`;
  els.playhead.style.height = `${25 + lanesHeight}px`;
  renderRuler(width, total, pxPerSecond);
  els.totalTime.textContent = timecode(sequenceDuration());
  renderTemplateContract();
  updatePlayhead();
}

function deleteTrack(trackId) {
  const track = trackById(trackId);
  if (!track) return;
  if (track.template_locked) { toast("This reusable template layer is protected", true); return; }
  const fallback = app.state.tracks.find(item => item.kind === track.kind && item.id !== track.id);
  if (!fallback) {
    toast(`Keep at least one ${track.kind} track`, true);
    return;
  }
  pushHistory();
  for (const segment of app.state.segments) {
    if (segment.track_id === track.id) segment.track_id = fallback.id;
    if (segment.audio_track_id === track.id) segment.audio_track_id = fallback.id;
  }
  app.state.tracks = app.state.tracks.filter(item => item.id !== track.id);
  markDirty();
  renderTimeline();
  renderInspector();
}

function dropOnTrack(event, track) {
  event.preventDefault();
  if (track.locked) return;
  const rect = event.currentTarget.getBoundingClientRect();
  const rawStart = Math.max(0, (event.clientX - rect.left) / Number(els.zoom.value));
  const timelineStart = Number((Math.round(rawStart * fps()) / fps()).toFixed(3));
  const assetId = event.dataTransfer.getData("application/x-llm-editor-asset");
  if (assetId) {
    addAssetClip(assetId, track.id, timelineStart);
    return;
  }
  let drag;
  try { drag = JSON.parse(event.dataTransfer.getData("application/x-llm-editor-clip")); } catch (_error) { return; }
  const segment = app.state?.segments.find(item => item.id === drag.id);
  if (!segment) return;
  if (segmentLocked(segment)) { toast("This reusable template layer is protected", true); return; }
  if ((drag.representation === "audio") !== (track.kind === "audio")) return;
  pushHistory();
  const videoTrackId = drag.representation === "video" ? track.id : segment.track_id;
  const legalStart = segment.kind === "video" ? nonOverlappingVideoStart(segment, timelineStart, videoTrackId) : timelineStart;
  segment.timeline_start = legalStart;
  if (drag.representation === "audio" && segment.kind === "video") segment.audio_track_id = track.id;
  else segment.track_id = track.id;
  app.selectedId = segment.id;
  markDirty();
  renderTimeline();
  renderInspector();
  if (Math.abs(legalStart - timelineStart) > .001) toast("Video snapped beside the nearest clip");
}

function beginTrim(event, id, edge) {
  event.preventDefault();
  event.stopPropagation();
  app.selectedGap = null;
  app.selectedId = id;
  const segment = app.state.segments.find(item => item.id === id);
  if (!segment) return;
  if (segmentLocked(segment)) { toast("This reusable template layer is protected", true); return; }
  pushHistory();
  const session = { startX: event.clientX, originalStart: segment.source_start, originalEnd: segment.source_end, originalTimeline: Number(segment.timeline_start || 0), originalDuration: durationOf(segment), edge, changed: false };
  const move = moveEvent => {
    const delta = (moveEvent.clientX - session.startX) / Number(els.zoom.value);
    if (["title", "caption"].includes(segment.kind)) {
      if (edge === "left") {
        const adjusted = Math.max(-session.originalTimeline, Math.min(delta, session.originalDuration - .1));
        segment.timeline_start = Number((session.originalTimeline + adjusted).toFixed(3));
        segment.duration = Number((session.originalDuration - adjusted).toFixed(3));
      } else segment.duration = Number(Math.max(.1, session.originalDuration + delta).toFixed(3));
    } else {
      const assetDuration = assetById(segment.asset_id)?.duration || app.state.duration;
      const sourceDelta = delta * Math.max(.1, segment.speed || 1);
      if (edge === "left") {
        const minimumTimeline = previousVideoEnd(segment, session.originalTimeline);
        const minimumSource = session.originalStart + (minimumTimeline - session.originalTimeline) * Math.max(.1, segment.speed || 1);
        const nextStart = Math.max(0, minimumSource, Math.min(session.originalStart + sourceDelta, segment.source_end - 0.1));
        const appliedTimelineDelta = (nextStart - session.originalStart) / Math.max(.1, segment.speed || 1);
        segment.source_start = Number(nextStart.toFixed(3));
        segment.timeline_start = Number((session.originalTimeline + appliedTimelineDelta).toFixed(3));
      } else segment.source_end = clampVideoSourceEnd(segment, Math.min(assetDuration, session.originalEnd + sourceDelta));
    }
    session.changed = true;
    markDirty();
    renderTimeline();
    renderInspector();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    if (!session.changed) return;
    if (segment.kind === "video" && edge === "right") {
      trimCaptionsAtVideoEnd(segment, session.originalTimeline + session.originalDuration);
    }
    const boundary = edge === "left"
      ? Number(segment.timeline_start || 0)
      : Number(segment.timeline_start || 0) + durationOf(segment);
    markDirty();
    renderTimeline();
    renderInspector();
    seekEditTime(boundary);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  renderTimeline();
  renderInspector();
}

function renderInspector() {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  els.inspectorEmpty.hidden = Boolean(segment);
  els.inspectorForm.hidden = !segment;
  if (!segment) return;
  const isText = ["title", "caption"].includes(segment.kind);
  const locked = segmentLocked(segment);
  const slot = templateSlotById(segment.slot_id);
  const asset = assetById(segment.asset_id);
  els.clipLabel.value = segment.label;
  els.clipStart.value = segment.source_start.toFixed(2);
  els.clipEnd.value = segment.source_end.toFixed(2);
  els.clipStart.disabled = isText;
  els.clipEnd.disabled = isText;
  els.clipStart.max = asset?.duration || app.state.duration;
  els.clipEnd.max = asset?.duration || app.state.duration;
  els.clipVolume.value = segment.volume;
  els.volumeValue.textContent = `${Math.round(segment.volume * 100)}%`;
  els.clipDuration.textContent = timecode(durationOf(segment));
  els.sequenceDuration.textContent = timecode(sequenceDuration());
  els.formatValue.textContent = `${app.state.resolution[0]} × ${app.state.resolution[1]}`;
  els.fpsValue.textContent = `${app.state.fps.toFixed(2)} fps`;
  const trackKind = segment.kind === "audio" ? "audio" : "video";
  els.clipTrack.innerHTML = app.state.tracks.filter(track => track.kind === trackKind).map(track => `<option value="${escapeHtml(track.id)}"${track.id === segment.track_id ? " selected" : ""}>${escapeHtml(track.name)}</option>`).join("");
  els.clipSpeed.value = Number(segment.speed || 1).toFixed(2);
  els.clipSpeed.disabled = isText;
  els.clipTimelineStart.value = Number(segment.timeline_start || 0).toFixed(2);
  els.unlinkAudio.disabled = segment.kind !== "video" || !segment.audio_linked;
  els.transitionIn.value = segment.transition_in?.type || "none";
  els.transitionInDuration.value = Number(segment.transition_in?.duration || 0).toFixed(2);
  els.transitionOut.value = segment.transition_out?.type || "none";
  els.transitionOutDuration.value = Number(segment.transition_out?.duration || 0).toFixed(2);
  els.clipScale.value = Number(segment.transform?.scale || 1).toFixed(2);
  els.clipX.value = Number(segment.transform?.x || 0).toFixed(0);
  els.clipY.value = Number(segment.transform?.y || 0).toFixed(0);
  els.clipRotation.value = Number(segment.transform?.rotation || 0).toFixed(0);
  els.cropTop.value = Number(segment.transform?.crop_top || 0).toFixed(1);
  els.cropRight.value = Number(segment.transform?.crop_right || 0).toFixed(1);
  els.cropBottom.value = Number(segment.transform?.crop_bottom || 0).toFixed(1);
  els.cropLeft.value = Number(segment.transform?.crop_left || 0).toFixed(1);
  els.colorBrightness.value = Number(segment.color?.brightness || 0).toFixed(2);
  els.colorContrast.value = Number(segment.color?.contrast || 1).toFixed(2);
  els.colorSaturation.value = Number(segment.color?.saturation || 1).toFixed(2);
  els.colorTemperature.value = Number(segment.color?.temperature || 0).toFixed(2);
  els.audioFadeIn.value = Number(segment.audio_fade_in || 0).toFixed(2);
  els.audioFadeOut.value = Number(segment.audio_fade_out || 0).toFixed(2);
  els.keyframeCount.textContent = `${segment.keyframes?.length || 0} keyframe${segment.keyframes?.length === 1 ? "" : "s"}`;
  els.textControls.hidden = !isText;
  els.slotContext.hidden = !slot;
  if (slot) {
    els.slotName.textContent = `${slot.label} · ${slot.start.toFixed(1)}–${slot.end.toFixed(1)}s`;
    els.slotPrompt.textContent = slot.prompt;
  }
  const replacementAssets = (app.state.assets || []).filter(candidate => (
    candidate.id !== segment.asset_id
    && (segment.kind === "audio" ? candidate.has_audio : candidate.resolution?.[0] > 0)
  ));
  els.replaceMedia.hidden = !["video", "audio"].includes(segment.kind) || locked;
  els.replacementMedia.innerHTML = replacementAssets.map(candidate => `<option value="${escapeHtml(candidate.id)}">${escapeHtml(candidate.name)}</option>`).join("");
  els.replaceMediaButton.disabled = !replacementAssets.length;
  els.delete.disabled = locked;
  [
    els.clipLabel, els.clipStart, els.clipEnd, els.clipVolume, els.clipTrack, els.clipSpeed, els.clipTimelineStart,
    els.unlinkAudio, els.transitionIn, els.transitionInDuration, els.transitionOut, els.transitionOutDuration,
    els.clipScale, els.clipX, els.clipY, els.clipRotation, els.cropTop, els.cropRight, els.cropBottom, els.cropLeft,
    els.colorBrightness, els.colorContrast, els.colorSaturation, els.colorTemperature, els.audioFadeIn, els.audioFadeOut,
    els.addKeyframe, els.clipText, els.fontSize, els.textColor,
  ].forEach(control => { control.disabled = locked || (control === els.unlinkAudio && (segment.kind !== "video" || !segment.audio_linked)); });
  if (!locked) {
    els.clipStart.disabled = isText;
    els.clipEnd.disabled = isText;
    els.clipSpeed.disabled = isText;
  }
  if (isText) {
    els.clipText.value = segment.text || "";
    els.fontSize.value = segment.font_size || 54;
    els.textColor.value = /^#[0-9a-f]{6}$/i.test(segment.text_color || "") ? segment.text_color : "#ffffff";
  }
}

function renderProjectList() {
  const query = els.search.value.trim().toLowerCase();
  const projects = app.projects.filter(project => project.name.toLowerCase().includes(query));
  els.projectList.innerHTML = "";
  projects.forEach(project => {
    const row = document.createElement("div");
    row.className = `project-item${app.project?.id === project.id ? " active" : ""}`;
    const savedLabel = project.has_project_file ? " · saved project" : "";
    row.innerHTML = `<button class="project-open" type="button"><span class="project-thumb">▶</span><span class="project-meta"><strong>${escapeHtml(project.name)}</strong><small>${project.media.length} version${project.media.length === 1 ? "" : "s"}${savedLabel}</small></span></button><button class="project-delete" type="button" title="Move project media to Trash" aria-label="Delete ${escapeHtml(project.name)}">⌫</button>`;
    row.querySelector(".project-open").addEventListener("click", () => selectProject(project.id));
    row.querySelector(".project-delete").addEventListener("click", event => {
      event.stopPropagation();
      trashProject(project);
    });
    els.projectList.appendChild(row);
  });
  els.projectCount.textContent = `${app.projects.length} project${app.projects.length === 1 ? "" : "s"}`;
}

function renderAssetList() {
  els.assetList.innerHTML = "";
  if (!app.state?.assets?.length) {
    els.assetList.innerHTML = '<span class="asset-empty">Add media to create a project.</span>';
    els.importMedia.disabled = false;
    return;
  }
  els.importMedia.disabled = false;
  app.state.assets.forEach(asset => {
    const row = document.createElement("div");
    row.className = "asset-item";
    row.draggable = true;
    row.innerHTML = `<span class="asset-icon">▶</span><span class="asset-copy"><strong>${escapeHtml(asset.name)}</strong><small>${timecode(asset.duration, false)} · ${asset.resolution[0]}×${asset.resolution[1]}</small></span><button type="button" title="Add at playhead">＋</button>`;
    row.addEventListener("dragstart", event => {
      event.dataTransfer.effectAllowed = "copy";
      event.dataTransfer.setData("application/x-llm-editor-asset", asset.id);
    });
    row.querySelector("button").addEventListener("click", () => addAssetClip(asset.id));
    els.assetList.appendChild(row);
  });
}

function makeMediaSegment(asset, track, kind = "video", start = app.editTime) {
  const hasLinkedAudio = kind === "video" && asset.has_audio;
  const slot = episodeTemplate()?.slots?.find(candidate => start >= candidate.start - .001 && start < candidate.end - .001);
  return {
    id: `clip-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`,
    label: asset.name,
    kind,
    asset_id: asset.id,
    track_id: track.id,
    audio_track_id: hasLinkedAudio ? app.state.tracks.find(item => item.kind === "audio")?.id || null : null,
    audio_linked: Boolean(hasLinkedAudio && app.state.tracks.some(item => item.kind === "audio")),
    timeline_start: Number(start.toFixed(3)),
    source_start: 0,
    source_end: asset.duration,
    speed: 1,
    volume: 1,
    audio_fade_in: 0,
    audio_fade_out: 0,
    transition_in: { type: "none", duration: 0 },
    transition_out: { type: "none", duration: 0 },
    transform: { scale: 1, x: 0, y: 0, rotation: 0, crop_top: 0, crop_right: 0, crop_bottom: 0, crop_left: 0 },
    color: { brightness: 0, contrast: 1, saturation: 1, temperature: 0 },
    keyframes: [],
    enabled: true,
    slot_id: slot?.id || null,
    template_role: slot ? "daily" : null,
    template_source: false,
    locked: false,
  };
}

function fitReplacementToSegment(segment, asset) {
  const targetDuration = Math.max(.04, durationOf(segment));
  const previousSpeed = Math.max(.1, Number(segment.speed) || 1);
  const available = Math.max(.04, Number(asset.duration) || targetDuration);
  const sourceDuration = Math.min(available, targetDuration * previousSpeed);
  segment.asset_id = asset.id;
  segment.label = asset.name;
  segment.source_start = 0;
  segment.source_end = Number(sourceDuration.toFixed(3));
  segment.speed = Number(Math.max(.1, Math.min(100, sourceDuration / targetDuration)).toFixed(3));
  segment.template_source = false;
}

function replaceSelectedMedia() {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  const asset = assetById(els.replacementMedia.value);
  if (!segment || !asset || !["video", "audio"].includes(segment.kind)) return;
  if (segmentLocked(segment)) { toast("This reusable template layer is protected", true); return; }
  pushHistory();
  const oldAssetId = segment.asset_id;
  const start = Number(segment.timeline_start || 0);
  const slotId = segment.slot_id;
  fitReplacementToSegment(segment, asset);
  app.state.segments.filter(item => (
    item !== segment
    && item.kind === "audio"
    && item.asset_id === oldAssetId
    && item.slot_id === slotId
    && Math.abs(Number(item.timeline_start || 0) - start) <= .01
  )).forEach(companion => {
    if (asset.has_audio) {
      fitReplacementToSegment(companion, asset);
      companion.enabled = true;
    } else companion.enabled = false;
  });
  markDirty();
  renderTimeline();
  renderInspector();
  seekEditTime(start);
  toast(`${templateSlotById(slotId)?.label || "Clip"} replaced · timing preserved`);
}

function addAssetClip(assetId, trackId = null, start = app.editTime) {
  const asset = assetById(assetId);
  if (!asset) return;
  const track = trackById(trackId) || app.state.tracks.find(item => item.kind === "video");
  if (!track) return;
  pushHistory();
  const segment = makeMediaSegment(asset, track, track.kind === "audio" ? "audio" : "video", start);
  if (segment.kind === "video") segment.timeline_start = nonOverlappingVideoStart(segment, start, track.id);
  app.state.segments.push(segment);
  app.selectedId = segment.id;
  markDirty();
  renderTimeline();
  renderInspector();
  seekEditTime(segment.timeline_start);
}

async function importMediaFiles(files) {
  if (!files.length) return;
  const pending = [...files];
  const originalCount = pending.length;
  els.importMedia.disabled = true;
  try {
    if (!app.project) {
      const first = pending.shift();
      const defaultName = first.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ");
      const projectName = window.prompt("Name this video project", defaultName);
      if (projectName === null) return;
      toast(`Creating ${projectName || defaultName}…`);
      const response = await fetch(`/api/projects/new?name=${encodeURIComponent(projectName || defaultName)}&filename=${encodeURIComponent(first.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-File-Size": String(first.size) }, body: first,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not create the project");
      await loadProjects(false);
      await selectProject(data.project.id, data.asset.id);
    }
    for (const file of pending) {
      toast(`Importing ${file.name}…`);
      const response = await fetch(`/api/projects/${encodeURIComponent(app.project.id)}/media?filename=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-File-Size": String(file.size) }, body: file,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Could not import ${file.name}`);
      if (!app.state.assets.some(asset => asset.id === data.asset.id)) app.state.assets.push(data.asset);
    }
    markDirty();
    renderAssetList();
    toast(`${originalCount} media file${originalCount === 1 ? "" : "s"} added`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.importMedia.disabled = false;
    els.importMediaInput.value = "";
  }
}

async function filesFromEntry(entry) {
  if (entry.isFile) {
    return new Promise((resolve, reject) => entry.file(file => resolve([file]), reject));
  }
  if (!entry.isDirectory) return [];
  const reader = entry.createReader();
  const children = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    children.push(...batch);
  }
  const nested = await Promise.all(children.map(filesFromEntry));
  return nested.flat();
}

async function filesFromDrop(drop) {
  if (!drop.entries.length) return drop.files.filter(file => file.size > 0);
  return (await Promise.all(drop.entries.map(filesFromEntry))).flat().filter(file => file.size > 0);
}

function terminalEscapePath(path) {
  return path.replace(/([\\\s"'`$!&;()\[\]{}*?<>|])/g, "\\$1");
}

async function importDroppedMedia(drop) {
  const files = (await filesFromDrop(drop)).filter(file => /\.(mp4|mov|m4v|webm|m4a|mp3|wav|aac|flac)$/i.test(file.name));
  if (!files.length) {
    toast("That folder does not contain supported media files", true);
    return;
  }
  importMediaFiles(files);
}

async function attachDropToOperator(drop) {
  if (!drop.files.length && !drop.entries.length) return;
  els.terminalHost.classList.remove("drop-target");
  try {
    const names = drop.entries.map(entry => entry.name);
    const resolved = names.length ? await request("/api/operator/resolve-paths", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ names }),
    }) : { paths: {} };
    const paths = Object.values(resolved.paths);
    const unresolvedEntries = drop.entries.filter(entry => !resolved.paths[entry.name]);
    const files = unresolvedEntries.length
      ? (await Promise.all(unresolvedEntries.map(filesFromEntry))).flat().filter(file => file.size > 0)
      : (!drop.entries.length ? drop.files.filter(file => file.size > 0) : []);
    for (const file of files) {
      toast(`Attaching ${file.name} to Operator…`);
      const projectQuery = app.project ? `&project=${encodeURIComponent(app.project.id)}` : "";
      const response = await fetch(`/api/operator/files?filename=${encodeURIComponent(file.name)}${projectQuery}`, {
        method: "POST", headers: { "Content-Type": "application/octet-stream", "X-File-Size": String(file.size) }, body: file,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Could not attach ${file.name}`);
      paths.push(data.file.path);
    }
    if (!paths.length) throw new Error("Could not resolve a local path for that drop");
    await startTerminal();
    queueTerminalInput(paths.map(terminalEscapePath).join(" "));
    terminalState.terminal?.focus();
    toast(`${paths.length} path${paths.length === 1 ? "" : "s"} inserted into Operator`);
  } catch (error) {
    toast(error.message, true);
  }
}

function enableFileDrop(target, handler) {
  target.addEventListener("dragover", event => {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    target.classList.add("drop-target");
  });
  target.addEventListener("dragleave", event => {
    if (!target.contains(event.relatedTarget)) target.classList.remove("drop-target");
  });
  target.addEventListener("drop", event => {
    const entries = [...event.dataTransfer.items].map(item => item.webkitGetAsEntry?.()).filter(Boolean);
    if (!event.dataTransfer.files.length && !entries.length) return;
    event.preventDefault();
    event.stopPropagation();
    target.classList.remove("drop-target");
    handler({ files: [...event.dataTransfer.files], entries });
  });
}

async function trashProject(project) {
  const versions = `${project.media.length} media version${project.media.length === 1 ? "" : "s"}`;
  if (!window.confirm(`Move “${project.name}” and its ${versions} to Trash?\n\nSource footage and saved work files will not be deleted.`)) return;
  try {
    await request(`/api/projects/${encodeURIComponent(project.id)}/trash`, { method: "POST" });
    const deletedCurrent = app.project?.id === project.id;
    app.projects = app.projects.filter(item => item.id !== project.id);
    if (deletedCurrent) {
      resetPreviewPool();
      app.project = null;
      app.state = null;
      app.selectedId = null;
      if (app.projects.length) await selectProject(app.projects[0].id);
      else resetEmptyEditor();
    }
    renderProjectList();
    toast(`${project.name} moved to Trash`);
  } catch (error) {
    toast(error.message, true);
  }
}

function resetEmptyEditor() {
  els.projectTitle.textContent = "No pipeline exports found";
  els.mediaSelect.innerHTML = "";
  els.emptyMonitor.hidden = false;
  els.videoFrame.classList.remove("portrait");
  els.trackLanes.innerHTML = "";
  els.trackControls.innerHTML = '<div class="ruler-spacer"></div>';
  els.ruler.innerHTML = "";
  els.templateContract.hidden = true;
  els.duplicateEpisode.hidden = true;
  renderAssetList();
  els.currentTime.textContent = "00:00:00:00";
  els.totalTime.textContent = "00:00:00:00";
  renderInspector();
  updateAssistantContext();
}

async function request(url, options) {
  const response = await fetch(url, { cache: "no-store", ...(options || {}) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function initializeSecuritySession() {
  const response = await fetch("/api/session", { cache: "no-store", credentials: "same-origin" });
  if (!response.ok) throw new Error("Could not establish the local editor session");
}

function setTerminalStatus(label, state = "") {
  els.terminalStatus.textContent = label;
  els.terminalStatusDot.className = `status-dot${state ? ` ${state}` : ""}`;
}

async function loadTerminalStatus() {
  try {
    const status = await request("/api/terminal/status");
    terminalState.fullAccess = Boolean(status.full_access);
    if (els.operatorAccessBadge) {
      els.operatorAccessBadge.textContent = status.full_access ? "Full access" : "Approval-aware";
      els.operatorAccessBadge.title = status.full_access
        ? "Codex can run commands without approval prompts"
        : "Codex requests approval for sensitive commands";
      els.operatorAccessBadge.classList.toggle("full-access", Boolean(status.full_access));
    }
    setTerminalStatus(status.label, status.authenticated ? "online" : "error");
    return status;
  } catch (error) {
    setTerminalStatus("Codex unavailable", "error");
    return { available: false, authenticated: false, label: error.message };
  }
}

function ensureTerminalUi() {
  if (terminalState.terminal) return;
  if (!window.Terminal || !window.FitAddon?.FitAddon) throw new Error("Terminal renderer did not load");
  const terminal = new window.Terminal({
    cursorBlink: true,
    cursorStyle: "bar",
    scrollback: 5000,
    fontFamily: "SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    fontSize: 10,
    lineHeight: 1.2,
    theme: {
      background: "#080a0d", foreground: "#d7d9df", cursor: "#b7adff",
      selectionBackground: "#514a7a88", black: "#15171b", red: "#ed6b75",
      green: "#5ac9a9", yellow: "#d8b267", blue: "#7f9fec", magenta: "#aa96f5",
      cyan: "#6dc7d7", white: "#d7d9df", brightBlack: "#686d77",
    },
  });
  const fitAddon = new window.FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(els.codexTerminal);
  terminal.onData(queueTerminalInput);
  terminalState.terminal = terminal;
  terminalState.fitAddon = fitAddon;
  requestAnimationFrame(fitTerminal);
}

function decodeTerminalOutput(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function fitTerminal() {
  if (!terminalState.fitAddon) return;
  try {
    terminalState.fitAddon.fit();
  } catch (_error) {
    return;
  }
  if (!terminalState.sessionId) return;
  clearTimeout(terminalState.resizeTimer);
  terminalState.resizeTimer = setTimeout(() => {
    if (!terminalState.sessionId) return;
    request(`/api/terminal/${terminalState.sessionId}/resize`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: terminalState.terminal.cols, rows: terminalState.terminal.rows }),
    }).catch(() => {});
  }, 90);
}

function queueTerminalInput(data) {
  if (!terminalState.sessionId || !terminalState.alive) return;
  terminalState.inputBuffer += data;
  if (terminalState.inputTimer) return;
  terminalState.inputTimer = setTimeout(() => {
    terminalState.inputTimer = null;
    const input = terminalState.inputBuffer;
    terminalState.inputBuffer = "";
    const sessionId = terminalState.sessionId;
    terminalState.writeChain = terminalState.writeChain.then(() => {
      if (!sessionId || sessionId !== terminalState.sessionId) return;
      return request(`/api/terminal/${sessionId}/input`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: input }),
      });
    }).catch(error => setTerminalStatus(error.message, "error"));
  }, 10);
}

async function pollTerminal() {
  clearTimeout(terminalState.pollTimer);
  const sessionId = terminalState.sessionId;
  if (!sessionId) return;
  try {
    const data = await request(`/api/terminal/${sessionId}/output?cursor=${terminalState.cursor}`);
    if (sessionId !== terminalState.sessionId) return;
    terminalState.cursor = data.cursor;
    if (data.data) terminalState.terminal.write(decodeTerminalOutput(data.data));
    terminalState.alive = data.alive;
    updateAssistantContext();
    if (data.alive) terminalState.pollTimer = setTimeout(pollTerminal, 45);
    else setTerminalStatus("Codex stopped", "");
  } catch (error) {
    if (sessionId !== terminalState.sessionId) return;
    terminalState.alive = false;
    updateAssistantContext();
    setTerminalStatus(error.message, "error");
  }
}

async function startTerminal() {
  if (terminalState.starting || terminalState.sessionId) return;
  terminalState.starting = true;
  els.terminalLoading.hidden = false;
  try {
    ensureTerminalUi();
    requestAnimationFrame(fitTerminal);
    const status = await loadTerminalStatus();
    if (!status.available || !status.authenticated) throw new Error(status.label);
    const data = await request("/api/terminal/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: terminalState.terminal.cols, rows: terminalState.terminal.rows }),
    });
    terminalState.sessionId = data.id;
    terminalState.cursor = 0;
    terminalState.alive = true;
    setTerminalStatus(status.label, "online");
    els.terminalLoading.hidden = true;
    updateAssistantContext();
    pollTerminal();
    requestAnimationFrame(() => {
      fitTerminal();
      terminalState.terminal.focus();
    });
  } catch (error) {
    terminalState.alive = false;
    els.terminalLoading.hidden = true;
    setTerminalStatus(error.message, "error");
    terminalState.terminal?.writeln(`\r\n\x1b[31m${error.message}\x1b[0m`);
  } finally {
    terminalState.starting = false;
  }
}

async function stopTerminal() {
  const sessionId = terminalState.sessionId;
  terminalState.sessionId = null;
  terminalState.alive = false;
  clearTimeout(terminalState.pollTimer);
  updateAssistantContext();
  if (!sessionId) return;
  try {
    await request(`/api/terminal/${sessionId}/stop`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
  } catch (_error) {
    // The process may already have exited.
  }
}

async function restartTerminal() {
  els.terminalRestart.disabled = true;
  const previousSessionId = terminalState.sessionId;
  terminalState.sessionId = null;
  terminalState.alive = false;
  clearTimeout(terminalState.pollTimer);
  try {
    const data = await request("/api/terminal/restart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: terminalState.terminal.cols, rows: terminalState.terminal.rows }),
    });
    terminalState.terminal?.reset();
    terminalState.sessionId = data.id;
    terminalState.cursor = 0;
    terminalState.alive = true;
    const status = await loadTerminalStatus();
    setTerminalStatus(status.label, status.authenticated ? "online" : "error");
    pollTerminal();
    requestAnimationFrame(() => { fitTerminal(); terminalState.terminal?.focus(); });
  } catch (error) {
    terminalState.sessionId = previousSessionId;
    setTerminalStatus(error.message, "error");
  } finally {
    updateAssistantContext();
    els.terminalRestart.disabled = false;
  }
}

function buildReviewPrompt() {
  const offsets = segmentOffsets();
  const clips = offsets.slice(0, 80).map((item, index) => (
    `${index + 1}. ${item.segment.label}: sequence ${item.start.toFixed(2)}-${item.end.toFixed(2)}s, ` +
    `source ${item.segment.source_start.toFixed(2)}-${item.segment.source_end.toFixed(2)}s, ` +
    `volume ${Math.round(item.segment.volume * 100)}%`
  )).join("\n");
  const templateStatus = currentTemplateStatus();
  const templateLines = templateStatus ? [
    `Episode mechanism: ${episodeTemplate().slots.map(slot => slot.label).join(" → ")}`,
    `Missing required beats: ${templateStatus.missing.map(slot => slot.label).join(", ") || "none"}`,
  ] : [];
  return [
    "Review the current LLM Video Editor timeline and give concise, actionable editing feedback with exact timestamps.",
    terminalState.fullAccess
      ? "You have unrestricted terminal access. Do not modify project files or timeline state unless I explicitly ask you to implement a change."
      : "You have an approval-aware terminal. Prefer structured editor actions and request approval for sensitive commands.",
    `Project: ${app.project.name}`,
    `Media: ${app.state.media}`,
    `Playhead: ${app.editTime.toFixed(2)}s`,
    `Sequence duration: ${sequenceDuration().toFixed(2)}s`,
    `Format: ${app.state.resolution[0]}x${app.state.resolution[1]} at ${app.state.fps.toFixed(2)} fps`,
    ...templateLines,
    "Timeline clips:",
    clips || "No enabled clips",
  ].join("\n");
}

function mergeClipPatch(segment, patch) {
  const nested = new Set(["transform", "color", "transition_in", "transition_out"]);
  for (const [key, value] of Object.entries(patch || {})) {
    if (nested.has(key) && value && typeof value === "object") segment[key] = { ...(segment[key] || {}), ...value };
    else if (!["id", "project_id"].includes(key)) segment[key] = value;
  }
}

function applyOperatorActions(actions) {
  if (!app.state || !actions.length) return;
  pushHistory();
  let lastSelected = app.selectedId;
  for (const action of actions) {
    const segment = app.state.segments.find(item => item.id === action.clip_id);
    if (action.type === "add_clip") {
      const asset = assetById(action.asset_id);
      const track = trackById(action.track_id) || app.state.tracks.find(item => item.kind === "video");
      if (!asset || !track || track.locked) continue;
      const added = makeMediaSegment(asset, track, track.kind === "audio" ? "audio" : "video", Number(action.timeline_start ?? app.editTime));
      if (action.source_start != null) added.source_start = Math.max(0, Number(action.source_start));
      if (action.source_end != null) added.source_end = Math.min(asset.duration, Number(action.source_end));
      app.state.segments.push(added);
      lastSelected = added.id;
    } else if (action.type === "replace_clip" && segment && !segmentLocked(segment)) {
      const asset = assetById(action.asset_id);
      if (!asset || !["video", "audio"].includes(segment.kind)) continue;
      const oldAssetId = segment.asset_id;
      const start = Number(segment.timeline_start || 0);
      const slotId = segment.slot_id;
      fitReplacementToSegment(segment, asset);
      app.state.segments.filter(item => (
        item !== segment && item.kind === "audio" && item.asset_id === oldAssetId
        && item.slot_id === slotId && Math.abs(Number(item.timeline_start || 0) - start) <= .01
      )).forEach(companion => {
        if (asset.has_audio) { fitReplacementToSegment(companion, asset); companion.enabled = true; }
        else companion.enabled = false;
      });
      lastSelected = segment.id;
    } else if (action.type === "move_clip" && segment && !segmentLocked(segment)) {
      if (action.track_id && trackById(action.track_id)) segment.track_id = action.track_id;
      if (action.timeline_start != null) segment.timeline_start = Math.max(0, Number(action.timeline_start));
      lastSelected = segment.id;
    } else if (action.type === "trim_clip" && segment && !segmentLocked(segment) && !["title", "caption"].includes(segment.kind)) {
      const assetDuration = assetById(segment.asset_id)?.duration || app.state.duration;
      if (action.source_start != null) segment.source_start = Math.max(0, Math.min(Number(action.source_start), segment.source_end - .04));
      if (action.source_end != null) segment.source_end = Math.min(assetDuration, Math.max(Number(action.source_end), segment.source_start + .04));
      lastSelected = segment.id;
    } else if (action.type === "delete_clip" && segment && !segmentLocked(segment)) {
      app.state.segments = app.state.segments.filter(item => item.id !== segment.id);
      if (lastSelected === segment.id) lastSelected = null;
    } else if (action.type === "set_clip" && segment && !segmentLocked(segment)) {
      mergeClipPatch(segment, action.patch);
      lastSelected = segment.id;
    } else if (action.type === "split_clip" && segment && !segmentLocked(segment) && !["title", "caption"].includes(segment.kind)) {
      const splitTime = Number(action.time);
      const start = Number(segment.timeline_start || 0);
      const end = start + durationOf(segment);
      if (splitTime <= start + .04 || splitTime >= end - .04) continue;
      const sourceTime = segment.source_start + (splitTime - start) * Math.max(.1, segment.speed || 1);
      const right = structuredClone(segment);
      right.id = `clip-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
      right.label = `${segment.label} B`;
      right.source_start = Number(sourceTime.toFixed(3));
      right.timeline_start = Number(splitTime.toFixed(3));
      segment.source_end = Number(sourceTime.toFixed(3));
      app.state.segments.push(right);
      lastSelected = right.id;
    } else if (action.type === "add_track" && ["video", "audio"].includes(action.kind)) {
      const count = app.state.tracks.filter(track => track.kind === action.kind).length + 1;
      app.state.tracks.push({ id: `${action.kind === "video" ? "v" : "a"}${count}`, kind: action.kind, name: `${action.kind === "video" ? "V" : "A"}${count}`, muted: false, locked: false });
    } else if (action.type === "unlink_audio" && segment?.kind === "video" && segment.audio_linked) {
      const audioTrack = trackById(segment.audio_track_id) || app.state.tracks.find(track => track.kind === "audio");
      if (!audioTrack) continue;
      const audio = structuredClone(segment);
      audio.id = `audio-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
      audio.kind = "audio";
      audio.label = `${segment.label} audio`;
      audio.track_id = audioTrack.id;
      audio.audio_linked = false;
      audio.audio_track_id = null;
      segment.audio_linked = false;
      segment.audio_track_id = null;
      app.state.segments.push(audio);
      lastSelected = audio.id;
    } else if (["add_title", "add_caption"].includes(action.type)) {
      const kind = action.type === "add_title" ? "title" : "caption";
      const text = String(action.text || (kind === "title" ? "New title" : "New caption"));
      const added = makeTextSegment(kind, text, Number(action.timeline_start ?? app.editTime), Math.max(.1, Number(action.duration ?? 3)));
      if (added) { app.state.segments.push(added); lastSelected = added.id; }
    } else if (action.type === "set_export_preset" && ["source", "youtube", "vertical", "draft"].includes(action.preset)) {
      app.state.export_preset = action.preset;
      els.exportPreset.value = action.preset;
    }
  }
  app.selectedId = lastSelected;
  markDirty();
  renderTimeline();
  renderInspector();
  seekEditTime(app.editTime);
  toast(`Operator applied ${actions.length} timeline action${actions.length === 1 ? "" : "s"}`);
}

function reconcileOperatorState(data, quiet = false) {
  const revision = Number(data?.revision || 0);
  const projectId = String(data?.project_id || data?.state?.project_id || "");
  if (!app.project || projectId !== app.project.id) return false;
  if (!data?.state || revision <= operatorState.revision) return false;
  if (app.dirty) {
    if (!quiet) toast("A newer saved edit is available; save or reload to reconcile it", true);
    return false;
  }
  const previous = {
    state: app.state,
    selectedId: app.selectedId,
    editTime: app.editTime,
    proxyStale: previewState.proxyStale,
    historyLength: app.history.length,
  };
  try {
    if (app.state?.segments?.length) pushHistory();
    app.state = { ...app.state, ...structuredClone(data.state) };
    previewState.proxyStale = true;
    if (!app.state.segments.some(segment => segment.id === app.selectedId)) app.selectedId = app.state.segments[0]?.id || null;
    app.editTime = Math.min(app.editTime, sequenceDuration());
    renderAssetList();
    renderTimeline();
    renderInspector();
    ensureNativeMonitorRect();
    syncNativeTimeline(revision);
    seekEditTime(app.editTime);
    operatorState.revision = revision;
    operatorState.projectId = projectId;
    markSaved();
    if (!quiet) toast(`Viewer synced to edit revision ${revision}`);
    return true;
  } catch (error) {
    app.state = previous.state;
    app.selectedId = previous.selectedId;
    app.editTime = previous.editTime;
    previewState.proxyStale = previous.proxyStale;
    app.history.length = previous.historyLength;
    console.error(`Viewer failed to apply edit revision ${revision}`, error);
    if (!quiet) toast(`Viewer could not apply edit revision ${revision}; retrying`, true);
    return false;
  }
}

async function pollOperatorActions() {
  clearTimeout(operatorState.pollTimer);
  if (!app.state) {
    operatorState.pollTimer = setTimeout(pollOperatorActions, 350);
    return;
  }
  try {
    const projectId = app.project.id;
    const data = await request(`/api/operator/actions?cursor=${operatorState.cursor}&project_id=${encodeURIComponent(projectId)}`);
    const reconciled = reconcileOperatorState(data);
    if (reconciled || Number(data?.revision || 0) <= operatorState.revision) operatorState.cursor = data.cursor;
  } catch (_error) {
    // Retry quietly; terminal feedback remains visible if an action is malformed.
  }
  operatorState.pollTimer = setTimeout(pollOperatorActions, 350);
}

async function initializeOperatorBridge() {
  try {
    await syncOperatorContext();
    const projectId = app.project?.id;
    if (!projectId) throw new Error("No Operator project selected");
    const data = await request(`/api/operator/actions?cursor=0&project_id=${encodeURIComponent(projectId)}`);
    reconcileOperatorState(data, true);
    operatorState.cursor = data.cursor;
  } catch (_error) {
    operatorState.cursor = 0;
  }
  pollOperatorActions();
}

async function loadProjects(selectFirst = true) {
  try {
    const data = await request("/api/projects");
    app.projects = data.projects;
    renderProjectList();
    if (selectFirst && app.projects.length) await selectProject(app.project?.id || app.projects[0].id);
    if (!app.projects.length) els.projectTitle.textContent = "No pipeline exports found";
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshCurrentProject() {
  const projectId = app.project?.id;
  const mediaId = app.state?.media || null;
  await loadProjects(false);
  if (!projectId || !app.projects.some(project => project.id === projectId)) return;
  await selectProject(projectId, mediaId);
  toast("Project timeline and viewer reloaded");
}

async function selectProject(id, mediaId = null) {
  try {
    const project = app.projects.find(item => item.id === id);
    if (!project) return;
    const mediaQuery = mediaId ? `?media=${encodeURIComponent(mediaId)}` : "";
    const cacheBuster = `${mediaQuery ? "&" : "?"}_=${Date.now()}`;
    const state = await request(`/api/projects/${encodeURIComponent(id)}${mediaQuery}${cacheBuster}`);
    const selectedMedia = state.media;
    app.project = project;
    app.state = state;
    operatorState.projectId = project.id;
    operatorState.revision = 0;
    operatorState.cursor = 0;
    app.selectedId = state.segments[0]?.id || null;
    app.selectedGap = null;
    app.editTime = 0;
    app.activeIndex = 0;
    app.history = [];
    app.future = [];
    previewState.proxyStale = false;
    previewState.proxyAsset = findRenderedPreviewAsset();
    els.projectTitle.textContent = `${project.name}  /  ${selectedMedia}`;
    els.sequenceName.textContent = project.name;
    els.mediaSelect.innerHTML = project.media.map(item => `<option value="${escapeHtml(item.id)}"${item.id === selectedMedia ? " selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
    resetPreviewPool();
    els.emptyMonitor.hidden = true;
    els.videoFrame.classList.toggle("portrait", state.resolution[1] > state.resolution[0]);
    state.saved ? markSaved() : (els.saveState.textContent = "New edit");
    renderProjectList();
    renderAssetList();
    renderTimeline();
    renderInspector();
    els.exportPreset.value = state.export_preset || "source";
    updateHistoryButtons();
    ensureNativeMonitorRect();
    syncNativeTimeline();
    seekEditTime(0);
    await syncOperatorContext();
  } catch (error) {
    toast(error.message, true);
  }
}

async function duplicateEpisode() {
  const template = episodeTemplate();
  if (!app.project || !template) return;
  if (app.dirty) {
    await save();
    if (app.dirty) return;
  }
  const nextEpisode = Number(template.episode_number || 1) + 1;
  const suggested = `Episode ${nextEpisode}`;
  const name = window.prompt("Name the next episode", suggested);
  if (name === null) return;
  try {
    const data = await request(`/api/projects/${encodeURIComponent(app.project.id)}/duplicate`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() || suggested }),
    });
    await loadProjects(false);
    await selectProject(data.project.id);
    toast(`Episode ${nextEpisode} created from the template`);
  } catch (error) {
    toast(error.message, true);
  }
}

function splitAtPlayhead() {
  if (!app.state) return;
  const item = atEditTime(app.editTime);
  if (!item) return;
  if (segmentLocked(item.segment)) { toast("This reusable template layer is protected", true); return; }
  if (["title", "caption"].includes(item.segment.kind)) {
    toast("Trim text clips or add another text clip instead", true);
    return;
  }
  const sourceTime = item.segment.source_start + (app.editTime - item.start) * Math.max(.1, item.segment.speed || 1);
  if (sourceTime - item.segment.source_start < 0.08 || item.segment.source_end - sourceTime < 0.08) {
    toast("Move the playhead inside a clip to split it", true);
    return;
  }
  pushHistory();
  const original = item.segment;
  const newId = `clip-${Date.now()}`;
  const left = { ...original, source_end: Number(sourceTime.toFixed(3)) };
  const right = { ...structuredClone(original), id: newId, label: `${original.label} B`, source_start: Number(sourceTime.toFixed(3)), timeline_start: Number(app.editTime.toFixed(3)) };
  const rawIndex = app.state.segments.indexOf(original);
  app.state.segments.splice(rawIndex, 1, left, right);
  app.selectedId = right.id;
  markDirty();
  renderTimeline();
  renderInspector();
}

function addTrack(kind) {
  if (!app.state) return;
  pushHistory();
  const count = app.state.tracks.filter(track => track.kind === kind).length + 1;
  app.state.tracks.push({ id: `${kind === "video" ? "v" : "a"}${count}`, kind, name: `${kind === "video" ? "V" : "A"}${count}`, muted: false, locked: false });
  markDirty();
  renderTimeline();
  renderInspector();
}

function makeTextSegment(kind, text, start = app.editTime, duration = 3) {
  const track = app.state.tracks.find(item => item.kind === "video");
  if (!track) return null;
  const slot = episodeTemplate()?.slots?.find(candidate => start >= candidate.start - .001 && start < candidate.end - .001);
  return {
    id: `${kind}-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`,
    label: text.split("\n")[0].slice(0, 80) || (kind === "title" ? "New title" : "New caption"),
    kind, asset_id: null, track_id: track.id, audio_track_id: null, audio_linked: false,
    timeline_start: Number(start.toFixed(3)), source_start: 0, source_end: duration, duration,
    speed: 1, volume: 1, audio_fade_in: 0, audio_fade_out: 0,
    transition_in: { type: "fade", duration: .25 }, transition_out: { type: "fade", duration: .25 },
    transform: { scale: 1, x: 0, y: 0, rotation: 0, crop_top: 0, crop_right: 0, crop_bottom: 0, crop_left: 0 },
    color: { brightness: 0, contrast: 1, saturation: 1, temperature: 0 },
    text, font_size: kind === "title" ? 64 : 72,
    text_color: "#ffffff", keyframes: [], enabled: true,
    slot_id: slot?.id || null, template_role: "caption", template_source: false, locked: false,
  };
}

function addTextClip(kind) {
  if (!app.state) return;
  pushHistory();
  const segment = makeTextSegment(kind, kind === "title" ? "New title" : "New caption");
  if (!segment) return;
  app.state.segments.push(segment);
  app.selectedId = segment.id;
  markDirty();
  renderTimeline();
  renderInspector();
  updatePreviewOverlays();
}

function unlinkSelectedAudio() {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  if (!segment || segment.kind !== "video" || !segment.audio_linked) return;
  if (segmentLocked(segment)) { toast("This reusable template layer is protected", true); return; }
  let audioTrack = trackById(segment.audio_track_id);
  if (!audioTrack) {
    addTrack("audio");
    audioTrack = app.state.tracks.find(item => item.kind === "audio");
  }
  pushHistory();
  const audio = structuredClone(segment);
  audio.id = `audio-${Date.now()}`;
  audio.kind = "audio";
  audio.label = `${segment.label} audio`;
  audio.track_id = audioTrack.id;
  audio.audio_track_id = null;
  audio.audio_linked = false;
  segment.audio_linked = false;
  segment.audio_track_id = null;
  app.state.segments.push(audio);
  app.selectedId = audio.id;
  markDirty();
  renderTimeline();
  renderInspector();
}

function deleteSelected() {
  if (!app.state) return;
  if (app.selectedGap) {
    closeTimelineGap(app.selectedGap);
    return;
  }
  if (!app.selectedId) return;
  const index = app.state.segments.findIndex(item => item.id === app.selectedId);
  if (index < 0) return;
  if (segmentLocked(app.state.segments[index])) { toast("This reusable template layer is protected", true); return; }
  pushHistory();
  app.state.segments.splice(index, 1);
  app.selectedId = app.state.segments[Math.min(index, app.state.segments.length - 1)]?.id || null;
  app.editTime = Math.min(app.editTime, sequenceDuration());
  markDirty();
  renderTimeline();
  renderInspector();
  seekEditTime(app.editTime);
}

async function save() {
  if (!app.state || !app.project) return;
  els.save.disabled = true;
  try {
    const localSegments = app.state.segments;
    const data = await request(`/api/projects/${encodeURIComponent(app.project.id)}?media=${encodeURIComponent(app.state.media)}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Editor-Client-ID": editorClientId, "X-Editor-Page-ID": editorPageId }, body: JSON.stringify(app.state),
    });
    app.state = { ...app.state, ...data.state };
    // Preserve new client-side transform fields while a running server still uses
    // an older normalizer. The next server launch persists them natively.
    app.state.segments = app.state.segments.map((segment, index) => {
      const local = localSegments[index]?.id === segment.id
        ? localSegments[index]
        : localSegments.find(item => item.id === segment.id && item.kind === segment.kind && item.timeline_start === segment.timeline_start);
      if (local?.transform?.rotation == null) return segment;
      return { ...segment, transform: { ...segment.transform, rotation: local.transform.rotation } };
    });
    operatorState.projectId = data.project_id || app.project.id;
    operatorState.revision = Number(data.revision || operatorState.revision);
    markSaved();
    app.project.has_project_file = true;
    app.project.active_media = app.state.media;
    renderProjectList();
    toast(`Project saved · ${app.state.project_file}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.save.disabled = false;
  }
}

async function renderEdit() {
  if (!app.state || !app.project) return;
  const templateStatus = currentTemplateStatus();
  if (templateStatus?.missing.length) {
    const missing = templateStatus.missing.map(slot => slot.label).join(", ");
    if (!window.confirm(`This episode is missing required story beats: ${missing}.\n\nRender anyway?`)) return;
  }
  els.render.disabled = true;
  els.render.textContent = "Rendering…";
  els.saveState.textContent = "FFmpeg is rendering";
  els.renderProgress.hidden = false;
  els.renderProgressBar.style.width = "1%";
  els.renderProgressLabel.textContent = `Preparing ${els.exportPreset.options[els.exportPreset.selectedIndex].text}…`;
  try {
    app.state.export_preset = els.exportPreset.value;
    const preflight = await request(`/api/projects/${encodeURIComponent(app.project.id)}/check?media=${encodeURIComponent(app.state.media)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(app.state),
    });
    if (preflight.issues?.length) {
      const errors = preflight.issues.filter(issue => issue.severity === "error");
      const warnings = preflight.issues.filter(issue => issue.severity === "warning");
      const summary = [...errors, ...warnings].slice(0, 8).map(issue => `• ${issue.message}`).join("\n");
      const heading = `${errors.length} error${errors.length === 1 ? "" : "s"}, ${warnings.length} warning${warnings.length === 1 ? "" : "s"}`;
      if (!window.confirm(`Preflight found ${heading}:\n\n${summary}\n\nRender anyway where safe?`)) return;
    }
    const data = await request(`/api/projects/${encodeURIComponent(app.project.id)}/render?media=${encodeURIComponent(app.state.media)}&preset=${encodeURIComponent(els.exportPreset.value)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(app.state),
    });
    let job;
    do {
      await new Promise(resolve => setTimeout(resolve, 350));
      job = await request(`/api/render/${data.job_id}`);
      const percent = Math.max(1, Math.round(job.progress * 100));
      els.renderProgressBar.style.width = `${percent}%`;
      els.renderProgressLabel.textContent = job.status === "queued" ? "Waiting to render…" : `Rendering ${percent}%`;
    } while (["queued", "rendering"].includes(job.status));
    if (job.status === "failed") throw new Error(job.error || "Render failed");
    markSaved();
    els.renderProgressLabel.textContent = `Complete · ${job.name}`;
    toast(`Rendered ${job.name}`);
    await loadProjects(false);
    setTimeout(() => { els.renderProgress.hidden = true; }, 1800);
  } catch (error) {
    const message = error?.message || "Render failed";
    toast(message, true);
    els.saveState.textContent = app.dirty ? "Unsaved changes" : "Saved locally";
    els.renderProgressBar.style.width = "0%";
    els.renderProgressLabel.textContent = `Render failed · ${message}`;
  } finally {
    els.render.disabled = false;
    els.render.textContent = "Render edit";
  }
}

function updateField(change) {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  if (!segment) return;
  if (segmentLocked(segment)) { toast("This reusable template layer is protected", true); return; }
  pushHistory();
  change(segment);
  markDirty();
  renderTimeline();
  renderInspector();
}

function updatePreviewPlayback(video, forceBoundary = false) {
  if (video !== els.preview || !app.state) return;
  if (previewState.usingProxy) {
    app.editTime = Math.min(sequenceDuration(), Math.max(0, video.currentTime));
    if (forceBoundary || app.editTime >= sequenceDuration() - .012) {
      video.pause();
      app.editTime = sequenceDuration();
    }
    updatePlayhead();
    return;
  }
  if (previewState.pendingBoundary) {
    if (activatePreparedPreview(previewState.pendingBoundary)) updatePlayhead();
    return;
  }
  if (!forceBoundary && video.paused) return;
  let item = atEditTime(app.editTime);
  if (!item) return;
  maybeStartPreviewPreroll(item, video);
  if (forceBoundary || video.currentTime >= item.segment.source_end - 0.012) {
    const next = nextVideoItem(item);
    if (!next) {
      video.pause();
      app.editTime = sequenceDuration();
      updatePlayhead();
      return;
    }
    if (activatePreparedPreview(next)) {
      updatePlayhead();
      return;
    }
    previewState.pendingBoundary = next;
    app.editTime = item.end;
    video.pause();
    preparePreviewQueue(item);
    runPreviewPlaybackLoop();
    updatePlayhead();
    return;
  }
  const sourceWithin = Math.max(0, video.currentTime - item.segment.source_start) / Math.max(.1, Number(item.segment.speed) || 1);
  app.editTime = Math.min(item.end, item.start + sourceWithin);
  updatePlayhead();
}

function runPreviewPlaybackLoop() {
  if (previewState.animationFrame !== null) cancelAnimationFrame(previewState.animationFrame);
  const tick = () => {
    updatePreviewPlayback(els.preview);
    if (!els.preview.paused || previewState.pendingBoundary) previewState.animationFrame = requestAnimationFrame(tick);
    else previewState.animationFrame = null;
  };
  previewState.animationFrame = requestAnimationFrame(tick);
}

function attachPreviewEvents(video) {
  video.addEventListener("play", () => {
    if (video !== els.preview) return;
    els.play.textContent = "Ⅱ";
    runPreviewPlaybackLoop();
  });
  video.addEventListener("pause", () => {
    if (video !== els.preview) return;
    els.play.textContent = previewState.pendingBoundary ? "Ⅱ" : "▶";
    if (previewState.pendingBoundary) {
      runPreviewPlaybackLoop();
      return;
    }
    stopQueuedPreviewPlayback();
    const active = atEditTime(app.editTime);
    if (active) preparePreviewQueue(active);
    if (previewState.animationFrame !== null) cancelAnimationFrame(previewState.animationFrame);
    previewState.animationFrame = null;
  });
  video.addEventListener("timeupdate", () => updatePreviewPlayback(video));
  video.addEventListener("ended", () => updatePreviewPlayback(video, true));
}

[els.preview, ...previewState.queue].forEach(attachPreviewEvents);

els.play.addEventListener("click", () => {
  if (!app.state) return;
  if (nativeEditor.enabled) {
    nativePost({ type: "transport", command: "toggle", time: app.editTime });
    return;
  }
  if (els.preview.paused) {
    if (app.editTime >= sequenceDuration() - .02) seekEditTime(0);
    else seekEditTime(app.editTime);
    if (!previewState.usingProxy) primePreviewPoolPlayback();
    els.preview.play().catch(() => {});
  } else els.preview.pause();
});
els.timelineContent.addEventListener("click", event => {
  if (event.target.closest(".clip")) return;
  const rect = els.timelineContent.getBoundingClientRect();
  seekEditTime((event.clientX - rect.left) / Number(els.zoom.value));
});
els.timelineScroll.addEventListener("scroll", () => { els.trackControls.scrollTop = els.timelineScroll.scrollTop; });
els.zoom.addEventListener("input", renderTimeline);
els.search.addEventListener("input", renderProjectList);
els.refresh.addEventListener("click", refreshCurrentProject);
els.mediaSelect.addEventListener("change", () => selectProject(app.project.id, els.mediaSelect.value));
els.importMedia.addEventListener("click", () => els.importMediaInput.click());
els.importMediaInput.addEventListener("change", () => importMediaFiles([...els.importMediaInput.files]));
els.save.addEventListener("click", save);
els.duplicateEpisode.addEventListener("click", duplicateEpisode);
els.render.addEventListener("click", renderEdit);
els.split.addEventListener("click", splitAtPlayhead);
els.delete.addEventListener("click", deleteSelected);
els.replaceMediaButton.addEventListener("click", replaceSelectedMedia);
els.undo.addEventListener("click", undo);
els.redo.addEventListener("click", redo);
els.addVideoTrack.addEventListener("click", () => addTrack("video"));
els.addAudioTrack.addEventListener("click", () => addTrack("audio"));
els.addTitle.addEventListener("click", () => addTextClip("title"));
els.addCaption.addEventListener("click", () => addTextClip("caption"));
els.unlinkAudio.addEventListener("click", unlinkSelectedAudio);
els.jumpBack.addEventListener("click", () => seekEditTime(0));
els.jumpForward.addEventListener("click", () => seekEditTime(sequenceDuration()));
els.terminalInterrupt.addEventListener("click", async () => {
  if (!terminalState.sessionId) return;
  try {
    await request(`/api/terminal/${terminalState.sessionId}/interrupt`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    terminalState.terminal?.focus();
  } catch (error) {
    toast(error.message, true);
  }
});
els.terminalRestart.addEventListener("click", restartTerminal);
els.reviewEdit.addEventListener("click", () => {
  if (!app.state || !terminalState.alive) return;
  queueTerminalInput(`${buildReviewPrompt()}\r`);
  terminalState.terminal?.focus();
});

els.clipLabel.addEventListener("change", () => updateField(segment => { segment.label = els.clipLabel.value.trim() || "Clip"; }));
els.clipStart.addEventListener("change", () => updateField(segment => { segment.source_start = Math.max(0, Math.min(Number(els.clipStart.value), segment.source_end - .04)); }));
els.clipEnd.addEventListener("change", () => updateField(segment => {
  const previousTimelineEnd = Number(segment.timeline_start || 0) + durationOf(segment);
  const assetDuration = assetById(segment.asset_id)?.duration || app.state.duration;
  const requestedEnd = Math.min(assetDuration, Math.max(Number(els.clipEnd.value), segment.source_start + .04));
  segment.source_end = segment.kind === "video" ? clampVideoSourceEnd(segment, requestedEnd) : requestedEnd;
  trimCaptionsAtVideoEnd(segment, previousTimelineEnd);
}));
els.clipVolume.addEventListener("input", () => {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  if (!segment) return;
  segment.volume = Number(els.clipVolume.value);
  els.volumeValue.textContent = `${Math.round(segment.volume * 100)}%`;
  markDirty();
});
els.clipVolume.addEventListener("change", () => { renderTimeline(); renderInspector(); });
els.clipTrack.addEventListener("change", () => updateField(segment => { segment.track_id = els.clipTrack.value; }));
els.clipSpeed.addEventListener("change", () => updateField(segment => {
  let requested = Math.max(.1, Math.min(100, Number(els.clipSpeed.value) || 1));
  if (segment.kind === "video") {
    const nextStart = nextVideoStart(segment);
    if (Number.isFinite(nextStart)) {
      const available = Math.max(.001, nextStart - Number(segment.timeline_start || 0));
      requested = Math.max(requested, (segment.source_end - segment.source_start) / available);
    }
  }
  segment.speed = Math.min(100, Number(requested.toFixed(3)));
}));
els.clipTimelineStart.addEventListener("change", () => updateField(segment => {
  const requested = Math.max(0, Number(els.clipTimelineStart.value) || 0);
  segment.timeline_start = segment.kind === "video" ? nonOverlappingVideoStart(segment, requested) : requested;
}));
els.transitionIn.addEventListener("change", () => updateField(segment => { segment.transition_in.type = els.transitionIn.value; }));
els.transitionInDuration.addEventListener("change", () => updateField(segment => { segment.transition_in.duration = Math.max(0, Number(els.transitionInDuration.value) || 0); }));
els.transitionOut.addEventListener("change", () => updateField(segment => { segment.transition_out.type = els.transitionOut.value; }));
els.transitionOutDuration.addEventListener("change", () => updateField(segment => { segment.transition_out.duration = Math.max(0, Number(els.transitionOutDuration.value) || 0); }));

function bindNestedNumber(element, group, key, fallback = 0) {
  element.addEventListener("change", () => updateField(segment => {
    segment[group] ||= {};
    segment[group][key] = Number(element.value) || fallback;
  }));
}

bindNestedNumber(els.clipScale, "transform", "scale", 1);
bindNestedNumber(els.clipX, "transform", "x");
bindNestedNumber(els.clipY, "transform", "y");
bindNestedNumber(els.clipRotation, "transform", "rotation");
bindNestedNumber(els.cropTop, "transform", "crop_top");
bindNestedNumber(els.cropRight, "transform", "crop_right");
bindNestedNumber(els.cropBottom, "transform", "crop_bottom");
bindNestedNumber(els.cropLeft, "transform", "crop_left");
bindNestedNumber(els.colorBrightness, "color", "brightness");
bindNestedNumber(els.colorContrast, "color", "contrast", 1);
bindNestedNumber(els.colorSaturation, "color", "saturation", 1);
bindNestedNumber(els.colorTemperature, "color", "temperature");
els.audioFadeIn.addEventListener("change", () => updateField(segment => { segment.audio_fade_in = Math.max(0, Number(els.audioFadeIn.value) || 0); }));
els.audioFadeOut.addEventListener("change", () => updateField(segment => { segment.audio_fade_out = Math.max(0, Number(els.audioFadeOut.value) || 0); }));
els.clipText.addEventListener("change", () => updateField(segment => { segment.text = els.clipText.value; segment.label = els.clipText.value.split("\n")[0].slice(0, 80) || segment.label; }));
els.fontSize.addEventListener("change", () => updateField(segment => { segment.font_size = Math.max(12, Math.min(240, Number(els.fontSize.value) || 54)); }));
els.textColor.addEventListener("input", () => {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  if (!segment) return;
  segment.text_color = els.textColor.value;
  markDirty();
  updatePreviewOverlays();
});
els.addKeyframe.addEventListener("click", () => {
  const segment = app.state?.segments.find(item => item.id === app.selectedId);
  if (!segment) return;
  pushHistory();
  const time = Number(Math.max(0, app.editTime - Number(segment.timeline_start || 0)).toFixed(3));
  const keyframe = { time, transform: structuredClone(segment.transform), color: structuredClone(segment.color), volume: segment.volume };
  const existing = segment.keyframes.findIndex(item => Math.abs(item.time - time) < 1 / fps());
  if (existing >= 0) segment.keyframes[existing] = keyframe;
  else segment.keyframes.push(keyframe);
  segment.keyframes.sort((a, b) => a.time - b.time);
  markDirty();
  renderInspector();
  toast(`Keyframe added at ${timecode(time)}`);
});
els.exportPreset.addEventListener("change", () => {
  if (!app.state) return;
  app.state.export_preset = els.exportPreset.value;
  markDirty();
});

window.addEventListener("keydown", event => {
  const editing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  const command = event.metaKey || event.ctrlKey;
  if (command && event.key.toLowerCase() === "s") { event.preventDefault(); save(); return; }
  if (command && event.key.toLowerCase() === "z") { event.preventDefault(); event.shiftKey ? redo() : undo(); return; }
  if (editing) return;
  if (event.code === "Space") { event.preventDefault(); els.play.click(); }
  if (event.key.toLowerCase() === "s") splitAtPlayhead();
  if (event.key === "Backspace" || event.key === "Delete") deleteSelected();
  if (event.key === "ArrowLeft") seekEditTime(app.editTime - (event.shiftKey ? 1 : 1 / fps()));
  if (event.key === "ArrowRight") seekEditTime(app.editTime + (event.shiftKey ? 1 : 1 / fps()));
});

window.addEventListener("beforeunload", event => {
  if (!app.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});
window.addEventListener("resize", () => {
  renderTimeline();
  fitTerminal();
  ensureNativeMonitorRect();
});
window.addEventListener("focus", scheduleOperatorContext);
document.addEventListener("visibilitychange", scheduleOperatorContext);
if (window.ResizeObserver) new ResizeObserver(fitTerminal).observe(els.terminalHost);
if (window.ResizeObserver && nativeEditor.enabled) new ResizeObserver(() => ensureNativeMonitorRect()).observe(els.videoFrame);
enableFileDrop(els.assetList, importDroppedMedia);
enableFileDrop(els.terminalHost, attachDropToOperator);

async function initializeApp() {
  await initializeSecuritySession();
  await loadProjects();
  renderAssetList();
  updateAssistantContext();
  ensureNativeMonitorRect();
  await initializeOperatorBridge();
  await startTerminal();
}

initializeApp();
