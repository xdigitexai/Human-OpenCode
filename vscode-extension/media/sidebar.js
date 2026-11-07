const vscode = acquireVsCodeApi();
const $ = id => document.getElementById(id);
let currentPlan;
let detectedTests = [];

function send(type, value = {}) { vscode.postMessage({ type, value }); }
function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}
function clear(id) { $(id).replaceChildren(); }
function button(text, className, callback) {
  const element = node("button", className, text);
  element.addEventListener("click", callback);
  return element;
}

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab,.panel").forEach(item => item.classList.remove("active"));
  tab.classList.add("active");
  $(tab.dataset.tab).classList.add("active");
}));
$("refresh").addEventListener("click", () => send("refresh"));
$("emergency-stop").addEventListener("click", () => send("stopDesktop"));
$("inspect").addEventListener("click", () => send("inspect"));
$("index").addEventListener("click", () => send("index"));
$("make-plan").addEventListener("click", () => {
  const task = $("task").value.trim();
  if (task) send("plan", { task });
});
$("search").addEventListener("click", () => {
  const query = $("search-query").value.trim();
  if (query) send("search", { query });
});
$("github-search").addEventListener("click", () => {
  const query = $("github-query").value.trim();
  if (query) send("githubSearch", { query });
});
$("add-local").addEventListener("click", () => send("addLocal"));
$("git-diff").addEventListener("click", () => send("gitDiff"));
$("screenshot").addEventListener("click", () => send("screenshot"));
$("output").addEventListener("click", () => send("showOutput"));
$("run-tests").addEventListener("click", () => {
  if (detectedTests.length) send("runTests", { command: detectedTests });
});
$("find-repair").addEventListener("click", () => {
  const file = $("repair-path").value.trim();
  const diagnostic = $("diagnostic").value.trim();
  if (file && diagnostic) send("proposeRepair", { path: file, diagnostic });
});
$("run-browser").addEventListener("click", () => {
  try {
    const steps = JSON.parse($("browser-steps").value);
    const commandText = $("browser-command").value.trim();
    const applicationCommand = commandText ? JSON.parse(commandText) : undefined;
    if (!Array.isArray(steps) || (applicationCommand && !Array.isArray(applicationCommand))) throw new Error("Steps and launch command must be JSON arrays");
    send("browserRun", { baseUrl: $("browser-url").value.trim(), steps, applicationCommand });
  } catch (error) {
    $("notice").textContent = error.message;
  }
});

window.addEventListener("message", event => {
  const message = event.data;
  $("notice").textContent = "";
  if (message.type === "overview") renderOverview(message.value);
  if (message.type === "desktopStatus") renderDesktopStatus(message.value);
  if (message.type === "desktopStop") $("activity").lastElementChild.textContent = "Emergency stop requested";
  if (message.type === "inspection") renderInspection(message.value);
  if (message.type === "activity") $("activity").lastElementChild.textContent = message.value;
  if (message.type === "result") renderResult(message.value);
  if (message.type === "repositoriesFound") renderGithub(message.value);
  if (message.type === "matches") renderMatches(message.value);
  if (message.type === "textMatches") renderTextMatches(message.value);
  if (message.type === "plan") renderPlan(message.value);
  if (message.type === "repairPlan") renderRepairPlan(message.value);
  if (message.type === "repairResult") renderJsonResult("repair-results", message.value);
  if (message.type === "browserTestResult") renderJsonResult("browser-results", message.value);
  if (message.type === "applied") {
    $("activity").lastElementChild.textContent = "Changes applied and saved";
    $("run-tests").disabled = detectedTests.length === 0;
    renderResult(message.value);
    send("refresh");
  }
  if (message.type === "testResult") renderTests(message.value);
  if (message.type === "screenshot") renderResult(message.value);
  if (message.type === "error") $("notice").textContent = message.value;
});

function renderOverview(data) {
  renderInspection(data.inspection);
  if (data.desktopTasks?.length) renderDesktopStatus(data.desktopTasks[0]);
  renderDesktopQueue(data.desktopTasks || []);
  clear("repositories");
  for (const repo of data.repositories) {
    const card = node("article", "item");
    card.append(node("h3", "", repo.name));
    card.append(node("div", "meta", repo.url || repo.root));
    card.append(node("span", repo.license ? "tag" : "tag warn", repo.license ? "LICENSE FOUND" : "LICENSE NOT FOUND"));
    $("repositories").append(card);
  }
  clear("history-list");
  for (const entry of data.history) {
    const card = node("article", "item");
    card.append(node("h3", "", entry.category.toUpperCase()));
    card.append(node("div", "description", entry.message));
    card.append(node("div", "meta", entry.created_at));
    $("history-list").append(card);
  }
}

function renderInspection(data) {
  $("project-name").textContent = data.root.split(/[\\/]/).filter(Boolean).pop() || data.root;
  clear("project-facts");
  const facts = [...data.languages.map(language => `${language.name} · ${language.files}`), ...data.frameworks];
  for (const fact of facts) $("project-facts").append(node("span", "fact", fact));
  if (!facts.length) $("project-facts").append(node("span", "empty", "No supported source files detected"));
  detectedTests = data.languages[0]?.tests || [];
  $("run-tests").textContent = detectedTests.length ? `Run ${detectedTests.join(" ")}` : "No test command detected";
  $("run-tests").disabled = detectedTests.length === 0;
}

function renderResult(value) {
  const card = node("pre", "result-box", JSON.stringify(value, null, 2));
  $("activity").lastElementChild.textContent = "Operation complete";
  $("plan-area").replaceChildren(card);
}

function renderGithub(repositories) {
  clear("github-results");
  for (const repo of repositories) {
    const card = node("article", "item");
    card.append(node("h3", "", repo.name));
    card.append(node("div", "description", repo.description || "No description"));
    card.append(node("div", "meta", `${repo.stars} stars · ${repo.license || "license not reported"}`));
    card.append(button("Open in browser", "secondary", () => send("openRepository", { url: repo.url.replace(/\.git$/, "") })));
    card.append(button("Clone and index", "secondary", () => send("clone", repo)));
    $("github-results").append(card);
  }
}

function renderMatches(matches) {
  clear("matches");
  for (const match of matches) {
    const card = node("article", "item");
    card.append(node("h3", "", `${match.kind} ${match.name}`));
    card.append(node("div", "meta", `${match.repository} · ${match.path}:${match.line} · score ${match.score}`));
    card.append(node("span", match.licensed ? "tag" : "tag warn", match.licensed ? "LICENSE FOUND" : "LICENSE NOT FOUND"));
    card.append(node("span", match.compatibility.language_match ? "tag" : "tag warn", match.language));
    card.append(node("pre", "result-box", match.source));
    $("matches").append(card);
  }
}

function renderPlan(plan) {
  currentPlan = plan;
  const area = $("plan-area");
  area.replaceChildren();
  const summary = node("article", "item");
  summary.append(node("h3", "", plan.status === "needs_source" ? "No reusable source found" : "Approval required"));
  summary.append(node("div", "description", plan.notice));
  summary.append(node("div", "meta", `${plan.candidates.length} licensed same-language candidate(s) · no code generated`));
  area.append(summary);
  for (const [index, candidate] of plan.candidates.entries()) {
    const card = node("article", "candidate");
    card.append(node("h3", "", `${candidate.kind} ${candidate.name}`));
    card.append(node("div", "meta", `${candidate.repository} · ${candidate.path}:${candidate.line}`));
    card.append(node("div", "description", candidate.license.slice(0, 240)));
    const form = node("div", "form-grid");
    const target = node("input"); target.placeholder = "Target path, e.g. src/auth.py";
    const anchor = node("textarea"); anchor.rows = 2; anchor.placeholder = "Exact existing text to replace (must be unique)";
    const replacement = node("textarea"); replacement.rows = 4; replacement.placeholder = "Replacement text adapted from the reviewed source";
    form.append(target, anchor, replacement);
    form.append(button("Add proposed edit", "secondary", () => {
      if (!target.value || !anchor.value || !replacement.value) return;
      send("addReplacement", { plan_id: plan.id, candidate_index: index, path: target.value,
        old: anchor.value, new: replacement.value });
    }));
    card.append(form);
    area.append(card);
  }
  if (plan.changes.length) {
    for (const change of plan.changes) {
      const item = node("article", "change");
      item.append(node("h3", "", change.path));
      item.append(node("div", "meta", `Source: ${change.attribution.repository} · ${change.attribution.license.slice(0, 160)}`));
      item.append(node("pre", "", change.diff));
      area.append(item);
    }
    area.append(button("Approve and apply changes", "primary full", () => send("apply", { planId: plan.id })));
  }
}

function renderTests(result) {
  const box = $("tests");
  box.className = `result-box ${result.returncode === 0 ? "good" : "bad"}`;
  box.textContent = `$ ${result.command.join(" ")}\nexit code: ${result.returncode}\n\n${result.stdout}\n${result.stderr}`;
}

function renderTextMatches(matches) {
  if (!matches.length) return;
  const heading = node("div", "meta", "TEXT MATCHES");
  $("matches").prepend(heading);
  for (const match of matches) {
    const card = node("article", "item");
    card.append(node("h3", "", `${match.repository} · ${match.path}:${match.line}`));
    card.append(node("pre", "result-box", match.text));
    $("matches").append(card);
  }
}

function renderDesktopStatus(task) {
  const box = $("desktop-status");
  if (!task || !task.id) {
    box.className = "result-box empty";
    box.textContent = "Physical desktop queue is idle.";
    return;
  }
  const percentage = task.total ? Math.round((task.step / task.total) * 100) : 0;
  box.className = "result-box";
  box.replaceChildren();
  box.append(node("strong", "", `${task.status.toUpperCase()} · ${percentage}%`));
  box.append(node("div", "description", task.action || "Waiting for desktop action"));
  if (task.current_path) box.append(node("div", "meta", `${task.step}/${task.total} · ${task.current_path}`));
  if (task.error) box.append(node("div", "bad", task.error));
  for (const screenshot of task.screenshots || []) box.append(node("div", "meta", `Screenshot: ${screenshot}`));
  for (const file of task.completed_files || []) box.append(node("div", "good", `Saved: ${file}`));
  if (task.command_result) {
    const result = task.command_result;
    box.append(node("div", result.exit_code === 0 ? "good" : "bad",
      result.verified ? `Test exit code: ${result.exit_code}` : "Test exit code could not be verified"));
    if (result.output) box.append(node("pre", "result-box", result.output));
  }
  if (task.checkpoint?.id) {
    box.append(button("Restore checkpoint", "secondary", () => send("rollback", { id: task.checkpoint.id })));
  }
}

function renderDesktopQueue(tasks) {
  clear("desktop-queue");
  for (const task of tasks) {
    const item = node("article", "item");
    item.append(node("h3", "", `${task.status.toUpperCase()} · ${task.step}/${task.total}`));
    item.append(node("div", "description", task.action || task.current_path || "Queued task"));
    if (task.error) item.append(node("div", "bad", task.error));
    if (task.checkpoint?.id) item.append(button("Restore checkpoint", "secondary", () => send("rollback", { id: task.checkpoint.id })));
    $("desktop-queue").append(item);
  }
}

function renderRepairPlan(plan) {
  const area = $("repair-results");
  area.replaceChildren();
  area.append(node("div", plan.status === "repair_pending" ? "tag warn" : "tag", plan.status.toUpperCase()));
  if (plan.notice) area.append(node("div", "description", plan.notice));
  if (!plan.candidates?.length) {
    area.append(node("div", "empty", plan.classification?.detail || "No compatible predefined repair was found."));
    return;
  }
  for (const [index, candidate] of plan.candidates.entries()) {
    const card = node("article", "change");
    card.append(node("h3", "", `${candidate.recipe_id} · ${candidate.repository}`));
    card.append(node("div", "meta", candidate.license.slice(0, 180)));
    card.append(node("pre", "", candidate.diff));
    const validation = node("input");
    validation.placeholder = 'Validation command JSON array, e.g. ["python", "-m", "pytest"]';
    card.append(validation);
    card.append(button("Approve, apply, validate", "secondary", () => {
      try {
        const command = JSON.parse(validation.value);
        if (!Array.isArray(command) || command.some(part => typeof part !== "string")) throw new Error("Validation command must be a JSON string array");
        send("applyRepair", { planId: plan.id, path: plan.target, candidateIndex: index, validationCommand: command });
      } catch (error) { $("notice").textContent = error.message; }
    }));
    area.append(card);
  }
}

function renderJsonResult(id, value) {
  const box = $(id);
  box.className = `result-box ${value.status === "passed" || value.status === "verified" ? "good" : value.status === "failed" || value.status === "rolled_back" ? "bad" : ""}`;
  box.textContent = JSON.stringify(value, null, 2);
}