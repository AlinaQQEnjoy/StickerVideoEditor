const state = {
  segments: [],
  busy: false,
};

const $ = (id) => document.getElementById(id);

function selectedIds() {
  return state.segments.filter((item) => item.selected).map((item) => item.index);
}

function setBusy(isBusy) {
  state.busy = isBusy;
  $("analyzeBtn").disabled = isBusy;
  $("exportBtn").disabled = isBusy || selectedIds().length === 0;
}

function updateCounts() {
  $("countText").textContent = state.segments.length;
  $("selectedText").textContent = selectedIds().length;
  $("exportBtn").disabled = state.busy || selectedIds().length === 0;
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  state.segments.forEach((segment) => {
    const card = document.createElement("article");
    card.className = `card${segment.selected ? " selected" : ""}`;

    const video = document.createElement("video");
    video.className = "preview";
    video.src = segment.clip;
    video.poster = segment.thumb;
    video.controls = true;
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `
      <div class="row">
        <span class="kind">#${segment.index} ${segment.kind}</span>
        <span class="score">${segment.score}</span>
      </div>
      <div class="time">${segment.start} - ${segment.end} / ${segment.duration}s</div>
      <div class="time">${segment.note}</div>
    `;

    const checkline = document.createElement("label");
    checkline.className = "checkline";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = segment.selected;
    checkbox.addEventListener("change", () => {
      segment.selected = checkbox.checked;
      card.classList.toggle("selected", segment.selected);
      updateCounts();
    });
    const label = document.createElement("span");
    label.textContent = "保留这个片段";
    checkline.append(checkbox, label);
    meta.append(checkline);

    card.append(video, meta);
    grid.append(card);
  });
  updateCounts();
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function refreshState() {
  const response = await fetch("/api/state");
  const data = await response.json();
  $("statusText").textContent = data.error ? `${data.message} ${data.error}` : data.message;
  $("outputText").textContent = data.finalVideo ? `成品: ${data.finalVideo}` : `输出目录: ${data.outputDir || ""}`;
  setBusy(Boolean(data.busy));

  const incoming = data.segments || [];
  if (JSON.stringify(incoming.map((x) => x.index)) !== JSON.stringify(state.segments.map((x) => x.index))) {
    state.segments = incoming.map((item) => ({ ...item, selected: item.selected !== false }));
    renderGrid();
  }
}

async function analyze() {
  const payload = {
    inputVideo: $("inputVideo").value,
    roi: $("roi").value,
    motionThreshold: Number($("motionThreshold").value),
    maxActions: Number($("maxActions").value),
    disableAudio: $("disableAudio").checked,
  };
  state.segments = [];
  renderGrid();
  $("statusText").textContent = "Starting analysis...";
  await postJson("/api/analyze", payload);
  await refreshState();
}

async function exportSelected() {
  const selected = selectedIds();
  $("statusText").textContent = "Starting export...";
  await postJson("/api/export", { selected });
  await refreshState();
}

$("analyzeBtn").addEventListener("click", () => analyze().catch((err) => {
  $("statusText").textContent = err.message;
}));

$("exportBtn").addEventListener("click", () => exportSelected().catch((err) => {
  $("statusText").textContent = err.message;
}));

$("selectAllBtn").addEventListener("click", () => {
  state.segments.forEach((item) => { item.selected = true; });
  renderGrid();
});

$("selectNoneBtn").addEventListener("click", () => {
  state.segments.forEach((item) => { item.selected = false; });
  renderGrid();
});

setInterval(refreshState, 1500);
refreshState();
