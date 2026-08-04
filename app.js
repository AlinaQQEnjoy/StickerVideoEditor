const state = {
  segments: [],
  busy: false,
  page: 1,
  pageSize: 24,
};

const $ = (id) => document.getElementById(id);

function selectedIds() {
  return state.segments.filter((item) => item.selected).map((item) => item.index);
}

function pageCount() {
  return Math.max(1, Math.ceil(state.segments.length / state.pageSize));
}

function visibleSegments() {
  const start = (state.page - 1) * state.pageSize;
  return state.segments.slice(start, start + state.pageSize);
}

function setBusy(isBusy) {
  state.busy = isBusy;
  $("analyzeBtn").disabled = isBusy;
  $("exportBtn").disabled = isBusy || selectedIds().length === 0;
  $("prevPageBtn").disabled = isBusy || state.page <= 1;
  $("nextPageBtn").disabled = isBusy || state.page >= pageCount();
}

function updateCounts() {
  $("countText").textContent = state.segments.length;
  $("selectedText").textContent = selectedIds().length;
  $("pageText").textContent = `\u7b2c ${state.segments.length ? state.page : 0} / ${state.segments.length ? pageCount() : 0} \u9875`;
  $("exportBtn").disabled = state.busy || selectedIds().length === 0;
  $("prevPageBtn").disabled = state.busy || state.page <= 1;
  $("nextPageBtn").disabled = state.busy || state.page >= pageCount();
}

function configureAudio(video) {
  video.muted = false;
  video.defaultMuted = false;
  video.volume = 1;
  video.addEventListener("play", () => {
    video.muted = false;
    video.volume = 1;
  });
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  visibleSegments().forEach((segment) => {
    const card = document.createElement("article");
    card.className = `card${segment.selected ? " selected" : ""}`;

    const video = document.createElement("video");
    video.className = "preview";
    video.src = segment.clip;
    if (segment.thumb) {
      video.poster = segment.thumb;
    }
    video.controls = true;
    video.playsInline = true;
    video.preload = "none";
    configureAudio(video);

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `
      <div class="row">
        <span class="kind">#${segment.index} ${segment.kind}</span>
        <span class="score">${segment.score}</span>
      </div>
      <div class="time">${segment.start} - ${segment.end} / ${segment.duration}s</div>
      <div class="time">\u5cf0\u503c: ${segment.anchor}</div>
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
    label.textContent = "\u4fdd\u7559\u8fd9\u4e2a\u7247\u6bb5";
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
  const response = await fetch("/api/state", { cache: "no-store" });
  const data = await response.json();
  $("statusText").textContent = data.error ? `${data.message} ${data.error}` : data.message;
  $("outputText").textContent = data.finalVideo ? `\u6210\u54c1: ${data.finalVideo}` : `\u8f93\u51fa\u76ee\u5f55: ${data.outputDir || ""}`;
  setBusy(Boolean(data.busy));

  const incoming = data.segments || [];
  const incomingKey = JSON.stringify(incoming.map((x) => `${x.index}:${x.thumb || ""}`));
  const currentKey = JSON.stringify(state.segments.map((x) => `${x.index}:${x.thumb || ""}`));
  if (incomingKey !== currentKey) {
    state.segments = incoming.map((item) => ({ ...item, selected: item.selected !== false }));
    state.page = 1;
    renderGrid();
  } else {
    updateCounts();
  }
}

async function analyze() {
  const payload = {
    inputVideo: $("inputVideo").value,
    thresholdDbfs: Number($("thresholdDbfs").value),
    preRoll: Number($("preRoll").value),
    postRoll: Number($("postRoll").value),
    minGap: Number($("minGap").value),
    maxPeaks: Number($("maxPeaks").value),
  };
  state.segments = [];
  state.page = 1;
  renderGrid();
  $("statusText").textContent = "Starting audio peak analysis...";
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

$("prevPageBtn").addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  renderGrid();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("nextPageBtn").addEventListener("click", () => {
  state.page = Math.min(pageCount(), state.page + 1);
  renderGrid();
  window.scrollTo({ top: 0, behavior: "smooth" });
});

setInterval(refreshState, 3000);
refreshState();
