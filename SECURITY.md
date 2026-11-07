# Security

## Approval boundaries

- Repository cloning requires explicit approval and accepts only public HTTPS GitHub clone URLs.
- Existing target files are never changed during planning. A proposed replacement must be anchored to exactly one occurrence in an existing file.
- Applying a plan requires explicit approval and creates a file-content checkpoint first.
- Terminal commands, screenshot capture, and rollback each require explicit approval. Commands are passed as argument arrays without `shell=True`; execution receives a reduced environment and displayed output is redacted against known secret values and common credential flags.
- Physical mode takes over keyboard input after a confirmation dialog. Keep VS Code focused and do not use it around sensitive windows.
- Physical mode pins the foreground VS Code HWND/PID and workspace title at task start, checks them before each input step, then asks the extension to confirm the active file and editor focus before typing. Focus can retry briefly only for that same HWND; a different VS Code window is never accepted. `Ctrl+Alt+Escape` or the sidebar Stop control requests an emergency stop; an interrupted edit attempts to restore its pre-edit checkpoint.
- Integrated-terminal execution is a separate approval step. It uses VS Code shell integration, captures output and reports only a shell-provided end event as a verified exit code. If shell integration is unavailable, the command is not run and no pass is reported.

## Source handling

Repository candidates without a root license/copyright file are excluded from task plans. The complete detected license text and source path are displayed and stored with every proposal. This is a conservative filter, not legal advice or a substitute for reviewing the license and file-level notices. GitHub search is unauthenticated and subject to public API rate limits.

The index may contain source snippets and project paths. It is local in `.opencode-human-bot/index.sqlite3`; cloned sources, agent workspaces, and screenshots are local as well. Do not share that directory without reviewing it. Deterministic operations send no source to a hosted service. **Autonomous AI sessions intentionally send eligible repository context to the provider endpoint selected by the user.** Review the provider's retention/training policy and do not use autonomous mode with code that may not leave the machine. API keys are supplied from VS Code SecretStorage at runtime and are not persisted in session state.

## Path and process controls

Target paths are resolved under the active workspace and rejected if they escape it. Cloned repositories are stored under the local cache. The engine skips common generated/dependency directories, environment files, and source files larger than one megabyte. Approved build/test commands can still modify project files or access resources available to the process; inspect each command before approval.

The deterministic engine does not create Git commits. The autonomous engine initializes a new local Git branch inside its disposable workspace but does not commit or configure a remote. It stores affected-file snapshots in SQLite. Rollback restores only the selected agent checkpoint files and does not reset or modify the source repository. A user can still change a file after a checkpoint; review before restoring it.

## Autonomous agent boundaries

- The agent writes only to a newly copied local workspace beneath `.opencode-human-bot/agent-workspaces/`. The copy skips Git metadata, known environment/credential names and patterns, symlinks, dependency/build folders, binary data, and oversized files. This is heuristic secret filtering, not a proof that all secrets or confidential information are removed.
- The model receives bounded file context and can return proposed complete file contents. It has no direct shell, SSH, browser, database, or deployment tools. Provider HTTP requests use HTTPS except explicitly loopback-only HTTP for local testing.
- PAUSE/STOP controls are checked during response streaming and before applying changes. A generated step must pass path, content-size, and file-snapshot conflict checks before an atomic checkpoint/write. Resume after an engine restart requires the user to re-supply the provider key and confirm resending eligible source context plus any saved validation output.
- Validation is disabled unless the user confirms a recognized test/lint/compile command. The engine blocks common SSH/SCP/SFTP, deployment, production and network clients, passes an argument array without `shell=True`, runs in the isolated workspace, gives it a private HOME and reduced environment, and enforces a timeout. **This is not a network sandbox**: an approved repository test script can still contain arbitrary local behavior or attempt network access. Inspect project scripts before approval.
- There is no automatic push, merge, deploy, production DB connection, or remote shell integration in the autonomous worker. The separate legacy workflows retain their own approval gates.

## Limitations

Repair automation recognizes a fixed set of diagnostic patterns. Repository recipes must be explicit, licensed, exact-anchor transformations; each repair and its validation command require approval, and a nonzero validation restores the checkpoint. Unknown diagnostics are not repaired. This is not a complete package/version/ABI resolver or automated license compatibility analysis.

Playwright navigation is restricted to localhost/loopback and browser network requests to other hosts are aborted. Application launch commands require approval, use argument arrays without `shell=True`, and receive a reduced environment. Browser form values are not written to activity logs; screenshots remain local and may contain entered data. Arbitrary approved commands and cloned repositories are still untrusted. Physical keyboard automation is timing/focus-sensitive. Treat all indexed code as untrusted and inspect every proposed diff.
