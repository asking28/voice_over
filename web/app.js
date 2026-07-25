/* revoice — frontend. Talks to the FastAPI backend; input is always a local path. */

const $ = (id) => document.getElementById(id);
const STAGES = ["extract", "transcribe", "synthesize", "mux"];

const state = {
  jobId: null,
  status: null,
  segments: [],
  duration: 0,
  dirty: new Set(),
  poller: null,
  defaults: {},
};

/* ─────────────────────────────────────────────────────────────── helpers */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("err", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), isError ? 7000 : 3200);
}

const secs = (v) => `${(+v || 0).toFixed(2)}s`;
const ms = (v) => `${((+v || 0) * 1000).toFixed(0)} ms`;
const bytes = (n) => {
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
};
const clock = (t) => {
  const total = Math.max(0, +t || 0);
  const m = Math.floor(total / 60);
  const s = total - m * 60;
  return `${m}:${s.toFixed(2).padStart(5, "0")}`;
};

/* ─────────────────────────────────────────────────────────────── health + voices */

async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.defaults = h.defaults || {};
    applyDefaults(state.defaults);
    $("health").innerHTML = [
      ["ffmpeg", h.ffmpeg && h.ffprobe],
      ["deepgram", h.deepgram_key],
      ["cartesia", h.cartesia_key],
    ].map(([name, ok]) => `<span class="pill ${ok ? "ok" : ""}">${name}</span>`).join("");
    if (!h.ffmpeg) toast("ffmpeg not found on PATH — install with: brew install ffmpeg", true);
    if (!h.deepgram_key || !h.cartesia_key) toast("API keys missing from .env", true);
  } catch (e) {
    toast(`backend unreachable: ${e.message}`, true);
  }
}

function applyDefaults(d) {
  const wanted = d.tts_provider || "deepgram";
  $("opt-provider").querySelectorAll("button").forEach((b) =>
    b.classList.toggle("on", b.dataset.value === wanted)
  );
  syncCloneToggle();
  $("opt-language").value = d.language ?? "en";
  $("opt-tts-model").value = d.tts_model ?? "";
  $("opt-stt-model").value = d.stt_model ?? "";
  $("opt-max-tempo").value = d.max_tempo ?? 1.6;
  $("opt-min-tempo").value = d.min_tempo ?? 0.75;
  $("opt-utt-split").value = d.utt_split ?? 0.6;
  $("opt-merge-gap").value = d.merge_gap ?? 0.18;
  $("opt-room-tone").value = d.room_tone ?? 0.9;
  $("opt-max-chars").value = d.max_segment_chars ?? 320;
  $("opt-workers").value = d.workers ?? 6;
  $("opt-sample-rate").value = d.sample_rate ?? 44100;
  $("opt-audio-codec").value = d.audio_codec ?? "aac";
  $("opt-adaptive").checked = d.adaptive_retry ?? true;
  $("opt-filler").checked = d.filler_words ?? true;
  $("opt-diarize").checked = d.diarize ?? false;
  $("opt-keep-original").checked = d.keep_original_track ?? false;
}

const provider = () => $("opt-provider").querySelector("button.on")?.dataset.value || "deepgram";

async function loadVoices() {
  const select = $("opt-voice");
  select.innerHTML = `<option value="">loading…</option>`;
  try {
    const { voices, error } = await api(`/api/voices?provider=${provider()}`);
    if (error) throw new Error(error);
    const mine = voices.filter((v) => v.is_owner);
    const rest = voices.filter((v) => !v.is_owner);
    // Aura's model name *is* the voice, so show it — otherwise the id you'd put in
    // --voice-id or read back in a report appears nowhere in the UI.
    const label = (v) =>
      provider() === "deepgram" ? `${v.name} · ${v.id}` : `${v.name} — ${v.language}`;
    const group = (heading, list) =>
      list.length
        ? `<optgroup label="${heading}">${list
            .map((v) => `<option value="${v.id}" title="${v.id}&#10;${v.description}">${label(v)}</option>`)
            .join("")}</optgroup>`
        : "";
    select.innerHTML = group("your voices", mine) + group("library", rest);
    const preferred = provider() === state.defaults.tts_provider ? state.defaults.voice_id : "";
    if (preferred) select.value = preferred;
    if (!select.value && select.options.length) select.selectedIndex = 0;
    showVoiceId();
  } catch (e) {
    select.innerHTML = `<option value="">(couldn't load — using default)</option>`;
    toast(`voices: ${e.message}`, true);
  }
}

/* ─────────────────────────────────────────────────────────────── file browser */

async function openBrowser(dir) {
  const dialog = $("browser");
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(dir || "")}`);
    $("browser-dir").textContent = data.dir;
    $("browser-shortcuts").innerHTML = data.shortcuts
      .map((s) => `<button class="btn ghost sm" data-path="${s.path}">${s.name}</button>`)
      .join("");
    const rows = [];
    if (data.parent) rows.push(`<li class="up" data-dir="${data.parent}">↑ ..</li>`);
    for (const e of data.entries) {
      rows.push(
        e.is_dir
          ? `<li data-dir="${e.path}">📁 ${e.name}</li>`
          : `<li data-file="${e.path}">${e.kind === "video" ? "🎬" : "🎵"} ${e.name}<span class="size">${bytes(e.size)}</span></li>`
      );
    }
    $("browser-list").innerHTML = rows.join("") || `<li class="up">no media files here</li>`;
    if (!dialog.open) dialog.showModal();
  } catch (e) {
    toast(e.message, true);
  }
}

$("btn-browse").onclick = () => {
  const current = $("input-path").value.trim();
  openBrowser(current ? current.replace(/\/[^/]*$/, "") : "");
};
$("browser-close").onclick = () => $("browser").close();
$("browser-shortcuts").onclick = (e) => {
  const path = e.target.dataset.path;
  if (path) openBrowser(path);
};
$("browser-list").onclick = (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  if (li.dataset.dir) return openBrowser(li.dataset.dir);
  if (li.dataset.file) {
    $("input-path").value = li.dataset.file;
    $("browser").close();
    probeInput();
  }
};

/* ─────────────────────────────────────────────────────────────── probe */

async function probeInput() {
  const path = $("input-path").value.trim();
  if (!path) return;
  const strip = $("media-info");
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(path)}`);
    const cells = [
      ["duration", clock(info.duration)],
      ["video", info.has_video ? `${info.width}×${info.height} ${info.video_codec}` : "—"],
      ["fps", info.fps ? info.fps.toFixed(2) : "—"],
      ["audio", info.has_audio ? `${info.audio_codec} ${info.audio_sample_rate}Hz ${info.audio_channels}ch` : "none"],
    ];
    strip.innerHTML = cells.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
    strip.classList.remove("hidden");
    if (!info.has_audio) toast("this file has no audio track", true);
  } catch (e) {
    strip.classList.add("hidden");
    toast(e.message, true);
  }
}
$("btn-probe").onclick = probeInput;
$("input-path").addEventListener("change", probeInput);

/* ─────────────────────────────────────────────────────────────── options */

$("btn-toggle-adv").onclick = () => {
  const advanced = $("advanced");
  advanced.classList.toggle("hidden");
  $("btn-toggle-adv").textContent = advanced.classList.contains("hidden") ? "advanced ▾" : "advanced ▴";
};
document.querySelectorAll(".segmented").forEach((group) => {
  group.onclick = (e) => {
    if (e.target.tagName !== "BUTTON") return;
    [...group.children].forEach((b) => b.classList.toggle("on", b === e.target));
    if (group.id === "opt-provider") {
      syncCloneToggle();
      loadVoices();  // the two providers have entirely different voice catalogues
    }
  };
});

/** Cloning is a Cartesia capability; Aura only offers its fixed catalogue. */
function syncCloneToggle() {
  const isCartesia = provider() === "cartesia";
  const clone = $("opt-clone");
  clone.disabled = !isCartesia;
  if (!isCartesia) clone.checked = false;
  $("clone-note").textContent = isCartesia
    ? "(uses ~15 s of the recording)"
    : "(Cartesia only — Aura has a fixed voice catalogue)";
}
$("btn-reload-voices").onclick = loadVoices;

/** Always show the literal id being sent to the API — it's what --voice-id takes and what
 *  report.json records, and a friendly display name alone leaves you guessing. */
function showVoiceId() {
  const select = $("opt-voice");
  $("voice-id").textContent = select.value || "—";
}
$("opt-voice").addEventListener("change", showVoiceId);

/* ─────────────────────────────────────────────────────────────── voice preview */

/** Speak one line in the currently selected voice. Empty text → the stock sample sentence.
 *  Cheap enough to use as a preflight check before committing to a whole video. */
async function speakPreview(text, button) {
  const target = button || $("btn-preview");
  const label = target.textContent;
  target.disabled = true;
  target.textContent = "…";
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ options: collectOptions(), text }),
    });
    if (!res.ok) {
      let detail = `${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    const player = $("preview-audio");
    if (player.src) URL.revokeObjectURL(player.src);
    player.src = URL.createObjectURL(await res.blob());
    player.classList.remove("hidden");
    await player.play().catch(() => {});
    showRunError("");
  } catch (e) {
    toast(`preview failed — ${e.message}`, true);
    showRunError(e.message);
  } finally {
    target.disabled = false;
    target.textContent = label;
  }
}

$("btn-preview").onclick = () => speakPreview("");

function showRunError(message) {
  const banner = $("run-error");
  if (!message) return banner.classList.add("hidden");
  const credits = /credit|402|upgrade|subscription/i.test(message);
  banner.innerHTML =
    `<strong>${credits ? "Cartesia is out of credits" : "Run failed"}</strong>` +
    `<span>${escapeHtml(message)}</span>` +
    (credits
      ? `<span class="muted">Top up or enable overages at <a href="https://play.cartesia.ai/subscription" target="_blank" rel="noreferrer">play.cartesia.ai/subscription</a>, then re-run — segments already synthesized are cached and won't be charged again.</span>`
      : "");
  banner.classList.remove("hidden");
}

function collectOptions() {
  return {
    tts_provider: provider(),
    voice_id: $("opt-voice").value,
    language: $("opt-language").value.trim() || "en",
    tts_model: $("opt-tts-model").value.trim(),
    stt_model: $("opt-stt-model").value.trim(),
    fit_mode: $("opt-fit-mode").querySelector("button.on")?.dataset.value || "natural",
    max_tempo: $("opt-max-tempo").value,
    min_tempo: $("opt-min-tempo").value,
    utt_split: $("opt-utt-split").value,
    merge_gap: $("opt-merge-gap").value,
    room_tone: $("opt-room-tone").value,
    max_segment_chars: $("opt-max-chars").value,
    workers: $("opt-workers").value,
    sample_rate: $("opt-sample-rate").value,
    audio_codec: $("opt-audio-codec").value,
    adaptive_retry: $("opt-adaptive").checked,
    filler_words: $("opt-filler").checked,
    diarize: $("opt-diarize").checked,
    keep_original_track: $("opt-keep-original").checked,
  };
}

/* ─────────────────────────────────────────────────────────────── run */

async function startJob(full) {
  const path = $("input-path").value.trim();
  if (!path) return toast("pick a local file first", true);

  $("btn-transcribe").disabled = $("btn-runall").disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        input_path: path,
        output_path: $("opt-output").value.trim(),
        options: collectOptions(),
        full,
        clone: $("opt-clone").checked,
      }),
    });
    state.jobId = job.id;
    state.dirty.clear();
    showRunError("");
    $("job-id").textContent = job.id;
    $("pipeline").classList.remove("hidden");
    $("btn-reset").classList.remove("hidden");
    $("card-transcript").classList.add("hidden");
    $("card-result").classList.add("hidden");
    startPolling();
  } catch (e) {
    toast(e.message, true);
    $("btn-transcribe").disabled = $("btn-runall").disabled = false;
  }
}

$("btn-transcribe").onclick = () => startJob(false);
$("btn-runall").onclick = () => startJob(true);
$("btn-reset").onclick = () => {
  stopPolling();
  state.jobId = null;
  state.segments = [];
  state.dirty.clear();
  $("pipeline").classList.add("hidden");
  $("card-transcript").classList.add("hidden");
  $("card-result").classList.add("hidden");
  $("btn-reset").classList.add("hidden");
  $("job-id").textContent = "";
  $("btn-transcribe").disabled = $("btn-runall").disabled = false;
};

function startPolling() {
  stopPolling();
  poll();
  state.poller = setInterval(poll, 800);
}
function stopPolling() {
  if (state.poller) clearInterval(state.poller);
  state.poller = null;
}

async function poll() {
  if (!state.jobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch (e) {
    stopPolling();
    return toast(e.message, true);
  }
  renderPipeline(job);

  if (job.status === state.status) return;
  const previous = state.status;
  state.status = job.status;

  if (job.status === "failed") {
    stopPolling();
    toast(job.error || "job failed", true);
    showRunError(job.error || "job failed");
    $("btn-transcribe").disabled = $("btn-runall").disabled = false;
    $("btn-synthesize").disabled = false;
    state.smoothing = false;
    $("btn-smooth").disabled = false;
    $("btn-smooth").textContent = "✨ Auto-smooth";
    if (!state.segments.length) await loadTranscript().catch(() => {});
  } else if (job.status === "needs_review") {
    stopPolling();
    await loadTranscript();
    $("btn-transcribe").disabled = $("btn-runall").disabled = false;
    if (state.smoothing) {
      state.smoothing = false;
      $("btn-smooth").disabled = false;
      $("btn-smooth").textContent = "✨ Auto-smooth";
      renderSmoothEdits(job);
      toast(job.message || "smoothed");
    } else {
      toast("transcript ready — edit it, then synthesize");
    }
  } else if (job.status === "completed") {
    stopPolling();
    if (previous !== "completed") {
      if (!state.segments.length) await loadTranscript();
      await renderResult(job);
      toast(job.message || "done");
    }
    $("btn-transcribe").disabled = $("btn-runall").disabled = false;
    $("btn-synthesize").disabled = false;
  }
}

function renderPipeline(job) {
  if (job.stage === "smooth") {
    // Not one of the five pipeline stages — it edits the transcript between 2 and 3.
    $("bar-fill").style.width = `${((job.progress || 0) * 100).toFixed(1)}%`;
    $("stage-msg").textContent = `smoothing — ${job.message || ""}`;
    $("log").textContent = (job.log || []).map((l) => `${l.t.slice(11)}  ${l.stage.padEnd(11)} ${l.message}`).join("\n");
    return;
  }
  const index = STAGES.indexOf(job.stage);
  document.querySelectorAll(".stage").forEach((el, i) => {
    el.classList.toggle("active", i === index && job.status === "running");
    el.classList.toggle("done", index > i || job.status === "completed");
  });
  const span = 1 / STAGES.length;
  const overall = job.status === "completed" ? 1 : Math.max(0, index) * span + (job.progress || 0) * span;
  $("bar-fill").style.width = `${(overall * 100).toFixed(1)}%`;
  $("stage-msg").textContent = `${job.stage || "…"} — ${job.message || ""}`;
  $("log").textContent = (job.log || []).map((l) => `${l.t.slice(11)}  ${l.stage.padEnd(11)} ${l.message}`).join("\n");
  const log = $("log");
  if (!log.classList.contains("hidden")) log.scrollTop = log.scrollHeight;
}

$("btn-toggle-log").onclick = () => {
  const log = $("log");
  log.classList.toggle("hidden");
  $("btn-toggle-log").textContent = log.classList.contains("hidden") ? "log ▾" : "log ▴";
  log.scrollTop = log.scrollHeight;
};

/* ─────────────────────────────────────────────────────────────── transcript */

async function loadTranscript() {
  const data = await api(`/api/jobs/${state.jobId}/transcript`);
  state.segments = data.segments;
  state.duration = data.duration;
  state.dirty.clear();
  updateStats(data.stats);
  $("dl-json").href = `/api/jobs/${state.jobId}/file/transcript?download=true`;
  $("dl-srt").href = `/api/jobs/${state.jobId}/file/srt?download=true`;
  // Reveal before rendering: inside a display:none subtree every measurement reads 0, so
  // the textarea auto-sizing would collapse every row to zero height.
  $("card-transcript").classList.remove("hidden");
  renderSegments();
  $("card-transcript").scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Segment counts double as the cost estimate: one TTS call per spoken segment. */
function updateStats(stats) {
  if (stats) state.stats = stats;
  const s = state.stats || {};
  const spoken = state.segments.filter((seg) => !seg.skip && seg.text.trim());
  const chars = spoken.reduce((n, seg) => n + seg.text.length, 0);
  $("transcript-stats").textContent =
    `${state.segments.length} segments · ${s.speech_seconds ?? "?"}s speech · ` +
    `${s.silence_seconds ?? "?"}s silence · total ${clock(state.duration)} · ` +
    `${spoken.length} TTS calls, ~${chars.toLocaleString()} chars`;
}

function pauseAfter(i) {
  const seg = state.segments[i];
  const next = state.segments[i + 1];
  return Math.max(0, (next ? next.start : state.duration) - seg.end);
}

function renderSegments() {
  const filter = $("segment-filter").value.trim().toLowerCase();
  const body = $("segments").querySelector("tbody");
  body.innerHTML = state.segments
    .map((seg, i) => {
      if (filter && !seg.text.toLowerCase().includes(filter)) return "";
      const slot = Math.max(0, seg.end - seg.start);
      return `<tr data-i="${i}" class="${seg.skip ? "skipped" : ""} ${state.dirty.has(i) ? "dirty" : ""}">
        <td class="num">${seg.index}</td>
        <td class="num"><input class="tiny" type="number" step="0.01" data-field="start" value="${seg.start.toFixed(2)}"></td>
        <td class="num"><input class="tiny" type="number" step="0.01" min="0" data-field="slot" value="${slot.toFixed(2)}"></td>
        <td class="num pause">${pauseAfter(i).toFixed(2)}s</td>
        <td><textarea data-field="text" rows="1">${escapeHtml(seg.text)}</textarea></td>
        <td><input type="checkbox" data-field="skip" ${seg.skip ? "checked" : ""}></td>
        <td><button class="btn link play" data-play="${seg.index}" title="hear this line in the chosen voice">▶</button></td>
        <td class="rowops">
          <button class="btn link" data-merge="${seg.index}" ${i === 0 ? "disabled" : ""}
                  title="merge into the segment above (one TTS call, no seam)">⇧</button>
          <button class="btn link del" data-del="${seg.index}" title="delete — the slot stays silent">✕</button>
        </td>
      </tr>`;
    })
    .join("");
  // Second pass once column widths settle. rAF doesn't fire in a backgrounded tab, so this
  // is a refinement, not the thing correctness depends on — the caller reveals the card first.
  body.querySelectorAll("textarea").forEach(autoGrow);
  requestAnimationFrame(() => body.querySelectorAll("textarea").forEach(autoGrow));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function autoGrow(el) {
  el.style.height = "auto";
  // box-sizing is border-box globally, so `height` covers the border too — scrollHeight
  // already includes padding but not the border, hence the delta.
  const border = el.offsetHeight - el.clientHeight;
  el.style.height = `${el.scrollHeight + border}px`;
}

$("segments").addEventListener("input", (e) => {
  const row = e.target.closest("tr");
  if (!row) return;
  const i = +row.dataset.i;
  const seg = state.segments[i];
  const field = e.target.dataset.field;
  if (field === "text") { seg.text = e.target.value; autoGrow(e.target); updateStats(); }
  else if (field === "skip") {
    seg.skip = e.target.checked;
    row.classList.toggle("skipped", seg.skip);
    updateStats();
  }
  else if (field === "start") {
    const slot = seg.end - seg.start;
    seg.start = +e.target.value || 0;
    seg.end = seg.start + slot;
  } else if (field === "slot") {
    seg.end = seg.start + Math.max(0, +e.target.value || 0);
  }
  state.dirty.add(i);
  row.classList.add("dirty");
  // pauses are derived from neighbours, so refresh the two cells that can change
  [i - 1, i].forEach((j) => {
    const cell = $("segments").querySelector(`tr[data-i="${j}"] .pause`);
    if (cell && state.segments[j]) cell.textContent = `${pauseAfter(j).toFixed(2)}s`;
  });
});
$("segment-filter").oninput = renderSegments;

/** Fuse a segment into the one above: one slot, one TTS call, no seam between them. */
function mergeUp(index) {
  const at = state.segments.findIndex((s) => s.index === index);
  if (at < 1) return;
  const above = state.segments[at - 1];
  const seg = state.segments[at];
  above.end = Math.max(above.end, seg.end);
  above.text = [above.text, seg.text].filter((t) => t.trim()).join(" ").trim();
  above.skip = above.skip && seg.skip;
  state.segments.splice(at, 1);
  reindex();
}

function deleteSegment(index) {
  const at = state.segments.findIndex((s) => s.index === index);
  if (at < 0) return;
  state.segments.splice(at, 1);
  reindex();
}

/** Renumber after a structural edit, mark everything dirty, redraw. */
function reindex() {
  state.segments.forEach((s, i) => (s.index = i));
  state.dirty = new Set(state.segments.map((_, i) => i));
  updateStats();
  renderSegments();
}

$("btn-automerge").onclick = () => {
  const gap = Number(state.defaults.merge_gap ?? 0.18);
  let fused = 0;
  for (let i = state.segments.length - 1; i > 0; i--) {
    if (state.segments[i].start - state.segments[i - 1].end <= gap) {
      mergeUp(state.segments[i].index);
      fused++;
    }
  }
  toast(fused ? `fused ${fused} segments — remember to Save edits` : "nothing close enough to merge");
};

$("segments").addEventListener("click", async (e) => {
  const merge = e.target.closest("button[data-merge]");
  if (merge) return mergeUp(+merge.dataset.merge);
  const del = e.target.closest("button[data-del]");
  if (del) return deleteSegment(+del.dataset.del);

  const button = e.target.closest("button[data-play]");
  if (!button) return;
  // Preview the text as currently edited, not what's on disk.
  const index = +button.dataset.play;
  const seg = state.segments.find((s) => s.index === index);
  if (!seg || !seg.text.trim()) return toast("nothing to speak in that segment", true);
  await speakPreview(seg.text, button);  // previews the edited text, not what's on disk
});

/** Show what the agent actually changed, so the pass is reviewable rather than a black box. */
function renderSmoothEdits(job) {
  const panel = $("smooth-report");
  const edits = job.smooth_edits || [];
  const s = job.smooth_summary || {};
  if (!edits.length) {
    panel.innerHTML = `<strong>Auto-smooth</strong><span>No changes proposed — the transcript already reads cleanly.</span>`;
    panel.classList.remove("hidden");
    return;
  }
  const line = (e) => {
    if (e.op === "merge") return `<li><b>merged ${e.first}–${e.last}</b> — ${escapeHtml(e.reason || "split sentence")}</li>`;
    if (e.op === "delete") return `<li><b>deleted #${e.index}</b> — ${escapeHtml(e.reason || "duplicate")}</li>`;
    return `<li><b>#${e.index}</b> <s>${escapeHtml(e.before || "")}</s> → ${escapeHtml(e.text || "")}</li>`;
  };
  panel.innerHTML =
    `<strong>Auto-smooth · ${escapeHtml(s.model || "")}</strong>` +
    `<span>${s.merges || 0} merged · ${s.rewrites || 0} rewritten · ${s.deletes || 0} deleted → ` +
    `${s.segments_after || state.segments.length} segments. Review below, then Save edits or Synthesize.</span>` +
    `<ul>${edits.map(line).join("")}</ul>`;
  panel.classList.remove("hidden");
}

async function saveTranscript() {
  const payload = { segments: state.segments };
  const data = await api(`/api/jobs/${state.jobId}/transcript`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
  state.segments = data.segments;
  state.duration = data.duration;
  state.dirty.clear();
  renderSegments();
  return data;
}

$("btn-save-transcript").onclick = async () => {
  try {
    await saveTranscript();
    toast("transcript saved");
  } catch (e) {
    toast(e.message, true);
  }
};

$("btn-smooth").onclick = async () => {
  const button = $("btn-smooth");
  button.disabled = true;
  button.textContent = "✨ smoothing…";
  try {
    // send the pending edits along so the agent works on what's on screen
    await api(`/api/jobs/${state.jobId}/smooth`, {
      method: "POST",
      body: JSON.stringify({ segments: state.segments }),
    });
    state.status = "running";       // force poll() to notice the transition back
    state.smoothing = true;
    startPolling();
  } catch (e) {
    toast(e.message, true);
    showRunError(e.message);
    button.disabled = false;
    button.textContent = "✨ Auto-smooth";
  }
};

$("btn-synthesize").onclick = async () => {
  $("btn-synthesize").disabled = true;
  try {
    await api(`/api/jobs/${state.jobId}/synthesize`, {
      method: "POST",
      body: JSON.stringify({ segments: state.segments, options: collectOptions() }),
    });
    state.dirty.clear();
    state.status = "running";
    $("card-result").classList.add("hidden");
    startPolling();
  } catch (e) {
    toast(e.message, true);
    $("btn-synthesize").disabled = false;
  }
};

/* ─────────────────────────────────────────────────────────────── result */

async function renderResult(job) {
  const s = job.summary || {};
  const stamp = Date.now();
  const deltaMs = Math.abs((s.length_delta || 0) * 1000);
  const driftMs = (s.max_drift || 0) * 1000;

  $("result-path").textContent = job.output_path;
  $("tiles").innerHTML = [
    ["output length", clock(s.duration), "good"],
    ["vs source", `${(s.length_delta || 0) * 1000 >= 0 ? "+" : ""}${((s.length_delta || 0) * 1000).toFixed(0)} ms`, deltaMs < 50 ? "good" : "warn"],
    ["max drift", `${driftMs.toFixed(0)} ms`, driftMs < 120 ? "good" : "warn"],
    ["segments spoken", `${s.spoken || 0}/${s.segments || 0}`, s.failed ? "bad" : "good"],
    ["time-stretched", `${s.stretched || 0}`, ""],
    ["max tempo", `${(s.max_tempo || 1).toFixed(2)}×`, (s.max_tempo || 1) > 1.5 ? "warn" : ""],
    ["TTS calls", `${s.api_calls || 0}`, ""],
    ["took", `${(s.seconds || 0).toFixed(0)}s`, ""],
  ]
    .map(([label, value, cls]) => `<div class="tile ${cls}"><span>${label}</span><b>${value}</b></div>`)
    .join("");

  $("v-original").src = `/api/jobs/${job.id}/file/source`;
  $("v-revoiced").src = `/api/jobs/${job.id}/file/output?v=${stamp}`;
  $("dl-output").href = `/api/jobs/${job.id}/file/output?download=true`;
  $("dl-track").href = `/api/jobs/${job.id}/file/track?download=true`;
  $("dl-report").href = `/api/jobs/${job.id}/file/report?download=true`;

  try {
    const report = await api(`/api/jobs/${job.id}/file/report`);
    // Say which voice actually produced this file — you can't tell from the video.
    if (report.voice?.voice_id) {
      const { provider, voice_id } = report.voice;
      $("result-path").innerHTML =
        `${escapeHtml(job.output_path)} · <b>${escapeHtml(provider)}</b> ${escapeHtml(voice_id)}` +
        (report.room_tone ? ` · room tone: ${escapeHtml(report.room_tone)}` : "");
    }
    const worst = Math.max(0.001, ...report.segments.map((r) => Math.abs(r.drift)));
    $("timing").querySelector("tbody").innerHTML = report.segments
      .map((r) => {
        const d = Math.abs(r.drift);
        const cls = r.error ? "bad" : d > 0.25 ? "bad" : d > 0.1 ? "warn" : "";
        return `<tr class="${r.error ? "failed" : ""} ${r.skipped ? "skipped" : ""}">
          <td class="num">${r.index}</td>
          <td class="num">${r.start.toFixed(2)}</td>
          <td class="num">${r.target.toFixed(2)}</td>
          <td class="num">${r.tts_seconds.toFixed(2)}</td>
          <td class="num">${r.tempo.toFixed(2)}×</td>
          <td class="num">${r.final_seconds.toFixed(2)}</td>
          <td class="num"><span class="drift ${cls}"><i style="width:${(d / worst) * 34 + 2}px"></i>${(r.drift * 1000).toFixed(0)}ms</span></td>
          <td>${escapeHtml(r.error || r.text).slice(0, 110)}</td>
        </tr>`;
      })
      .join("");
  } catch { /* report is optional */ }

  $("card-result").classList.remove("hidden");
  $("card-result").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("btn-toggle-timing").onclick = () => {
  const wrap = $("timing-wrap");
  wrap.classList.toggle("hidden");
  $("btn-toggle-timing").textContent = wrap.classList.contains("hidden")
    ? "per-segment timing ▾" : "per-segment timing ▴";
};

/* ─────────────────────────────────────────────────────────────── boot */

loadHealth().then(loadVoices);
