// rb-llm console — plain JS, no build step, no external resources (works offline).
"use strict";

const PROTOCOL = "rb-llm/1";
const $ = (id) => document.getElementById(id);
let task = "ask";
let customSnapshot = null;

const HELP = {
  ask: "Question answered from the live aircraft status (GPS, AHRS, baro, EMS, radio, traffic). POST /v1/ask",
  command: "Pilot request to the RB → rbvoice command (action / component.parameter / args). POST /v1/parse task=command",
  atc: "ATC transmission → typed instructions, callsign match, readback. POST /v1/parse task=atc",
  auto: "rb-llm decides: command or ATC (wake word / push to talk ⇒ command). POST /v1/parse task=auto",
};

const EXAMPLES = {
  ask: {
    en: ["where are we?", "what are our coordinates?", "how high are we?", "what's the nearest airfield?",
         "how are the engine temperatures?", "is there any traffic?", "how much fuel is left?", "what is the radio on?"],
    it: ["dove siamo?", "dammi le coordinate", "a che quota siamo?", "qual è il campo più vicino?",
         "come sono le temperature del motore?", "ci sono traffici?", "quanto carburante resta?"],
    fr: ["où sommes-nous ?", "quelles sont nos coordonnées ?", "quel est l'aérodrome le plus proche ?"],
    es: ["¿dónde estamos?", "¿cuáles son nuestras coordenadas?", "¿cuál es el aeródromo más cercano?"],
    de: ["wo sind wir?", "wie sind unsere Koordinaten?", "welcher Flugplatz ist am nächsten?"],
  },
  command: {
    en: ["computer what are the cylinder temperatures", "computer tune the radio to one one eight decimal five",
         "computer take me to perugia", "computer start a timer for ten minutes", "primary show map",
         "computer stop the autopilot", "nice view of the lake today"],
    it: ["computer dimmi le temperature dei cilindri", "computer imposta frequenza radio centodiciotto virgola cinque",
         "computer portami a perugia", "computer quanto carburante nel secondo serbatoio", "primario visualizza mappa",
         "guarda che bel lago"],
    fr: ["ordinateur quelle est la température des cylindres", "ordinateur montre la carte"],
    es: ["computadora cuál es la temperatura de los cilindros", "computadora muestra el mapa"],
    de: ["computer wie ist die Zylindertemperatur", "computer zeige Karte"],
  },
  atc: {
    en: ["india charlie delta perugia tower climb altitude five thousand feet qnh one zero one three",
         "india charlie delta contact perugia approach one two zero decimal seven",
         "india alfa bravo charlie delta runway zero one cleared to land wind three six zero degrees eight knots",
         "india charlie delta squawk four three two one",
         "india charlie delta turn left heading two seven zero descend two thousand feet",
         "india xray yankee report downwind runway zero one",
         "climb five thousand feet qnh one zero one three india charlie delta"],
    it: ["india charlie delta perugia torre pista zero uno autorizzati all'atterraggio vento tre sei zero gradi otto nodi",
         "india charlie delta salite a cinque mila piedi qnh uno zero uno tre"],
  },
};
EXAMPLES.auto = { en: [...EXAMPLES.command.en.slice(0, 3), ...EXAMPLES.atc.en.slice(0, 2)],
                  it: [...EXAMPLES.command.it.slice(0, 3), ...EXAMPLES.atc.it] };

// ---------------------------------------------------------------------------------------------
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  return { http: res.status, data };
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

function pill(text, cls = "") { return el("span", { class: `pill ${cls}`, text }); }

// --- health ----------------------------------------------------------------------------------
async function refreshHealth() {
  const box = $("health");
  try {
    const { data } = await api("GET", "/v1/health");
    const info = (await api("GET", "/v1/info")).data;
    box.replaceChildren(
      pill(`engine ${data.status}`, data.status === "ready" ? "ok" : data.status === "loading" ? "warn" : "bad"),
      pill(((info.models || [])[0] || {}).id || "no model"),
      pill(`catalogs: ${Object.keys(data.catalogs || {}).join(" ") || "none"}`, Object.keys(data.catalogs || {}).length ? "" : "warn"),
    );
    if (data.soc_temp_c !== undefined) box.append(pill(`SoC ${data.soc_temp_c} °C`, data.soc_temp_c > 80 ? "bad" : data.soc_temp_c > 70 ? "warn" : ""));
    if (data.throttled !== undefined) box.append(pill(data.throttled ? "THROTTLED" : "no throttling", data.throttled ? "bad" : "ok"));
    if (info.engine) box.append(pill(info.engine.replace(/built=.*$/, "").replace(/tag=/, "llama.cpp ").replace(/ commit=.*/, "")));
  } catch (e) {
    box.replaceChildren(pill("rb-llm unreachable", "bad"));
  }
}

// --- aircraft status -------------------------------------------------------------------------
function renderFacts(st) {
  const ul = $("facts");
  ul.replaceChildren();
  if (st.error) { ul.append(el("li", { class: "bad", text: st.error.message })); return; }
  ul.append(el("li", { class: "muted", text: `source: ${st.source}` }));
  for (const f of st.facts || []) {
    ul.append(el("li", { "data-key": f.key }, el("b", { text: f.key }), " ", f.text));
  }
}

async function refreshStatus() {
  const src = $("status-source").value;
  $("status-custom").classList.toggle("hidden", src !== "custom");
  if (src === "custom") {
    if (!customSnapshot) {
      const { data } = await api("GET", "/v1/status?source=demo&raw=1");
      customSnapshot = data.snapshot || {};
      $("status-json").value = JSON.stringify(customSnapshot, null, 2);
    }
    return previewCustom();
  }
  const { data } = await api("GET", `/v1/status?source=${src}`);
  renderFacts(data);
}

async function previewCustom() {
  try {
    customSnapshot = JSON.parse($("status-json").value);
  } catch (e) {
    renderFacts({ error: { message: `invalid JSON: ${e.message}` } });
    return;
  }
  const { data } = await api("POST", "/v1/status", customSnapshot);
  renderFacts(data);
}

// --- requests --------------------------------------------------------------------------------
function buildRequest() {
  const lang = $("lang").value;
  const text = $("text").value.trim();
  const options = { debug: $("debug").checked };
  if ($("deadline").value) options.deadline_ms = parseInt($("deadline").value, 10);
  const req = { protocol: PROTOCOL, request_id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()), lang, text };
  if (task === "ask") {
    const src = $("status-source").value;
    if (src === "custom" && customSnapshot) req.aircraft_status = customSnapshot;
    else options.status_source = src;
    req.options = options;
    return ["/v1/ask", req];
  }
  req.task = task;
  req.source = $("ptt").checked ? "push_to_talk" : "intercom";
  const ctx = {};
  const cs = $("callsign").value.trim().toUpperCase();
  if (cs) ctx.own_callsign = { registration: cs };
  const prev = $("previous").value.trim();
  if (prev.includes(".")) {
    const [c, p] = prev.split(".", 2);
    ctx.session = { last_action: "GET", last_component: c, last_parameter: p };
  }
  req.context = ctx;
  req.options = options;
  return ["/v1/parse", req];
}

function kv(rows) {
  const t = el("table", { class: "kv" });
  for (const [k, v] of rows) {
    if (v === undefined || v === null || v === "") continue;
    t.append(el("tr", {}, el("th", { text: k }), el("td", { text: typeof v === "object" ? JSON.stringify(v) : String(v) })));
  }
  return t;
}

function renderResult(http, r, elapsed) {
  $("result").classList.remove("hidden");
  const main = $("result-main");
  const detail = $("result-detail");
  detail.replaceChildren();
  let headline = "";
  let cls = "";

  if (r.status === "error") {
    headline = `Error: ${(r.error || {}).code} — ${(r.error || {}).message}`;
    cls = "bad";
  } else if (r.kind === "ask") {
    headline = (r.answer || {}).speak || "(no answer)";
    cls = r.status === "refused" ? "bad" : "";
    if (r.facts_used && r.facts_used.length) {
      detail.append(kv([["facts used", r.facts_used.join(", ")], ["status source", r.status_source]]));
      document.querySelectorAll("#facts li").forEach((li) =>
        li.classList.toggle("used", r.facts_used.includes(li.dataset.key)));
    }
  } else if (r.kind === "command" && r.command) {
    const c = r.command;
    headline = `${c.action} ${c.component}.${c.parameter}`;
    if (c.requires_confirmation) cls = "warn";
    detail.append(kv([["action", c.action], ["component", c.component], ["parameter", c.parameter],
      ["args", Object.keys(c.args || {}).length ? c.args : null], ["target", c.target],
      ["from context", c.from_context ? "yes" : null], ["needs confirmation", c.requires_confirmation ? "YES" : null],
      ["normalized", r.normalized_text]]));
  } else if ((r.kind === "atc" || r.kind === "readback") && r.atc) {
    const a = r.atc;
    headline = (r.answer || {}).display || `${r.kind} (${a.instructions.length} instructions)`;
    detail.append(kv([["kind", r.kind], ["addressee", r.addressee],
      ["callsign", `${a.callsign.heard || "—"} → ${a.callsign.match} (${a.callsign.score})`],
      ["station", a.station], ["normalized", r.normalized_text]]));
    const ol = el("ol", { class: "instr" });
    for (const i of a.instructions) {
      const { type, readback_required, ...rest } = i;
      ol.append(el("li", {}, el("b", { text: type }), " ", JSON.stringify(rest), readback_required ? el("span", { class: "tag", text: "readback" }) : ""));
    }
    detail.append(ol);
    if (a.readback && a.readback.text) detail.append(el("p", { class: "readback" }, el("b", { text: "Readback: " }), a.readback.text));
    for (const s of a.suggested_actions || []) {
      detail.append(el("p", { class: "suggest", text: `Suggested (needs pilot confirmation): ${s.action} ${s.component} ${JSON.stringify(s.args)}` }));
    }
  } else {
    headline = `${r.status}${r.kind ? " · " + r.kind : ""}`;
    cls = "muted";
    if (r.normalized_text) detail.append(kv([["normalized", r.normalized_text]]));
  }
  main.textContent = headline;
  main.className = `answer ${cls}`;

  const e = r.engine || {};
  const badges = $("result-badges");
  badges.replaceChildren(pill(`HTTP ${http}`, http === 200 ? "ok" : "bad"), pill(r.status, r.status === "ok" ? "ok" : r.status === "error" || r.status === "refused" ? "bad" : "warn"));
  if (r.confidence !== undefined) badges.append(pill(`conf ${r.confidence}`));
  if (e.path) badges.append(pill(e.path));
  if (e.latency_ms !== undefined) badges.append(pill(`${e.latency_ms} ms engine`));
  badges.append(pill(`${elapsed} ms total`));
  if (e.tokens_in) badges.append(pill(`${e.tokens_in} in (${e.cached_tokens || 0} cached) / ${e.tokens_out} out`));
  if (e.tok_per_s) badges.append(pill(`${e.tok_per_s} tok/s`));
  if (r.kind === "ask" && (r.answer || {}).speak && window.speechSynthesis && r.status === "ok") {
    const b = el("button", { class: "small", type: "button", text: "🔊 speak" });
    b.onclick = () => { const u = new SpeechSynthesisUtterance(r.answer.speak); u.lang = r.answer.lang; speechSynthesis.speak(u); };
    badges.append(b);
  }

  const w = $("result-warnings");
  w.replaceChildren(...(r.warnings || []).map((x) => el("li", { text: x })));
  const { debug, ...rest } = r;
  $("result-json").textContent = JSON.stringify(rest, null, 2);
  $("debug-box").classList.toggle("hidden", !debug);
  $("result-debug").textContent = debug ? JSON.stringify(debug, null, 2).replace(/\\n/g, "\n") : "";
  return headline;
}

function addHistory(t, text, result, ms) {
  const tb = document.querySelector("#history tbody");
  const tr = el("tr", {}, el("td", { text: t }), el("td", { text }), el("td", { text: result }), el("td", { text: String(ms) }));
  tr.onclick = () => { $("text").value = text; };
  tb.prepend(tr);
  while (tb.children.length > 25) tb.lastChild.remove();
}

async function send(ev) {
  ev.preventDefault();
  if (!$("text").value.trim()) return;
  const [path, req] = buildRequest();
  const btn = $("send");
  btn.disabled = true;
  btn.textContent = "…";
  const t0 = performance.now();
  try {
    const { http, data } = await api("POST", path, req);
    const ms = Math.round(performance.now() - t0);
    const h = renderResult(http, data, ms);
    addHistory(task, req.text, h, ms);
  } catch (e) {
    renderResult(0, { status: "error", error: { code: "network", message: String(e) } }, 0);
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

// --- tabs & examples -------------------------------------------------------------------------
function setTask(t) {
  task = t;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.task === t));
  $("task-help").textContent = HELP[t];
  document.querySelectorAll("[class*='opt-']").forEach((n) => {
    const show = [...n.classList].some((c) => c === `opt-${t}`);
    n.style.display = show ? "" : "none";
  });
  renderExamples();
}

function renderExamples() {
  const lang = $("lang").value;
  const list = (EXAMPLES[task] || {})[lang] || (EXAMPLES[task] || {}).en || [];
  $("examples").replaceChildren(...list.map((x) => {
    const b = el("button", { class: "chip", type: "button", text: x });
    b.onclick = () => { $("text").value = x; $("text").focus(); };
    return b;
  }));
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTask(b.dataset.task)));
$("lang").addEventListener("change", renderExamples);
$("form").addEventListener("submit", send);
$("status-source").addEventListener("change", refreshStatus);
$("status-refresh").addEventListener("click", refreshStatus);
$("status-apply").addEventListener("click", previewCustom);

setTask("ask");
refreshHealth();
refreshStatus();
setInterval(refreshHealth, 10000);
