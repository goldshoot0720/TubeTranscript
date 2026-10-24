/* TubeTranscript 前端 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  jobId: null,
  data: null,
  player: null,
  playerReady: false,
  cueIdx: -1,
  wordIdx: -1,
  wordSpans: [],
  offset: 0,
  mode: "both",
  size: 1,
  follow: true,
  pollTimer: null,
  tickStarted: false,
  items: [],
  lastScroll: 0,
};

const NO_SPACE = ["ja", "zh", "th", "yue"];

/* ------------------------------------------------------------ 工具 */

const fmtTime = (ms) => {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m % 60).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `請求失敗（${res.status}）`;
    throw new Error(msg);
  }
  return data;
}

/* ------------------------------------------------------------ 初始化 */

function init() {
  const savedKey = localStorage.getItem("tt_key") || "";
  if (savedKey) $("#key").value = savedKey;

  $("#rememberKey").addEventListener("change", (e) => {
    if (!e.target.checked) localStorage.removeItem("tt_key");
  });
  $("#key").addEventListener("input", (e) => {
    if ($("#rememberKey").checked) localStorage.setItem("tt_key", e.target.value.trim());
    setKeyStatus("", "");
  });

  $("#toggleKey").addEventListener("click", () => {
    const el = $("#key");
    el.type = el.type === "password" ? "text" : "password";
  });

  $("#verifyKey").addEventListener("click", verifyKey);
  $("#url").addEventListener("input", () => {
    $("#clearUrl").hidden = !$("#url").value;
  });
  $("#pasteBtn").addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        $("#url").value = text.trim();
        $("#clearUrl").hidden = false;
        $("#url").focus();
      }
    } catch {
      $("#url").focus();
    }
  });
  $("#clearUrl").addEventListener("click", () => {
    $("#url").value = "";
    $("#clearUrl").hidden = true;
    $("#url").focus();
  });

  $("#advToggle").addEventListener("click", () => {
    const body = $("#advBody");
    body.hidden = !body.hidden;
    $("#advToggle").classList.toggle("open", !body.hidden);
  });

  $("#runBtn").addEventListener("click", startJob);
  $("#url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") startJob();
  });

  $("#modeSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b === btn));
    setMode(btn.dataset.mode);
  });

  $("#sizeSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    $$("#sizeSeg button").forEach((b) => b.classList.toggle("on", b === btn));
    state.size = parseFloat(btn.dataset.size);
    $("#subs").style.setProperty("--scale", state.size);
  });

  $("#offset").addEventListener("input", (e) => {
    state.offset = parseFloat(e.target.value);
    $("#offsetVal").textContent = `${state.offset > 0 ? "+" : ""}${state.offset.toFixed(1)}s`;
  });

  $("#fsBtn").addEventListener("click", () => {
    const stage = $("#stage");
    if (document.fullscreenElement) document.exitFullscreen();
    else stage.requestFullscreen?.();
  });

  $("#exportBtns").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn || !state.jobId) return;
    window.location.href = `/api/jobs/${state.jobId}/export?fmt=${btn.dataset.fmt}&mode=${btn.dataset.mode}`;
  });

  $("#copyAll").addEventListener("click", copyAll);
  $("#search").addEventListener("input", debounce(applyFilter, 140));

  $("#historyBtn").addEventListener("click", openDrawer);
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerMask").addEventListener("click", closeDrawer);

  document.addEventListener("keydown", onKey);

  checkHealth();
  loadHistory();

  const m = location.hash.match(/job=([a-f0-9]{6,})/);
  if (m) {
    $("#emptyState").hidden = true;
    showResult(m[1]).catch(() => {
      $("#emptyState").hidden = false;
    });
  }
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function checkHealth() {
  const pill = $("#envPill");
  try {
    const h = await api("/api/health");
    if (h.ytdlp && h.ffmpeg) {
      pill.innerHTML = '<i class="dot"></i>環境就緒';
      pill.className = "pill ok";
    } else {
      pill.innerHTML = `<i class="dot"></i>${h.ytdlp ? "缺少 ffmpeg" : "缺少 yt-dlp"}`;
      pill.className = "pill bad";
    }
  } catch {
    pill.innerHTML = '<i class="dot"></i>服務未連線';
    pill.className = "pill bad";
  }
}

function setKeyStatus(msg, cls) {
  const el = $("#keyStatus");
  el.textContent = msg;
  el.className = `key-status ${cls || ""}`;
}

async function verifyKey() {
  const key = $("#key").value.trim();
  if (!key) return setKeyStatus("請先輸入 Key", "bad");
  setKeyStatus("驗證中…", "");
  try {
    const r = await api("/api/validate-key", {
      method: "POST",
      body: JSON.stringify({ api_key: key }),
    });
    setKeyStatus(r.ok ? "✓ Key 有效" : r.message, r.ok ? "ok" : "bad");
  } catch (err) {
    setKeyStatus(err.message, "bad");
  }
}

/* ------------------------------------------------------------ 建立任務 */

async function startJob() {
  const url = $("#url").value.trim();
  const apiKey = $("#key").value.trim();
  if (!url) return shake($("#url"));
  if (!apiKey) return shake($("#key"));
  if ($("#rememberKey").checked) localStorage.setItem("tt_key", apiKey);

  $("#runBtn").disabled = true;
  $("#runBtn .btn-label").textContent = "處理中";
  $("#runBtn .spinner").hidden = false;
  $("#errorBox").hidden = true;
  $("#result").hidden = true;
  $("#emptyState").hidden = true;
  $("#progressCard").hidden = false;
  document.body.classList.remove("has-result");
  $("#jobTitle").textContent = "準備中…";
  $("#progressPct").textContent = "0%";
  $("#barFill").style.width = "0%";
  $("#logs").innerHTML = "";
  renderSteps(0);

  destroyPlayer();

  try {
    const r = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        url,
        api_key: apiKey,
        src_lang: $("#srcLang").value,
        target_lang: $("#targetLang").value,
        cookies: $("#cookies").value,
      }),
    });
    state.jobId = r.job_id;
    poll();
  } catch (err) {
    finishRun();
    showError(err.message);
  }
}

function shake(el) {
  el.focus();
  el.animate(
    [{ transform: "translateX(0)" }, { transform: "translateX(-5px)" },
     { transform: "translateX(5px)" }, { transform: "translateX(0)" }],
    { duration: 220 }
  );
}

function renderSteps(active) {
  const names = ["解析影片", "下載音訊", "上傳音訊", "語音辨識", "切分字幕", "翻譯字幕"];
  $("#steps").innerHTML = names
    .map((n, i) => {
      const done = i < active;
      const cls = done ? "done" : i === active ? "active" : "";
      return `<li class="step ${cls}"><i class="dot">${done ? "✓" : i + 1}</i><span>${n}</span></li>`;
    })
    .join("");
}

function renderLogs(logs) {
  const box = $("#logs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = logs
    .map((l) => `<div class="log-line ${l.level}"><span class="t">${l.t}</span><span class="m">${escapeHtml(l.msg)}</span></div>`)
    .join("");
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function poll() {
  clearTimeout(state.pollTimer);
  if (!state.jobId) return;
  try {
    const s = await api(`/api/jobs/${state.jobId}`);
    $("#jobTitle").textContent = s.title ? `《${s.title}》` : s.message;
    $("#progressPct").textContent = `${Math.round(s.progress)}%`;
    $("#barFill").style.width = `${s.progress}%`;
    renderSteps(Math.min(s.step, 5));
    renderLogs(s.logs || []);

    if (s.status === "done") {
      finishRun();
      await showResult(state.jobId);
      return;
    }
    if (s.status === "error") {
      finishRun();
      showError(s.error || "處理失敗");
      return;
    }
  } catch (err) {
    finishRun();
    showError(err.message);
    return;
  }
  state.pollTimer = setTimeout(poll, 800);
}

function finishRun() {
  $("#runBtn").disabled = false;
  $("#runBtn .btn-label").textContent = "產生字幕";
  $("#runBtn .spinner").hidden = true;
}

function showError(msg) {
  const box = $("#errorBox");
  box.hidden = false;
  box.textContent = msg;
}

/* ------------------------------------------------------------ 結果 */

async function showResult(jobId) {
  const data = await api(`/api/jobs/${jobId}/result`);
  state.jobId = jobId;
  state.data = data;
  state.cueIdx = -1;
  state.wordIdx = -1;

  const v = data.video;
  $("#rTitle").textContent = v.title || "未命名影片";
  $("#rTitleTrans").textContent = v.title_trans || "";
  $("#rTitleTrans").hidden = !v.title_trans;

  const thumb = $("#rThumb");
  if (v.thumbnail) {
    thumb.src = v.thumbnail;
    thumb.hidden = false;
  } else {
    thumb.hidden = true;
  }
  document.body.classList.add("has-result");

  const langName = { en: "英文", ja: "日文", zh: "中文", ko: "韓文", es: "西班牙文", fr: "法文", de: "德文" };
  const det = (data.lang.detected || "").split("_")[0];
  $("#rMeta").innerHTML = [
    fmtTime((v.duration || 0) * 1000),
    `${data.stats.cues} 句`,
    `${langName[det] || det.toUpperCase()} → ${data.lang.target === "zh-TW" ? "繁中" : "簡中"}`,
    v.uploader,
  ]
    .filter(Boolean)
    .map((x) => `<span>${escapeHtml(x)}</span>`)
    .join("");

  $("#cueCount").textContent = `${data.stats.cues} 句`;
  $("#result").hidden = false;
  $("#progressCard").hidden = true;
  $("#emptyState").hidden = true;

  renderList();
  setMode(state.mode);
  $("#subs").style.setProperty("--scale", state.size);
  $("#subs").hidden = true;
  $("#subOrig").textContent = "";
  $("#subTrans").textContent = "";

  destroyPlayer();
  initPlayer(v);

  $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
  loadHistory();
}

function renderList() {
  const list = $("#cueList");
  const frag = document.createDocumentFragment();
  state.items = [];

  state.data.cues.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "cue";
    el.dataset.i = i;
    el.innerHTML = `<div class="time">${fmtTime(c.start)}</div>
      <div><div class="o">${escapeHtml(c.orig || "")}</div>
      <div class="t">${escapeHtml(c.trans || "")}</div></div>`;
    el.addEventListener("click", () => seekTo(c.start));
    frag.appendChild(el);
    state.items.push(el);
  });
  list.innerHTML = "";
  list.appendChild(frag);
}

function applyFilter() {
  const q = $("#search").value.trim().toLowerCase();
  state.items.forEach((el, i) => {
    if (!q) { el.style.display = ""; return; }
    const c = state.data.cues[i];
    const hit = (c.orig || "").toLowerCase().includes(q) || (c.trans || "").toLowerCase().includes(q);
    el.style.display = hit ? "" : "none";
  });
}

function setMode(mode) {
  state.mode = mode;
  const subs = $("#subs");
  subs.classList.toggle("hide-orig", mode === "trans");
  subs.classList.toggle("hide-trans", mode === "orig");
}

function seekTo(ms) {
  if (state.player && state.playerReady) {
    state.player.seekTo(ms / 1000 + 0.01, true);
    if (state.player.getPlayerState() !== 1) state.player.playVideo();
  } else {
    syncTo(ms);
  }
}

async function copyAll() {
  if (!state.data) return;
  const text = state.data.cues
    .map((c) => `[${fmtTime(c.start)}] ${c.orig}${c.trans ? " / " + c.trans : ""}`)
    .join("\n");
  try {
    await navigator.clipboard.writeText(text);
    const btn = $("#copyAll");
    const old = btn.textContent;
    btn.textContent = "已複製";
    setTimeout(() => (btn.textContent = old), 1400);
  } catch {
    alert("複製失敗，請改用下載 TXT。");
  }
}

/* ------------------------------------------------------------ 播放器 */

function loadYT() {
  return new Promise((resolve, reject) => {
    if (window.YT && window.YT.Player) return resolve(window.YT);
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (prev) prev();
      resolve(window.YT);
    };
    if (!document.querySelector('script[src*="iframe_api"]')) {
      const s = document.createElement("script");
      s.src = "https://www.youtube.com/iframe_api";
      s.onerror = () => reject(new Error("無法載入 YouTube 播放器"));
      document.head.appendChild(s);
    }
    setTimeout(() => {
      if (window.YT && window.YT.Player) resolve(window.YT);
      else reject(new Error("YouTube 播放器載入逾時"));
    }, 9000);
  });
}

async function initPlayer(video) {
  const msg = $("#stageMsg");
  msg.hidden = true;
  $("#subs").hidden = false;

  if (!video.id) {
    msg.hidden = false;
    msg.textContent = "此連結不是 YouTube 影片，無法內嵌播放；字幕仍可瀏覽與下載。";
    return;
  }

  try {
    const YT = await loadYT();
    state.player = new YT.Player("ytplayer", {
      videoId: video.id,
      playerVars: {
        rel: 0,
        modestbranding: 1,
        playsinline: 1,
        enablejsapi: 1,
        origin: window.location.origin,
      },
      events: {
        onReady: () => {
          state.playerReady = true;
          startTick();
        },
        onError: (e) => {
          state.playerReady = false;
          msg.hidden = false;
          msg.innerHTML =
            "這段影片不允許嵌入播放（錯誤 " + e.data + "）。<br>你仍可瀏覽右側字幕，或" +
            `<a href="https://www.youtube.com/watch?v=${video.id}" target="_blank" rel="noopener" style="color:#a5b4fc">在 YouTube 開啟</a>。`;
          $("#subs").hidden = true;
        },
      },
    });
  } catch (err) {
    msg.hidden = false;
    msg.textContent = `${err.message}。字幕列表與下載功能仍可使用。`;
  }
}

function destroyPlayer() {
  if (state.player && state.player.destroy) {
    try { state.player.destroy(); } catch { /* noop */ }
  }
  state.player = null;
  state.playerReady = false;
  $("#ytplayer").innerHTML = "";
}

function startTick() {
  if (state.tickStarted) return;
  state.tickStarted = true;
  const loop = () => {
    if (state.playerReady && state.player && state.player.getCurrentTime) {
      const t = state.player.getCurrentTime() * 1000 + state.offset * 1000;
      syncTo(t);
    }
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);
}

/* ------------------------------------------------------------ 同步字幕 */

function findCue(ms) {
  const cues = state.data.cues;
  let lo = 0, hi = cues.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cues[mid].start <= ms) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

function syncTo(ms) {
  if (!state.data) return;
  const i = findCue(ms);
  const cue = i >= 0 ? state.data.cues[i] : null;
  const visible = cue && ms <= cue.end + 220;

  if (!visible) {
    if (state.cueIdx !== -1) {
      state.cueIdx = -1;
      $("#subOrig").textContent = "";
      $("#subTrans").textContent = "";
      state.wordSpans = [];
      markActive(-1);
    }
    return;
  }

  if (i !== state.cueIdx) {
    state.cueIdx = i;
    state.wordIdx = -1;
    paintCue(cue);
    markActive(i);
  }
  paintWords(ms);
}

function paintCue(cue) {
  const orig = $("#subOrig");
  const trans = $("#subTrans");
  const lang = (state.data.lang.detected || "").split("_")[0];
  const noSpace = NO_SPACE.includes(lang);

  orig.textContent = "";
  state.wordSpans = [];

  if (cue.words && cue.words.length > 1) {
    cue.words.forEach((w, k) => {
      const sp = document.createElement("span");
      sp.className = "w";
      sp.textContent = w.t;
      orig.appendChild(sp);
      state.wordSpans.push(sp);
      if (!noSpace && k < cue.words.length - 1) orig.appendChild(document.createTextNode(" "));
    });
  } else {
    orig.textContent = cue.orig || "";
  }

  trans.textContent = cue.trans || "";
  orig.classList.remove("pop");
  trans.classList.remove("pop");
  void orig.offsetWidth;
  orig.classList.add("pop");
  if (cue.trans) trans.classList.add("pop");
}

function paintWords(ms) {
  if (!state.wordSpans.length) return;
  const words = state.data.cues[state.cueIdx].words;
  let idx = -1;
  for (let k = 0; k < words.length; k++) {
    if (words[k].s <= ms) idx = k;
    else break;
  }
  if (idx === state.wordIdx) return;
  state.wordIdx = idx;
  state.wordSpans.forEach((sp, k) => sp.classList.toggle("on", k <= idx));
}

function markActive(i) {
  state.items.forEach((el, k) => el.classList.toggle("active", k === i));
  if (i < 0 || !state.follow) return;
  const el = state.items[i];
  if (!el) return;
  const list = $("#cueList");
  const top = el.offsetTop - list.offsetTop;
  const view = list.scrollTop;
  if (top < view + 10 || top > view + list.clientHeight - 50) {
    list.scrollTo({ top: top - list.clientHeight / 2 + 24, behavior: "smooth" });
  }
}

/* ------------------------------------------------------------ 快捷鍵 */

function onKey(e) {
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag)) return;
  if ($("#result").hidden) return;

  if (e.code === "Space") {
    e.preventDefault();
    if (!state.playerReady) return;
    const st = state.player.getPlayerState();
    if (st === 1) state.player.pauseVideo();
    else state.player.playVideo();
  } else if (e.key === "ArrowRight") {
    if (state.playerReady) state.player.seekTo(state.player.getCurrentTime() + 5, true);
  } else if (e.key === "ArrowLeft") {
    if (state.playerReady) state.player.seekTo(Math.max(0, state.player.getCurrentTime() - 5), true);
  } else if (e.key === "ArrowDown") {
    const i = Math.min(state.data.cues.length - 1, Math.max(0, state.cueIdx) + 1);
    seekTo(state.data.cues[i].start);
  } else if (e.key === "ArrowUp") {
    const i = Math.max(0, (state.cueIdx < 0 ? 1 : state.cueIdx) - 1);
    seekTo(state.data.cues[i].start);
  }
}

/* ------------------------------------------------------------ 歷史 */

async function loadHistory() {
  try {
    state.history = await api("/api/jobs");
  } catch {
    state.history = [];
  }
  const box = $("#historyList");
  if (!state.history.length) {
    box.innerHTML = '<div class="empty">還沒有完成的紀錄</div>';
    return;
  }
  box.innerHTML = state.history
    .map(
      (h) => `<div class="hist" data-id="${h.id}">
        <img src="${h.thumb || ""}" alt="" loading="lazy">
        <div>
          <div class="h-title">${escapeHtml(h.title || "未命名")}</div>
          <div class="h-meta">${fmtTime((h.duration || 0) * 1000)} · ${h.cues} 句 · ${escapeHtml(h.created || "")}</div>
        </div>
      </div>`
    )
    .join("");
  $$(".hist", box).forEach((el) =>
    el.addEventListener("click", async () => {
      closeDrawer();
      $("#progressCard").hidden = true;
      await showResult(el.dataset.id);
    })
  );
}

function openDrawer() {
  $("#drawer").hidden = false;
  $("#drawerMask").hidden = false;
  loadHistory();
}
function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawerMask").hidden = true;
}

init();
