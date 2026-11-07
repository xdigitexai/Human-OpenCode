import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import * as path from "node:path";
import { randomBytes } from "node:crypto";

type JsonRecord = Record<string, any>;

class PythonBridge implements vscode.Disposable {
  private child?: ChildProcessWithoutNullStreams;
  private pending: Array<{ resolve: (value: any) => void; reject: (error: Error) => void }> = [];
  private buffered = "";
  private ready?: Promise<void>;
  private readonly output = vscode.window.createOutputChannel("OpenCode Human Bot");

  constructor(private readonly root: string) {}

  async request<T>(op: string, values: JsonRecord = {}): Promise<T> {
    await this.start();
    return new Promise<T>((resolve, reject) => {
      this.pending.push({ resolve, reject });
      this.child!.stdin.write(`${JSON.stringify({ op, ...values })}\n`, error => {
        if (error) {
          this.pending.pop();
          reject(error);
        }
      });
    });
  }

  private start(): Promise<void> {
    if (this.ready) return this.ready;
    this.ready = new Promise((resolve, reject) => {
      const python = vscode.workspace.getConfiguration("opencodeHumanBot").get<string>("pythonPath", "python");
      const env = { ...process.env, PYTHONPATH: [this.root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) };
      this.child = spawn(python, ["-m", "opencode_human_bot", "--root", this.root], { cwd: this.root, env });
      let initialized = false;
      this.child.stdout.on("data", (chunk: Buffer) => {
        this.buffered += chunk.toString("utf8");
        let boundary = this.buffered.indexOf("\n");
        while (boundary >= 0) {
          const line = this.buffered.slice(0, boundary);
          this.buffered = this.buffered.slice(boundary + 1);
          boundary = this.buffered.indexOf("\n");
          try {
            const message = JSON.parse(line);
            if (!initialized) {
              initialized = true;
              if (!message.ready) reject(new Error(message.error || "Engine failed to start"));
              else resolve();
              continue;
            }
            const waiter = this.pending.shift();
            if (!waiter) continue;
            if (message.ok) waiter.resolve(message.result);
            else waiter.reject(new Error(message.error || "Engine request failed"));
          } catch (error) {
            if (!initialized) reject(error instanceof Error ? error : new Error(String(error)));
          }
        }
      });
      this.child.stderr.on("data", (chunk: Buffer) => this.output.append(chunk.toString("utf8")));
      this.child.on("error", error => reject(error));
      this.child.on("exit", code => {
        const error = new Error(`Coding engine exited (${code ?? "unknown"}). See OpenCode Human Bot output.`);
        for (const waiter of this.pending.splice(0)) waiter.reject(error);
        this.child = undefined;
        this.ready = undefined;
        if (!initialized) reject(error);
      });
    });
    return this.ready;
  }

  dispose(): void {
    if (this.child) {
      const child = this.child;
      const timeout = setTimeout(() => child.kill(), 5000);
      timeout.unref();
      child.once("exit", () => clearTimeout(timeout));
      child.stdin.end();
    }
    this.output.dispose();
  }
}

class Sidebar implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private inspection?: JsonRecord;
  private detectedTests: string[] = [];
  private desktopTaskId?: string;
  private polling = false;
  private activeTestTerminal?: vscode.Terminal;
  private readonly commandPrompts = new Set<string>();

  constructor(private readonly context: vscode.ExtensionContext,
              private readonly root: string,
              private readonly bridge: PythonBridge) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage(message => void this.handle(message));
    void this.refresh();
  }

  private post(type: string, value: any): void {
    void this.view?.webview.postMessage({ type, value });
  }

  private async refresh(): Promise<void> {
    try {
      this.inspection = await this.bridge.request("inspect");
      this.detectedTests = this.inspection.languages?.[0]?.tests ?? [];
      const [repositories, history, desktopTasks] = await Promise.all([
        this.bridge.request("repositories"), this.bridge.request("history", { limit: 30 }),
        this.bridge.request<JsonRecord[]>("desktop_status", { limit: 20 })
      ]);
      const activeTask = desktopTasks.find((task: JsonRecord) =>
        ["queued", "running", "verify_file", "awaiting_command_approval", "command_ready", "command_result"].includes(task.status));
      if (activeTask) {
        this.desktopTaskId = activeTask.id;
        this.pollDesktop();
      }
      this.post("overview", { inspection: this.inspection, repositories, history, desktopTasks });
    } catch (error) { this.error(error); }
  }

  private async handle(message: JsonRecord): Promise<void> {
    const { type, value = {} } = message;
    try {
      switch (type) {
        case "refresh": return await this.refresh();
        case "inspect":
          this.inspection = await this.bridge.request("inspect");
          return this.post("inspection", this.inspection);
        case "index":
          this.post("activity", "Indexing source files...");
          this.post("result", await this.bridge.request("index"));
          return await this.refresh();
        case "addLocal": {
          const selected = await vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true,
            canSelectMany: false, openLabel: "Add Repository" });
          if (!selected?.[0]) return;
          const repository = await this.bridge.request("add_local_repository", { path: selected[0].fsPath });
          const indexed = await this.bridge.request("index", { path: selected[0].fsPath });
          this.post("result", { repository, indexed });
          return await this.refresh();
        }
        case "githubSearch":
          this.post("repositoriesFound", await this.bridge.request("search_github", { query: value.query, limit: 8 }));
          return;
        case "clone": {
          const choice = await vscode.window.showWarningMessage(
            `Clone public repository ${value.name}? Review its license before reuse.`, { modal: true }, "Clone and index");
          if (choice !== "Clone and index") return;
          const repository = await this.bridge.request<JsonRecord>("clone", { url: value.url, approved: true });
          const indexed = await this.bridge.request("index", { path: repository.root });
          this.post("result", { repository, indexed });
          return await this.refresh();
        }
        case "openRepository": {
          const uri = vscode.Uri.parse(value.url);
          if (uri.scheme !== "https" || uri.authority !== "github.com") throw new Error("Only public GitHub pages can be opened");
          await vscode.env.openExternal(uri);
          return;
        }
        case "search":
          this.post("matches", await this.bridge.request("search", { query: value.query, language: value.language || undefined }));
          this.post("textMatches", await this.bridge.request("search_text", { query: value.query }));
          return;
        case "plan": {
          this.post("activity", "Inspecting project and searching indexed source...");
          const plan = await this.bridge.request("plan", { task: value.task });
          this.post("plan", plan);
          return await this.refresh();
        }
        case "addReplacement": {
          const plan = await this.bridge.request("add_replacement", value);
          this.post("plan", plan);
          return;
        }
        case "proposeRepair": {
          const plan = await this.bridge.request("propose_repair", {
            diagnostic: value.diagnostic, path: value.path
          });
          this.post("repairPlan", plan);
          return;
        }
        case "applyRepair": {
          const choice = await vscode.window.showWarningMessage(
            `Apply the reviewed repair to ${value.path} and run ${value.validationCommand.join(" ")}?`,
            { modal: true }, "Apply and validate");
          if (choice !== "Apply and validate") return;
          const result = await this.bridge.request("apply_repair", {
            plan_id: value.planId, candidate_index: value.candidateIndex,
            validation_command: value.validationCommand, approved: true
          });
          this.post("repairResult", result);
          return;
        }
        case "browserRun": {
          const choice = await vscode.window.showWarningMessage(
            `Run this Playwright scenario against local URL ${value.baseUrl}?${value.applicationCommand?.length ? ` Launch: ${value.applicationCommand.join(" ")}` : ""}`,
            { modal: true }, "Run browser test");
          if (choice !== "Run browser test") return;
          const result = await this.bridge.request("browser_run", {
            base_url: value.baseUrl, steps: value.steps,
            application_command: value.applicationCommand?.length ? value.applicationCommand : undefined,
            approved: true
          });
          this.post("browserTestResult", result);
          return;
        }
        case "apply":
          return await this.apply(value.planId);
        case "stopDesktop":
          return await this.emergencyStop();
        case "runTests": {
          const choice = await vscode.window.showWarningMessage(
            `Run approved test command: ${value.command.join(" ")}`, { modal: true }, "Run tests");
          if (choice !== "Run tests") return;
          const result = await this.bridge.request("command", { command: value.command, approved: true,
            timeout: vscode.workspace.getConfiguration("opencodeHumanBot").get("commandTimeoutSeconds", 300) });
          this.post("testResult", result);
          return;
        }
        case "gitDiff": {
          const result = await this.bridge.request<JsonRecord>("command", { command: ["git", "diff", "--"], approved: true });
          const document = await vscode.workspace.openTextDocument({ language: "diff", content: result.stdout || "No unstaged changes." });
          await vscode.window.showTextDocument(document, { preview: true });
          return;
        }
        case "rollback": {
          const choice = await vscode.window.showWarningMessage(`Restore checkpoint ${value.id}?`, { modal: true }, "Restore");
          if (choice !== "Restore") return;
          this.post("result", await this.bridge.request("rollback", { checkpoint_id: value.id, approved: true }));
          return await this.refresh();
        }
        case "screenshot": {
          const choice = await vscode.window.showWarningMessage("Capture the entire desktop, which may include sensitive content?",
            { modal: true }, "Capture screenshot");
          if (choice !== "Capture screenshot") return;
          const result = await this.bridge.request("screenshot", { approved: true });
          this.post("screenshot", result);
          return;
        }
        case "showOutput":
          vscode.commands.executeCommand("workbench.action.output.toggleOutput");
          return;
      }
    } catch (error) { this.error(error); }
  }

  private async apply(planId: string): Promise<void> {
    const mode = vscode.workspace.getConfiguration("opencodeHumanBot").get<string>("editingMode", "native");
    if (mode === "physical") {
      const choice = await vscode.window.showWarningMessage("Physically type the approved changes into the active VS Code window?",
        { modal: true }, "Type changes");
      if (choice !== "Type changes") return;
      const configuration = vscode.workspace.getConfiguration("opencodeHumanBot.desktop");
      const captureScreenshots = configuration.get<boolean>("captureScreenshots", true);
      let screenshotChoice: string | undefined;
      if (captureScreenshots) {
        screenshotChoice = await vscode.window.showWarningMessage(
          "Capture the entire desktop at development stages? Screenshots may include sensitive windows.",
          { modal: true }, "Capture screenshots", "Continue without screenshots");
        if (!screenshotChoice) return;
      }
      const settings = { ...configuration.get<JsonRecord>("coordinates", {}),
        typing_interval: configuration.get<number>("typingInterval", 0.035),
        mouse_duration: configuration.get<number>("mouseDuration", 0.25),
        action_delay: configuration.get<number>("actionDelay", 0.6),
        explorer_items: configuration.get<JsonRecord>("explorerItems", {}),
        cursor_line: configuration.get<number>("cursorLine", 0) || undefined,
        capture_screenshots: captureScreenshots && screenshotChoice === "Capture screenshots" };
      const task = await this.bridge.request<JsonRecord>("desktop_start", {
        plan_id: planId, settings, command: this.detectedTests.length ? this.detectedTests : undefined, approved: true
      });
      this.desktopTaskId = task.id;
      this.post("desktopStatus", task);
      this.pollDesktop();
      return;
    }
    const prepared = await this.bridge.request<any>("prepare_apply", { plan_id: planId, approved: true });
    const documents: Array<{ document: vscode.TextDocument; before: string; expected: string }> = [];
    try {
      const edit = new vscode.WorkspaceEdit();
      for (const change of prepared.changes) {
        const target = path.resolve(this.root, change.path);
        if (target !== this.root && !target.startsWith(`${this.root}${path.sep}`)) throw new Error("Path escapes workspace root");
        const document = await vscode.workspace.openTextDocument(vscode.Uri.file(target));
        if (document.getText() !== change.before) throw new Error(`File changed after planning: ${change.path}`);
        documents.push({ document, before: change.before, expected: change.after });
        await vscode.window.showTextDocument(document, { preview: false });
        edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), change.after);
      }
      if (!await vscode.workspace.applyEdit(edit)) throw new Error("VS Code rejected the workspace edit");
      for (const { document, expected } of documents) {
        if (document.getText() !== expected) throw new Error(`Editor content did not match the approved text: ${path.basename(document.fileName)}`);
        if (!await document.save()) throw new Error(`Could not save ${path.basename(document.fileName)}`);
      }
      const result = await this.bridge.request("complete_apply", { plan_id: planId });
      this.post("applied", result);
    } catch (error) {
      await this.bridge.request("abort_apply", { plan_id: planId }).catch(() => undefined);
      const recovery = new vscode.WorkspaceEdit();
      for (const { document, before } of documents) {
        recovery.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), before);
      }
      await vscode.workspace.applyEdit(recovery);
      for (const { document } of documents) {
        try { await document.save(); } catch { }
      }
      throw error;
    }
  }

  async emergencyStop(): Promise<void> {
    this.activeTestTerminal?.dispose();
    this.activeTestTerminal = undefined;
    const result = await this.bridge.request<JsonRecord>("desktop_cancel", { task_id: this.desktopTaskId });
    this.post("desktopStop", result);
    void vscode.window.showWarningMessage("Emergency stop requested. Physical input will stop at its next guarded action.");
  }

  async openFolderInNewWindow(): Promise<void> {
    const selected = await vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true,
      canSelectMany: false, openLabel: "Open in New Window" });
    if (!selected?.[0]) return;
    if (path.resolve(selected[0].fsPath).toLowerCase() === path.resolve(this.root).toLowerCase()) {
      throw new Error("Choose a different workspace folder; the current window will remain open.");
    }
    await vscode.commands.executeCommand("vscode.openFolder", selected[0], {
      forceNewWindow: true, noRecentEntry: true,
    });
  }

  private pollDesktop(): void {
    if (this.polling || !this.desktopTaskId) return;
    this.polling = true;
    void this.pollDesktopOnce().finally(() => { this.polling = false; });
  }

  private async pollDesktopOnce(): Promise<void> {
    if (!this.desktopTaskId) return;
    try {
      const task = await this.bridge.request<JsonRecord>("desktop_status", { task_id: this.desktopTaskId });
      if (!task) return;
      this.post("desktopStatus", task);
      if (task.status === "verify_file") {
        const editorPath = vscode.window.activeTextEditor?.document.uri.fsPath;
        const expected = path.resolve(this.root, task.current_path);
        if (editorPath && path.resolve(editorPath).toLowerCase() === expected.toLowerCase()) {
          await this.bridge.request("desktop_verify_file", { task_id: task.id, verified: true });
        }
      }
      if (task.status === "verify_editor_focus") {
        await vscode.commands.executeCommand("workbench.action.focusActiveEditorGroup");
        const editorPath = vscode.window.activeTextEditor?.document.uri.fsPath;
        const expected = path.resolve(this.root, task.current_path);
        const verified = !!editorPath && path.resolve(editorPath).toLowerCase() === expected.toLowerCase();
        await this.bridge.request("desktop_verify_focus", { task_id: task.id, verified });
      }
      if (task.status === "awaiting_command_approval" && !this.commandPrompts.has(task.id)) {
        this.commandPrompts.add(task.id);
        await this.approveAndRunTerminal(task);
      }
      if (["completed", "cancelled", "blocked", "tests_failed", "approval_denied", "verification_required"].includes(task.status)) {
        this.desktopTaskId = undefined;
        await this.refresh();
        return;
      }
    } catch (error) {
      this.error(error);
      return;
    }
    setTimeout(() => this.pollDesktop(), 250);
  }

  private async approveAndRunTerminal(task: JsonRecord): Promise<void> {
    const command: string[] = task.command || [];
    const choice = await vscode.window.showWarningMessage(
      `Run test command in the integrated terminal?\n${command.join(" ")}`, { modal: true }, "Run tests", "Cancel task");
    const approved = choice === "Run tests";
    await this.bridge.request("desktop_approve_command", { task_id: task.id, approved });
    if (!approved) {
      if (choice === "Cancel task") await this.bridge.request("desktop_cancel", { task_id: task.id });
      return;
    }
    const terminal = vscode.window.createTerminal({ name: "OpenCode Human Bot Tests", cwd: this.root });
    this.activeTestTerminal = terminal;
    const integration = await this.waitForTerminalIntegration(terminal);
    if (!integration) {
      await this.bridge.request("desktop_report_command", {
        task_id: task.id, result: { verified: false, exit_code: null, output: "VS Code shell integration is unavailable; command was not run." }
      });
      terminal.dispose();
      this.activeTestTerminal = undefined;
      return;
    }
    terminal.show(false);
    this.post("desktopStatus", { ...task, action: "Running approved test command in integrated terminal" });
    const execution = integration.executeCommand(command[0], command.slice(1));
    const exitCodePromise = new Promise<number | undefined>(resolve => {
      const listener = vscode.window.onDidEndTerminalShellExecution(event => {
        if (event.execution === execution) {
          listener.dispose();
          resolve(event.exitCode);
        }
      });
    });
    const timeoutMs = vscode.workspace.getConfiguration("opencodeHumanBot").get<number>("commandTimeoutSeconds", 300) * 1000;
    let output = "";
    let exitCode: number | undefined;
    let executionError: string | undefined;
    const timeout = setTimeout(() => terminal.dispose(), timeoutMs);
    try {
      for await (const chunk of execution.read()) output += chunk;
      exitCode = await exitCodePromise;
    } catch (error) {
      executionError = error instanceof Error ? error.message : String(error);
    } finally {
      clearTimeout(timeout);
      this.activeTestTerminal = undefined;
    }
    await this.bridge.request("desktop_report_command", {
      task_id: task.id, result: { verified: exitCode !== undefined, exit_code: exitCode ?? null,
        output: output.slice(-12000), error: executionError }
    });
  }

  private async waitForTerminalIntegration(terminal: vscode.Terminal): Promise<vscode.TerminalShellIntegration | undefined> {
    if (terminal.shellIntegration) return terminal.shellIntegration;
    return new Promise(resolve => {
      let done = false;
      const listener = vscode.window.onDidChangeTerminalShellIntegration(event => {
        if (event.terminal === terminal) finish(event.shellIntegration);
      });
      const timeout = setTimeout(() => finish(), 5000);
      const finish = (integration?: vscode.TerminalShellIntegration) => {
        if (done) return;
        done = true;
        listener.dispose();
        clearTimeout(timeout);
        resolve(integration);
      };
      terminal.show(true);
    });
  }

  private error(error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    this.post("error", message);
    this.context.globalState.update("lastError", message);
  }

  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media", "sidebar.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, "media", "sidebar.css"));
    return `<!doctype html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}' ${webview.cspSource}; img-src ${webview.cspSource} data:;">
      <link rel="stylesheet" href="${style}"><title>OpenCode Human Bot</title></head>
      <body><header><div class="brand-mark">OH</div><div><h1>OpenCode</h1><p>HUMAN BOT <span class="dot"></span> LOCAL ENGINE</p></div><button id="refresh" title="Refresh project state">↻</button></header>
      <nav><button class="tab active" data-tab="work">Work</button><button class="tab" data-tab="repos">Repositories</button><button class="tab" data-tab="history">History</button></nav>
      <main>
        <section id="work" class="panel active">
          <div class="section-head"><div><label>PROJECT</label><h2 id="project-name">Loading workspace</h2></div><button id="inspect" class="icon-btn" title="Inspect project">⌕</button></div>
          <div class="action-row"><span class="meta">Physical desktop control</span><button id="emergency-stop" class="secondary" title="Emergency stop (Ctrl+Alt+Escape)">Stop</button></div>
          <div id="project-facts" class="facts"></div>
          <div id="desktop-status" class="result-box empty">Physical desktop queue is idle.</div>
          <div id="desktop-queue" class="results"></div>
          <button id="index" class="secondary full">Index workspace source</button>
          <div class="divider"></div>
          <label for="task">DEVELOPMENT TASK</label><textarea id="task" rows="3" placeholder="Describe a change to look for in indexed source"></textarea>
          <button id="make-plan" class="primary full">Search source and propose</button>
          <div id="activity" class="activity"><span class="pulse"></span><span>Ready for a deterministic task</span></div>
          <div id="plan-area"></div>
          <div class="divider"></div><div class="section-head"><div><label>CODE SEARCH</label><h2>Indexed symbols</h2></div></div>
          <div class="inline"><input id="search-query" placeholder="Function, class, or phrase"><button id="search" class="secondary">Search</button></div>
          <div id="matches" class="results"></div>
          <div class="divider"></div><div class="section-head"><div><label>VERIFICATION</label><h2>Test results</h2></div><button id="git-diff" class="icon-btn" title="Open Git diff">⧉</button></div>
          <div id="tests" class="result-box empty">No test command run in this session.</div>
          <div class="action-row"><button id="run-tests" class="secondary" disabled>Run detected tests</button><button id="screenshot" class="icon-btn" title="Capture desktop screenshot">▣</button></div>
          <div class="divider"></div>
          <label for="repair-path">KNOWN-ERROR REPAIR</label>
          <input id="repair-path" placeholder="Target path, e.g. src/module.py">
          <textarea id="diagnostic" rows="3" placeholder="Paste compiler or test diagnostic"></textarea>
          <button id="find-repair" class="secondary full">Find deterministic repair</button>
          <div id="repair-results" class="results"></div>
          <div class="divider"></div>
          <label for="browser-url">PLAYWRIGHT LOCAL BROWSER TEST</label>
          <input id="browser-url" value="http://127.0.0.1:3000" aria-label="Local browser URL">
          <textarea id="browser-command" rows="2" placeholder='Optional app command as JSON array, e.g. ["node", "server.js"]'></textarea>
          <textarea id="browser-steps" rows="5" aria-label="Playwright steps">[{"action":"navigate","url":"/"},{"action":"expect_visible","selector":"body"},{"action":"screenshot","name":"smoke"}]</textarea>
          <button id="run-browser" class="secondary full">Run localhost browser scenario</button>
          <div id="browser-results" class="result-box empty">No browser scenario run.</div>
        </section>
        <section id="repos" class="panel">
          <label>PUBLIC GITHUB SEARCH</label><div class="inline"><input id="github-query" placeholder="Repository or topic"><button id="github-search" class="secondary">Find</button></div>
          <div id="github-results" class="results"></div>
          <div class="divider"></div><div class="section-head"><div><label>LOCAL LIBRARY</label><h2>Repositories</h2></div><button id="add-local" class="icon-btn" title="Add local folder">＋</button></div>
          <div id="repositories" class="results"></div>
        </section>
        <section id="history" class="panel"><div class="section-head"><div><label>SESSION RECORD</label><h2>Execution history</h2></div><button id="output" class="icon-btn" title="Show engine output">≡</button></div><div id="history-list" class="results"></div></section>
        <div id="notice" role="status" aria-live="polite"></div>
      </main><script nonce="${nonce}" src="${script}"></script></body></html>`;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    vscode.window.showWarningMessage("Open a workspace folder to use OpenCode Human Bot.");
    return;
  }
  const bridge = new PythonBridge(folder.uri.fsPath);
  const sidebar = new Sidebar(context, folder.uri.fsPath, bridge);
  context.subscriptions.push(bridge,
    vscode.commands.registerCommand("opencodeHumanBot.emergencyStop", () => sidebar.emergencyStop()),
    vscode.commands.registerCommand("opencodeHumanBot.openFolderInNewWindow", () => sidebar.openFolderInNewWindow()),
    vscode.window.registerWebviewViewProvider("opencodeHumanBot.sidebar", sidebar));
}

export function deactivate(): void {}