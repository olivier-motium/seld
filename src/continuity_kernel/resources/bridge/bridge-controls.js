let correctionDraft = "";

export function renderControlPanel(snapshot, { bridgeToken, currentControls, refresh }) {
  const controls = snapshot.controls || {};
  const panel = element("section", "control-panel");
  const copy = element("div");
  copy.append(
    textElement("p", "section-label", "Tell GSV what changed"),
    textElement("h2", "", "Queue a correction for review"),
    textElement(
      "p",
      "control-explainer",
      "The Bridge records your wording exactly. Reviewing it only acknowledges or rejects what you wrote; it never applies the correction or takes action.",
    ),
  );
  const review = renderReviewActions(controls);
  if (review) copy.append(review);

  const recent = element("ul", "issue-list control-history");
  for (const item of (controls.items || []).slice(-5).reverse()) {
    const entry = element("li");
    const status =
      item.status === "accepted"
        ? "Acknowledged, not applied"
        : item.status === "rejected"
          ? "Rejected"
          : "Waiting";
    entry.append(
      textElement("strong", "", item.event?.choice || "Bridge intent"),
      textElement("span", "", status),
      textElement("small", "", item.disposition?.reason_code || relativeTime(item.event?.created_at)),
    );
    recent.append(entry);
  }
  if ((controls.history || []).length) {
    recent.append(
      textElement("li", "control-history-label", "Previous queue · history only"),
    );
    for (const item of controls.history.slice(-3).reverse()) {
      const entry = element("li", "is-archived");
      const status =
        item.status === "accepted" ? "Acknowledged, not applied" : "Rejected";
      entry.append(
        textElement("strong", "", item.event?.choice || "Bridge intent"),
        textElement("span", "", status),
        textElement(
          "small",
          "",
          `${item.disposition?.reason_code || "Reviewed"} · History`,
        ),
      );
      recent.append(entry);
    }
  }
  if (!(controls.items || []).length) {
    recent.append(
      textElement(
        "li",
        "control-empty",
        controls.available ? "No queued corrections." : "The intent queue is unavailable.",
      ),
    );
  }

  const form = element("form", "control-form");
  const label = textElement("label", "", "What should GSV correct?");
  const input = element("textarea", "control-input", {
    maxlength: "4096",
    name: "correction",
    required: "",
    rows: "3",
  });
  label.htmlFor = "bridge-correction";
  input.id = "bridge-correction";
  input.disabled = !controls.available;
  input.value = correctionDraft;
  input.addEventListener("input", () => {
    correctionDraft = input.value;
  });
  const status = element("p", "control-status", { "aria-live": "polite", role: "status" });
  const submit = textElement("button", "command-copy", "Queue correction");
  submit.type = "submit";
  submit.disabled = !controls.available;
  form.append(label, input, submit, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submittedDraft = input.value;
    const choice = submittedDraft.trim();
    if (!choice) return;
    submit.disabled = true;
    status.textContent = "Saving locally…";
    try {
      await appendControlIntent(snapshot, bridgeToken, {
        choice,
        kind: "correction",
        subject: "mind:user-correction",
      });
      const newerDraftExists = correctionDraft !== submittedDraft;
      if (!newerDraftExists) correctionDraft = "";
      try {
        await refresh();
      } catch (_refreshError) {
        input.value = correctionDraft;
        status.textContent = newerDraftExists
          ? "Queued the submitted correction, but the view could not refresh. Your newer draft is still here; refresh before retrying."
          : "Queued the correction, but the view could not refresh. Refresh before entering another correction.";
        return;
      }
      const refreshedInput = document.querySelector("#bridge-correction");
      const refreshedStatus = document.querySelector(".control-form .control-status");
      if (refreshedInput) refreshedInput.value = correctionDraft;
      if (refreshedStatus) {
        refreshedStatus.textContent = newerDraftExists
          ? "Queued the submitted correction. Your newer draft is still here."
          : "Queued. GSV will show an acknowledge or reject disposition here.";
      }
    } catch (error) {
      if (error.status === 409) {
        try {
          await refresh();
        } catch (_refreshError) {
          input.value = correctionDraft;
          status.textContent =
            "The queue changed while you were editing. Your current draft is still here; refresh the queue, then retry.";
          return;
        }
        const refreshedInput = document.querySelector("#bridge-correction");
        const refreshedStatus = document.querySelector(".control-form .control-status");
        if (refreshedInput) {
          refreshedInput.value = correctionDraft;
          refreshedInput.focus();
        }
        if (refreshedStatus) {
          refreshedStatus.textContent =
            "The queue changed while you were editing. Your current draft is still here; review the refreshed queue, then retry.";
        }
        return;
      }
      status.textContent =
        error.status >= 400 && error.status < 500
          ? `The correction was not queued: ${error.message}`
          : "GSV could not confirm whether the correction was saved. Refresh the queue before retrying.";
    } finally {
      submit.disabled = !currentControls()?.available;
    }
  });
  panel.append(copy, recent, form);
  return panel;
}

function renderReviewActions(controls) {
  if (!controls.available || !controls.review_prompt) return null;
  const actions = element("div", "control-review-actions");
  if (controls.review_url) {
    const link = textElement("a", "primary-action", "Review in Codex");
    link.href = controls.review_url;
    actions.append(link);
  }

  const fallback = element("details", "control-prompt-fallback");
  fallback.append(textElement("summary", "", "Copy prompt instead"));
  const promptAction = element("div", "command-action");
  const prompt = textElement("code", "", controls.review_prompt);
  const copy = textElement("button", "command-copy", "Copy prompt");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    let copied = false;
    try {
      await navigator.clipboard.writeText(controls.review_prompt);
      copied = true;
    } catch (_error) {
      window.getSelection()?.selectAllChildren(prompt);
    }
    copy.textContent = copied ? "Copied" : "Prompt selected";
    window.setTimeout(() => {
      copy.textContent = "Copy prompt";
    }, 1_600);
  });
  promptAction.append(prompt, copy);
  fallback.append(promptAction);
  actions.append(fallback);
  return actions;
}

export function controlSystemCopy(snapshot) {
  if (!snapshot.controls?.available) {
    return "Unavailable. Canonical Mind, task, and storyline reads remain usable.";
  }
  const history = snapshot.controls.archived_decided || 0;
  const historyCopy = history ? `; ${history} in recent history` : "";
  return `${snapshot.controls.pending} waiting; ${snapshot.controls.decided} reviewed here${historyCopy}.`;
}

export function controlSystemStatus(snapshot) {
  if (snapshot.bridge?.control_queue === false) return "Not available on this platform";
  if (!snapshot.controls?.available) return "Needs repair";
  return snapshot.controls.pending ? "Review needed" : "Ready";
}

export async function appendControlIntent(snapshot, bridgeToken, intent) {
  if (!bridgeToken || !snapshot.controls?.available || !snapshot.controls.queue_revision) {
    throw new Error("The local Bridge intent queue is not ready.");
  }
  const response = await fetch("/api/v1/control", {
    body: JSON.stringify({
      ...intent,
      expected_revision: snapshot.controls.queue_revision,
    }),
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${bridgeToken}`,
      "Content-Type": "application/json",
    },
    method: "POST",
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `Bridge returned ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
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
