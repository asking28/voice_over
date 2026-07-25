/* Cut & trim tab — mark sections to remove, see where the audio actually is, export the rest.
   Reuses $/api/toast/clock/escapeHtml and the file browser from app.js. */

const cut = {
  path: "",
  duration: 0,
  peaks: [],
  speech: [],
  silence: [],
  cuts: [],          // [[start, end], …] — the ranges to remove
  history: [],       // snapshots for undo
  selection: null,   // pending [start, end] from a drag
  jobId: null,
  poller: null,
  view: { start: 0, end: 0 },   // visible time window — the zoom state
  viewPeaks: [],                // high-resolution envelope for that window
  viewRange: null,              // the window viewPeaks was computed for
  previewing: false,            // skip-the-cuts playback
};

const MIN_SPAN = 0.4;           // don't zoom past ~half a second across the canvas

/* ───────────────────────────────────────────────────────────────── tabs */

$("tabs").onclick = (e) => {
  const button = e.target.closest("button[data-tab]");
  if (!button) return;
  [...e.currentTarget.children].forEach((b) => b.classList.toggle("on", b === button));
  $("tab-revoice").classList.toggle("hidden", button.dataset.tab !== "revoice");
  $("tab-cut").classList.toggle("hidden", button.dataset.tab !== "cut");
  if (button.dataset.tab === "cut" && cut.duration) drawWave();
};

function showTab(name) {
  $("tabs").querySelector(`button[data-tab="${name}"]`).click();
}

/* ─────────────────────────────────────────────────────────── load & analyze */

$("cut-browse").onclick = () => {
  browserPick = (path) => {
    $("cut-path").value = path;
    analyzeFile();
  };
  const current = $("cut-path").value.trim();
  openBrowser(current ? current.replace(/\/[^/]*$/, "") : "");
};

$("cut-analyze").onclick = () => analyzeFile();

async function analyzeFile() {
  const path = $("cut-path").value.trim();
  if (!path) return toast("pick a local file first", true);

  const button = $("cut-analyze");
  button.disabled = true;
  button.textContent = "analyzing…";
  try {
    const data = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({ path, buckets: 1800 }),
    });
    Object.assign(cut, {
      path,
      duration: data.duration,
      peaks: data.peaks,
      speech: data.speech,
      silence: data.silence,
      cuts: [],
      history: [],
      selection: null,
      view: { start: 0, end: data.duration },
      viewPeaks: [],
      viewRange: null,
    });
    updateSilenceCount();

    const m = data.media;
    $("cut-info").innerHTML = [
      ["duration", clock(data.duration)],
      ["video", m.has_video ? `${m.width}×${m.height} ${m.video_codec}` : "—"],
      ["audio", m.has_audio ? `${m.audio_codec} ${m.audio_sample_rate}Hz` : "none"],
      ["speech", `${data.speech_seconds}s`],
      ["silence", `${data.silence_seconds}s`],
    ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");
    $("cut-info").classList.remove("hidden");

    $("cut-stats").textContent =
      `${data.speech.length} speech regions · ${data.silence.length} silences · ` +
      `${data.speech_seconds}s speech / ${data.silence_seconds}s silence`;

    $("cut-video").src = `/api/media?path=${encodeURIComponent(path)}`;
    $("cut-editor").classList.remove("hidden");
    $("cut-export").classList.remove("hidden");
    $("cut-result").classList.add("hidden");
    $("cut-error").classList.add("hidden");
    setPreview(false);
    renderCuts();
    setView(0, cut.duration);   // also initializes the zoom label + envelope fetch
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Analyze audio";
  }
}

/* ───────────────────────────────────────────────────────────── waveform */

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

/** Envelope value at time t — from the zoomed fetch when it covers t, else the full-file one. */
function sampleEnvelope(t) {
  if (cut.viewRange && cut.viewPeaks.length) {
    const [a, b] = cut.viewRange;
    if (t >= a && t <= b && b > a) {
      const i = Math.floor(((t - a) / (b - a)) * cut.viewPeaks.length);
      return cut.viewPeaks[Math.min(i, cut.viewPeaks.length - 1)] || 0;
    }
  }
  if (!cut.duration || !cut.peaks.length) return 0;
  const i = Math.floor((t / cut.duration) * cut.peaks.length);
  return cut.peaks[Math.min(i, cut.peaks.length - 1)] || 0;
}

/** A tick spacing that gives roughly a dozen labels across the visible span. */
function tickInterval(span) {
  for (const step of [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600]) {
    if (span / step <= 12) return step;
  }
  return 900;
}

function drawWave() {
  const canvas = $("wave");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !cut.duration) return;

  const view = cut.view;
  const span = Math.max(0.001, view.end - view.start);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const RULER = 18;
  const x = (t) => ((t - view.start) / span) * width;
  const body = height - RULER;
  const mid = RULER + body / 2;

  // silence bands, so "where is there actually audio" reads at a glance
  ctx.fillStyle = cssVar("--panel");
  for (const [start, end] of cut.silence) {
    if (end < view.start || start > view.end) continue;
    ctx.fillRect(x(start), RULER, Math.max(1, x(end) - x(start)), body);
  }

  // time ruler
  const step = tickInterval(span);
  ctx.fillStyle = cssVar("--muted");
  ctx.strokeStyle = cssVar("--line");
  ctx.font = "10px ui-monospace, Menlo, monospace";
  ctx.textBaseline = "top";
  for (let t = Math.ceil(view.start / step) * step; t <= view.end; t += step) {
    const px = x(t);
    ctx.globalAlpha = 0.5;
    ctx.beginPath();
    ctx.moveTo(px + 0.5, RULER);
    ctx.lineTo(px + 0.5, height);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillText(clock(t), px + 3, 3);
  }

  // the envelope itself, mirrored around the centre line
  const accent = cssVar("--accent");
  ctx.strokeStyle = accent;
  ctx.globalAlpha = 0.85;
  ctx.beginPath();
  for (let px = 0; px < width; px++) {
    const peak = sampleEnvelope(view.start + (px / width) * span);
    const half = Math.max(0.5, peak * (body / 2) * 0.92);
    ctx.moveTo(px + 0.5, mid - half);
    ctx.lineTo(px + 0.5, mid + half);
  }
  ctx.stroke();
  ctx.globalAlpha = 1;

  // centre line
  ctx.strokeStyle = cssVar("--line");
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(width, mid);
  ctx.stroke();

  // ranges marked for removal
  const err = cssVar("--err");
  for (const [start, end] of cut.cuts) {
    if (end < view.start || start > view.end) continue;
    const left = x(start);
    const w = Math.max(2, x(end) - left);
    ctx.fillStyle = err;
    ctx.globalAlpha = 0.26;
    ctx.fillRect(left, RULER, w, body);
    ctx.globalAlpha = 1;
    ctx.fillRect(left, RULER, 2, body);
    ctx.fillRect(left + w - 2, RULER, 2, body);
  }

  // pending selection
  if (cut.selection) {
    const [start, end] = cut.selection;
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.22;
    ctx.fillRect(x(start), RULER, Math.max(2, x(end) - x(start)), body);
    ctx.globalAlpha = 1;
  }

  // playhead
  const video = $("cut-video");
  const t = video.currentTime;
  if (t >= view.start && t <= view.end) {
    ctx.fillStyle = cssVar("--accent-2");
    ctx.fillRect(x(t), RULER, 2, body);
  }
}

/* ─────────────────────────────────────────────────────────────── zoom & pan */

let envelopeTimer;
let envelopeSeq = 0;

/** Fetch a high-resolution envelope for the visible window. Debounced, and stale replies are
 *  discarded — otherwise a slow response for an old zoom level overwrites a newer one. */
function refreshEnvelope() {
  clearTimeout(envelopeTimer);
  envelopeTimer = setTimeout(async () => {
    const seq = ++envelopeSeq;
    const { start, end } = cut.view;
    try {
      const data = await api("/api/envelope", {
        method: "POST",
        body: JSON.stringify({
          path: cut.path,
          start,
          end,
          buckets: Math.max(400, Math.round($("wave").clientWidth * 2)),
        }),
      });
      if (seq !== envelopeSeq) return;
      cut.viewPeaks = data.peaks;
      cut.viewRange = [data.start, data.end];
      drawWave();
    } catch {
      /* keep drawing from the full-file envelope */
    }
  }, 130);
}

function setView(start, end) {
  const span = Math.max(MIN_SPAN, Math.min(cut.duration, end - start));
  let a = Math.max(0, Math.min(start, cut.duration - span));
  cut.view = { start: a, end: a + span };

  const zoomed = span < cut.duration - 0.01;
  $("zoom-label").textContent = zoomed
    ? `${clock(cut.view.start)} – ${clock(cut.view.end)}  (${(cut.duration / span).toFixed(1)}×)`
    : "whole file";
  const scroll = $("wave-scroll");
  scroll.classList.toggle("hidden", !zoomed);
  if (zoomed) {
    const max = Math.max(0.001, cut.duration - span);
    scroll.value = String(Math.round((cut.view.start / max) * 1000));
  }
  drawWave();
  refreshEnvelope();
}

function zoomAt(factor, focus) {
  const span = cut.view.end - cut.view.start;
  const next = Math.max(MIN_SPAN, Math.min(cut.duration, span * factor));
  const ratio = (focus - cut.view.start) / span;      // keep `focus` under the cursor
  setView(focus - ratio * next, focus - ratio * next + next);
}

$("zoom-in").onclick = () => zoomAt(0.5, (cut.view.start + cut.view.end) / 2);
$("zoom-out").onclick = () => zoomAt(2, (cut.view.start + cut.view.end) / 2);
$("zoom-fit").onclick = () => setView(0, cut.duration);

$("wave-scroll").oninput = (e) => {
  const span = cut.view.end - cut.view.start;
  const max = Math.max(0.001, cut.duration - span);
  const start = (+e.target.value / 1000) * max;
  setView(start, start + span);
};

$("wave").addEventListener(
  "wheel",
  (e) => {
    if (!cut.duration) return;
    e.preventDefault();
    const span = cut.view.end - cut.view.start;
    if (e.shiftKey) {
      const shift = (e.deltaY || e.deltaX) / 400 * span;   // shift-scroll pans
      setView(cut.view.start + shift, cut.view.end + shift);
    } else {
      zoomAt(e.deltaY > 0 ? 1.25 : 0.8, timeAt(e));
    }
  },
  { passive: false }
);

window.addEventListener("resize", () => cut.duration && drawWave());
$("cut-video").addEventListener("timeupdate", () => {
  // Fires ~4×/s and keeps firing in a background tab, unlike rAF.
  if (cut.previewing && !$("cut-video").paused) applySkip();
  if (!$("tab-cut").classList.contains("hidden")) drawWave();
});

/* ─────────────────────────────────────────────────── drag to select */

let dragging = null;

function timeAt(event) {
  const rect = $("wave").getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  return cut.view.start + ratio * (cut.view.end - cut.view.start);
}

$("wave").addEventListener("pointerdown", (e) => {
  if (!cut.duration) return;
  $("wave").setPointerCapture(e.pointerId);
  dragging = { from: timeAt(e), moved: false };
});

$("wave").addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const to = timeAt(e);
  if (Math.abs(to - dragging.from) > (cut.view.end - cut.view.start) / 400) dragging.moved = true;
  if (dragging.moved) {
    cut.selection = [Math.min(dragging.from, to), Math.max(dragging.from, to)];
    updateSelectionLabel();
    drawWave();
  }
});

$("wave").addEventListener("pointerup", (e) => {
  if (!dragging) return;
  if (!dragging.moved) {          // a plain click seeks instead of selecting
    $("cut-video").currentTime = timeAt(e);
    cut.selection = null;
    updateSelectionLabel();
    drawWave();
  }
  dragging = null;
});

function updateSelectionLabel() {
  const label = $("cut-selection");
  if (!cut.selection) {
    label.textContent = "drag across the waveform to select · click to seek";
    $("cut-add").disabled = true;
    return;
  }
  const [start, end] = cut.selection;
  label.textContent = `selected ${clock(start)} → ${clock(end)}  (${(end - start).toFixed(2)}s)`;
  $("cut-add").disabled = false;
}

/* ────────────────────────────────────────────────────── the cut list */

function snapshot() {
  cut.history.push(JSON.stringify(cut.cuts));
  if (cut.history.length > 50) cut.history.shift();
}

/** Collapse overlapping/touching ranges, so the list matches what the backend will do. */
function mergeCuts() {
  cut.cuts.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const span of cut.cuts) {
    const last = merged[merged.length - 1];
    if (last && span[0] <= last[1] + 0.001) {
      last[1] = Math.max(last[1], span[1]);
    } else {
      merged.push([...span]);
    }
  }
  cut.cuts = merged;
}

function addCut(start, end) {
  snapshot();
  cut.cuts.push([start, end]);
  mergeCuts();
  cut.selection = null;
  updateSelectionLabel();
  renderCuts();
  drawWave();
}

$("cut-add").onclick = () => cut.selection && addCut(...cut.selection);

const silenceMin = () => Math.max(0.2, +$("silence-min").value || 1.0);
const silencePad = () => Math.max(0, +$("silence-pad").value || 0);

/** Live count of what "Mark silences" would take, so the thresholds are tunable by eye. */
function updateSilenceCount() {
  if (!cut.duration) return;
  const found = cut.silence.filter(([a, b]) => b - a >= silenceMin());
  const seconds = found.reduce((n, [a, b]) => n + Math.max(0, b - a - 2 * silencePad()), 0);
  $("silence-count").textContent = found.length
    ? `${found.length} match — ${seconds.toFixed(1)}s would be removed`
    : "nothing matches these thresholds";
}

["silence-min", "silence-pad"].forEach((id) => {
  $(id).addEventListener("input", updateSilenceCount);
});

// Changing the noise floor needs ffmpeg to look again — re-detect, then re-count.
$("silence-db").addEventListener("change", async () => {
  if (!cut.path) return;
  try {
    const data = await api("/api/silences", {
      method: "POST",
      body: JSON.stringify({
        path: cut.path,
        noise_db: +$("silence-db").value || -35,
        min_silence: 0.2,          // detect finely; the UI threshold filters afterwards
      }),
    });
    cut.silence = data.silence;
    cut.speech = data.speech;
    updateSilenceCount();
    drawWave();
    toast(`re-detected at ${$("silence-db").value} dB: ${data.silence.length} silences`);
  } catch (e) {
    toast(e.message, true);
  }
});

$("cut-silences").onclick = () => {
  const threshold = silenceMin();
  const pad = silencePad();      // keep a breath either side so words aren't clipped
  const found = cut.silence.filter(([a, b]) => b - a >= threshold);
  if (!found.length) return toast(`no silences longer than ${threshold}s`, true);

  snapshot();
  let added = 0;
  for (const [a, b] of found) {
    const start = a + pad;
    const end = b - pad;
    if (end - start > 0.05) {
      cut.cuts.push([start, end]);
      added++;
    }
  }
  mergeCuts();
  renderCuts();
  drawWave();
  toast(`marked ${added} silence${added === 1 ? "" : "s"} longer than ${threshold}s`);
};

/* ────────────────────────────────────────────── preview with the cuts skipped */

let previewFrame = null;

/** The kept-time equivalent of a source time — what the exported file would show. */
function keptTime(t) {
  let removed = 0;
  for (const [a, b] of cut.cuts) {
    if (b <= t) removed += b - a;
    else if (a < t) removed += t - a;
  }
  return Math.max(0, t - removed);
}

function nextKeptMoment(t) {
  const hit = cut.cuts.find(([a, b]) => t >= a - 0.02 && t < b);
  return hit ? hit[1] : t;
}

/** The actual skip. Returns false once playback has run past the last kept moment.
 *
 *  Driven by rAF while the tab is visible (~16 ms, so the jump is imperceptible) and by the
 *  video's own timeupdate as a fallback — rAF is suspended entirely in a background tab, and
 *  without the fallback switching tabs mid-preview would play straight through the cuts.
 */
function applySkip() {
  const video = $("cut-video");
  const t = video.currentTime;
  const jump = nextKeptMoment(t);
  if (jump !== t) {
    if (jump >= cut.duration - 0.05) {
      video.pause();                   // the tail is cut — stop rather than seek past the end
      setPreview(false);
      toast("preview finished");
      return false;
    }
    video.currentTime = jump;
  }
  // keep the playhead in view when zoomed in
  const span = cut.view.end - cut.view.start;
  if (span < cut.duration - 0.01 && (t < cut.view.start || t > cut.view.end - span * 0.1)) {
    setView(t - span * 0.2, t - span * 0.2 + span);
  }
  const total = cut.duration - cut.cuts.reduce((n, [a, b]) => n + (b - a), 0);
  $("preview-note").textContent = `${clock(keptTime(t))} of ${clock(total)} in the cut version`;
  return true;
}

function previewTick() {
  const video = $("cut-video");
  if (!cut.previewing || video.paused || video.ended) {
    previewFrame = null;
    return;
  }
  if (!applySkip()) {
    previewFrame = null;
    return;
  }
  previewFrame = requestAnimationFrame(previewTick);
}

function setPreview(on) {
  cut.previewing = on;
  const button = $("cut-preview-play");
  button.classList.toggle("playing", on);
  button.textContent = on ? "⏸ Stop preview" : "▶ Preview with cuts skipped";
  if (!on) {
    $("preview-note").textContent =
      "plays the result without exporting — jumps over every marked section";
    if (previewFrame) cancelAnimationFrame(previewFrame);
    previewFrame = null;
  }
}

$("cut-preview-play").onclick = async () => {
  const video = $("cut-video");
  if (cut.previewing) {
    video.pause();
    setPreview(false);
    return;
  }
  if (!cut.cuts.length) return toast("mark at least one section first", true);

  setPreview(true);
  video.currentTime = nextKeptMoment(video.currentTime || 0);
  try {
    await video.play();
  } catch (e) {
    setPreview(false);
    return toast(`couldn't start playback: ${e.message}`, true);
  }
  previewFrame = requestAnimationFrame(previewTick);
};

$("cut-video").addEventListener("pause", () => {
  if (cut.previewing && previewFrame) {
    cancelAnimationFrame(previewFrame);
    previewFrame = null;
  }
});
$("cut-video").addEventListener("play", () => {
  if (cut.previewing && !previewFrame) previewFrame = requestAnimationFrame(previewTick);
});

$("cut-undo").onclick = () => {
  const previous = cut.history.pop();
  if (previous === undefined) return;
  cut.cuts = JSON.parse(previous);
  renderCuts();
  drawWave();
};

$("cut-clear").onclick = () => {
  snapshot();
  cut.cuts = [];
  renderCuts();
  drawWave();
};

function renderCuts() {
  const body = $("cut-list").querySelector("tbody");
  body.innerHTML = cut.cuts
    .map(
      ([start, end], i) => `<tr>
        <td class="num">${i + 1}</td>
        <td class="num">${clock(start)}</td>
        <td class="num">${clock(end)}</td>
        <td class="num">${(end - start).toFixed(2)}s</td>
        <td><button class="btn link" data-play-cut="${i}" title="jump here">▶</button></td>
        <td><button class="btn link del" data-drop-cut="${i}" title="keep this section after all">✕</button></td>
      </tr>`
    )
    .join("") || `<tr><td colspan="6" class="hint" style="padding:14px">No sections marked yet.</td></tr>`;

  $("cut-undo").disabled = !cut.history.length;
  $("cut-clear").disabled = !cut.cuts.length;
  $("cut-run").disabled = !cut.cuts.length;

  const removed = cut.cuts.reduce((n, [a, b]) => n + (b - a), 0);
  $("cut-plan").textContent = cut.cuts.length
    ? `${cut.cuts.length} cut(s) · removing ${removed.toFixed(1)}s · ` +
      `${clock(cut.duration)} → ${clock(cut.duration - removed)}`
    : "nothing marked for removal yet";
}

$("cut-list").onclick = (e) => {
  const play = e.target.closest("button[data-play-cut]");
  if (play) {
    $("cut-video").currentTime = cut.cuts[+play.dataset.playCut][0];
    $("cut-video").play().catch(() => {});
    return;
  }
  const drop = e.target.closest("button[data-drop-cut]");
  if (drop) {
    snapshot();
    cut.cuts.splice(+drop.dataset.dropCut, 1);
    renderCuts();
    drawWave();
  }
};

/* ───────────────────────────────────────────────────────────── export */

$("cut-mode").onclick = (e) => {
  if (e.target.tagName !== "BUTTON") return;
  [...e.currentTarget.children].forEach((b) => b.classList.toggle("on", b === e.target));
};

$("cut-run").onclick = async () => {
  if (!cut.cuts.length) return toast("mark at least one section to remove", true);
  $("cut-run").disabled = true;
  $("cut-error").classList.add("hidden");
  $("cut-progress").classList.remove("hidden");
  $("cut-result").classList.add("hidden");
  try {
    const job = await api("/api/clip", {
      method: "POST",
      body: JSON.stringify({
        path: cut.path,
        cuts: cut.cuts,
        output_path: $("cut-output").value.trim(),
        mode: $("cut-mode").querySelector("button.on")?.dataset.value || "precise",
      }),
    });
    cut.jobId = job.id;
    $("cut-job").textContent = job.id;
    pollCut();
    cut.poller = setInterval(pollCut, 900);
  } catch (e) {
    showCutError(e.message);
    $("cut-run").disabled = false;
  }
};

async function pollCut() {
  if (!cut.jobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${cut.jobId}`);
  } catch (e) {
    clearInterval(cut.poller);
    return showCutError(e.message);
  }
  $("cut-bar").style.width = `${((job.progress || 0) * 100).toFixed(0)}%`;
  $("cut-msg").textContent = job.message || job.stage || "";

  if (job.status === "completed") {
    clearInterval(cut.poller);
    $("cut-run").disabled = false;
    const p = job.cut_plan || {};
    $("cut-tiles").innerHTML = [
      ["source", clock(p.source_duration || cut.duration), ""],
      ["removed", `${(p.removed_seconds || 0).toFixed(1)}s`, "warn"],
      ["final", clock(p.final_duration || 0), "good"],
      ["pieces joined", `${p.pieces || 0}`, ""],
    ].map(([k, v, c]) => `<div class="tile ${c}"><span>${k}</span><b>${v}</b></div>`).join("");
    $("cut-preview").src = `/api/jobs/${job.id}/file/output?v=${Date.now()}`;
    $("cut-download").href = `/api/jobs/${job.id}/file/output?download=true`;
    $("cut-result").classList.remove("hidden");
    $("cut-result").dataset.output = job.output_path;
    toast(job.message || "cut complete");
  } else if (job.status === "failed") {
    clearInterval(cut.poller);
    $("cut-run").disabled = false;
    showCutError(job.error || "cut failed");
  }
}

function showCutError(message) {
  const banner = $("cut-error");
  banner.innerHTML = `<strong>Cut failed</strong><span>${escapeHtml(message)}</span>`;
  banner.classList.remove("hidden");
  toast(message, true);
}

// Hand the trimmed file straight to the re-voicing flow.
$("cut-send").onclick = () => {
  const output = $("cut-result").dataset.output;
  if (!output) return;
  $("input-path").value = output;
  showTab("revoice");
  probeInput();
  toast("loaded the cut video into Re-voice");
};
