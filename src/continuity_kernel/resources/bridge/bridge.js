const bridgeToken = captureBridgeToken();
const thinkingOrbStops = new WeakMap();

const ui = {
  closeInspector: document.querySelector("#close-inspector"),
  connectionCopy: document.querySelector("#connection-copy"),
  connectionNotice: document.querySelector("#connection-notice"),
  connectionOrb: document.querySelector("#connection-orb"),
  continueInCodex: document.querySelector("#continue-in-codex"),
  inspector: document.querySelector("#inspector"),
  inspectorBackdrop: document.querySelector("#inspector-backdrop"),
  inspectorBody: document.querySelector("#inspector-body"),
  inspectorEyebrow: document.querySelector("#inspector-eyebrow"),
  inspectorFoot: document.querySelector("#inspector-foot"),
  inspectorTitle: document.querySelector("#inspector-title"),
  localStatus: document.querySelector("#local-status"),
  localStatusCopy: document.querySelector("#local-status-copy"),
  main: document.querySelector("#main"),
  menuButton: document.querySelector("#menu-button"),
  openCodex: document.querySelector("#open-codex"),
  pageIntro: document.querySelector("#page-intro"),
  pageTitle: document.querySelector("#page-title"),
  rail: document.querySelector("#rail"),
  railBackdrop: document.querySelector("#rail-backdrop"),
  retry: document.querySelector("#retry-button"),
  statusDot: document.querySelector("#local-status .status-dot"),
  taskCount: document.querySelector("#task-count"),
  view: document.querySelector("#view"),
};

startThinkingOrb(ui.connectionOrb);

const viewCopy = {
  commitments: ["Commitments", "Outcomes that remain open when an execution hand ends."],
  mind: ["Mind", "The durable point of view that owns meaning and judgment."],
  now: ["Your Mind, between tasks.", "Current orientation and the work that survives every hand."],
  storylines: ["Storylines", "The longer concerns connecting people, work, and next moves."],
  system: ["The vehicle", "Local health, integration, and the boundaries around your Mind."],
};

const state = {
  currentView: viewFromHash(),
  lastSuccessAt: null,
  previouslyFocused: null,
  selectedTaskId: null,
  snapshot: null,
  snapshotSignature: null,
};

for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => navigate(button.dataset.view));
}

for (const link of document.querySelectorAll("[data-view-link]")) {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(link.dataset.viewLink);
  });
}

ui.menuButton.addEventListener("click", () => setRailOpen(!ui.rail.classList.contains("is-open")));
ui.railBackdrop.addEventListener("click", () => setRailOpen(false));
ui.closeInspector.addEventListener("click", closeInspector);
ui.inspectorBackdrop.addEventListener("click", closeInspector);
ui.retry.addEventListener("click", loadSnapshot);
window.addEventListener("hashchange", () => {
  state.currentView = viewFromHash();
  render();
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeInspector();
    setRailOpen(false);
  }
  if (event.key === "Tab" && ui.inspector.classList.contains("is-open")) {
    keepFocusInsideInspector(event);
  }
});

loadSnapshot();
window.setInterval(() => loadSnapshot({ quiet: true }), 10_000);
window.setInterval(updateFreshness, 5_000);

async function loadSnapshot({ quiet = false } = {}) {
  if (!quiet) {
    ui.view.setAttribute("aria-busy", "true");
    showConnectionOrb("searching", "Reading local GSV");
  }
  try {
    const response = await fetch("/api/v1/snapshot", {
      cache: "no-store",
      headers: {
        Accept: "application/json",
        Authorization: bridgeToken ? `Bearer ${bridgeToken}` : "",
      },
    });
    if (!response.ok) {
      throw new Error(`Bridge returned ${response.status}`);
    }
    const snapshot = await response.json();
    const snapshotSignature = JSON.stringify(snapshot);
    const snapshotChanged = snapshotSignature !== state.snapshotSignature;
    state.snapshot = snapshot;
    state.snapshotSignature = snapshotSignature;
    state.lastSuccessAt = Date.now();
    setConnectionState(state.snapshot.doctor.healthy ? "healthy" : "partial");
    if (snapshotChanged) render();
  } catch (error) {
    const message = connectionMessage(error);
    if (state.snapshot) {
      setConnectionState("stale", `${message} Showing the last local snapshot.`);
    } else {
      setConnectionState("unavailable", message);
      renderUnavailable();
    }
  } finally {
    ui.view.setAttribute("aria-busy", "false");
  }
}

function render() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const [title, intro] = viewCopy[state.currentView] || viewCopy.now;
  ui.pageTitle.textContent = title;
  ui.pageIntro.textContent = intro;
  ui.openCodex.href = snapshot.codex.new_mind_url;
  ui.openCodex.hidden = false;

  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  ui.taskCount.textContent = String(openTasks.length);
  ui.taskCount.hidden = openTasks.length === 0;
  setActiveNavigation();

  const views = {
    commitments: () => renderCommitments(snapshot),
    mind: () => renderMind(snapshot),
    now: () => renderNow(snapshot),
    storylines: () => renderStorylines(snapshot),
    system: () => renderSystem(snapshot),
  };
  for (const orb of ui.view.querySelectorAll(".thinking-orb")) stopThinkingOrb(orb);
  ui.view.replaceChildren((views[state.currentView] || views.now)());

  if (state.selectedTaskId) {
    const selected = snapshot.tasks.find((task) => task.identifier === state.selectedTaskId);
    if (selected) renderInspector(selected);
    else closeInspector();
  }
}

function renderNow(snapshot) {
  const fragment = document.createDocumentFragment();
  const orientation = element("section", "orientation");
  const kicker = element("div", "orientation-kicker");
  kicker.append(
    textElement("p", "section-label", "Latest pulse"),
    textElement("strong", "", "Current orientation"),
    textElement("span", "", revisionLabel(snapshot.now.revision)),
  );
  orientation.append(kicker, renderDocument(snapshot.now.content));
  fragment.append(orientation, renderContinuity(snapshot));

  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  if (openTasks.length === 0) {
    fragment.append(renderFirstRun(snapshot));
    return fragment;
  }

  const heading = element("div", "section-head");
  const headingText = element("div");
  headingText.append(
    textElement("p", "section-label", "Open work"),
    textElement("h2", "", "What remains true after this task"),
  );
  heading.append(headingText, textElement("p", "section-meta", `${openTasks.length} open`));
  fragment.append(heading, renderTaskBoard(openTasks, 3));
  return fragment;
}

function renderContinuity(snapshot) {
  const line = element("section", "continuity-line", { "aria-label": "GSV continuity" });
  const open = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status)).length;
  const inMotion = snapshot.tasks.filter((task) => task.status === "doing").length;
  line.append(
    continuityStage("01", "Mind", snapshot.mind.revision ? "Authored" : "Ready", "is-persistent"),
    continuityStage(
      "02",
      "Hands",
      inMotion ? `${inMotion} in motion` : "Replaceable",
      "",
      inMotion ? "working" : "",
    ),
    continuityStage("03", "Commitments", open ? `${open} held` : "Clear", "is-held"),
    continuityStage("04", "Next hand", open ? "Can resume" : "Ready when needed"),
  );
  return line;
}

function continuityStage(index, label, value, className = "", orbState = "") {
  const item = element("div", `continuity-stage ${className}`.trim());
  const head = element("div", "continuity-stage-head");
  if (orbState) head.append(createThinkingOrb(orbState, `${label}: ${value}`));
  head.append(textElement("span", "continuity-index", index));
  item.append(
    head,
    textElement("strong", "", label),
    textElement("span", "continuity-value", value),
  );
  return item;
}

function renderFirstRun(snapshot) {
  const empty = element("section", "empty-state");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Your first hand"),
    textElement("h2", "", "Shape the Mind that will meet you here."),
    textElement(
      "p",
      "",
      "Codex will read the local GSV context, ask only what matters, and leave the first durable orientation behind.",
    ),
  );
  const action = textElement("a", "primary-action", "Start in Codex");
  action.href = snapshot.codex.new_mind_url;
  empty.append(copy, action);
  return empty;
}

function renderCommitments(snapshot) {
  const container = element("div");
  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  if (openTasks.length === 0) {
    container.append(renderFirstRun(snapshot));
    return container;
  }
  container.append(renderTaskBoard(openTasks));

  const completed = snapshot.tasks.filter((task) => ["done", "dropped"].includes(task.status));
  if (completed.length > 0) {
    const heading = element("div", "section-head");
    const headingText = element("div");
    headingText.append(
      textElement("p", "section-label", "Record"),
      textElement("h2", "", "Closed deliberately"),
    );
    heading.append(headingText, textElement("p", "section-meta", `${completed.length} recorded`));
    heading.style.marginTop = "48px";
    container.append(heading, renderCompactTasks(completed));
  }
  return container;
}

function renderTaskBoard(tasks, limit = null) {
  const lanes = taskLanes(tasks);
  const board = element("div", "commitment-grid");
  for (const lane of lanes) {
    const section = element("section", "lane");
    const head = element("header", "lane-head");
    head.append(textElement("h3", "", lane.title), textElement("span", "", String(lane.tasks.length)));
    const list = element("div", "task-list");
    const visible = limit ? lane.tasks.slice(0, limit) : lane.tasks;
    if (visible.length === 0) {
      list.append(textElement("div", "empty-lane", lane.empty));
    } else {
      for (const task of visible) list.append(renderTaskCard(task));
    }
    section.append(head, list);
    board.append(section);
  }
  return board;
}

function taskLanes(tasks) {
  const human = tasks.filter((task) => task.next_actor === "human");
  const waiting = tasks.filter(
    (task) => task.next_actor !== "human" && (task.status === "waiting" || task.next_actor === "external"),
  );
  const agent = tasks.filter((task) => !human.includes(task) && !waiting.includes(task));
  return [
    { empty: "Nothing needs you now.", tasks: human, title: "Needs you" },
    { empty: "No hand is carrying work.", tasks: agent, title: "In motion" },
    { empty: "Nothing is waiting outside.", tasks: waiting, title: "Waiting" },
  ];
}

function renderTaskCard(task) {
  const card = element("button", "task-card", { type: "button" });
  card.addEventListener("click", () => openInspector(task.identifier));
  const pillClass = task.next_actor === "human" ? "is-human" : `is-${task.status}`;
  const pill = textElement("span", `status-pill ${pillClass}`, statusLabel(task));
  card.append(
    pill,
    textElement("h4", "", task.title),
    textElement("p", "task-next", task.next_action || task.waiting_on || task.outcome),
  );
  const foot = element("div", "task-foot");
  foot.append(
    textElement("span", "", task.next_actor ? actorLabel(task.next_actor) : "Actor open"),
    textElement("span", "", relativeTime(task.updated_at)),
  );
  card.append(foot);
  return card;
}

function renderCompactTasks(tasks) {
  const list = element("div", "storyline-list");
  for (const task of tasks) {
    const row = element("button", "storyline task-row", { type: "button" });
    row.addEventListener("click", () => openInspector(task.identifier));
    row.append(
      textElement("h3", "", task.title),
      textElement("p", "", task.outcome),
      textElement("span", "system-state", task.status),
    );
    list.append(row);
  }
  return list;
}

function renderStorylines(snapshot) {
  const container = element("div");
  const heading = element("div", "section-head");
  const headingText = element("div");
  headingText.append(
    textElement("p", "section-label", "Across tasks"),
    textElement("h2", "", "Concerns with a longer life"),
  );
  heading.append(headingText, textElement("p", "section-meta", `${snapshot.threads.length} total`));
  container.append(heading);
  if (snapshot.threads.length === 0) {
    container.append(textElement("div", "empty-lane", "No storylines have been authored yet."));
    return container;
  }
  const list = element("div", "storyline-list");
  for (const thread of snapshot.threads) {
    const row = element("article", "storyline");
    const title = element("div");
    title.append(
      textElement("span", `status-pill is-${thread.status}`, thread.status),
      textElement("h3", "", thread.title),
    );
    const body = element("div");
    body.append(textElement("p", "", thread.summary));
    if (thread.next_move) body.append(textElement("p", "storyline-next", `Next: ${thread.next_move}`));
    row.append(title, body, textElement("span", "system-state", `${thread.task_ids.length} tasks`));
    list.append(row);
  }
  container.append(list);
  return container;
}

function renderMind(snapshot) {
  const layout = element("div", "mind-layout");
  const main = element("section");
  const head = element("div", "section-head");
  const headText = element("div");
  headText.append(
    textElement("p", "section-label", "Authored judgment"),
    textElement("h2", "", "The point of view that persists"),
  );
  head.append(headText, textElement("p", "section-meta", revisionLabel(snapshot.mind.revision)));
  main.append(head, renderDocument(snapshot.mind.content));

  const side = element("aside", "mind-side");
  side.append(textElement("h3", "", "Known entities"));
  if (snapshot.entities.length === 0) {
    side.append(textElement("p", "section-meta", "No canonical entities yet."));
  } else {
    const list = element("ul", "entity-list");
    for (const entity of snapshot.entities.slice(0, 12)) {
      const item = element("li");
      item.append(
        textElement("strong", "", entity.title),
        textElement("span", "", entity.entity_type),
      );
      list.append(item);
    }
    side.append(list);
  }
  layout.append(main, side);
  return layout;
}

function renderSystem(snapshot) {
  const container = element("div");
  const list = element("div", "system-list");
  list.append(
    systemRow(
      "GSV vehicle",
      `Version ${snapshot.bridge.version}; local Markdown and atomic writes.`,
      snapshot.doctor.healthy ? "Healthy" : "Needs attention",
      snapshot.doctor.healthy ? "" : "is-error",
    ),
    systemRow(
      "Mind",
      "MIND.md and NOW.md are present in the user-owned vault.",
      "Present",
      "",
    ),
    systemRow(
      "Codex hands",
      snapshot.codex.available
        ? "The GSV plugin can load durable context in a fresh Codex task."
        : "The local vault works, but Codex integration could not be verified.",
      snapshot.codex.plugin_installed ? "Connected" : "Check setup",
      snapshot.codex.plugin_installed ? "" : "is-warning",
    ),
    systemRow(
      "Bridge",
      "Loopback-only, read-only, and rendered from exact authored records.",
      "Local",
      "",
    ),
    systemRow(
      "Pulse",
      "The latest bounded orientation is visible in Now; no continuous-awake claim is made.",
      "Discrete",
      "",
    ),
    systemRow(
      "Shipyard",
      "Changes remain ordinary reviewed work under the same approval boundaries.",
      "Bounded",
      "",
    ),
  );
  container.append(list);
  return container;
}

function systemRow(title, copy, status, statusClass) {
  const row = element("div", "system-row");
  row.append(
    textElement("h3", "", title),
    textElement("p", "", copy),
    textElement("span", `system-state ${statusClass}`, status),
  );
  return row;
}

function renderDocument(content) {
  const root = element("div", "document");
  let list = null;
  for (const rawLine of String(content || "").split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      list = null;
      continue;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      list = null;
      const level = Math.min(4, heading[1].length + 1);
      root.append(textElement(`h${level}`, "", heading[2]));
      continue;
    }
    const bullet = /^[-*]\s+(.+)$/.exec(line);
    if (bullet) {
      if (!list) {
        list = element("ul");
        root.append(list);
      }
      list.append(textElement("li", "", bullet[1]));
      continue;
    }
    list = null;
    root.append(textElement("p", "", line));
  }
  return root;
}

function openInspector(taskId) {
  state.selectedTaskId = taskId;
  const task = state.snapshot?.tasks.find((item) => item.identifier === taskId);
  if (!task) return;
  state.previouslyFocused = document.activeElement;
  renderInspector(task);
  ui.inspector.classList.add("is-open");
  ui.inspector.setAttribute("aria-hidden", "false");
  ui.inspectorBackdrop.hidden = false;
  window.setTimeout(() => ui.closeInspector.focus(), 0);
}

function renderInspector(task) {
  ui.inspectorEyebrow.textContent = `${statusLabel(task)} commitment`;
  ui.inspectorTitle.textContent = task.title;
  ui.inspectorBody.replaceChildren(
    detail("Outcome", task.outcome),
    detail("Next action", task.next_action || "Not recorded."),
    detail("Waiting on", task.waiting_on || "Nothing recorded."),
    detail("Record", `${task.identifier}\nUpdated ${task.updated_at}\nRevision ${task.revision}`),
  );
  ui.continueInCodex.href = task.codex_url || state.snapshot.codex.new_mind_url;
  ui.inspectorFoot.hidden = false;
}

function detail(label, value) {
  const block = element("section", "detail-block");
  block.append(textElement("h3", "", label), textElement("p", "", value));
  return block;
}

function closeInspector() {
  const wasOpen = ui.inspector.classList.contains("is-open");
  state.selectedTaskId = null;
  ui.inspector.classList.remove("is-open");
  ui.inspector.setAttribute("aria-hidden", "true");
  ui.inspectorBackdrop.hidden = true;
  ui.inspectorFoot.hidden = true;
  if (wasOpen && state.previouslyFocused instanceof HTMLElement) {
    state.previouslyFocused.focus();
  }
  state.previouslyFocused = null;
}

function navigate(view) {
  if (!viewCopy[view]) return;
  state.currentView = view;
  window.location.hash = view;
  setRailOpen(false);
  render();
  ui.main?.focus?.({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "auto" });
}

function viewFromHash() {
  const candidate = window.location.hash.replace(/^#/, "").split("?")[0];
  return viewCopy[candidate] ? candidate : "now";
}

function setActiveNavigation() {
  for (const button of document.querySelectorAll("[data-view]")) {
    const active = button.dataset.view === state.currentView;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function setRailOpen(open) {
  ui.rail.classList.toggle("is-open", open);
  ui.menuButton.setAttribute("aria-expanded", String(open));
  ui.railBackdrop.hidden = !open;
}

function setConnectionState(connectionState, message = "") {
  const healthy = connectionState === "healthy";
  const partial = connectionState === "partial";
  const stale = connectionState === "stale";
  const unavailable = connectionState === "unavailable";
  ui.connectionNotice.hidden = healthy;
  ui.connectionNotice.className = `connection-notice is-${connectionState}`;
  ui.localStatus.classList.toggle("is-healthy", healthy);
  ui.localStatus.classList.toggle("is-partial", partial);
  ui.localStatus.classList.toggle("is-stale", stale);
  ui.localStatus.classList.toggle("is-unavailable", unavailable);
  const labels = {
    healthy: "Local GSV healthy",
    partial: "Local GSV needs attention",
    stale: "Showing last local snapshot",
    unavailable: "Local GSV unavailable",
  };
  ui.localStatusCopy.textContent = labels[connectionState];
  if (stale) {
    showConnectionOrb("searching", labels[connectionState]);
  } else {
    ui.connectionOrb.hidden = true;
    ui.statusDot.hidden = false;
  }
  if (partial) {
    ui.connectionCopy.textContent = "The vault is readable, but its integrity check needs attention.";
  } else if (!healthy) {
    ui.connectionCopy.textContent = message;
  }
}

function showConnectionOrb(orbState, label) {
  ui.connectionOrb.hidden = false;
  ui.statusDot.hidden = true;
  ui.connectionOrb.dataset.orbState = orbState;
  ui.connectionOrb.setAttribute("aria-label", label);
  ui.localStatusCopy.textContent = label;
}

function renderUnavailable() {
  const unavailable = element("section", "unavailable-state");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Bridge unavailable"),
    textElement("h2", "", "Your local files have not been changed."),
    textElement("p", "", "Run gsv again to open a fresh private session."),
  );
  unavailable.append(copy);
  ui.view.replaceChildren(unavailable);
}

function connectionMessage(_error) {
  return "The private Bridge session could not be reached. Your local files were not changed.";
}

function updateFreshness() {
  if (!state.snapshot || !state.lastSuccessAt) return;
  if (Date.now() - state.lastSuccessAt > 30_000) {
    setConnectionState("stale", "Showing the last successful local snapshot while The Bridge reconnects.");
  }
}

function keepFocusInsideInspector(event) {
  const focusable = [...ui.inspector.querySelectorAll("button, a[href]")].filter(
    (node) => !node.hidden && node.getAttribute("aria-hidden") !== "true",
  );
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function captureBridgeToken() {
  const match = /^#token=([0-9a-f]{48})$/.exec(window.location.hash);
  if (match) {
    window.sessionStorage.setItem("gsv_bridge_token", match[1]);
    window.history.replaceState(null, "", `${window.location.pathname}#now`);
    return match[1];
  }
  return window.sessionStorage.getItem("gsv_bridge_token") || "";
}

function statusLabel(task) {
  if (task.next_actor === "human") return "Needs you";
  const labels = {
    captured: "Captured",
    doing: "In motion",
    done: "Done",
    dropped: "Dropped",
    ready: "Ready",
    someday: "Later",
    waiting: "Waiting",
  };
  return labels[task.status] || task.status;
}

function actorLabel(actor) {
  return { agent: "Agent next", external: "World next", human: "You next" }[actor] || actor;
}

function relativeTime(timestamp) {
  const parsed = Date.parse(timestamp);
  if (!Number.isFinite(parsed)) return "Recorded";
  const seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

function revisionLabel(revision) {
  return revision ? `Revision ${revision.slice(0, 8)}` : "No revision";
}

function element(tag, className = "", attributes = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function textElement(tag, className, text) {
  const node = element(tag, className);
  node.textContent = text;
  return node;
}

function createThinkingOrb(orbState, label) {
  const canvas = element("canvas", "thinking-orb continuity-orb", {
    "aria-label": label,
    "data-orb-state": orbState,
    height: "20",
    role: "img",
    width: "20",
  });
  startThinkingOrb(canvas);
  return canvas;
}

function stopThinkingOrb(canvas) {
  thinkingOrbStops.get(canvas)?.();
  thinkingOrbStops.delete(canvas);
}

/*
 * Working and searching modes adapted from thinking-orbs 0.1.1.
 * Source commit 382be79c472cd600277f01e14f98f8c0ee18dcb0, MIT license.
 * The Bridge keeps only the two useful 20px canvas modes and no React runtime.
 */
function startThinkingOrb(canvas) {
  if (!(canvas instanceof HTMLCanvasElement) || thinkingOrbStops.has(canvas)) return;
  const size = 20;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const context = canvas.getContext("2d");
  if (!context) return;
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);

  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let animationFrame = 0;
  let disposed = false;
  let reduced = motion.matches;
  let running = false;
  let visible = true;

  const drawFrame = (time) => {
    const orbState = canvas.dataset.orbState === "working" ? "working" : "searching";
    const speed = orbState === "working" ? 3.9 : 2.665;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, size, size);
    drawThinkingOrb(context, size, time * speed, orbState);
  };

  const stop = () => {
    running = false;
    window.cancelAnimationFrame(animationFrame);
  };

  const loop = () => {
    if (!canvas.isConnected) {
      cleanup();
      return;
    }
    drawFrame(window.performance.now() / 1000);
    if (running) animationFrame = window.requestAnimationFrame(loop);
  };

  const start = () => {
    if (disposed || running || reduced || !visible || document.visibilityState === "hidden") return;
    running = true;
    animationFrame = window.requestAnimationFrame(loop);
  };

  const observer =
    typeof IntersectionObserver === "undefined"
      ? null
      : new IntersectionObserver(([entry]) => {
          visible = entry.isIntersecting;
          if (visible) start();
          else stop();
        });

  const onVisibility = () => {
    if (document.visibilityState === "hidden") stop();
    else start();
  };

  const onMotion = (event) => {
    reduced = event.matches;
    if (reduced) {
      stop();
      drawFrame(0.6);
    } else {
      start();
    }
  };

  function cleanup() {
    if (disposed) return;
    disposed = true;
    stop();
    observer?.disconnect();
    motion.removeEventListener("change", onMotion);
    document.removeEventListener("visibilitychange", onVisibility);
    thinkingOrbStops.delete(canvas);
  }

  thinkingOrbStops.set(canvas, cleanup);
  drawFrame(0.6);
  observer?.observe(canvas);
  motion.addEventListener("change", onMotion);
  document.addEventListener("visibilitychange", onVisibility);
  if (!observer) start();
}

function drawThinkingOrb(context, size, time, orbState) {
  if (orbState === "working") drawWorkingOrb(context, size, time);
  else drawSearchingOrb(context, size, time);
}

function orbHash(a, b) {
  const value = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function orbProjection(yaw, tilt, centerX, centerY, scale) {
  const sinTilt = Math.sin(tilt);
  const cosTilt = Math.cos(tilt);
  const sinYaw = Math.sin(yaw);
  const cosYaw = Math.cos(yaw);
  return (x, y, z) => {
    const rotatedX = x * cosYaw + z * sinYaw;
    const rotatedZ = -x * sinYaw + z * cosYaw;
    const rotatedY = y * cosTilt - rotatedZ * sinTilt;
    const depth = y * sinTilt + rotatedZ * cosTilt;
    return [centerX + rotatedX * scale, centerY - rotatedY * scale, depth];
  };
}

function paintOrbDots(context, dots, minimumRadius = 0.3) {
  dots.sort((left, right) => left.z - right.z);
  for (const dot of dots) {
    const alpha = dot.alpha ?? 1;
    if (alpha < 0.02) continue;
    const white = Math.min(1, Math.max(0, dot.white));
    const gray = Math.round(white * 255);
    context.fillStyle = `rgba(${gray},${gray},${gray},${alpha})`;
    context.beginPath();
    context.arc(dot.x, dot.y, Math.max(minimumRadius, dot.radius), 0, Math.PI * 2);
    context.fill();
  }
}

function orbRadiusScale(size, exponent) {
  return (size / 300) ** exponent;
}

function drawWorkingOrb(context, size, time) {
  const center = size / 2;
  const radius = (size / 2) * 0.82;
  const project = orbProjection(time * 0.12, 0.3, center, center, 1);
  const radiusScale = orbRadiusScale(size, 0.6);
  const dots = [];

  for (let orbit = 0; orbit < 3; orbit += 1) {
    const first = orbHash(orbit, 1.7);
    const second = orbHash(orbit, 5.2);
    const third = orbHash(orbit, 8.9);
    const orbitRadius = radius * (0.45 + 0.52 * first);
    const theta = first * 2 * Math.PI;
    const phi = Math.acos(2 * second - 1);
    const normalX = Math.sin(phi) * Math.cos(theta);
    const normalY = Math.cos(phi);
    const normalZ = Math.sin(phi) * Math.sin(theta);
    let basisX = -normalY;
    let basisY = normalX;
    const basisZ = 0;
    const basisLength = Math.max(1e-6, Math.sqrt(basisX * basisX + basisY * basisY));
    basisX /= basisLength;
    basisY /= basisLength;
    const crossX = normalY * basisZ - normalZ * basisY;
    const crossY = normalZ * basisX - normalX * basisZ;
    const crossZ = normalX * basisY - normalY * basisX;
    const speed = (0.25 + 0.55 * third) * (third > 0.5 ? 1 : -1);

    for (let point = 0; point < 10; point += 1) {
      const angle = (point / 10) * 2 * Math.PI;
      const [x, y, z] = project(
        (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
        (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
        (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
      );
      const depth = (z / orbitRadius + 1) / 2;
      dots.push({
        alpha: 0.5 * (0.4 + 0.6 * depth),
        radius: 2.16 * radiusScale,
        white: 0.72,
        x,
        y,
        z,
      });
    }

    for (let particle = 0; particle < 3; particle += 1) {
      const angle = time * speed + (particle / 3) * 2 * Math.PI + second * 6;
      const [x, y, z] = project(
        (basisX * Math.cos(angle) + crossX * Math.sin(angle)) * orbitRadius,
        (basisY * Math.cos(angle) + crossY * Math.sin(angle)) * orbitRadius,
        (basisZ * Math.cos(angle) + crossZ * Math.sin(angle)) * orbitRadius,
      );
      const depth = (z / orbitRadius + 1) / 2;
      dots.push({
        radius: (2.88 + 3.84 * depth) * radiusScale,
        white: 0.3 - 0.22 * depth,
        x,
        y,
        z,
      });
    }
  }
  paintOrbDots(context, dots);
}

function drawSearchingOrb(context, size, time) {
  const spin = 0.5;
  const center = size / 2;
  const radius = (size / 2) * 0.82;
  const tilt = 0.4 + 0.06 * Math.sin(time * 0.35);
  const project = orbProjection(time * spin, tilt, center, center, radius);
  const scan = time * (spin + (1.7 - spin) * 4.335);
  const radiusScale = orbRadiusScale(size, 0.6);
  const dots = [];

  for (let ring = 0; ring <= 6; ring += 1) {
    const latitude = -Math.PI / 2 + (ring / 6) * Math.PI;
    const cosLatitude = Math.cos(latitude);
    const sinLatitude = Math.sin(latitude);
    const longitudeCount = Math.max(1, Math.round(Math.abs(cosLatitude) * 14));
    for (let point = 0; point < longitudeCount; point += 1) {
      const longitude = (point / longitudeCount) * 2 * Math.PI;
      const [x, y, z] = project(
        cosLatitude * Math.cos(longitude),
        sinLatitude,
        cosLatitude * Math.sin(longitude),
      );
      const depth = (z + 1) / 2;
      const delta = Math.atan2(
        Math.sin(longitude + time * spin - scan),
        Math.cos(longitude + time * spin - scan),
      );
      const boost = Math.exp(-(delta * delta) / 0.18) * Math.max(0, z);
      dots.push({
        alpha: 0.45 + 0.55 * Math.min(1, boost),
        radius: (1.05 + 2.975 * depth + 1.75 * boost) * radiusScale,
        white: 0.62 - 0.54 * depth,
        x,
        y,
        z,
      });
    }
  }
  paintOrbDots(context, dots, 0.525);
}
