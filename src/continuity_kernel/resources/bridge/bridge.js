import { startThinkingOrb, stopThinkingOrb } from "./thinking-orbs.js";
import {
  appendControlIntent,
  controlSystemCopy,
  controlSystemStatus,
  renderControlPanel,
} from "./bridge-controls.js";

const bridgeToken = captureBridgeToken();

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
  commitments: ["Work", "Everything you want to keep moving or revisit later."],
  mind: ["Context", "The saved notes and decisions you want Codex to use."],
  now: ["Your work in Codex, in one place.", "See what is open, in progress, waiting, or closed."],
  storylines: ["Related work", "Tasks and decisions that belong to the same larger effort."],
  system: ["System", "Your local files, Codex connection, and dashboard health."],
};

const state = {
  currentView: viewFromHash(),
  integrationRetryTimer: null,
  lastSuccessAt: null,
  previouslyFocused: null,
  selectedTaskId: null,
  snapshot: null,
  snapshotSignature: null,
};
let guidedReviewDraft = "";

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
window.setInterval(updateLiveLabels, 1_000);

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
    scheduleIntegrationRefresh(snapshot);
    return true;
  } catch (error) {
    const message = connectionMessage(error);
    if (state.snapshot) {
      setConnectionState("stale", `${message} Showing the last local snapshot.`);
    } else {
      setConnectionState("unavailable", message);
      renderUnavailable();
    }
    return false;
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
  if (codexReady(snapshot) && snapshot.codex.new_hand_url) {
    ui.openCodex.href = snapshot.codex.new_hand_url;
    ui.openCodex.hidden = false;
  } else {
    ui.openCodex.removeAttribute("href");
    ui.openCodex.hidden = true;
  }

  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  const taskProjection = projectionSection(snapshot, "tasks");
  ui.taskCount.textContent =
    taskProjection.state === "unavailable"
      ? "!"
      : taskProjection.state === "partial"
        ? `${openTasks.length}+`
        : String(openTasks.length);
  ui.taskCount.hidden = taskProjection.state === "complete" && openTasks.length === 0;
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
    textElement("p", "section-label", "Last saved update"),
    textElement("strong", "", "Current status"),
    textElement("span", "", revisionLabel(snapshot.now.revision)),
  );
  orientation.append(kicker, renderDocument(snapshot.now.content));
  fragment.append(orientation, renderContinuity(snapshot));

  const taskProjection = projectionSection(snapshot, "tasks");
  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  const completed = snapshot.tasks.filter((task) => ["done", "dropped"].includes(task.status));
  if (taskProjection.state !== "complete") {
    fragment.append(renderProjectionWarning(taskProjection, "work"));
    if (openTasks.length > 0) {
      fragment.append(renderOpenTaskHeading(openTasks.length), renderTaskBoard(openTasks, 3));
    }
    appendClosedHistory(fragment, completed);
    return fragment;
  }
  if (snapshot.tasks.length === 0) {
    fragment.append(renderFirstRun(snapshot));
    return fragment;
  }
  if (openTasks.length === 0) {
    fragment.append(renderAllClear(snapshot));
    appendClosedHistory(fragment, completed);
    return fragment;
  }

  fragment.append(renderOpenTaskHeading(openTasks.length), renderTaskBoard(openTasks, 3));
  return fragment;
}

function renderOpenTaskHeading(count) {
  const heading = element("div", "section-head");
  const headingText = element("div");
  headingText.append(
    textElement("p", "section-label", "Current records"),
    textElement("h2", "", "Open work"),
  );
  heading.append(headingText, textElement("p", "section-meta", `${count} open`));
  return heading;
}

function renderContinuity(snapshot) {
  const line = element("section", "continuity-line", { "aria-label": "GSV work flow" });
  const taskProjection = projectionSection(snapshot, "tasks");
  const open = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status)).length;
  const inMotion = snapshot.tasks.filter((task) => task.status === "doing").length;
  const openRecords =
    taskProjection.state === "unavailable"
      ? "Unavailable"
      : taskProjection.state === "partial"
        ? `${open} shown · partial`
        : open
          ? `${open} open`
          : "Clear";
  const integration = snapshot.codex.checking
    ? "Checking"
    : codexReady(snapshot)
      ? "Installed"
      : snapshot.codex.error
        ? "Status unknown"
        : snapshot.codex.available
          ? "Setup incomplete"
          : "Codex not found";
  const inProgress =
    taskProjection.state === "unavailable"
      ? "Unavailable"
      : taskProjection.state === "partial"
        ? `${inMotion} shown · partial`
        : String(inMotion);
  line.append(
    continuityStage("01", "Saved context", snapshot.mind.revision ? "Available" : "Not set up", "is-persistent"),
    continuityStage(
      "02",
      "Open work",
      openRecords,
      "is-held",
    ),
    continuityStage(
      "03",
      "Marked in progress",
      inProgress,
      "",
      inMotion ? "working" : "",
    ),
    continuityStage("04", "Codex integration", integration),
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
    textElement("p", "section-label", "Get started"),
    textElement("h2", "", "Tell Codex what you want GSV to keep track of."),
    textElement(
      "p",
      "",
      "Codex will ask a few questions and save the context you choose to keep.",
    ),
  );
  empty.append(copy, renderCodexAction(snapshot, "Start in Codex", "new_mind_url"));
  return empty;
}

function renderAllClear(snapshot) {
  const empty = element("section", "empty-state");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Work"),
    textElement("h2", "", "All clear"),
    textElement("p", "", "Nothing is open right now."),
  );
  empty.append(copy, renderCodexAction(snapshot, "Start a Codex task"));
  return empty;
}

function renderCommitments(snapshot) {
  const container = element("div");
  const taskProjection = projectionSection(snapshot, "tasks");
  const openTasks = snapshot.tasks.filter((task) => !["done", "dropped"].includes(task.status));
  const completed = snapshot.tasks.filter((task) => ["done", "dropped"].includes(task.status));
  if (taskProjection.state !== "complete") {
    container.append(renderProjectionWarning(taskProjection, "work"));
  }
  container.append(renderGuidedReview(snapshot));
  if (taskProjection.state === "complete" && snapshot.tasks.length === 0) {
    container.append(renderFirstRun(snapshot));
    return container;
  }
  if (taskProjection.state === "complete" && openTasks.length === 0) {
    container.append(renderAllClear(snapshot));
  } else if (openTasks.length > 0) {
    container.append(renderTaskBoard(openTasks));
  }
  appendClosedHistory(container, completed);
  return container;
}

function renderGuidedReview(snapshot) {
  const portfolio = snapshot.portfolio || {};
  const review = portfolio.review || {};
  const section = element("section", "guided-review");
  const head = element("div", "guided-review-head");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Guided all-open review"),
    textElement("h2", "", "Work through every open outcome"),
    textElement(
      "p",
      "guided-review-progress",
      review.state === "active"
        ? `${review.checked_count || 0} checked this session · ${review.uncovered_count || 0} still to check. Checked never means resolved.`
        : "One exact outcome at a time, with authored priority and native compare-and-swap changes.",
    ),
  );
  head.append(copy);
  section.append(head);

  if (!portfolio.available || review.state === "available" || review.state === "unavailable") {
    const empty = element("div", "guided-review-empty");
    empty.append(
      textElement(
        "p",
        "",
        portfolio.available
          ? "No finite review session is open. Start one Codex review hand; answers then travel through the authenticated Bridge intent queue."
          : "The complete authored Portfolio is not available yet. Codex must author it from the full open task set before review can begin.",
      ),
    );
    if (review.start_url) {
      const start = textElement("a", "primary-action", "Start the review hand");
      start.href = review.start_url;
      empty.append(start);
    }
    section.append(empty);
    return section;
  }

  if (review.issue) {
    const warning = element("div", "guided-review-warning", { role: "alert" });
    warning.append(
      textElement("strong", "", "Review state needs repair"),
      textElement("p", "", review.issue),
    );
    section.append(warning);
  }

  const subject = review.subject;
  const task = subject?.task;
  if (subject && task) {
    const card = element("article", "guided-review-subject");
    const facts = element("div", "guided-review-facts");
    facts.append(
      textElement("span", "status-pill", statusLabel(task)),
      textElement("span", "", `Portfolio ${subject.position} of ${review.open_count}`),
      textElement("span", "", task.rank === null ? "No authored rank" : `Rank ${task.rank}`),
    );
    card.append(
      facts,
      textElement("h3", "", task.title),
      textElement("p", "", task.outcome),
      textElement("strong", "guided-review-label", "The Mind recommends"),
      textElement("p", "", review.recommendation || "No recommendation has been authored yet."),
      textElement("strong", "guided-review-label", "One question"),
      textElement("p", "", review.question || "The current question has not been authored yet."),
    );
    section.append(card);
  }

  if (!review.active_thread_id) {
    const resume = element("div", "guided-review-empty");
    resume.append(
      textElement("p", "", "The session is durable, but no exact Codex hand is currently claimed."),
    );
    if (review.start_url) {
      const link = textElement("a", "primary-action", "Resume the review hand");
      link.href = review.start_url;
      resume.append(link);
    }
    section.append(resume);
    return section;
  }

  if (review.pending_intent) {
    const pending = element("div", "guided-review-empty", { role: "status" });
    pending.append(
      textElement("strong", "", "Your answer is queued"),
      textElement(
        "p",
        "",
        "The review agent must read current truth, apply any justified native CAS changes, and acknowledge this exact receipt before another answer is accepted.",
      ),
    );
    section.append(pending);
    return section;
  }

  if (!review.actionable || !subject || !task) return section;
  const form = element("form", "guided-review-form");
  const intentList = element("div", "guided-review-intents", {
    "aria-label": "Direction for the current exact outcome",
    role: "group",
  });
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const send = async (choice, trigger) => {
    trigger.disabled = true;
    status.textContent = "Saving your exact wording locally…";
    try {
      await appendControlIntent(snapshot, bridgeToken, {
        choice,
        kind: "correction",
        subject: `record:task/${review.session.identifier}`,
        target_revision: review.session_revision,
      });
      await loadSnapshot({ quiet: true });
      status.textContent = "Queued for the review agent to read. No task meaning changed in the browser.";
    } catch (error) {
      if (error.status === 409) {
        await loadSnapshot({ quiet: true });
        status.textContent = "The queue or review changed. Your draft is still here; review current truth and retry.";
      } else {
        status.textContent = error.message || "The review answer could not be queued.";
      }
    } finally {
      trigger.disabled = false;
    }
  };
  for (const [label, intent] of [
    ["Keep current", "keep"],
    ["Do / next", "act-next"],
    ["Defer", "defer"],
    ["Reprioritize", "reprioritize"],
    ["Edit", "reshape"],
    ["Drop / merge", "drop-or-merge"],
    ["Skip for now", "skip"],
  ]) {
    const button = textElement("button", "command-copy", label);
    button.type = "button";
    button.addEventListener("click", () => send(
      `For task:${task.identifier}, my explicit guided-review answer is: ${intent}. Interpret this in the exact current context; do not infer completion or broader authority.`,
      button,
    ));
    intentList.append(button);
  }
  const label = textElement("label", "guided-review-label", "Tell the Mind what you want");
  const input = element("textarea", "control-input", {
    maxlength: "4096",
    rows: "3",
  });
  input.id = "guided-review-answer";
  label.htmlFor = input.id;
  input.value = guidedReviewDraft;
  input.addEventListener("input", () => { guidedReviewDraft = input.value; });
  const submit = textElement("button", "primary-action", "Send and keep going");
  submit.type = "submit";
  const sessionActions = element("div", "guided-review-session-actions");
  for (const [labelText, intent] of [["Pause here", "pause"], ["End review", "end-review"]]) {
    const button = textElement("button", "quiet-action", labelText);
    button.type = "button";
    button.addEventListener("click", () => send(
      `For review session task:${review.session.identifier}, my explicit session instruction is: ${intent}. Preserve checked-versus-resolved semantics.`,
      button,
    ));
    sessionActions.append(button);
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const answer = input.value.trim();
    if (!answer) return;
    await send(
      `For task:${task.identifier}, my verbatim guided-review answer is:\n${answer}`,
      submit,
    );
    if (!status.textContent.startsWith("The queue or review changed")) {
      guidedReviewDraft = "";
      input.value = "";
    }
  });
  form.append(intentList, label, input, submit, sessionActions, status);
  section.append(form);
  return section;
}

function appendClosedHistory(container, tasks) {
  if (tasks.length === 0) return;
  const heading = element("div", "section-head");
  const headingText = element("div");
  headingText.append(
    textElement("p", "section-label", "History"),
    textElement("h2", "", "Closed work"),
  );
  heading.append(headingText, textElement("p", "section-meta", `${tasks.length} recorded`));
  heading.style.marginTop = "48px";
  container.append(heading, renderCompactTasks(tasks));
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
      const remaining = lane.tasks.length - visible.length;
      if (remaining > 0) {
        const disclosure = textElement(
          "button",
          "primary-action",
          `${remaining} more · View all work`,
        );
        disclosure.type = "button";
        disclosure.addEventListener("click", () => navigate("commitments"));
        list.append(disclosure);
      }
    }
    section.append(head, list);
    board.append(section);
  }
  return board;
}

function taskLanes(tasks) {
  const schemaOrder = ["captured", "ready", "doing", "waiting", "someday", "done", "dropped"];
  const statuses = [...new Set(tasks.map((task) => String(task.status)))].sort((left, right) => {
    const leftIndex = schemaOrder.indexOf(left);
    const rightIndex = schemaOrder.indexOf(right);
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right);
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });
  return statuses.map((status) => ({
    empty: "Nothing here.",
    tasks: tasks.filter((task) => String(task.status) === status),
    title: statusLabel({ status }),
  }));
}

function renderTaskCard(task) {
  const card = element("button", "task-card", { type: "button" });
  card.addEventListener("click", () => openInspector(task.identifier));
  const pillClass = `is-${task.status}`;
  const pill = textElement("span", `status-pill ${pillClass}`, statusLabel(task));
  card.append(
    pill,
    textElement("h4", "", task.title),
    textElement("p", "task-next", task.next_action || task.waiting_on || task.outcome),
  );
  const foot = element("div", "task-foot");
  foot.append(
    textElement("span", "", task.next_actor ? actorLabel(task.next_actor) : "Next step not assigned"),
    relativeTimeElement(task.updated_at),
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
      textElement("span", "system-state", statusLabel(task)),
    );
    list.append(row);
  }
  return list;
}

function renderStorylines(snapshot) {
  const container = element("div");
  const threadProjection = projectionSection(snapshot, "threads");
  const heading = element("div", "section-head");
  const headingText = element("div");
  headingText.append(
    textElement("p", "section-label", "Across tasks"),
    textElement("h2", "", "Work that belongs together"),
  );
  heading.append(headingText, textElement("p", "section-meta", `${snapshot.threads.length} total`));
  container.append(heading);
  if (threadProjection.state !== "complete") {
    container.append(renderProjectionWarning(threadProjection, "related work"));
  }
  if (snapshot.threads.length === 0) {
    if (threadProjection.state === "complete") {
      container.append(textElement("div", "empty-lane", "No related work has been saved yet."));
    }
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
    const taskCount = thread.task_ids.length;
    row.append(
      title,
      body,
      textElement("span", "system-state", `${taskCount} ${taskCount === 1 ? "task" : "tasks"}`),
    );
    list.append(row);
  }
  container.append(list);
  return container;
}

function renderMind(snapshot) {
  const entityProjection = projectionSection(snapshot, "entities");
  const layout = element("div", "mind-layout");
  const main = element("section");
  const head = element("div", "section-head");
  const headText = element("div");
  headText.append(
    textElement("p", "section-label", "Saved context"),
    textElement("h2", "", "What you want Codex to know"),
  );
  head.append(headText, textElement("p", "section-meta", revisionLabel(snapshot.mind.revision)));
  main.append(head, renderDocument(snapshot.mind.content));

  const side = element("aside", "mind-side");
  side.append(textElement("h3", "", "People and projects"));
  if (entityProjection.state !== "complete") {
    side.append(renderProjectionWarning(entityProjection, "people and projects"));
  }
  if (snapshot.entities.length === 0) {
    if (entityProjection.state === "complete") {
      side.append(textElement("p", "section-meta", "No people or projects have been saved yet."));
    }
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
    const remaining = snapshot.entities.length - 12;
    if (remaining > 0) {
      side.append(textElement("p", "section-meta", `${remaining} more not shown.`));
    }
  }
  layout.append(main, side);
  return layout;
}

function projectionSection(snapshot, name) {
  const section = snapshot.projection?.sections?.[name];
  if (section && ["complete", "partial", "unavailable"].includes(section.state)) return section;
  const fallback = Array.isArray(snapshot[name]) ? snapshot[name].length : 0;
  return { issues: [], readable: fallback, state: "unavailable", unreadable: 0 };
}

function renderProjectionWarning(section, label) {
  const warning = element("section", "unavailable-state");
  const title = section.state === "unavailable" ? `${capitalize(label)} unavailable` : `Some ${label} could not be read`;
  const paths = (section.issues || [])
    .map((issue) => issue.path)
    .filter(Boolean)
    .join(", ");
  const readable = Number(section.readable || 0);
  const summary =
    section.state === "unavailable"
      ? `GSV could not safely read these files. Review ${paths || label} with gsv doctor.`
      : `${readable} readable ${readable === 1 ? "record is" : "records are"} shown; some files could not be read. Review ${paths || label} with gsv doctor.`;
  warning.append(
    textElement("p", "section-label", "Integrity warning"),
    textElement("h2", "", title),
    textElement("p", "", summary),
  );
  return warning;
}

function renderSystem(snapshot) {
  const container = element("div");
  const list = element("div", "system-list");
  list.append(
    systemRow(
      "GSV files",
      `Version ${snapshot.bridge.version}; readable Markdown. Older processes cannot overwrite newer changes.`,
      snapshot.doctor.healthy ? "Healthy" : "Needs attention",
      snapshot.doctor.healthy ? "" : "is-error",
    ),
    systemRow(
      "Saved context",
      "Your context and current status files are available.",
      "Present",
      "",
    ),
    systemRow(
      "Codex",
      codexSystemCopy(snapshot),
      codexSystemStatus(snapshot),
      codexReady(snapshot) ? "" : "is-warning",
    ),
    systemRow(
      "Local dashboard",
      "Local canonical views plus a bounded queue for your explicit choices and corrections.",
      "Local",
      "",
    ),
    systemRow(
      "Bridge intent queue",
      controlSystemCopy(snapshot),
      controlSystemStatus(snapshot),
      snapshot.controls?.available || snapshot.bridge?.control_queue === false
        ? ""
        : "is-error",
    ),
    systemRow(
      "Automatic task updates",
      "GSV shows saved changes but does not change task status on its own.",
      "Off",
      "",
    ),
  );
  container.append(
    list,
    renderControlPanel(snapshot, {
      bridgeToken,
      currentControls: () => state.snapshot?.controls,
      refresh: async () => {
        if (!(await loadSnapshot({ quiet: true }))) {
          throw new Error("Bridge snapshot refresh failed");
        }
      },
    }),
  );
  if (!snapshot.doctor.healthy) container.append(renderDoctorRecovery(snapshot.doctor));
  if (!codexReady(snapshot) && !snapshot.codex.checking) {
    container.append(renderRecoveryCommand("Connect Codex", "gsv codex install"));
  }
  return container;
}

function renderDoctorRecovery(doctor) {
  const panel = element("section", "recovery-panel");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Integrity check"),
    textElement("h2", "", "Your GSV files need attention"),
  );
  const issues = element("ul", "issue-list");
  for (const issue of doctor.issues || []) {
    const item = element("li");
    item.append(
      textElement("strong", "", issue.path || issue.code || "File issue"),
      textElement("span", "", issue.message || "The integrity check did not pass."),
      textElement("small", "", issue.repairable ? "Repairable by GSV" : "Review required"),
    );
    issues.append(item);
  }
  const actions = element("div", "recovery-actions");
  if ((doctor.issues || []).some((issue) => issue.repairable)) {
    actions.append(commandAction("gsv doctor --repair"));
  }
  if ((doctor.issues || []).some((issue) => !issue.repairable)) {
    actions.append(commandAction("gsv doctor"));
  }
  panel.append(copy, issues, actions);
  return panel;
}

function renderRecoveryCommand(title, command) {
  const panel = element("section", "recovery-panel is-compact");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Recovery"),
    textElement("h2", "", title),
  );
  panel.append(copy, commandAction(command));
  return panel;
}

function commandAction(command) {
  const action = element("div", "command-action");
  const code = textElement("code", "", command);
  const button = textElement("button", "command-copy", "Copy command");
  button.type = "button";
  button.addEventListener("click", async () => {
    const copied = await copyText(command);
    button.textContent = copied ? "Copied" : "Select command";
    if (!copied) window.getSelection()?.selectAllChildren(code);
    window.setTimeout(() => {
      button.textContent = "Copy command";
    }, 1_600);
  });
  action.append(code, button);
  return action;
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch (_error) {
    return false;
  }
}

function renderCodexAction(snapshot, label, urlKey = "new_hand_url") {
  if (codexReady(snapshot) && snapshot.codex[urlKey]) {
    const action = textElement("a", "primary-action", label);
    action.href = snapshot.codex[urlKey];
    return action;
  }
  if (snapshot.codex.checking) {
    const checking = element("div", "checking-action");
    checking.append(createThinkingOrb("searching", "Checking Codex setup"));
    checking.append(textElement("span", "", "Checking Codex"));
    return checking;
  }
  const recovery = element("div", "codex-recovery");
  if (snapshot.codex.error) {
    recovery.append(textElement("p", "recovery-message", snapshot.codex.error));
  }
  recovery.append(commandAction("gsv codex install"));
  return recovery;
}

function codexReady(snapshot) {
  return snapshot.codex.ready === true;
}

function codexSystemCopy(snapshot) {
  if (snapshot.codex.checking) return "Checking the local Codex integration.";
  if (codexReady(snapshot)) {
    return "The GSV integration is installed. New-task links point Codex to your saved GSV record.";
  }
  if (snapshot.codex.error) return snapshot.codex.error;
  if (snapshot.codex.available) return "Codex is present, but its GSV integration is incomplete.";
  return "Your GSV files are available, but Codex could not be found.";
}

function codexSystemStatus(snapshot) {
  if (snapshot.codex.checking) return "Checking";
  if (codexReady(snapshot)) return "Installed";
  if (snapshot.codex.available) return "Run setup";
  return "Unavailable";
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
  ui.inspectorEyebrow.textContent = `${statusLabel(task)} work item`;
  ui.inspectorTitle.textContent = task.title;
  ui.inspectorBody.replaceChildren(
    detail("Outcome", task.outcome),
    detail("Next action", task.next_action || "Not recorded."),
    detail("Waiting on", task.waiting_on || "Nothing recorded."),
    detail("Record", `${task.identifier}\nUpdated ${task.updated_at}\nRevision ${task.revision}`),
  );
  if (codexReady(state.snapshot) && task.codex_url) {
    ui.continueInCodex.href = task.codex_url;
    ui.inspectorFoot.replaceChildren(ui.continueInCodex);
    ui.inspectorFoot.hidden = false;
  } else {
    ui.continueInCodex.removeAttribute("href");
    ui.inspectorFoot.replaceChildren(renderCodexAction(state.snapshot, "Start a Codex task"));
    ui.inspectorFoot.hidden = false;
  }
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
    ui.connectionCopy.textContent = "Your GSV files are readable, but an integrity check needs attention.";
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
    textElement("p", "section-label", "Dashboard unavailable"),
    textElement("h2", "", "Your local files have not been changed."),
    textElement("p", "", "Run gsv again to open a fresh private session."),
  );
  unavailable.append(copy);
  ui.view.replaceChildren(unavailable);
}

function connectionMessage(_error) {
  return "The private GSV dashboard could not be reached. Your local files were not changed.";
}

function updateFreshness() {
  if (!state.snapshot || !state.lastSuccessAt) return;
  if (Date.now() - state.lastSuccessAt > 30_000) {
    setConnectionState("stale", "Showing the last successful local snapshot while GSV reconnects.");
  }
}

function updateLiveLabels() {
  updateFreshness();
  for (const node of document.querySelectorAll("[data-relative-time]")) {
    node.textContent = relativeTime(node.dataset.relativeTime);
  }
}

function scheduleIntegrationRefresh(snapshot) {
  if (!snapshot.codex.checking) {
    if (state.integrationRetryTimer !== null) window.clearTimeout(state.integrationRetryTimer);
    state.integrationRetryTimer = null;
    return;
  }
  if (state.integrationRetryTimer !== null) return;
  state.integrationRetryTimer = window.setTimeout(() => {
    state.integrationRetryTimer = null;
    loadSnapshot({ quiet: true });
  }, 1_000);
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
  const labels = {
    captured: "Captured",
    doing: "Doing",
    done: "Done",
    dropped: "Dropped",
    ready: "Ready",
    someday: "Someday",
    waiting: "Waiting",
  };
  const status = String(task.status || "Unknown");
  return labels[status] || `${status.charAt(0).toUpperCase()}${status.slice(1)}`;
}

function capitalize(value) {
  const text = String(value || "");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

function actorLabel(actor) {
  return {
    agent: "Codex acts next",
    external: "Waiting outside GSV",
    human: "You act next",
  }[actor] || actor;
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

function relativeTimeElement(timestamp) {
  const node = textElement("span", "task-time", relativeTime(timestamp));
  node.dataset.relativeTime = timestamp;
  return node;
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
