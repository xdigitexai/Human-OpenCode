# OpenCode Human Bot

Local-first coding assistance with deterministic approval-gated workflows and an opt-in provider-backed autonomous development agent. Existing source indexing and licensed replacement workflows remain available.

The autonomous agent works only in a disposable local Git branch copy; it does not edit the original repository, push, deploy, or expose shell tools to the model. Eligible source context is sent to the user-selected provider. Configure provider keys through VS Code SecretStorage. Test commands are never automatic and require confirmation; review repository scripts before running them. Approved test scripts are not network-sandboxed.

Install the Python project with its `codebot` extra, compile this extension with `npm.cmd install` and `npm.cmd run compile`, then package it with `npm.cmd run package`. Use **API KEY** and the **Autonomous Local Development** dashboard for AI sessions. See the workspace's `INSTALLATION.md`, `README.md`, and `SECURITY.md` for setup, current capability gaps, and data-handling details.
