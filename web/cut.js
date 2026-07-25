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
};

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
    });

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
    renderCuts();
    drawWave();
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

function drawWave() {
  const canvas = $("wave");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !cut.duration) return;

  const dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const x = (t) => (t / cut.duration) * width;
  const mid = height / 2;

  // silence bands, so "where is there actually audio" reads at a glance
  ctx.fillStyle = cssVar("--panel");
  for (const [start, end] of cut.silence) {
    ctx.fillRect(x(start), 0, Math.max(1, x(end) - x(start)), height);
  }

  // the envelope itself, mirrored around the centre line
  const accent = cssVar("--accent");
  ctx.strokeStyle = accent;
  ctx.globalAlpha = 0.85;
  ctx.beginPath();
  for (let px = 0; px < width; px++) {
    const peak = cut.peaks[Math.floor((px / width) * cut.peaks.length)] || 0;
    const half = Math.max(0.5, peak * mid * 0.92);
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
    const left = x(start);
    const w = Math.max(2, x(end) - left);
    ctx.fillStyle = err;
    ctx.globalAlpha = 0.26;
    ctx.fillRect(left, 0, w, height);
    ctx.globalAlpha = 1;
    ctx.fillRect(left, 0, 2, height);
    ctx.fillRect(left + w - 2, 0, 2, height);
  }

  // pending selection
  if (cut.selection) {
    const [start, end] = cut.selection;
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.22;
    ctx.fillRect(x(start), 0, Math.max(2, x(end) - x(start)), height);
    ctx.globalAlpha = 1;
  }

  // playhead
  const video = $("cut-video");
  if (video.currentTime) {
    ctx.fillStyle = cssVar("--accent-2");
    ctx.fillRect(x(video.currentTime), 0, 2, height);
  }
}

window.addEventListener("resize", () => cut.duration && drawWave());
$("cut-video").addEventListener("timeupdate", () => {
  if (!$("tab-cut").classList.contains("hidden")) drawWave();
});

/* ─────────────────────────────────────────────────── drag to select */

let dragging = null;

function timeAt(event) {
  const rect = $("wave").getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  return ratio * cut.duration;
}

$("wave").addEventListener("pointerdown", (e) => {
  if (!cut.duration) return;
  $("wave").setPointerCapture(e.pointerId);
  dragging = { from: timeAt(e), moved: false };
});

$("wave").addEventListener("pointermove", (e) => {
  if (!dragging) return;
  const to = timeAt(e);
  if (Math.abs(to - dragging.from) > cut.duration / 400) dragging.moved = true;
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

$("cut-silences").onclick = () => {
  const threshold = 1.0;   // seconds — shorter gaps are natural speech rhythm, leave them
  const pad = 0.15;        // keep a breath either side so words aren't clipped
  const found = cut.silence.filter(([a, b]) => b - a >= threshold);
  if (!found.length) return toast(`no silences longer than ${threshold}s`);

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
