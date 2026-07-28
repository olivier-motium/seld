import { startThinkingOrb, stopThinkingOrb } from "./thinking-orbs.js";
import {
  appendControlIntent,
  controlSystemCopy,
  controlSystemStatus,
  MAX_REVIEW_CONTROL_TEXT_BYTES,
  readReviewTurn,
  renderControlPanel,
  renderControlReviewActions,
  triggerReviewTurn,
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
let guidedReviewDelivery = null;
let guidedReviewNotice = "";
let guidedReviewNoticeContext = null;
let snapshotAppliedSequence = 0;
let snapshotRequestSequence = 0;
let guidedReviewSendPending = false;
let guidedReviewPollGeneration = 0;
let guidedReviewActivePollEventId = null;
let guidedReviewOverviewVisible = false;
let guidedReviewPreparedFocus = 0;
let guidedReviewPreparedBatch = {
  drafts: new Map(),
  editorOpen: false,
  editorSelection: new Set(),
  key: "",
  picks: new Map(),
};
let guidedReviewPreparedRecovery = null;
const guidedReviewRestoredPendingEvents = new Set();

const GUIDED_REVIEW_POLL_INTERVAL_MS = 750;
const GUIDED_REVIEW_POLL_LIMIT = 680;
const GUIDED_REVIEW_DISPOSITION_GRACE_MS = 2_250;

const guidedReviewOptionLabels = {
  "act-next": "Do / next",
  "defer": "Defer",
  "drop-or-merge": "Drop / merge",
  "keep": "Keep current",
  "reprioritize": "Reprioritize",
  "reshape": "Edit",
  "skip": "Skip for now",
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
window.setInterval(updateLiveLabels, 1_000);

async function loadSnapshot({ quiet = false } = {}) {
  const requestSequence = ++snapshotRequestSequence;
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
    if (requestSequence < snapshotAppliedSequence) return true;
    snapshotAppliedSequence = requestSequence;
    const snapshotSignature = JSON.stringify(snapshot);
    const snapshotChanged = snapshotSignature !== state.snapshotSignature;
    state.snapshot = snapshot;
    state.snapshotSignature = snapshotSignature;
    if (
      guidedReviewNotice &&
      guidedReviewNoticeContext !== null &&
      guidedReviewNoticeContext !== guidedReviewContextFingerprint(snapshot)
    ) {
      guidedReviewNotice = "";
      guidedReviewNoticeContext = null;
    }
    const review = snapshot.portfolio?.review || {};
    syncGuidedReviewDelivery(
      snapshot.guided_review_transport?.event,
      review.hand_url,
      review,
      snapshot.controls,
    );
    state.lastSuccessAt = Date.now();
    setConnectionState(state.snapshot.doctor.healthy ? "healthy" : "partial");
    if (snapshotChanged) render();
    scheduleIntegrationRefresh(snapshot);
    return true;
  } catch (error) {
    if (requestSequence < snapshotAppliedSequence) return true;
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
  const reviewState = snapshot.portfolio?.review?.state;
  if (["active", "paused"].includes(reviewState) && !guidedReviewOverviewVisible) {
    return container;
  }
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
  const transport = snapshot.guided_review_transport || {};
  const section = element("section", "guided-review");
  const head = element("div", "guided-review-head");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Guided all-open review"),
    textElement("h2", "", "Work through every open outcome"),
    textElement(
      "p",
      "guided-review-progress",
      ["active", "paused"].includes(review.state)
        ? `${review.checked_current_count ?? review.checked_count ?? 0} checked on current evidence · ${review.uncovered_count || 0} still to check. Checked never means resolved.`
        : "One exact outcome at a time, with authored priority and native compare-and-swap changes.",
    ),
  );
  head.append(copy);
  if (["active", "paused"].includes(review.state)) {
    const overview = textElement(
      "button",
      "quiet-action guided-review-overview-toggle",
      guidedReviewOverviewVisible ? "Return to review" : "Show all work",
    );
    overview.type = "button";
    overview.setAttribute("aria-expanded", guidedReviewOverviewVisible ? "true" : "false");
    overview.addEventListener("click", () => {
      guidedReviewOverviewVisible = !guidedReviewOverviewVisible;
      render();
    });
    head.append(overview);
  }
  section.append(head);
  if (guidedReviewNotice) {
    const notice = element("p", "control-status guided-review-notice", {
      "aria-live": "polite",
      role: "status",
    });
    notice.textContent = guidedReviewNotice;
    section.append(notice);
  }

  if (review.issue) {
    const warning = element("div", "guided-review-warning", { role: "alert" });
    warning.append(
      textElement("strong", "", "Review state needs repair"),
      textElement("p", "", review.issue),
    );
    const repairHandUrl = retainedExactGuidedReviewHandUrl(review.hand_url);
    if (repairHandUrl && !guidedReviewDelivery?.receipt) {
      warning.append(exactHandFallback(repairHandUrl));
    }
    if (guidedReviewDraft) {
      warning.append(guidedReviewDraftRecovery(
        "The review state changed before this answer was saved. Your draft remains here; repair current truth, then retry.",
      ));
    }
    section.append(warning);
    if (["available", "unavailable"].includes(review.state)) {
      const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
      if (delivery) section.append(delivery);
      return section;
    }
  }

  if (review.state === "finished") {
    const finished = element("div", "guided-review-empty is-finished", { role: "status" });
    finished.append(
      textElement("strong", "", "Nothing open is waiting for review"),
      textElement(
        "p",
        "",
        "Every currently open outcome has been checked on its current task and storyline versions. Open outcomes may still remain unresolved.",
      ),
    );
    const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
    if (delivery) finished.append(delivery);
    section.append(finished);
    return section;
  }

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
    const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
    if (delivery) empty.append(delivery);
    const queuedRecovery = renderGuidedReviewQueuedRecovery(snapshot, review);
    if (queuedRecovery) empty.append(queuedRecovery);
    if (
      review.state === "available" &&
      transport.enabled &&
      transport.automatic_start &&
      snapshot.controls?.available &&
      !review.pending_start &&
      !guidedReviewDeliveryBlocksInput()
    ) {
      const status = element("p", "control-status", {
        "aria-live": "polite",
        role: "status",
      });
      const start = textElement("button", "primary-action", "Start review here");
      start.type = "button";
      start.addEventListener("click", async () => {
        const queued = await queueGuidedReviewIntent(
          snapshot,
          {
            choice: "start-all-open-review",
            kind: "correction",
            subject: "mind:guided-review",
            target_revision: review.start_target_revision,
          },
          { handUrl: review.hand_url, status },
        );
        if (queued) status.textContent = "Starting the review in this Bridge…";
      });
      empty.append(start, status);
    } else if (review.start_url && !review.pending_start && !guidedReviewDeliveryBlocksInput()) {
      const start = textElement("a", "primary-action", "Start in Codex");
      start.href = review.start_url;
      empty.append(
        start,
        textElement(
          "p",
          "guided-review-capability-note",
          transport.enabled
            ? "Automatic continuation is not currently available on this installed path."
            : "Same-hand Bridge continuation is off in this foundation build.",
        ),
      );
    }
    section.append(empty);
    return section;
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
    const title = textElement("h3", "", task.title);
    title.id = "guided-review-current-title";
    card.setAttribute("aria-labelledby", title.id);
    card.append(facts, title, textElement("p", "guided-review-outcome", task.outcome));

    const current = element("dl", "guided-review-current");
    appendReviewFact(current, "Current next move", task.next_action || "No next move is authored.");
    appendReviewFact(current, "Waiting for", task.waiting_on || "Nothing is explicitly waiting.");
    if (subject.work_thread) {
      appendReviewFact(
        current,
        "Storyline",
        subject.work_thread.next_move || subject.work_thread.summary,
      );
    }
    card.append(current);

    const staleFacts = subject.staleness || [];
    if (staleFacts.length || subject.stale) {
      const evidenceWarning = element("div", "guided-review-evidence is-warning", {
        role: "status",
      });
      evidenceWarning.append(
        textElement("strong", "", "Evidence changed"),
        textElement(
          "p",
          "",
          staleFacts.join(" ") ||
            "The Portfolio anchor is older than the current task or storyline.",
        ),
      );
      card.append(evidenceWarning);
    }
    if ((subject.evidence_refs || []).length) {
      const evidence = element("div", "guided-review-evidence");
      evidence.append(textElement("strong", "guided-review-label", "Evidence on this outcome"));
      const list = element("ul", "guided-review-evidence-list");
      for (const reference of subject.evidence_refs) {
        list.append(textElement("li", "", reference));
      }
      evidence.append(list);
      card.append(evidence);
    } else {
      card.append(
        textElement(
          "p",
          "guided-review-evidence-note",
          "No separate evidence reference is authored on this outcome.",
        ),
      );
    }

    const judgment = element("div", "guided-review-judgment");
    const recommendation = element("div", "guided-review-recommendation");
    recommendation.append(
      textElement("strong", "guided-review-label", "The Mind recommends"),
      textElement("p", "", review.recommendation || "No recommendation has been authored yet."),
    );
    const question = element("div", "guided-review-question");
    question.append(
      textElement("strong", "guided-review-label", "One useful question"),
      textElement("p", "", review.question || "The current question has not been authored yet."),
    );
    judgment.append(recommendation, question);
    card.append(judgment);
    section.append(card);
  }

  const currentTurnReceipt = guidedReviewDelivery?.receipt || transport.event || null;
  const preparedReceipt = currentTurnReceipt?.sheet?.length ? currentTurnReceipt : null;
  const preparedSheet = normalizeGuidedReviewSheet(preparedReceipt?.sheet);
  const preparedSubjects = [...(preparedReceipt?.subject_task_ids || [])].sort();
  const sheetSubjects = preparedSheet.map((entry) => entry.task).sort();
  const preparedSubjectsMatch = Boolean(
    preparedSheet.length &&
    JSON.stringify(preparedSubjects) === JSON.stringify(sheetSubjects),
  );
  const preparedSessionMatches = Boolean(
    ["active", "paused"].includes(review.state) &&
    review.session_revision &&
    review.session_revision === preparedReceipt?.session_revision,
  );
  if (preparedSubjectsMatch && preparedSessionMatches) {
    section.append(renderGuidedReviewPreparedBoard(
      snapshot,
      review,
      preparedReceipt,
      preparedSheet,
    ));
    if (review.state === "paused") {
      section.append(renderGuidedReviewPausedPanel(snapshot, review));
    }
    const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
    if (delivery) section.append(delivery);
    return section;
  }
  const retainedPreparedSetChanged = Boolean(
    preparedSheet.length && (!preparedSubjectsMatch || !preparedSessionMatches),
  );
  if (retainedPreparedSetChanged && guidedReviewPreparedBatch.key) {
    preserveGuidedReviewPreparedRecovery(preparedSheet);
    guidedReviewPreparedBatch = {
      drafts: new Map(),
      editorOpen: false,
      editorSelection: new Set(),
      key: "",
      picks: new Map(),
    };
  }
  if (
    retainedPreparedSetChanged ||
    (
      !preparedSheet.length &&
      [
        "canonical_state_unavailable",
        "session_changed",
        "subject_mismatch",
        "turn_returned_no_answer",
        "unavailable_after_restart",
      ].includes(currentTurnReceipt?.sheet_state)
    )
  ) {
    const unavailable = element("div", "guided-review-warning", { role: "status" });
    const changed = retainedPreparedSetChanged || ["session_changed", "subject_mismatch"].includes(
      currentTurnReceipt?.sheet_state,
    );
    unavailable.append(
      textElement(
        "strong",
        "",
        changed ? "This prepared set is no longer current" : "The prepared set is unavailable",
      ),
      textElement(
        "p",
        "",
        currentTurnReceipt?.sheet_state === "turn_returned_no_answer"
          ? "The exact review hand completed without a user-facing answer. GSV did not reuse output from an earlier turn; continue the same hand to prepare the current set."
          : changed
          ? "The review session changed after this receipt. GSV withheld the old questions and picks instead of rebinding them to the new session."
          : "GSV does not persist model output as a hidden cache. Continue the exact review hand to prepare the current set again; the old answer will not be replayed.",
      ),
    );
    const recovered = renderGuidedReviewPreparedRecovery();
    if (recovered) unavailable.append(recovered);
    const exactHand = retainedExactGuidedReviewHandUrl(review.hand_url);
    if (exactHand) unavailable.append(exactHandFallback(exactHand));
    section.append(unavailable);
    const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
    if (delivery) section.append(delivery);
    return section;
  }
  if (review.prepared && !review.pending_intent) {
    const unavailable = element("div", "guided-review-warning", { role: "status" });
    unavailable.append(
      textElement("strong", "", "The prepared decisions are not available in this process"),
      textElement(
        "p",
        "",
        "GSV does not persist model output as a hidden preparation cache. Continue the exact review hand to regenerate a subject-bound board from current records.",
      ),
    );
    const exactHand = retainedExactGuidedReviewHandUrl(review.hand_url);
    if (exactHand) unavailable.append(exactHandFallback(exactHand));
    section.append(unavailable);
  }

  const delivery = renderGuidedReviewDelivery(review, snapshot.controls);
  if (delivery) {
    section.append(delivery);
    if (guidedReviewDeliveryBlocksInput()) return section;
  }

  if (!review.active_thread_id) {
    const resume = element("div", "guided-review-empty");
    resume.append(
      textElement(
        "p",
        "",
        review.issue
          ? "The session needs a fresh Codex repair hand before review can continue."
          : "The session is durable, but no exact Codex hand is currently claimed.",
      ),
    );
    if (review.start_url) {
      const link = textElement(
        "a",
        "primary-action",
        review.issue ? "Repair in Codex" : "Resume the review hand",
      );
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
        "The same review hand is reading current truth. It must apply any justified native CAS changes and acknowledge this exact receipt before another answer is accepted.",
      ),
    );
    const pendingHandUrl = retainedExactGuidedReviewHandUrl(review.hand_url);
    if (pendingHandUrl) pending.append(exactHandFallback(pendingHandUrl));
    const queuedRecovery = renderGuidedReviewQueuedRecovery(snapshot, review);
    if (queuedRecovery) pending.append(queuedRecovery);
    if (guidedReviewDraft && !review.issue) {
      pending.append(guidedReviewDraftRecovery(
        "The review queue changed before this answer was saved. Your draft remains here; retry after the current receipt is resolved.",
      ));
    }
    section.append(pending);
    return section;
  }

  if (review.state === "paused") {
    section.append(renderGuidedReviewPausedPanel(snapshot, review));
    return section;
  }

  if (!review.actionable || !subject || !task) return section;
  if (!transport.enabled || !transport.automatic_resume) {
    const fallback = element("div", "guided-review-empty", { role: "status" });
    fallback.append(
      textElement("strong", "", "Continue in the exact Codex hand"),
      textElement(
        "p",
        "",
        transport.enabled
          ? "The restricted same-hand continuation check did not pass, so Bridge will not queue an answer it cannot deliver."
          : "Same-hand Bridge continuation is off in this foundation build.",
      ),
    );
    const unavailableHandUrl = retainedExactGuidedReviewHandUrl(review.hand_url);
    if (unavailableHandUrl) fallback.append(exactHandFallback(unavailableHandUrl));
    section.append(fallback);
    return section;
  }
  const form = element("form", "guided-review-form");
  form.setAttribute("aria-labelledby", "guided-review-current-title");
  const intentList = element("div", "guided-review-intents", {
    "aria-label": "Ways to answer about this outcome",
    role: "group",
  });
  intentList.append(textElement("p", "guided-review-options-intro", "Choose a direction, or answer in your own words."));
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const send = (choice) => queueGuidedReviewIntent(
    snapshot,
    {
      choice,
      kind: "correction",
      subject: `record:task/${review.session.identifier}`,
      target_revision: review.session_revision,
    },
    { form, handUrl: review.hand_url, status },
  );
  const authoredOptions = (review.options || []).filter(
    (option) => guidedReviewOptionLabels[option.intent] && option.consequence,
  );
  for (const { consequence, intent } of authoredOptions) {
    const label = guidedReviewOptionLabels[intent];
    const button = element("button", "guided-review-option", {
      "aria-label": `${label}: ${consequence}`,
    });
    button.type = "button";
    button.append(
      textElement("strong", "", label),
      textElement("span", "", consequence),
    );
    button.addEventListener("click", () => send(consequence));
    intentList.append(button);
  }
  if (!authoredOptions.length) {
    intentList.append(
      textElement(
        "p",
        "guided-review-options-empty",
        "No quick choices are authored for this outcome. Answer in your own words.",
      ),
    );
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
    ));
    sessionActions.append(button);
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const answer = input.value.trim();
    if (!answer) return;
    const queued = await send(
      `For task:${task.identifier}, my verbatim guided-review answer is:\n${answer}`,
    );
    if (queued) {
      guidedReviewDraft = "";
      input.value = "";
    }
  });
  form.append(intentList, label, input, submit, sessionActions, status);
  section.append(form);
  return section;
}

function normalizeGuidedReviewSheet(rawSheet) {
  if (!Array.isArray(rawSheet) || !rawSheet.length || rawSheet.length > 25) return [];
  const seen = new Set();
  const entries = [];
  for (const raw of rawSheet) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const task = typeof raw.task === "string" ? raw.task : "";
    const question = guidedReviewVisibleLine(raw.question);
    const recommendation = guidedReviewVisibleLine(raw.recommendation);
    const reasoning = guidedReviewVisibleLine(raw.reasoning, 600);
    const dissent = guidedReviewOptionalLine(raw.dissent, 600);
    const group = guidedReviewOptionalLine(raw.group);
    const choices = normalizeGuidedReviewPreparedChoices(raw.choices);
    if (
      !/^[a-z0-9][a-z0-9-]{0,95}$/.test(task) || seen.has(task) || !question ||
      !recommendation || !reasoning || dissent === null || group === null || !choices.length
    ) return [];
    seen.add(task);
    entries.push({
      anchor: typeof raw.anchor === "string" ? raw.anchor : "",
      choices,
      currentAnchor: typeof raw.current_anchor === "string" ? raw.current_anchor : "",
      dissent,
      group,
      question,
      reasoning,
      recommendation,
      stale: raw.stale === true,
      task,
    });
  }
  return entries;
}

function normalizeGuidedReviewPreparedChoices(rawChoices) {
  if (!Array.isArray(rawChoices) || rawChoices.length < 2 || rawChoices.length > 5) return [];
  const choices = [];
  const seen = new Set();
  let recommended = 0;
  for (const raw of rawChoices) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
    const answer = guidedReviewVisibleLine(raw.answer);
    const effect = guidedReviewOptionalLine(raw.effect);
    if (!answer || effect === null || typeof raw.recommended !== "boolean") return [];
    const identity = answer.toLocaleLowerCase();
    if (seen.has(identity)) return [];
    seen.add(identity);
    recommended += raw.recommended ? 1 : 0;
    choices.push({ answer, effect, recommended: raw.recommended });
  }
  return recommended <= 1 ? choices : [];
}

function guidedReviewOptionalLine(value, limit = 200) {
  if (value === undefined || value === "") return "";
  return guidedReviewVisibleLine(value, limit) || null;
}

function guidedReviewVisibleLine(value, limit = 200) {
  if (typeof value !== "string") return "";
  const line = value.trim();
  if (!line || [...line].length > limit || /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(line)) return "";
  return line;
}

function syncGuidedReviewPreparedBatch(receipt, sheet) {
  const key = JSON.stringify([
    receipt.event_id,
    sheet.map((entry) => [entry.task, entry.anchor]),
  ]);
  if (key === guidedReviewPreparedBatch.key) return;
  guidedReviewPreparedBatch = {
    drafts: new Map(),
    editorOpen: false,
    editorSelection: new Set(sheet.map((entry) => entry.task)),
    key,
    picks: new Map(),
  };
  guidedReviewPreparedFocus = 0;
}

function preserveGuidedReviewPreparedRecovery(sheet) {
  const recovered = sheet.flatMap((entry) => {
    const draft = (guidedReviewPreparedBatch.drafts.get(entry.task) || "").trim();
    const picked = guidedReviewPreparedBatch.picks.get(entry.task)?.answer || "";
    const answer = draft || picked;
    return answer ? [`task:${entry.task}\nUnsent answer: ${answer}`] : [];
  });
  if (!recovered.length) return;
  guidedReviewPreparedRecovery = recovered.join("\n\n");
}

function renderGuidedReviewPreparedRecovery() {
  if (!guidedReviewPreparedRecovery) return null;
  const recovery = element("section", "guided-review-draft-recovery", { role: "status" });
  const label = textElement(
    "label",
    "guided-review-label",
    "Unsent prepared answers from the previous set",
  );
  const text = element("textarea", "control-input", { rows: "6" });
  text.id = "guided-review-prepared-recovery";
  text.readOnly = true;
  text.value = guidedReviewPreparedRecovery;
  label.htmlFor = text.id;
  const dismiss = textElement("button", "quiet-action", "Dismiss recovered answers");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    guidedReviewPreparedRecovery = null;
    render();
  });
  recovery.append(
    textElement("strong", "", "Your previous answers were not sent"),
    textElement(
      "p",
      "",
      "The review changed before the queue accepted them. GSV kept the wording below as plain recovery text and will not bind it to the refreshed set. Compare it with the current questions and choose again.",
    ),
    label,
    text,
    dismiss,
  );
  return recovery;
}

function restoreGuidedReviewPreparedFocus() {
  window.queueMicrotask(() => {
    document.querySelector('[aria-current="true"]')?.focus();
  });
}

function restoreGuidedReviewBatchFocus(taskId) {
  window.queueMicrotask(() => {
    document.getElementById(`guided-review-batch-${taskId}`)?.focus();
  });
}

function renderGuidedReviewPreparedBoard(snapshot, review, receipt, sheet) {
  syncGuidedReviewPreparedBatch(receipt, sheet);
  const board = element("section", "guided-review-prepared-board");
  const tasks = new Map((snapshot.tasks || []).map((task) => [task.identifier, task]));
  const form = element("form", "guided-review-prepared-form");
  const head = element("div", "guided-review-prepared-head");
  const headCopy = element("div");
  headCopy.append(
    textElement("p", "section-label", `${sheet.length} worked and waiting`),
    textElement("h3", "", "Decisions that genuinely need you"),
    textElement(
      "p",
      "",
      "Answer any number. Only answered rows travel; silence about a row means nothing was decided.",
    ),
  );
  const editBatch = textElement(
    "button",
    "quiet-action guided-review-edit-batch",
    guidedReviewPreparedBatch.editorOpen ? "Close batch editor" : "Edit batch",
  );
  editBatch.type = "button";
  editBatch.id = "guided-review-edit-batch";
  editBatch.setAttribute("aria-expanded", guidedReviewPreparedBatch.editorOpen ? "true" : "false");
  editBatch.addEventListener("click", () => {
    guidedReviewPreparedBatch.editorOpen = !guidedReviewPreparedBatch.editorOpen;
    render();
    window.queueMicrotask(() => document.getElementById("guided-review-edit-batch")?.focus());
  });
  head.append(headCopy, editBatch);
  form.append(head);
  if (guidedReviewPreparedBatch.editorOpen) {
    form.append(renderGuidedReviewBatchEditor(snapshot, review, form));
  }
  const list = element("div", "guided-review-prepared-list");
  sheet.forEach((entry, entryIndex) => {
    const task = tasks.get(entry.task);
    const card = element("article", "guided-review-prepared-card");
    card.dataset.preparedTask = entry.task;
    card.tabIndex = entryIndex === guidedReviewPreparedFocus ? 0 : -1;
    if (entryIndex === guidedReviewPreparedFocus) card.setAttribute("aria-current", "true");
    if (entry.stale) card.classList.add("is-stale");
    const heading = textElement("h4", "", task?.title || `task:${entry.task}`);
    heading.id = `prepared-${entry.task}`;
    card.setAttribute("aria-labelledby", heading.id);
    const facts = element("div", "guided-review-prepared-facts");
    if (entry.group) facts.append(textElement("span", "status-pill", entry.group));
    if (entry.stale) {
      facts.append(textElement("span", "guided-review-moved", "Moved since preparation"));
    }
    card.append(facts, heading);
    if (task?.outcome) card.append(textElement("p", "guided-review-outcome", task.outcome));
    card.append(
      textElement("p", "guided-review-prepared-question", entry.question),
      textElement("p", "guided-review-prepared-recommendation", entry.recommendation),
      textElement("p", "guided-review-prepared-reasoning", entry.reasoning),
    );
    if (entry.dissent) {
      const dissent = element("div", "guided-review-prepared-dissent");
      dissent.append(
        textElement("strong", "", "The Mind disagrees with you"),
        textElement("p", "", entry.dissent),
      );
      card.append(dissent);
    }
    const choices = element("div", "guided-review-prepared-choices", {
      "aria-label": `Prepared answers for ${task?.title || entry.task}`,
      role: "group",
    });
    const picked = guidedReviewPreparedBatch.picks.get(entry.task)?.answer || "";
    entry.choices.forEach((choice, index) => {
      const button = element(
        "button",
        `guided-review-prepared-choice${choice.recommended ? " is-recommended" : ""}`
          + `${picked === choice.answer ? " is-picked" : ""}`,
      );
      button.type = "button";
      button.setAttribute("aria-pressed", picked === choice.answer ? "true" : "false");
      button.append(
        textElement("span", "guided-review-prepared-key", String(index + 1)),
        textElement("span", "guided-review-prepared-answer", choice.answer),
      );
      if (choice.effect) {
        button.append(textElement("span", "guided-review-prepared-effect", choice.effect));
      }
      button.addEventListener("click", () => {
        guidedReviewPreparedBatch.picks.set(entry.task, {
          answer: choice.answer,
          effect: choice.effect,
          stale: entry.stale,
        });
        guidedReviewPreparedBatch.drafts.delete(entry.task);
        render();
        restoreGuidedReviewPreparedFocus();
      });
      choices.append(button);
    });
    const label = textElement("label", "guided-review-prepared-own-label", "Or answer in your own words");
    const own = element("textarea", "control-input guided-review-prepared-own", {
      maxlength: "1000",
      rows: "2",
    });
    own.id = `prepared-answer-${entry.task}`;
    label.htmlFor = own.id;
    own.value = guidedReviewPreparedBatch.drafts.get(entry.task) || "";
    own.addEventListener("input", () => {
      const answer = own.value;
      guidedReviewPreparedBatch.drafts.set(entry.task, answer);
      if (answer.trim()) {
        guidedReviewPreparedBatch.picks.set(entry.task, {
          answer: answer.trim(),
          effect: "",
          stale: entry.stale,
        });
      } else {
        guidedReviewPreparedBatch.picks.delete(entry.task);
      }
      const answered = sheet.filter((item) =>
        guidedReviewPreparedBatch.picks.has(item.task)
      ).length;
      status.textContent = `${answered} of ${sheet.length} answered. Unanswered rows will not be sent.`;
    });
    card.append(choices, label, own);
    list.append(card);
  });
  const actions = element("div", "guided-review-prepared-actions");
  const send = textElement("button", "primary-action", "Send answered decisions");
  send.type = "submit";
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const answered = sheet.filter((entry) => guidedReviewPreparedBatch.picks.has(entry.task)).length;
  status.textContent = `${answered} of ${sheet.length} answered. Unanswered rows will not be sent.`;
  actions.append(send, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const decisions = sheet.flatMap((entry) => {
      const pick = guidedReviewPreparedBatch.picks.get(entry.task);
      return pick ? [{ task: entry.task, ...pick, stale: entry.stale || pick.stale }] : [];
    });
    const choice = guidedReviewPreparedInstruction(decisions);
    if (!choice) {
      status.textContent = "Choose or write at least one answer first.";
      return;
    }
    if (new TextEncoder().encode(choice).byteLength > MAX_REVIEW_CONTROL_TEXT_BYTES) {
      status.textContent = "These answers exceed the 24,000-byte local limit. Shorten a free-form answer; nothing was sent.";
      return;
    }
    await queueGuidedReviewIntent(
      snapshot,
      {
        choice,
        kind: "correction",
        subject: `record:task/${review.session.identifier}`,
        target_revision: review.session_revision,
      },
      {
        form,
        handUrl: review.hand_url,
        maxBytes: MAX_REVIEW_CONTROL_TEXT_BYTES,
        status,
      },
    );
  });
  form.addEventListener("keydown", (event) => {
    if (event.target instanceof HTMLTextAreaElement) return;
    if (["j", "ArrowDown", "k", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      const direction = ["j", "ArrowDown"].includes(event.key) ? 1 : -1;
      guidedReviewPreparedFocus = Math.max(
        0,
        Math.min(sheet.length - 1, guidedReviewPreparedFocus + direction),
      );
      render();
      restoreGuidedReviewPreparedFocus();
      return;
    }
    const choiceNumber = Number.parseInt(event.key, 10);
    if (choiceNumber >= 1 && choiceNumber <= 5) {
      const focused = list.children[guidedReviewPreparedFocus];
      const button = focused?.querySelectorAll(".guided-review-prepared-choice")[choiceNumber - 1];
      if (button) {
        event.preventDefault();
        button.click();
      }
      return;
    }
    if (event.key === "Escape") {
      const entry = sheet[guidedReviewPreparedFocus];
      if (!entry) return;
      event.preventDefault();
      guidedReviewPreparedBatch.picks.delete(entry.task);
      guidedReviewPreparedBatch.drafts.delete(entry.task);
      render();
      restoreGuidedReviewPreparedFocus();
    }
  });
  form.append(list, actions, renderGuidedReviewPreparedSessionControls(snapshot, review, sheet, form));
  if (review.state === "paused" || guidedReviewDeliveryBlocksInput()) {
    setGuidedReviewControlsDisabled(form, true);
  }
  board.append(form);
  return board;
}

function renderGuidedReviewBatchEditor(snapshot, review, form) {
  const editor = element("section", "guided-review-batch-editor");
  editor.append(
    textElement("h4", "", "Choose the exact outcomes to prepare"),
    textElement(
      "p",
      "",
      "This only changes what the review brings forward next. Selecting an outcome does not change it, decide it, or mark it checked.",
    ),
  );
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const openTasks = (snapshot.tasks || []).filter(
    (task) => !["done", "dropped"].includes(task.status) && task.identifier !== review.session.identifier,
  );
  const options = element("div", "guided-review-batch-options", {
    "aria-label": "Open outcomes available for this prepared set",
    role: "group",
  });
  for (const task of openTasks) {
    const label = element("label", "guided-review-batch-option");
    const input = element("input");
    input.type = "checkbox";
    input.id = `guided-review-batch-${task.identifier}`;
    input.checked = guidedReviewPreparedBatch.editorSelection.has(task.identifier);
    input.addEventListener("change", () => {
      if (input.checked && guidedReviewPreparedBatch.editorSelection.size >= 25) {
        input.checked = false;
        status.textContent = "One prepared set can contain at most 25 outcomes.";
        return;
      }
      if (input.checked) guidedReviewPreparedBatch.editorSelection.add(task.identifier);
      else guidedReviewPreparedBatch.editorSelection.delete(task.identifier);
      render();
      restoreGuidedReviewBatchFocus(task.identifier);
    });
    const copy = element("span");
    copy.append(
      textElement("strong", "", task.title),
      textElement("span", "", task.outcome),
    );
    label.append(input, copy);
    options.append(label);
  }
  const actions = element("div", "guided-review-batch-actions");
  const prepare = textElement(
    "button",
    "primary-action",
    `Prepare these ${guidedReviewPreparedBatch.editorSelection.size}`,
  );
  prepare.type = "button";
  prepare.disabled = guidedReviewPreparedBatch.editorSelection.size === 0;
  prepare.addEventListener("click", async () => {
    const selected = openTasks
      .map((task) => task.identifier)
      .filter((identifier) => guidedReviewPreparedBatch.editorSelection.has(identifier));
    const choice = guidedReviewEditBatchInstruction(selected);
    if (!choice) {
      status.textContent = "Choose at least one open outcome first.";
      return;
    }
    await queueGuidedReviewIntent(
      snapshot,
      {
        choice,
        kind: "correction",
        subject: `record:task/${review.session.identifier}`,
        target_revision: review.session_revision,
      },
      {
        conflictMessage: "The review queue changed before this batch request was saved. Your selected outcomes are still here; check the refreshed review, then retry.",
        form,
        handUrl: review.hand_url,
        maxBytes: MAX_REVIEW_CONTROL_TEXT_BYTES,
        status,
      },
    );
  });
  status.textContent = `${guidedReviewPreparedBatch.editorSelection.size} selected · 25 maximum`;
  actions.append(prepare, status);
  editor.append(options, actions);
  return editor;
}

function renderGuidedReviewPausedPanel(snapshot, review) {
  const transport = snapshot.guided_review_transport || {};
  const paused = element("div", "guided-review-empty", { role: "status" });
  paused.append(
    textElement("strong", "", "Review paused at this exact set"),
    textElement("p", "", "Resume continues the same durable session and Codex hand."),
  );
  if (
    transport.enabled &&
    transport.automatic_resume &&
    snapshot.controls?.available &&
    review.session_revision
  ) {
    const resume = textElement("button", "primary-action", "Resume review here");
    const status = element("p", "control-status", {
      "aria-live": "polite",
      role: "status",
    });
    resume.type = "button";
    resume.addEventListener("click", async () => {
      const queued = await queueGuidedReviewIntent(
        snapshot,
        {
          choice: "resume-guided-review",
          kind: "correction",
          subject: `record:task/${review.session.identifier}`,
          target_revision: review.session_revision,
        },
        { handUrl: review.hand_url, status },
      );
      if (queued) status.textContent = "Resuming the exact review hand…";
    });
    paused.append(resume, status);
  }
  const pausedHandUrl = retainedExactGuidedReviewHandUrl(review.hand_url);
  if (pausedHandUrl) paused.append(exactHandFallback(pausedHandUrl));
  return paused;
}

function renderGuidedReviewPreparedSessionControls(snapshot, review, sheet, form) {
  const controls = element("section", "guided-review-prepared-session");
  const label = textElement("label", "guided-review-label", "Tell the Mind something about this set");
  const input = element("textarea", "control-input", { maxlength: "4096", rows: "3" });
  input.id = "guided-review-answer";
  label.htmlFor = input.id;
  input.value = guidedReviewDraft;
  input.addEventListener("input", () => { guidedReviewDraft = input.value; });
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const send = (choice, { clearDraft = false } = {}) => queueGuidedReviewIntent(
    snapshot,
    {
      choice,
      kind: "correction",
      subject: `record:task/${review.session.identifier}`,
      target_revision: review.session_revision,
    },
    {
      form,
      handUrl: review.hand_url,
      maxBytes: MAX_REVIEW_CONTROL_TEXT_BYTES,
      status,
    },
  ).then((queued) => {
    if (queued && clearDraft) {
      guidedReviewDraft = "";
      input.value = "";
    }
    return queued;
  });
  const sendFreeform = textElement("button", "primary-action", "Send note and keep going");
  sendFreeform.type = "button";
  sendFreeform.addEventListener("click", async () => {
    const choice = guidedReviewPreparedFreeformInstruction(guidedReviewDraft, sheet);
    if (!choice) {
      status.textContent = "Write a note first.";
      input.focus();
      return;
    }
    await send(choice, { clearDraft: true });
  });
  const sessionActions = element("div", "guided-review-session-actions", {
    "aria-label": "Guided review session controls",
    role: "group",
  });
  const pause = textElement("button", "quiet-action", "Pause here");
  pause.type = "button";
  pause.addEventListener("click", () => send(guidedReviewPreparedPauseInstruction(sheet)));
  const end = textElement("button", "quiet-action", "End review");
  end.type = "button";
  end.addEventListener("click", () => send(guidedReviewPreparedEndInstruction(sheet)));
  sessionActions.append(pause, end);
  controls.append(label, input, sendFreeform, sessionActions, status);
  return controls;
}

function guidedReviewPreparedInstruction(decisions) {
  if (!Array.isArray(decisions) || !decisions.length) return "";
  const rows = decisions.map((decision) => {
    const effect = decision.effect
      ? `\nThe consequence shown for that answer: ${decision.effect}`
      : "";
    const moved = decision.stale
      ? "\nThis Task moved after preparation. Fresh-read it before applying anything, and say if the recommendation no longer holds."
      : "";
    return `task:${decision.task}\nMy answer: ${decision.answer}${effect}${moved}`;
  });
  return "For the authored review-scope:all-open session, I answered only these prepared decisions:\n\n"
    + `${rows.join("\n\n")}\n\n`
    + "Each answer is my explicit disposition for that one exact outcome. Any prepared outcome not named here remains undecided: do not infer, apply, or cover it. Handle each named outcome independently through fresh native CAS and readback; add current anchored coverage only after that exact disposition is durable. There is no group write: apply safe rows, retain stale or failed rows, and report each one that did not land instead of claiming the set succeeded. Then prepare the next intervention batch without repeating the opening scan.";
}

function guidedReviewEditBatchInstruction(taskIds) {
  if (!Array.isArray(taskIds) || !taskIds.length || taskIds.length > 25) return "";
  const rows = taskIds.map((identifier) => `task:${identifier}`).join("\n");
  return "For the authored review-scope:all-open session, replace the prepared subject set with exactly these open outcomes:\n\n"
    + `${rows}\n\n`
    + "This selection is an explicit navigation request only. It is not a semantic disposition, does not change any Task, WorkThread, Direction, or Portfolio judgment, and must add no review coverage. Fresh-read every named outcome, reject any missing or terminal row, replace the subject refs through fresh review-session CAS, and prepare a new transient bridge-sheet for the safe current rows. An explicit pull may bring forward a row even when the silent intervention threshold would not have surfaced it. Every unselected or previously prepared outcome remains open and uncovered unless separately checked with me.";
}

function guidedReviewPreparedFreeformInstruction(answer, sheet) {
  const value = typeof answer === "string" ? answer.trim() : "";
  if (!value || !Array.isArray(sheet) || !sheet.length) return "";
  const subjects = sheet.map((entry) => `task:${entry.task}`).join(", ");
  return `For the current prepared review set (${subjects}), my free-form instruction is:\n${value}\n\nApply only what I explicitly said. Do not infer a decision for an unanswered row or add coverage merely because it was visible. Use fresh native CAS and readback for any justified semantic change, retain every undecided or failed row, then prepare the next useful intervention set.`;
}

function guidedReviewPreparedPauseInstruction(sheet) {
  const subjects = sheet.map((entry) => `task:${entry.task}`).join(", ");
  return `Pause this guided all-open review now at the current prepared set (${subjects}). Do not change or cover any outcome merely because I paused. Preserve the all-open scope, exact subject refs, current anchored coverage, review WorkThread focus, and this exact hand; add only review-state:paused through fresh review-session CAS and read it back.`;
}

function guidedReviewPreparedEndInstruction(sheet) {
  const subjects = sheet.map((entry) => `task:${entry.task}`).join(", ");
  return `End this guided all-open review now because I explicitly asked to stop. The current prepared set is ${subjects}. Do not change, complete, cover, or imply a decision about any outcome merely because the review ends. Through fresh CAS, first clear the review WorkThread focus, then terminalize only the bounded review-session Task and clear its subject refs, paused state, active hand, shadow refs, and future-work fields. Preserve all-open scope and existing anchored coverage as session provenance, read back the terminal state, and say plainly that unchecked outcomes remain open.`;
}

function appendReviewFact(list, label, value) {
  const fact = element("div");
  fact.append(textElement("dt", "", label), textElement("dd", "", value));
  list.append(fact);
}

function exactHandFallback(url) {
  const link = textElement("a", "quiet-action", "Open the exact review hand");
  link.href = url;
  return link;
}

function guidedReviewDraftRecovery(message) {
  const recovery = element("div", "guided-review-draft-recovery");
  const label = textElement("label", "guided-review-label", "Your unsent answer");
  const draft = element("textarea", "control-input", {
    maxlength: "4096",
    rows: "3",
  });
  draft.id = "guided-review-answer";
  label.htmlFor = draft.id;
  draft.value = guidedReviewDraft;
  draft.addEventListener("input", () => { guidedReviewDraft = draft.value; });
  recovery.append(label, draft, textElement("p", "control-status", message));
  return recovery;
}

function setGuidedReviewControlsDisabled(form, disabled) {
  for (const control of form.querySelectorAll("button, textarea")) {
    control.disabled = disabled;
  }
  form.setAttribute("aria-busy", disabled ? "true" : "false");
}

function renderGuidedReviewQueuedRecovery(snapshot, review) {
  if (guidedReviewDelivery?.receipt) return null;
  const pending = review.pending_intent || review.pending_start;
  const eventId = pending?.event_id;
  if (!eventId) return null;
  const transport = snapshot.guided_review_transport || {};
  const isStart = review.pending_start?.event_id === eventId;
  const canContinue = transport.enabled && (
    isStart ? transport.automatic_start : transport.automatic_resume
  );
  if (!canContinue) return null;

  const recovery = element("div", "guided-review-queued-recovery");
  const button = textElement(
    "button",
    "primary-action",
    isStart ? "Continue saved review start" : "Continue saved answer",
  );
  button.type = "button";
  button.addEventListener("click", () => {
    if (guidedReviewSendPending || guidedReviewActivePollEventId === eventId) return;
    guidedReviewDelivery = {
      checkedAt: null,
      eventId,
      handUrl: review.hand_url || null,
      liveCheckedAt: null,
      message: "This exact intent was already saved. GSV is continuing its existing receipt without appending or replaying it.",
      pendingSeen: true,
      queueRevision: snapshot.controls?.queue_revision || null,
      resolvedAt: null,
      startedAt: Date.now(),
      receipt: {
        event_id: eventId,
        final_answer: null,
        mode: isStart ? "start" : "resume",
        reason_code: null,
        retryable: false,
        state: "pending",
        terminal: false,
        thread_id: review.active_thread_id || null,
      },
    };
    render();
    void continueGuidedReviewTurn(eventId, review.hand_url || null);
  });
  recovery.append(
    button,
    textElement(
      "small",
      "guided-review-support",
      "Uses the already queued event ID. It does not append the answer again.",
    ),
  );
  return recovery;
}

async function queueGuidedReviewIntent(
  snapshot,
  intent,
  { conflictMessage = null, form = null, handUrl = null, maxBytes = 4096, status },
) {
  if (guidedReviewSendPending) return false;
  guidedReviewSendPending = true;
  if (form) setGuidedReviewControlsDisabled(form, true);
  status.textContent = "Saving your exact wording locally…";
  guidedReviewNotice = "";
  guidedReviewNoticeContext = null;
  try {
    const payload = await appendControlIntent(snapshot, bridgeToken, intent, { maxBytes });
    const eventId = payload.event?.event_id;
    if (!eventId) throw new Error("The Bridge saved no exact review receipt.");
    guidedReviewDelivery = {
      checkedAt: null,
      eventId,
      handUrl,
      liveCheckedAt: null,
      message: "Your exact answer is saved locally once. You can leave this view; the review keeps its place.",
      pendingSeen: false,
      queueRevision: payload.revision || null,
      resolvedAt: null,
      startedAt: Date.now(),
      receipt: payload.transport || {
        event_id: eventId,
        final_answer: null,
        mode: intent.subject === "mind:guided-review" ? "start" : "resume",
        reason_code: null,
        retryable: false,
        state: "pending",
        terminal: false,
        thread_id: null,
      },
    };
    render();
    void continueGuidedReviewTurn(eventId, handUrl);
    return true;
  } catch (error) {
    if (error.status === 409) {
      guidedReviewNotice = conflictMessage || "The review queue changed while you were answering. Your draft is still here; review the refreshed outcome, then retry.";
      const refreshed = await loadSnapshot({ quiet: true });
      if (!refreshed) {
        guidedReviewNotice = conflictMessage || "The review queue changed while you were answering. Your draft is still here; refresh current truth, then retry.";
      }
      guidedReviewNoticeContext = guidedReviewContextFingerprint(state.snapshot);
      render();
      const currentInput = document.querySelector("#guided-review-answer");
      if (currentInput) {
        currentInput.value = guidedReviewDraft;
        currentInput.focus();
      }
    } else {
      status.textContent = error.message || "The review answer could not be queued.";
    }
    return false;
  } finally {
    guidedReviewSendPending = false;
    const currentForm = document.querySelector(
      ".guided-review-form, .guided-review-prepared-form",
    );
    if (currentForm) setGuidedReviewControlsDisabled(
      currentForm,
      guidedReviewDeliveryBlocksInput(),
    );
  }
}

async function continueGuidedReviewTurn(eventId, handUrl) {
  const generation = ++guidedReviewPollGeneration;
  guidedReviewActivePollEventId = eventId;
  try {
    const triggered = await triggerReviewTurn(bridgeToken, eventId);
    if (generation !== guidedReviewPollGeneration) return;
    if (!updateGuidedReviewReceipt(triggered, handUrl, eventId)) {
      throw new Error("Bridge returned a different review receipt.");
    }
    const triggeredCurrent = guidedReviewDelivery?.eventId === eventId
      ? guidedReviewDelivery.receipt
      : triggered;
    if (guidedReviewReceiptStopsPolling(triggeredCurrent)) {
      await finishGuidedReviewDelivery(triggeredCurrent);
      return;
    }
    for (let attempt = 0; attempt < GUIDED_REVIEW_POLL_LIMIT; attempt += 1) {
      await wait(GUIDED_REVIEW_POLL_INTERVAL_MS);
      if (generation !== guidedReviewPollGeneration) return;
      const receipt = await readReviewTurn(bridgeToken, eventId);
      if (generation !== guidedReviewPollGeneration) return;
      if (!updateGuidedReviewReceipt(receipt, handUrl, eventId)) {
        throw new Error("Bridge returned a different review receipt.");
      }
      const currentReceipt = guidedReviewDelivery?.eventId === eventId
        ? guidedReviewDelivery.receipt
        : receipt;
      if (guidedReviewReceiptStopsPolling(currentReceipt)) {
        await finishGuidedReviewDelivery(currentReceipt);
        return;
      }
    }
    updateGuidedReviewMessage(
      "The review hand is still working. GSV will not resend your answer; use the exact hand if you need to inspect it now.",
    );
  } catch (error) {
    if (guidedReviewDelivery?.eventId === eventId) {
      guidedReviewDelivery = { ...guidedReviewDelivery, liveCheckedAt: null };
    }
    updateGuidedReviewMessage(
      error.status === 409
        ? "The review changed after this answer was queued. Your wording remains saved once and was not replayed. Reload current truth, then reconcile the queued receipt before retrying."
        : error.status === 404
          ? "The review receipt is unavailable. Reload current truth; do not retry until the queued intent is reconciled."
          : "Bridge could not read the review receipt. Your queued answer was not replayed; inspect the exact hand before retrying.",
    );
    await loadSnapshot({ quiet: true });
  } finally {
    if (generation === guidedReviewPollGeneration) guidedReviewActivePollEventId = null;
  }
}

function guidedReviewReceiptStopsPolling(receipt) {
  return receipt?.terminal === true || receipt?.retryable === true;
}

function guidedReviewContextFingerprint(snapshot) {
  const review = snapshot?.portfolio?.review || {};
  return JSON.stringify([
    snapshot?.controls?.queue_revision || null,
    snapshot?.portfolio?.revision || null,
    review.issue || null,
    review.pending_intent?.event_id || null,
    review.pending_start?.event_id || null,
    review.session_revision || null,
    review.state || null,
  ]);
}

async function finishGuidedReviewDelivery(receipt) {
  if (receipt.state === "completed") {
    updateGuidedReviewMessage(
      receipt.final_answer
        ? "The same review hand replied. Canonical state has been refreshed below."
        : "The review turn completed. Canonical state has been refreshed below.",
    );
  } else if (receipt.state === "failed_safe") {
    updateGuidedReviewMessage(
      "The turn was proven not to have been delivered. You may retry this exact receipt once the cause is fixed.",
    );
  } else if (receipt.state === "delivery_uncertain") {
    updateGuidedReviewMessage(
      receipt.thread_id
        ? "GSV cannot prove whether Codex received this answer. It will not resend it; reconcile the exact hand and canonical records."
        : "GSV cannot recover the Codex hand for this turn. It will not resend it; inspect recent Codex tasks and canonical records before deciding what to do.",
    );
  } else {
    updateGuidedReviewMessage(
      "Automatic continuation is blocked. Your local receipt remains visible and has not been replayed.",
    );
  }
  await loadSnapshot({ quiet: true });
}

function updateGuidedReviewReceipt(receipt, handUrl, expectedEventId = null) {
  if (
    !receipt?.event_id ||
    (expectedEventId !== null && receipt.event_id !== expectedEventId)
  ) return false;
  const previous = guidedReviewDelivery?.eventId === receipt.event_id
    ? guidedReviewDelivery.receipt
    : null;
  const selectedReceipt = newerGuidedReviewReceipt(previous, receipt);
  const stateChanged = guidedReviewReceiptFingerprint(previous) !==
    guidedReviewReceiptFingerprint(selectedReceipt);
  guidedReviewDelivery = {
    checkedAt: Date.now(),
    eventId: receipt.event_id,
    handUrl: handUrl || guidedReviewDelivery?.handUrl || null,
    liveCheckedAt:
      selectedReceipt.state === "running" && exactGuidedReviewHandUrl(selectedReceipt.thread_id)
        ? Date.now()
        : null,
    message: stateChanged ? null : guidedReviewDelivery?.message || null,
    pendingSeen: guidedReviewDelivery?.pendingSeen || false,
    queueRevision: guidedReviewDelivery?.queueRevision || null,
    resolvedAt: guidedReviewDelivery?.resolvedAt || null,
    startedAt: guidedReviewStartedAt(selectedReceipt, guidedReviewDelivery),
    receipt: {
      ...selectedReceipt,
      final_answer: selectedReceipt.final_answer || previous?.final_answer || null,
    },
  };
  if (stateChanged) {
    render();
    window.queueMicrotask(() => pulseGuidedReviewObservation(receipt.event_id));
  } else {
    updateGuidedReviewActivity();
    pulseGuidedReviewObservation(receipt.event_id);
  }
  return true;
}

function updateGuidedReviewMessage(message) {
  if (!guidedReviewDelivery) return;
  guidedReviewDelivery = { ...guidedReviewDelivery, message };
  render();
}

function syncGuidedReviewDelivery(receipt, handUrl, review, controls) {
  if (!receipt?.event_id) {
    const current = guidedReviewDelivery?.receipt;
    const pendingEventIds = [review?.pending_intent?.event_id, review?.pending_start?.event_id]
      .filter(Boolean);
    if (current?.event_id && pendingEventIds.includes(current.event_id)) {
      guidedReviewDelivery = { ...guidedReviewDelivery, pendingSeen: true };
      return;
    }
    const observedQueuedRevision =
      guidedReviewDelivery?.queueRevision &&
      controls?.queue_revision === guidedReviewDelivery.queueRevision;
    const resolvedVisible = [...(controls?.items || []), ...(controls?.history || [])].some(
      (item) =>
        item?.event?.event_id === current?.event_id &&
        ["accepted", "rejected"].includes(item?.status),
    );
    if (
      current?.event_id &&
      current.state !== "completed" &&
      review?.state !== "unavailable" &&
      controls?.available === true &&
      (guidedReviewDelivery?.pendingSeen === true || observedQueuedRevision || resolvedVisible)
    ) {
      if (guidedReviewActivePollEventId === current.event_id) {
        if (!guidedReviewDelivery.resolvedAt) {
          const resolvedAt = Date.now();
          guidedReviewDelivery = { ...guidedReviewDelivery, resolvedAt };
          window.setTimeout(() => {
            const delivery = guidedReviewDelivery;
            if (
              delivery?.eventId !== current.event_id ||
              delivery.receipt?.state === "completed" ||
              delivery.resolvedAt !== resolvedAt ||
              Date.now() - resolvedAt < GUIDED_REVIEW_DISPOSITION_GRACE_MS
            ) return;
            guidedReviewPollGeneration += 1;
            guidedReviewActivePollEventId = null;
            guidedReviewDelivery = null;
            render();
          }, GUIDED_REVIEW_DISPOSITION_GRACE_MS);
          return;
        }
        if (
          Date.now() - guidedReviewDelivery.resolvedAt <
          GUIDED_REVIEW_DISPOSITION_GRACE_MS
        ) return;
      }
      guidedReviewPollGeneration += 1;
      guidedReviewActivePollEventId = null;
      guidedReviewDelivery = null;
    }
    return;
  }
  if (guidedReviewDelivery?.eventId && guidedReviewDelivery.eventId !== receipt.event_id) return;
  const previous = guidedReviewDelivery?.eventId === receipt.event_id
    ? guidedReviewDelivery.receipt
    : null;
  const selectedReceipt = newerGuidedReviewReceipt(previous, receipt);
  const stateChanged = previous !== null && previous.state !== selectedReceipt.state;
  guidedReviewDelivery = {
    checkedAt: guidedReviewDelivery?.checkedAt || Date.now(),
    eventId: receipt.event_id,
    handUrl: handUrl || guidedReviewDelivery?.handUrl || null,
    liveCheckedAt:
      selectedReceipt.state === "running" && exactGuidedReviewHandUrl(selectedReceipt.thread_id)
        ? Date.now()
        : null,
    message: stateChanged ? null : guidedReviewDelivery?.message || null,
    pendingSeen: true,
    queueRevision: guidedReviewDelivery?.queueRevision || controls?.queue_revision || null,
    resolvedAt: guidedReviewDelivery?.resolvedAt || null,
    startedAt: guidedReviewStartedAt(selectedReceipt, guidedReviewDelivery),
    receipt: {
      ...selectedReceipt,
      final_answer: selectedReceipt.final_answer || previous?.final_answer || null,
    },
  };
  if (
    receipt.state === "pending" &&
    previous === null &&
    !guidedReviewRestoredPendingEvents.has(receipt.event_id)
  ) {
    guidedReviewRestoredPendingEvents.add(receipt.event_id);
    window.queueMicrotask(() => {
      const current = guidedReviewDelivery?.receipt;
      if (current?.event_id !== receipt.event_id || current.state !== "pending") return;
      void continueGuidedReviewTurn(receipt.event_id, handUrl);
    });
  }
}

function newerGuidedReviewReceipt(previous, incoming) {
  if (!previous) return incoming;
  const previousAt = Date.parse(previous.updated_at || "");
  const incomingAt = Date.parse(incoming.updated_at || "");
  if (Number.isFinite(previousAt) && Number.isFinite(incomingAt)) {
    if (incomingAt < previousAt) return previous;
    if (incomingAt > previousAt) return incoming;
  }
  const progress = {
    blocked: 3,
    completed: 3,
    delivery_uncertain: 3,
    failed_safe: 3,
    pending: 0,
    running: 2,
    starting: 1,
  };
  return (progress[incoming.state] ?? -1) < (progress[previous.state] ?? -1)
    ? previous
    : incoming;
}

function guidedReviewReceiptFingerprint(receipt) {
  if (!receipt) return "";
  return JSON.stringify([
    receipt.state,
    receipt.thread_id,
    receipt.final_answer,
    receipt.sheet,
    receipt.subject_task_ids,
    receipt.reason_code,
    receipt.retryable,
    receipt.terminal,
  ]);
}

function guidedReviewStartedAt(receipt, current) {
  if (Number.isFinite(current?.startedAt) && current.startedAt > 0) return current.startedAt;
  const parsed = Date.parse(receipt?.created_at || receipt?.updated_at || "");
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function guidedReviewElapsedLabel(startedAt, now = Date.now()) {
  if (!Number.isFinite(startedAt) || startedAt <= 0) return "";
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1_000));
  if (seconds < 60) return `${seconds}s elapsed`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s elapsed`;
}

function guidedReviewCheckedLabel(checkedAt, now = Date.now()) {
  if (!Number.isFinite(checkedAt) || checkedAt <= 0) return "Checking the receipt";
  const seconds = Math.max(0, Math.floor((now - checkedAt) / 1_000));
  return seconds < 5 ? "Checked just now" : `Checked ${seconds}s ago`;
}

function guidedReviewActivityParts(delivery, now = Date.now()) {
  const receipt = delivery?.receipt;
  if (!receipt || !["pending", "starting", "running"].includes(receipt.state)) return [];
  const parts = [
    receipt.state === "pending"
      ? "Answer received"
      : receipt.state === "starting"
        ? "Confirming delivery"
        : "Applying against current records",
  ];
  const elapsed = guidedReviewElapsedLabel(delivery.startedAt, now);
  if (elapsed) parts.push(elapsed);
  parts.push(guidedReviewCheckedLabel(delivery.checkedAt, now));
  if (
    receipt.state === "running" &&
    exactGuidedReviewHandUrl(receipt.thread_id) &&
    Number.isFinite(delivery.liveCheckedAt) &&
    now - delivery.liveCheckedAt < 5_000
  ) {
    parts.push("Same review hand active");
  }
  return parts;
}

function updateGuidedReviewActivity() {
  const node = document.querySelector(".guided-review-delivery-activity-copy");
  if (node && guidedReviewDelivery) {
    node.textContent = guidedReviewActivityParts(guidedReviewDelivery).join(" · ");
  }
}

function pulseGuidedReviewObservation(eventId) {
  if (guidedReviewDelivery?.eventId !== eventId) return;
  const pulse = document.querySelector(".guided-review-delivery-pulse");
  if (!(pulse instanceof HTMLElement)) return;
  pulse.classList.remove("is-observed");
  void pulse.offsetWidth;
  pulse.classList.add("is-observed");
}

function guidedReviewDeliveryBlocksInput() {
  const stateName = guidedReviewDelivery?.receipt?.state;
  return [
    "blocked",
    "delivery_uncertain",
    "failed_safe",
    "pending",
    "running",
    "starting",
  ].includes(stateName);
}

function renderGuidedReviewDelivery(review, controls) {
  if (!guidedReviewDelivery?.receipt) return null;
  const { receipt } = guidedReviewDelivery;
  const panel = element("aside", `guided-review-delivery is-${receipt.state}`);
  const statusCopy = element("div", "guided-review-delivery-copy", {
    "aria-atomic": "true",
    "aria-live": "polite",
    role: "status",
  });
  const titles = {
    blocked: "Automatic continuation is unavailable",
    completed: "The review hand replied",
    delivery_uncertain: "Delivery could not be confirmed",
    failed_safe: "The turn did not start",
    pending: "Answer saved locally",
    running: "The Mind is working",
    starting: "Opening the exact review hand",
  };
  statusCopy.append(
    textElement("strong", "", titles[receipt.state] || "Review turn status"),
    textElement(
      "p",
      "",
      guidedReviewDelivery.message || guidedReviewStateCopy(receipt),
    ),
  );
  panel.append(statusCopy);
  const activityParts = guidedReviewActivityParts(guidedReviewDelivery);
  if (activityParts.length) {
    const activity = element("div", "guided-review-delivery-activity");
    const pulse = element("span", "guided-review-delivery-pulse", {
      "aria-hidden": "true",
    });
    activity.append(
      pulse,
      textElement("span", "guided-review-delivery-activity-copy", activityParts.join(" · ")),
    );
    panel.append(activity);
  }
  if (receipt.final_answer) {
    panel.append(textElement("p", "guided-review-final-answer", receipt.final_answer));
  }
  const actions = element("div", "guided-review-delivery-actions");
  if (receipt.retryable) {
    const retry = textElement("button", "primary-action", "Retry this exact turn");
    retry.type = "button";
    retry.addEventListener("click", () => {
      if (guidedReviewSendPending) return;
      guidedReviewSendPending = true;
      retry.disabled = true;
      void continueGuidedReviewTurn(receipt.event_id, guidedReviewDelivery.handUrl)
        .finally(() => { guidedReviewSendPending = false; });
    });
    actions.append(retry);
  }
  const receiptHandUrl = exactGuidedReviewHandUrl(receipt.thread_id);
  const fallbackUrl = receiptHandUrl ||
    retainedExactGuidedReviewHandUrl(guidedReviewDelivery.handUrl) ||
    retainedExactGuidedReviewHandUrl(review.hand_url);
  if (
    fallbackUrl &&
    ["blocked", "delivery_uncertain", "failed_safe", "running", "starting"].includes(
      receipt.state,
    )
  ) {
    actions.append(exactHandFallback(fallbackUrl));
  }
  const unresolvedPendingEvent =
    ["blocked", "delivery_uncertain"].includes(receipt.state) &&
    [review.pending_intent?.event_id, review.pending_start?.event_id].includes(receipt.event_id);
  if (unresolvedPendingEvent) {
    const resolution = renderControlReviewActions(controls || {}, {
      linkLabel: "Resolve queued receipt",
    });
    if (resolution) actions.append(resolution);
  }
  if (receipt.state === "completed") {
    const dismiss = textElement("button", "quiet-action", "Dismiss reply");
    dismiss.type = "button";
    dismiss.addEventListener("click", () => {
      guidedReviewDelivery = null;
      render();
    });
    actions.append(dismiss);
  }
  if (actions.childElementCount) panel.append(actions);
  if (receipt.reason_code && receipt.state !== "completed") {
    panel.append(
      textElement(
        "small",
        "guided-review-reason",
        `Reason: ${receipt.reason_code.replaceAll("_", " ")}`,
      ),
    );
  }
  return panel;
}

function exactGuidedReviewHandUrl(threadId) {
  if (
    typeof threadId !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(threadId)
  ) return null;
  return `codex://threads/${threadId}`;
}

function retainedExactGuidedReviewHandUrl(url) {
  if (typeof url !== "string" || !url.startsWith("codex://threads/")) return null;
  const threadId = url.slice("codex://threads/".length);
  return exactGuidedReviewHandUrl(threadId) === url ? url : null;
}

function guidedReviewStateCopy(receipt) {
  if (receipt.state === "pending") {
    return "Your exact answer is stored once while Bridge prepares delivery. You can leave this view; the review keeps its place.";
  }
  if (receipt.state === "starting") {
    return "Bridge is confirming delivery to one review hand. You can leave this view; the review keeps its place.";
  }
  if (receipt.state === "running") {
    return "The same review hand is reading current records and applying only what you asked for. This can take a minute or two; you can leave this view without losing your place.";
  }
  if (receipt.state === "completed") {
    return "The turn completed and canonical state has been refreshed.";
  }
  if (receipt.state === "failed_safe") {
    return "GSV proved the turn was not delivered, so this exact receipt is safe to retry.";
  }
  if (receipt.state === "delivery_uncertain") {
    return "GSV will not replay an answer that Codex may already have received.";
  }
  return "The queued receipt remains local. Continue in the exact hand or repair the installed capability.";
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
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
  updateGuidedReviewActivity();
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
