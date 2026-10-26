"use strict";

// ---- storage (every access guarded: private mode / blocked storage must not break the page)
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
  remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};
const KEY_STORAGE = "interview-evaluator.api-key";
const DRAFT_STORAGE = "interview-evaluator.draft";
const LIVE_STORAGE = "interview-evaluator.live-session";
const TAB_STORAGE = "interview-evaluator.tab";
const RUBRIC_STORAGE = "interview-evaluator.rubric-id";
const UPLOAD_STORAGE = "interview-evaluator.transcription-job";

// Mirrors EvaluationRequest in models.py.
const LIMITS = { jdMin: 20, jdMax: 20000, rubricMax: 10, transcriptMax: 30 };

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) node.append(child);
  return node;
}

// ---- API key
const keyDialog = $("key-dialog");
let keyResolve = null;

function askForKey() {
  $("key-input").value = store.get(KEY_STORAGE) || "";
  keyDialog.showModal();
  return new Promise((resolve) => { keyResolve = resolve; });
}

function closeKeyDialog(value) {
  keyDialog.close();
  if (keyResolve) { keyResolve(value); keyResolve = null; }
}

$("key-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const key = $("key-input").value.trim();
  if (key) store.set(KEY_STORAGE, key);
  closeKeyDialog(key || null);
  if (key) loadRubrics();
});
$("key-cancel").addEventListener("click", () => closeKeyDialog(null));
$("key-clear").addEventListener("click", () => {
  store.remove(KEY_STORAGE);
  $("key-input").value = "";
  closeKeyDialog(null);
});
keyDialog.addEventListener("cancel", () => closeKeyDialog(null));
$("key-button").addEventListener("click", () => askForKey());
$("key-icon").addEventListener("click", () => askForKey());

// Calls the API with the saved key; on a 401 asks for a key once and retries.
async function apiFetch(path, options = {}) {
  const send = () => {
    const headers = { ...(options.headers || {}) };
    const key = store.get(KEY_STORAGE);
    if (key) headers["X-API-Key"] = key;
    return fetch(path, { ...options, headers });
  };
  let res = await send();
  if (res.status === 401 && (await askForKey())) res = await send();
  return res;
}

// ---- Q&A editor
const pairsList = $("pairs");

function addPair(question = "", answer = "") {
  const item = $("pair-template").content.firstElementChild.cloneNode(true);
  item.querySelector(".q").value = question;
  item.querySelector(".a").value = answer;
  item.querySelector(".remove").addEventListener("click", () => { item.remove(); saveDraft(); });
  pairsList.append(item);
  return item;
}

function setPairs(pairs) {
  pairsList.replaceChildren();
  for (const p of pairs) addPair(p.question, p.answer);
}

function readPairs() {
  return [...pairsList.querySelectorAll(".pair")]
    .map((item) => ({
      question: item.querySelector(".q").value.trim(),
      answer: item.querySelector(".a").value.trim(),
    }))
    .filter((p) => p.question || p.answer);
}

$("add-pair").addEventListener("click", () => addPair().querySelector(".q").focus());

// Pure: "Q: a\nmore\nA: b" -> [{question: "a more", answer: "b"}]. Continuation lines join the
// current question or answer; an A: with no open question starts a pair with an empty question.
function parseTranscript(text) {
  const pairs = [];
  let current = null;
  let field = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(/^([QA])\s*:\s*(.*)$/i);
    if (m && m[1].toUpperCase() === "Q") {
      current = { question: m[2], answer: "" };
      pairs.push(current);
      field = "question";
    } else if (m) {
      if (!current || field === "answer") {
        current = { question: "", answer: "" };
        pairs.push(current);
      }
      current.answer = m[2];
      field = "answer";
    } else if (current) {
      current[field] = current[field] ? `${current[field]} ${line}` : line;
    }
  }
  return pairs;
}

$("parse").addEventListener("click", () => {
  const pairs = parseTranscript($("paste").value);
  if (!pairs.length) { showFormError("No Q: / A: lines found in the pasted transcript."); return; }
  showFormError("");
  setPairs(pairs);
  saveDraft();
});

// ---- recording upload: transcribe + segment as a server job, then fill the Q&A editor
const UPLOAD_STATUS = {
  pending: "Queued…",
  transcribing: "Transcribing the recording… long calls take a few minutes.",
  segmenting: "Splitting the conversation into questions and answers…",
};

function showUploadStatus(message, { spinner = false, warn = false } = {}) {
  const box = $("upload-status");
  box.replaceChildren();
  box.hidden = !message;
  box.classList.toggle("warn", warn);
  if (!message) return;
  if (spinner) box.append(el("span", { class: "spinner", "aria-hidden": "true" }));
  box.append(el("span", {}, message));
}

function renderDiarized(utterances, interviewer) {
  $("diarized").replaceChildren(...utterances.map((u) =>
    el("li", {},
      el("span", { class: `speaker${u.speaker === interviewer ? " interviewer" : ""}` },
        `${u.speaker}${u.speaker === interviewer ? " (interviewer)" : ""}:`),
      u.text)));
  $("diarized-box").hidden = !utterances.length;
}

async function pollTranscription(id) {
  $("upload-box").open = true;
  $("upload").disabled = true;
  try {
    for (;;) {
      const res = await apiFetch(`/transcriptions/${encodeURIComponent(id)}`);
      if (res.status === 404) {
        showUploadStatus("This transcription has expired. Upload the recording again.", { warn: true });
        break;
      }
      if (!res.ok) { showUploadStatus(await errorMessage(res), { warn: true }); break; }
      const job = await res.json();
      if (job.status === "done") {
        setPairs(job.qa);
        saveDraft();
        renderDiarized(job.diarized || [], job.interviewer_speaker);
        const dropped = job.dropped_smalltalk ? ` Left out ${job.dropped_smalltalk} small-talk exchange(s).` : "";
        if (job.warning) showUploadStatus(job.warning, { warn: true });
        else showUploadStatus(`Found ${job.qa.length} Q&A pair(s). Review and edit them below, then evaluate.${dropped}`);
        break;
      }
      if (job.status === "error") {
        showUploadStatus(`Transcription failed: ${job.error}`, { warn: true });
        break;
      }
      showUploadStatus(UPLOAD_STATUS[job.status] || "Working…", { spinner: true });
      await new Promise((r) => setTimeout(r, 2000));
    }
  } catch {
    showUploadStatus("Lost contact with the server. Reload the page to resume.", { warn: true });
    return;  // keep the job id so a reload resumes polling
  } finally {
    $("upload").disabled = false;
  }
  store.remove(UPLOAD_STORAGE);
}

$("upload").addEventListener("click", async () => {
  const file = $("audio-file").files[0];
  if (!file) { showUploadStatus("Choose a recording first.", { warn: true }); return; }
  if (file.size > 100 * 1024 * 1024) { showUploadStatus("The recording is larger than 100 MB.", { warn: true }); return; }
  const form = new FormData();
  form.append("file", file);
  showUploadStatus("Uploading…", { spinner: true });
  $("diarized-box").hidden = true;
  let res;
  try {
    res = await apiFetch("/transcriptions", { method: "POST", body: form });
  } catch {
    showUploadStatus("Could not reach the server. Check your connection and try again.", { warn: true });
    return;
  }
  if (!res.ok) { showUploadStatus(await errorMessage(res), { warn: true }); return; }
  const { id } = await res.json();
  store.set(UPLOAD_STORAGE, id);
  pollTranscription(id);
});

// ---- form
function readRequest() {
  return {
    job_description: $("job-description").value.trim(),
    rubric: $("rubric").value.split("\n").map((s) => s.trim()).filter(Boolean),
    transcript: readPairs(),
  };
}

// ---- rubric library (shown only when the server has persistence configured)
const library = { rubrics: [], selected: null };

// Sends the saved rubric's id instead of the competency list when one is picked.
function withRubricSource(body) {
  if (!library.selected) return body;
  const { rubric, ...rest } = body;
  return { ...rest, rubric_id: library.selected };
}

function showRubricError(message) {
  $("rubric-error").textContent = message || "";
  $("rubric-error").hidden = !message;
}

function selectRubric(id) {
  const rubric = library.rubrics.find((r) => r.id === id) || null;
  library.selected = rubric ? rubric.id : null;
  $("rubric-picker").value = library.selected || "";
  $("rubric").readOnly = !!rubric;
  if (rubric) $("rubric").value = rubric.competencies.join("\n");
  $("rubric-save-box").hidden = !!rubric;
  store.set(RUBRIC_STORAGE, library.selected || "");
}

function keyHeaders() {
  const key = store.get(KEY_STORAGE);
  return key ? { "X-API-Key": key } : {};
}

async function loadRubrics(selectId = store.get(RUBRIC_STORAGE)) {
  let res;
  try { res = await fetch("/rubrics", { headers: keyHeaders() }); } catch { return; }
  if (!res.ok) return;  // 503 = no persistence; 401 = no key yet (retried after the key is saved)
  library.rubrics = await res.json();
  $("rubric-picker").replaceChildren(
    el("option", { value: "" }, "Type a new rubric below"),
    ...library.rubrics.map((r) => el("option", { value: r.id }, r.name)));
  $("rubric-library").hidden = false;
  $("rubric-save-box").hidden = false;
  selectRubric(selectId);
}

$("rubric-picker").addEventListener("change", (e) => { showRubricError(""); selectRubric(e.target.value); });

$("rubric-suggest").addEventListener("click", async () => {
  const jd = $("job-description").value.trim();
  if (jd.length < LIMITS.jdMin) { showRubricError("Write the job description first."); return; }
  showRubricError("");
  const res = await apiFetch("/rubrics/suggest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_description: jd, limit: 3 }),
  });
  if (!res.ok) { showRubricError(await errorMessage(res)); return; }
  const ranked = await res.json();
  const hint = $("rubric-hint");
  if (!ranked.length) { hint.textContent = "No saved rubrics yet."; hint.hidden = false; return; }
  selectRubric(ranked[0].rubric.id);
  hint.textContent = "Closest matches: " + ranked
    .map((s) => `${s.rubric.name} (${Math.round(s.similarity * 100)}%)`).join(", ");
  hint.hidden = false;
});

$("rubric-save").addEventListener("click", async () => {
  const req = readRequest();
  const name = $("rubric-name").value.trim();
  if (!name) { showRubricError("Give the rubric a name."); return; }
  if (!req.rubric.length || req.rubric.length > LIMITS.rubricMax) {
    showRubricError(`A rubric needs 1 to ${LIMITS.rubricMax} competencies.`);
    return;
  }
  if (!req.job_description) { showRubricError("Write the job description first (it becomes the role description)."); return; }
  const res = await apiFetch("/rubrics", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, role_description: req.job_description, competencies: req.rubric }),
  });
  if (!res.ok) { showRubricError(await errorMessage(res)); return; }
  showRubricError("");
  $("rubric-name").value = "";
  await loadRubrics((await res.json()).id);
});

function fillForm(req) {
  selectRubric(null);
  $("job-description").value = req.job_description || "";
  $("rubric").value = (req.rubric || []).join("\n");
  setPairs(req.transcript || []);
}

$("load-example").addEventListener("click", async () => {
  const res = await fetch("example.json");
  if (res.ok) { fillForm(await res.json()); saveDraft(); }
});

function validate(req) {
  const jd = req.job_description.length;
  if (jd < LIMITS.jdMin) return `The job description needs at least ${LIMITS.jdMin} characters.`;
  if (jd > LIMITS.jdMax) return `The job description is too long (max ${LIMITS.jdMax} characters).`;
  if (!req.rubric.length) return "Add at least one rubric competency.";
  if (req.rubric.length > LIMITS.rubricMax) return `At most ${LIMITS.rubricMax} rubric competencies (you have ${req.rubric.length}).`;
  if (!req.transcript.length) return "Add at least one question and answer.";
  if (req.transcript.length > LIMITS.transcriptMax) return `At most ${LIMITS.transcriptMax} Q&A pairs (you have ${req.transcript.length}).`;
  const gap = req.transcript.findIndex((p) => !p.question || !p.answer);
  if (gap >= 0) return `Q&A ${gap + 1} is missing its ${req.transcript[gap].question ? "answer" : "question"}.`;
  return null;
}

// ---- draft persistence (survives reloads)
let draftTimer = null;
function saveDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    store.set(DRAFT_STORAGE, JSON.stringify({
      job_description: $("job-description").value,
      rubric: $("rubric").value,
      paste: $("paste").value,
      transcript: [...pairsList.querySelectorAll(".pair")].map((item) => ({
        question: item.querySelector(".q").value,
        answer: item.querySelector(".a").value,
      })),
    }));
  }, 300);
}

function restoreDraft() {
  let draft = null;
  try { draft = JSON.parse(store.get(DRAFT_STORAGE)); } catch { /* corrupt draft */ }
  if (!draft) return false;
  $("job-description").value = draft.job_description || "";
  $("rubric").value = draft.rubric || "";
  $("paste").value = draft.paste || "";
  if (draft.paste) $("paste-box").open = true;
  setPairs(draft.transcript || []);
  return true;
}

$("eval-form").addEventListener("input", saveDraft);

function showFormError(message) {
  const box = $("form-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function setStatus(message, { spinner = false } = {}) {
  const box = $("status");
  box.replaceChildren();
  if (!message) { box.hidden = true; return; }
  if (spinner) box.append(el("span", { class: "spinner", "aria-hidden": "true" }));
  box.append(el("span", {}, message));
  box.hidden = false;
}

async function errorMessage(res) {
  let detail = "";
  try { detail = (await res.json()).detail; } catch { /* not JSON */ }
  if (Array.isArray(detail)) {  // FastAPI validation errors
    detail = detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  }
  switch (true) {
    case res.status === 401:
      return "The API key is missing or wrong. Use the API key button to set it, then try again.";
    case res.status === 422:
      return `The request could not be evaluated: ${detail || "invalid input"}`;
    case res.status === 413 || res.status === 415:
      return detail || `The file was rejected (${res.status}).`;
    case res.status === 429: {
      const wait = res.headers.get("Retry-After");
      return `Too many requests. Try again in ${wait ? `${wait} seconds` : "a minute"}.`;
    }
    case res.status === 502 || res.status === 503:
      // 503 also means "feature not configured" (speech, database, embeddings): show the reason.
      return detail
        ? `Service unavailable (${res.status}): ${detail}`
        : `The service is unavailable right now (${res.status}). Try again shortly.`;
    case res.status >= 500:
      return `Server error (${res.status})${detail ? `: ${detail}` : ""}. Try again or contact the maintainer.`;
    default:
      return `Request failed (${res.status})${detail ? `: ${detail}` : ""}.`;
  }
}

$("eval-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const req = readRequest();
  const invalid = validate(req);
  showFormError(invalid);
  if (invalid) return;
  const button = $("evaluate");
  button.disabled = true;
  setStatus("Evaluating… this usually takes 20–60 seconds.", { spinner: true });
  try {
    const res = await apiFetch("/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withRubricSource(req)),
    });
    if (!res.ok) { setStatus(""); showFormError(await errorMessage(res)); return; }
    setStatus("");
    const result = await res.json();
    renderScorecard(result.evaluation, { result, qaCount: req.transcript.length });
  } catch {
    setStatus("");
    showFormError("Could not reach the server. Check your connection and try again.");
  } finally {
    button.disabled = false;
  }
});

// ---- scorecard
const REC_LABELS = { strong_yes: "Strong yes", yes: "Yes", lean_no: "Lean no", no: "No" };

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  for (const child of children) node.append(child);
  return node;
}

// "Root-cause analysis: breaks a problem..." -> ["Root-cause analysis", "breaks a problem..."]
function splitCompetency(text) {
  const m = text.match(/^(.*?)(?::\s+|\s[-–—]\s)(.+)$/);
  return m ? [m[1].trim(), m[2].trim()] : [text, ""];
}

function listCard(title, items, { dark = false } = {}) {
  const body = items && items.length
    ? el("ul", {}, ...items.map((t) => el("li", {}, t)))
    : el("p", { class: "none" }, "None");
  return el("div", { class: `card list-card${dark ? " dark" : ""}` }, el("h3", {}, title), body);
}

// Pure: the hero numbers for a scorecard.
function scoreStats(ev) {
  const scores = ev.scores.map((s) => s.score);
  const avg = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : 0;
  return {
    avg,
    strong: scores.filter((s) => s >= 4).length,
    adequate: scores.filter((s) => s === 3).length,
    weak: scores.filter((s) => s <= 2).length,
    evidenced: scores.filter((s) => s > 1).length,
    total: scores.length,
  };
}

function heroCard(ev, stats, title, qaCount) {
  const stat = (value, label) => el("div", { class: "hero-stat" },
    el("span", { class: "big" }, value), el("span", { class: "hero-label" }, label));
  const tile = (cls, value, label, range) => el("div", { class: `tile ${cls}`, title: `${label}: ${range}` },
    el("span", { class: "tile-value" }, String(value)), el("span", { class: "tile-label" }, label),
    el("span", { class: "tile-range" }, range));
  return el("div", { class: "card dark hero" },
    el("div", { class: "row-between" },
      el("h2", {}, title),
      el("span", { class: `recommendation rec-${ev.recommendation}` },
        REC_LABELS[ev.recommendation] || ev.recommendation)),
    el("div", { class: "hero-stats" },
      stat(stats.avg.toFixed(1), "average score out of 5"),
      ...(qaCount ? [el("span", { class: "hero-divider", "aria-hidden": "true" }),
        stat(String(qaCount), `Q&A${qaCount === 1 ? "" : "s"} scored`)] : [])),
    el("div", { class: "hero-bar", role: "img", "aria-label": `Average ${stats.avg.toFixed(1)} out of 5` },
      el("span", { style: `width:${(stats.avg / 5) * 100}%` })),
    el("div", { class: "tiles" },
      tile("tile-strong", stats.strong, "Strong", "scored 4–5"),
      tile("tile-adequate", stats.adequate, "Adequate", "scored 3"),
      tile("tile-weak", stats.weak, "Weak", "scored 1–2")));
}

function chartCard(ev) {
  const bars = ev.scores.map((s, i) => {
    const [name] = splitCompetency(s.competency);
    return el("div", { class: "chart-col", title: `${name}: ${s.score}/5` },
      el("span", { class: "chart-value" }, String(s.score)),
      el("div", { class: "chart-track" },
        el("span", { class: "chart-bar", style: `height:${(Math.max(0, Math.min(5, s.score)) / 5) * 100}%` })),
      el("span", { class: "chart-label" }, String(i + 1)));
  });
  const summary = ev.scores.map((s, i) => `${i + 1}. ${splitCompetency(s.competency)[0]}: ${s.score} of 5`).join("; ");
  return el("div", { class: "card gray chart-card" },
    el("h3", {}, "Score by competency"),
    el("div", { class: "chart", role: "img", "aria-label": summary }, ...bars));
}

function ringCard(ev, stats) {
  const pct = stats.total ? stats.evidenced / stats.total : 0;
  const r = 44, c = 2 * Math.PI * r;
  const ring = svg("svg", { viewBox: "0 0 120 120", class: "ring", role: "img",
    "aria-label": `${stats.evidenced} of ${stats.total} competencies have evidence` },
    svg("circle", { cx: 60, cy: 60, r, class: "ring-track" }),
    svg("circle", { cx: 60, cy: 60, r, class: "ring-fill",
      "stroke-dasharray": `${c * pct} ${c}`, transform: "rotate(-90 60 60)" }),
    svg("text", { x: 60, y: 66, "text-anchor": "middle", class: "ring-text" }, `${Math.round(pct * 100)}%`));
  const copy = el("button", { type: "button", class: "ghost" }, "Copy as JSON");
  copy.addEventListener("click", () => copyJson(ev, copy));
  return el("div", { class: "card ring-card" },
    el("h3", {}, "Evidence coverage"),
    el("p", { class: "ring-lead" }, el("b", {}, `${stats.evidenced}/${stats.total}`), " competencies backed by evidence"),
    el("div", { class: "ring-row" },
      el("ul", { class: "legend" },
        el("li", {}, el("span", { class: "dot dot-ink" }), "With evidence"),
        el("li", {}, el("span", { class: "dot dot-light" }), "No evidence")),
      ring),
    el("div", { class: "actions" }, copy));
}

function competencyCard(s, i, { previous, live }) {
  const [name, detail] = splitCompetency(s.competency);
  const before = previous ? previous[s.competency] : undefined;
  const changed = before !== undefined && before !== s.score;
  const value = el("span", { class: "comp-score" }, el("b", {}, String(s.score)), "/5");
  if (changed) {
    value.append(el("span", { class: "delta", "aria-label": `was ${before}` },
      `${s.score > before ? "▲" : "▼"} from ${before}`));
  }
  const title = el("div", { class: "comp-title" },
    el("span", { class: "comp-num" }, String(i + 1)),
    el("div", {}, el("h3", {}, name), ...(detail ? [el("p", { class: "hint" }, detail)] : [])));
  if (live && s.score <= 1) title.append(el("span", { class: "badge" }, "No evidence yet"));
  return el("li", { class: `card comp${changed ? " changed" : ""}` },
    el("div", { class: "comp-head" }, title, value),
    el("div", { class: "bar", role: "img", "aria-label": `${s.score} out of 5` },
      el("span", { style: `width:${Math.max(0, Math.min(5, s.score)) * 20}%` })),
    el("blockquote", { class: "evidence" }, s.evidence),
    el("p", { class: "reasoning" }, s.reasoning));
}

// previous: {competency: score} from the last live update, to highlight movement.
// live: flags score-1 competencies as still needing evidence.
// result: the stored-evaluation envelope {id, calibration_example_ids} for final scorecards.
// qaCount: how many Q&As this scorecard covers (shown in the hero when known).
function renderScorecard(ev, { title = "Scorecard", previous = null, live = false, scroll = true, result = null, qaCount = 0 } = {}) {
  const target = $("result");
  const stats = scoreStats(ev);
  target.replaceChildren(
    el("div", { class: "dash-row" }, heroCard(ev, stats, title, qaCount), chartCard(ev), ringCard(ev, stats)),
    el("div", { class: "card summary-card" }, el("h3", {}, "Summary"), el("p", {}, ev.summary), ...calibrationNote(result)),
    el("ul", { class: "comp-grid" }, ...ev.scores.map((s, i) => competencyCard(s, i, { previous, live }))),
    el("div", { class: "finding-row" },
      listCard("Strengths", ev.strengths, { dark: true }),
      listCard("Concerns", ev.concerns, { dark: true }),
      listCard("Red flags", ev.red_flags, { dark: true })),
    listCard("Follow-up questions", ev.follow_up_questions),
    ...(result && result.id ? [approvePanel(result.id, ev)] : []),
  );
  target.hidden = false;
  if (scroll) target.scrollIntoView({ behavior: "smooth", block: "start" });
}

function calibrationNote(result) {
  const ids = (result && result.calibration_example_ids) || [];
  if (!ids.length) return [];
  const n = ids.length;
  const note = el("p", { class: "hint" },
    `Calibrated against ${n} approved past evaluation${n === 1 ? "" : "s"} for this rubric: `);
  note.append(el("code", {}, ids.map((id) => id.slice(0, 8)).join(", ")));
  note.title = ids.join("\n");
  return [note];
}

// ---- approval: turns a stored evaluation into a calibration example, optionally corrected
function approvePanel(id, ev) {
  const panel = el("div", { class: "card approve" });
  const message = el("p", { class: "hint", role: "status" });
  const post = async (path, body) => {
    const res = await apiFetch(`/evaluations/${encodeURIComponent(id)}/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) { message.textContent = await errorMessage(res); return null; }
    return res.json();
  };

  const showApproved = (stored) => {
    const unapprove = el("button", { type: "button", class: "ghost" }, "Un-approve");
    unapprove.addEventListener("click", async () => {
      if (await post("unapprove")) showIdle();
    });
    const scores = Object.entries(stored.approved_scores || {}).map(([c, s]) => `${c}: ${s}`).join(", ");
    message.textContent = "";
    panel.replaceChildren(
      el("h3", {}, "Calibration"),
      el("p", {}, `Approved as a calibration example (${REC_LABELS[stored.approved_recommendation]}). ${scores}`),
      el("div", { class: "actions" }, unapprove), message);
  };

  const showEditor = () => {
    const rows = ev.scores.map((s) => {
      const select = el("select", { "aria-label": `Approved score for ${s.competency}` },
        ...[1, 2, 3, 4, 5].map((n) => el("option", { value: String(n) }, String(n))));
      select.value = String(s.score);
      return { competency: s.competency, model: s.score, select };
    });
    const rec = el("select", { "aria-label": "Approved recommendation" },
      ...Object.entries(REC_LABELS).map(([v, label]) => el("option", { value: v }, label)));
    rec.value = ev.recommendation;
    const approve = el("button", { type: "button" }, "Approve");
    const cancel = el("button", { type: "button", class: "ghost" }, "Cancel");
    cancel.addEventListener("click", showIdle);
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      const corrections = Object.fromEntries(rows
        .filter((r) => Number(r.select.value) !== r.model)
        .map((r) => [r.competency, Number(r.select.value)]));
      const body = {};
      if (Object.keys(corrections).length) body.scores = corrections;
      if (rec.value !== ev.recommendation) body.recommendation = rec.value;
      const stored = await post("approve", body);
      approve.disabled = false;
      if (stored) showApproved(stored);
    });
    panel.replaceChildren(
      el("h3", {}, "Approve with edits"),
      el("p", { class: "hint" }, "Adjust any score the model got wrong. Approved evaluations anchor future scoring for the same rubric."),
      el("div", { class: "approve-grid" }, ...rows.flatMap((r) => [el("span", {}, r.competency), r.select]),
        el("span", {}, "Recommendation"), rec),
      el("div", { class: "actions" }, cancel, approve), message);
  };

  const showIdle = () => {
    const open = el("button", { type: "button", class: "ghost" }, "Approve (with edits)");
    open.addEventListener("click", showEditor);
    message.textContent = "";
    panel.replaceChildren(el("div", { class: "actions" }, open), message);
  };

  showIdle();
  return panel;
}

function scoreMap(ev) {
  return Object.fromEntries(ev.scores.map((s) => [s.competency, s.score]));
}

async function copyJson(ev, button) {
  const text = JSON.stringify(ev, null, 2);
  try {
    await navigator.clipboard.writeText(text);
  } catch {  // insecure context or permission denied: fall back to a selected textarea
    const area = el("textarea", { readonly: "" });
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy as JSON"; }, 1500);
}

// ---- tabs
function selectTab(name) {
  for (const tab of ["transcript", "live"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`panel-${tab}`).hidden = tab !== name;
  }
  store.set(TAB_STORAGE, name);
}
$("tab-transcript").addEventListener("click", () => selectTab("transcript"));
$("tab-live").addEventListener("click", () => selectTab("live"));

// ---- live interview (session API + SSE)
// shownQa: qa_count of the scorecard on screen; replayed history at or below it is skipped.
const live = { id: null, source: null, previous: null, shownQa: 0, transcript: [], editingLast: false };

function showLiveError(message) {
  const box = $("live-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function renderLiveTranscript(transcript) {
  live.transcript = transcript;
  $("live-transcript").replaceChildren(...transcript.map((qa) =>
    el("li", {}, el("div", { class: "q" }, qa.question), el("div", { class: "a" }, qa.answer))));
  const n = transcript.length;
  $("live-meta").textContent = `Session in progress · ${n} answer${n === 1 ? "" : "s"} logged`;
  $("live-edit-last").hidden = !n;
}

function setEditingLast(on) {
  live.editingLast = on;
  $("live-add").textContent = on ? "Save fix" : "Add answer";
  $("live-edit-last").textContent = on ? "Cancel fix" : "Fix last Q&A";
  if (!on) { $("live-question").value = ""; $("live-answer").value = ""; }
}

$("live-edit-last").addEventListener("click", () => {
  if (live.editingLast) { setEditingLast(false); return; }
  const last = live.transcript[live.transcript.length - 1];
  if (!last) return;
  setEditingLast(true);
  $("live-question").value = last.question;
  $("live-answer").value = last.answer;
  $("live-answer").focus();
});

function setLiveActive(active) {
  $("live-idle").hidden = active;
  $("live-active").hidden = !active;
}

function showLiveScorecard(ev, { title = "Live scorecard", scroll = false, result = null, qaCount = 0 } = {}) {
  renderScorecard(ev, { title, previous: live.previous, live: !result, scroll, result, qaCount });
  live.previous = scoreMap(ev);
}

function closeLive() {
  if (live.source) live.source.close();
  stopMic({ reset: true });
  setEditingLast(false);
  Object.assign(live, { id: null, source: null, previous: null, shownQa: 0, transcript: [] });
  store.remove(LIVE_STORAGE);
  setLiveActive(false);
}

function connectEvents() {
  if (live.source) live.source.close();
  const key = store.get(KEY_STORAGE);
  const url = `/sessions/${encodeURIComponent(live.id)}/events${key ? `?key=${encodeURIComponent(key)}` : ""}`;
  const source = new EventSource(url);
  live.source = source;
  const data = (e) => JSON.parse(e.data);
  source.addEventListener("scoring", (e) => {
    const n = data(e).qa_count;
    if (n > live.shownQa) setStatus(`Scoring ${n} answer${n === 1 ? "" : "s"}…`, { spinner: true });
  });
  source.addEventListener("scorecard", (e) => {
    const d = data(e);
    if (d.qa_count <= live.shownQa) return;
    live.shownQa = d.qa_count;
    setStatus("");
    showLiveError("");
    showLiveScorecard(d.evaluation, { qaCount: d.qa_count });
  });
  source.addEventListener("error", (e) => {
    if (!e.data) return;  // a transport error (handled by onerror), not our `error` event
    const d = data(e);
    setStatus("");
    const retry = d.status === 429 ? " Wait a moment, then add the next answer to re-score." : "";
    showLiveError(`Scoring after answer ${d.qa_count} failed (${d.status}): ${d.detail}.${retry}`);
  });
  source.addEventListener("ended", () => source.close());
  source.onerror = async () => {
    if (source.readyState !== EventSource.CLOSED || live.source !== source) return;
    // The server refused the stream (expired session or bad key): check why.
    const res = await apiFetch(`/sessions/${encodeURIComponent(live.id)}`);
    if (res.status === 404) {
      closeLive();
      setStatus("");
      showLiveError("This live session has expired. Start a new one.");
    } else if (res.ok) {
      connectEvents();
    }
  };
}

async function liveRequest(path, body) {
  return apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

$("live-start").addEventListener("click", async () => {
  const req = readRequest();
  const invalid = validate({ ...req, transcript: [{ question: "-", answer: "-" }] });
  showLiveError(invalid);
  if (invalid) return;
  const button = $("live-start");
  button.disabled = true;
  try {
    const res = await liveRequest("/sessions",
      withRubricSource({ job_description: req.job_description, rubric: req.rubric }));
    if (!res.ok) { showLiveError(await errorMessage(res)); return; }
    live.id = (await res.json()).session_id;
    live.previous = null;
    live.shownQa = 0;
    store.set(LIVE_STORAGE, live.id);
    renderLiveTranscript([]);
    setLiveActive(true);
    $("result").hidden = true;
    connectEvents();
    $("live-question").focus();
  } catch {
    showLiveError("Could not reach the server. Check your connection and try again.");
  } finally {
    button.disabled = false;
  }
});

$("live-add").addEventListener("click", async () => {
  const qa = { question: $("live-question").value.trim(), answer: $("live-answer").value.trim() };
  if (!qa.question || !qa.answer) { showLiveError("Enter both the question and the answer."); return; }
  const button = $("live-add");
  button.disabled = true;
  const fixing = live.editingLast;
  try {
    const path = `/sessions/${encodeURIComponent(live.id)}/qa${fixing ? "/last" : ""}`;
    const res = await apiFetch(path, {
      method: fixing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(qa),
    });
    if (res.status === 404) { closeLive(); showLiveError("This live session has expired. Start a new one."); return; }
    if (!res.ok) { showLiveError(await errorMessage(res)); return; }
    showLiveError("");
    if (fixing) {
      renderLiveTranscript([...live.transcript.slice(0, -1), qa]);
      live.shownQa = Math.min(live.shownQa, live.transcript.length - 1);  // accept the re-score
      setEditingLast(false);
    } else {
      renderLiveTranscript([...live.transcript, qa]);
      $("live-question").value = "";
      $("live-answer").value = "";
    }
    $("live-question").focus();
  } catch {
    showLiveError("Could not reach the server. Your answer was not saved; try again.");
  } finally {
    button.disabled = false;
  }
});

$("live-end").addEventListener("click", async () => {
  const button = $("live-end");
  button.disabled = true;
  setStatus("Finishing the evaluation…", { spinner: true });
  try {
    await flushMic();  // the last spoken exchange counts too
    const res = await liveRequest(`/sessions/${encodeURIComponent(live.id)}/end`);
    setStatus("");
    if (!res.ok && res.status !== 404) { showLiveError(await errorMessage(res)); return; }
    const final = res.ok ? await res.json() : null;
    const qaCount = live.transcript.length;
    closeLive();
    if (final) showLiveScorecard(final.evaluation, { title: "Final scorecard", scroll: true, result: final, qaCount });
    else showLiveError("This live session has expired. Start a new one.");
  } catch {
    setStatus("");
    showLiveError("Could not reach the server. Try ending the interview again.");
  } finally {
    button.disabled = false;
  }
});

// ---- live microphone: Azure Speech in the browser. Two audio setups:
// - "room": one mic, ConversationTranscriber diarizes the voices (Guest-1, Guest-2, ...).
// - "remote": the mic is the interviewer and the shared call tab/screen audio is the candidate,
//   each with its own SpeechRecognizer, so the speaker is known without diarization.
// Finished utterances are buffered; when the interviewer speaks again after the candidate has
// answered, the buffered exchange is sent to /segment, which appends Q&As and re-scores.
const SPEECH_SDK_URL = "https://cdn.jsdelivr.net/npm/microsoft-cognitiveservices-speech-sdk@1.52.0/distrib/browser/microsoft.cognitiveservices.speech.sdk.bundle-min.js";
const MIC_MODE_STORAGE = "interview-evaluator.mic-mode";
const REMOTE_INTERVIEWER = "Interviewer";
const REMOTE_CANDIDATE = "Candidate";
// Tab/system audio capture only works in Chromium browsers (userAgentData is Chromium-only).
const CAN_SHARE_AUDIO = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia && navigator.userAgentData);
const mic = {
  recognizers: [], streams: [], tokenTimer: null, known: null,
  buffer: [], interviewer: null, sending: Promise.resolve(), finals: [], interim: {},
};
const micOn = () => mic.recognizers.length > 0;

function micMode() {
  const checked = document.querySelector('input[name="mic-mode"]:checked');
  return checked ? checked.value : "room";
}

function renderMicMode() {
  const remote = $("mic-mode-remote");
  remote.disabled = !CAN_SHARE_AUDIO;
  const saved = store.get(MIC_MODE_STORAGE);
  const mode = CAN_SHARE_AUDIO && saved !== "room" ? "remote" : "room";
  $(`mic-mode-${mode}`).checked = true;
  updateMicModeHint();
}

function updateMicModeHint() {
  $("mic-mode-hint").textContent = !CAN_SHARE_AUDIO
    ? "Remote call needs Chrome or Edge (this browser can't capture call audio). Use In the room, or upload a recording afterwards."
    : micMode() === "remote"
      ? "Use headphones. When asked, pick the call's tab and tick “Share tab audio” (for the Teams/Zoom desktop app on Windows, share your entire screen with system audio)."
      : "One microphone hears everyone in the room; voices are told apart automatically.";
}

document.querySelectorAll('input[name="mic-mode"]').forEach((input) => input.addEventListener("change", () => {
  store.set(MIC_MODE_STORAGE, micMode());
  updateMicModeHint();
}));

function loadSpeechSdk() {
  if (window.SpeechSDK) return Promise.resolve(window.SpeechSDK);
  return new Promise((resolve, reject) => {
    const script = el("script", { src: SPEECH_SDK_URL });
    script.onload = () => resolve(window.SpeechSDK);
    script.onerror = () => reject(new Error("Could not load the speech SDK. Check your connection."));
    document.head.append(script);
  });
}

async function fetchSpeechToken() {
  const res = await liveRequest("/speech/token");
  if (!res.ok) throw new Error(await errorMessage(res));
  return res.json();
}

function setMicState(text, { warn = false, on = false } = {}) {
  const state = $("mic-state");
  state.textContent = text;
  state.classList.toggle("warn", warn);
  const toggle = $("mic-toggle");
  toggle.setAttribute("aria-pressed", String(on));
  toggle.textContent = on ? "Pause microphone" : (mic.finals.length ? "Resume microphone" : "Start microphone");
}

function renderCaptions() {
  const box = $("captions");
  box.replaceChildren(...mic.finals.slice(-3).map((t) => el("div", {}, t)));
  for (const t of Object.values(mic.interim)) box.append(el("div", { class: "interim" }, t));
  box.hidden = !box.children.length;
  renderSendRow();
}

// "Send exchange now": for when turn detection can't fire (one speaker label) or is slow.
function renderSendRow() {
  const n = mic.buffer.length;
  $("mic-send-row").hidden = !micOn() && !n;
  $("mic-buffered").textContent = n ? `${n} utterance${n === 1 ? "" : "s"} not sent yet` : "Nothing to send yet";
  $("mic-send").disabled = !n;
}

function sendBuffer() {
  if (!mic.buffer.length) return mic.sending;
  const exchange = mic.buffer;
  mic.buffer = [];
  renderSendRow();
  return sendExchange(exchange);
}

$("mic-send").addEventListener("click", () => sendBuffer());

function onUtterance(u, source = u.speaker) {
  mic.finals.push(`${u.speaker}: ${u.text}`);
  delete mic.interim[source];
  renderCaptions();
  // Remote mode knows the interviewer; otherwise, until the server identifies them, assume
  // whoever spoke first is asking.
  const interviewer = mic.known || mic.interviewer || (mic.buffer[0] && mic.buffer[0].speaker) || u.speaker;
  const answered = mic.buffer.some((b) => b.speaker !== interviewer && b.speaker !== "Unknown");
  if (u.speaker === interviewer && answered) {
    const exchange = mic.buffer;
    mic.buffer = [u];
    sendExchange(exchange);
  } else {
    mic.buffer.push(u);
  }
  renderSendRow();
}

// Utterances the server didn't use yet (a trailing unanswered question), or all of them when the
// send failed, go back to the front of the buffer so nothing said is lost.
function keepUnsent(utterances) {
  if (!utterances.length || !live.id) return;
  mic.buffer = [...utterances, ...mic.buffer];
  renderSendRow();
}

function sendExchange(utterances) {
  const sessionId = live.id;
  const body = mic.known ? { utterances, interviewer_speaker: mic.known } : { utterances };
  mic.sending = mic.sending.then(async () => {
    let res;
    try {
      res = await liveRequest(`/sessions/${encodeURIComponent(sessionId)}/segment`, body);
    } catch {
      keepUnsent(utterances);
      showLiveError("Could not send the last exchange to the server. It is kept for the next send.");
      return;
    }
    if (!res.ok) {
      keepUnsent(utterances);
      showLiveError(`The last exchange was not added: ${await errorMessage(res)}`);
      return;
    }
    const d = await res.json();
    if (live.id !== sessionId) return;
    mic.interviewer = d.interviewer_speaker;
    if (d.unanswered_from != null) keepUnsent(utterances.slice(d.unanswered_from));
    if (d.qa.length) renderLiveTranscript([...live.transcript, ...d.qa]);
  });
  return mic.sending;
}

function micDeniedMessage(e) {
  const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
  return denied
    ? "Microphone access was denied. Allow it in the browser's site settings, then try again."
    : "No microphone is available on this device.";
}

async function checkMicPermission() {
  try {  // ask for permission up front so a denial gets a clear message
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
  } catch (e) {
    throw new Error(micDeniedMessage(e));
  }
}

function releaseStreams() {
  mic.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()));
  mic.streams = [];
}

// Remote mode: the call audio is asked for first, because the browser only shows the share
// picker right after the click. The share's video is dropped; only its audio is transcribed.
async function captureRemote() {
  let share;
  try {
    share = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
  } catch (e) {
    throw new Error(e && e.name === "NotAllowedError"
      ? "The call audio wasn't shared. Start again and pick the call's tab."
      : "This browser could not capture the call audio.");
  }
  mic.streams.push(share);
  share.getVideoTracks().forEach((t) => t.stop());
  const callAudio = share.getAudioTracks();
  if (!callAudio.length) {
    throw new Error("The share has no audio. Start again, pick the call's tab and tick “Share tab audio”.");
  }
  let voice;
  try {
    voice = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    throw new Error(micDeniedMessage(e));
  }
  mic.streams.push(voice);
  callAudio[0].addEventListener("ended", () => {  // "Stop sharing" in the browser's bar
    if (!micOn()) return;
    stopMic();
    setMicState("Call audio sharing stopped, so the microphone is paused. Resume to share it again.", { warn: true });
  });
  return { voice, call: new MediaStream(callAudio) };
}

function onCanceled(sdk) {
  return (_, e) => {
    if (e.reason !== sdk.CancellationReason.Error || !micOn()) return;
    stopMic();
    setMicState(`Speech service error: ${e.errorDetails}`, { warn: true });
  };
}

// One known speaker per stream (remote mode).
function streamRecognizer(sdk, config, stream, speaker) {
  const r = new sdk.SpeechRecognizer(config, sdk.AudioConfig.fromStreamInput(stream));
  r.recognizing = (_, e) => { mic.interim[speaker] = `${speaker}: ${e.result.text}`; renderCaptions(); };
  r.recognized = (_, e) => {
    if (e.result.reason !== sdk.ResultReason.RecognizedSpeech || !e.result.text) return;
    onUtterance({ speaker, text: e.result.text }, speaker);
  };
  r.canceled = onCanceled(sdk);
  return {
    r,
    start: (ok, fail) => r.startContinuousRecognitionAsync(ok, fail),
    stop: (done) => r.stopContinuousRecognitionAsync(done, done),
  };
}

// Everyone on one mic, voices diarized (room mode).
function roomTranscriber(sdk, config) {
  const r = new sdk.ConversationTranscriber(config, sdk.AudioConfig.fromDefaultMicrophoneInput());
  r.transcribing = (_, e) => { mic.interim.room = `${e.result.speakerId || "…"}: ${e.result.text}`; renderCaptions(); };
  r.transcribed = (_, e) => {
    if (e.result.reason !== sdk.ResultReason.RecognizedSpeech || !e.result.text) return;
    onUtterance({ speaker: e.result.speakerId || "Unknown", text: e.result.text }, "room");
  };
  r.canceled = onCanceled(sdk);
  return {
    r,
    start: (ok, fail) => r.startTranscribingAsync(ok, fail),
    stop: (done) => r.stopTranscribingAsync(done, done),
  };
}

async function startMic() {
  const remote = micMode() === "remote";
  $("mic-mode").disabled = true;
  let streams = null, sdk, token;
  try {
    if (remote) streams = await captureRemote();
    else await checkMicPermission();
    setMicState("Connecting…", { on: true });
    [sdk, token] = await Promise.all([loadSpeechSdk(), fetchSpeechToken()]);
  } catch (e) {
    releaseStreams();
    $("mic-mode").disabled = false;
    setMicState(e.message, { warn: true });
    return;
  }
  const config = sdk.SpeechConfig.fromAuthorizationToken(token.token, token.region);
  config.speechRecognitionLanguage = "en-US";
  mic.known = remote ? REMOTE_INTERVIEWER : null;
  mic.recognizers = remote
    ? [streamRecognizer(sdk, config, streams.voice, REMOTE_INTERVIEWER),
       streamRecognizer(sdk, config, streams.call, REMOTE_CANDIDATE)]
    : [roomTranscriber(sdk, config)];
  mic.tokenTimer = setInterval(async () => {  // tokens last 10 minutes
    try {
      const fresh = (await fetchSpeechToken()).token;
      mic.recognizers.forEach((x) => { x.r.authorizationToken = fresh; });
    } catch { /* retried next tick */ }
  }, token.expires_in_seconds * 1000);
  renderSendRow();
  Promise.all(mic.recognizers.map((x) => new Promise((ok, fail) => x.start(ok, fail))))
    .then(() => {
      if (micOn()) setMicState(remote ? "Listening to you and the call… captions appear below." : "Listening… captions appear below.", { on: true });
    })
    .catch((err) => { stopMic(); setMicState(`Could not start the microphone: ${err}`, { warn: true }); });
}

function stopMic({ reset = false } = {}) {
  clearInterval(mic.tokenTimer);
  const running = mic.recognizers;
  mic.recognizers = [];
  for (const x of running) x.stop(() => x.r.close());
  releaseStreams();  // also ends the tab/screen share
  mic.interim = {};
  $("mic-mode").disabled = false;
  if (reset) Object.assign(mic, { buffer: [], interviewer: null, known: null, finals: [], sending: Promise.resolve() });
  renderCaptions();
  setMicState(running.length ? "Paused. Nothing is captured until you resume." : "");
}

// Sends whatever has been said since the last exchange and waits; the server splits it by content
// when there's only one speaker label.
async function flushMic() {
  if (micOn()) stopMic();
  await sendBuffer();
}

$("mic-toggle").addEventListener("click", () => {
  if (micOn()) stopMic();
  else startMic();
});

renderMicMode();

async function restoreLive() {
  const id = store.get(LIVE_STORAGE);
  if (!id) return;
  live.id = id;
  try {
    const res = await apiFetch(`/sessions/${encodeURIComponent(id)}`);
    if (res.status === 404) { closeLive(); return; }
    if (!res.ok) { showLiveError(await errorMessage(res)); return; }
    const state = await res.json();
    renderLiveTranscript(state.transcript);
    setLiveActive(true);
    selectTab("live");
    if (state.latest_evaluation) showLiveScorecard(state.latest_evaluation, { qaCount: state.evaluated_qa_count });
    live.shownQa = state.evaluated_qa_count;
    connectEvents();
  } catch {
    showLiveError("Could not reach the server to restore your live session.");
  }
}

if (!restoreDraft() || !pairsList.children.length) addPair();
selectTab(store.get(TAB_STORAGE) === "live" ? "live" : "transcript");
restoreLive();
loadRubrics();
if (store.get(UPLOAD_STORAGE)) pollTranscription(store.get(UPLOAD_STORAGE));
