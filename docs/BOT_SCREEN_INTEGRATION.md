# Bot Desktop Screen Integration Guide

This document describes how Super Agent integrates with the Hermes Bot Desktop subsystem to provide isolated virtual GUI displays (`Xvnc` + minimal XFCE), live browser observation via the Web Dashboard `Display` tab, and deterministic human-takeover lease coordination across persistent worker pools.

---

## 1. Architecture Overview

```
+-------------------------------------------------------------------+
|                   Hermes Web Dashboard (Display Tab)              |
|     Live noVNC stream + Interactive Human Takeover Controls       |
+---------------------------------+---------------------------------+
                                  | WebSocket / Lease Control
                                  v
+-------------------------------------------------------------------+
|             Hermes Host Runtime (<HERMES_HOME>/bot-desktop)       |
|  - Isolated Xvnc + minimal XFCE session (loopback 127.0.0.1 only) |
|  - Control lease file: <HERMES_HOME>/bot-desktop/lease.json       |
+---------------------------------+---------------------------------+
                                  | Read-only Sanitized Observation
                                  v
+-------------------------------------------------------------------+
|                    Super Agent Cognitive Engine                   |
|  - agentcore.screen_lease: fail-closed lease observer             |
|  - agentcore.pool: pauses job timeout budget on human takeover    |
|  - agentcore.colleague_dev: role-scoped screen capability matrix  |
|  - agentcore.profile_spec: declarative WorkerSpec screen config   |
+-------------------------------------------------------------------+
```

Key architectural responsibilities:
- **Hermes Bot Desktop** owns the virtual X11 display server (`Xvnc`), lightweight window manager (`xfwm4`, `xfce4-panel`, `xfdesktop`), `computer_use` toolset execution, and the Web Dashboard `Display` tab.
- **Super Agent** declaratively provisions profile-level screen configuration, diagnoses host binary and memory readiness, exposes role-level screen contracts, and observes `<HERMES_HOME>/bot-desktop/lease.json` so worker supervision never races a human operator during interactive takeover.

---

## 2. Colleague Role Screen Capabilities

In `agentcore/colleague_dev.py`, the 8 canonical engineering colleague roles declare explicit Bot Desktop capabilities in `DEV_ROLE_SCREEN` (mirroring `DEV_ROLE_OWNERSHIP` while preserving strict 4-field catalog parity on `DEV_ROLES`) via `has_screen` and `screen_policy`:

| Role | `has_screen` | Primary Screen Usage |
| :--- | :--- | :--- |
| `architect` | `True` | Inspecting interactive system diagrams, web specifications, and UI flows |
| `test` | `True` | Observing browser test runners, HTML test reports, and visual assertions |
| `frontend` | `True` | Verifying layout rendering, responsive viewports, and interactive cards |
| `ops` | `True` | Checking monitoring dashboards, service consoles, and human-assisted login |
| `cases` | `False` | Headless code and specification authoring inside isolated worktrees |
| `reviewer` | `False` | Static anti-pattern, concurrency, and coverage verification |
| `triage` | `False` | Log, stack trace, and state trajectory root-cause analysis |
| `backend` | `False` | Server APIs, persistence, and queue transaction engineering |

Helper APIs in `agentcore.colleague_dev`:
- `role_supports_screen(role: str) -> bool`
- `get_role_screen_policy(role: str) -> Dict[str, Any]`
- `get_dev_roles(include_screen: bool = True) -> Dict[str, Dict[str, Any]]`

All helpers consult `get_dev_roles()`, ensuring runtime customizations registered through `register_role_override()` take effect immediately.

---

## 3. Declarative `WorkerSpec` Screen Fields

Business profiles (`profile.yaml` or `profile.json`) can enable Bot Desktop per worker in `agentcore/profile_spec.py`:

```yaml
profile_id: "<profile-id>"
description: "Visual verification profile"
workers:
  - role: "frontend"
    model_tier: "pro"
    workdir: "/opt/workspace"
    enable_screen: true
    screen_geometry: "1440x900"
    screen_auto_start: false
    screen_idle_stop_minutes: 30
    screen_min_free_memory_mb: 1536
    screen_placement: "auto"
```

### Field Reference & Validation Rules

| Field | Type | Default | Validation Constraint |
| :--- | :--- | :--- | :--- |
| `enable_screen` | `bool` | `False` | Enables `computer_use` toolset and systemd screen env vars |
| `screen_geometry` | `str` | `"1440x900"` | Must match `^\d{3,5}x\d{3,5}\Z` |
| `screen_auto_start` | `bool` | `False` | Maps to `bot_desktop.auto_start` and `computer_use.autostart` |
| `screen_idle_stop_minutes` | `int` | `30` | Must be `>= 0` (`0` disables idle auto-stop) |
| `screen_min_free_memory_mb` | `int` | `1536` | Must be `>= 0` (minimum available host RAM in MiB) |
| `screen_placement` | `str` | `"auto"` | Must be one of `"auto"`, `"terminal"`, `"gateway"` |

When `enable_screen: true` is set on a worker:
1. `WorkerSpec.render_bot_desktop_config()` and `BusinessProfileSpec.render_hermes_screen_overlay()` emit exact upstream Hermes schema keys (`platform_toolsets`, `bot_desktop`, and `computer_use`).
2. `BusinessProfileSpec.render_worker_service(worker)` injects `SUPERAGENT_ENABLE_SCREEN=1` and `SUPERAGENT_SCREEN_GEOMETRY=<geometry>` into the generated systemd service unit.
3. `./install/superagent.sh profile inspect <spec-path>` displays the worker screen configuration summary.

---

## 4. Operations CLI (`superagent.sh screen`)

Super Agent provides a unified operational entrypoint via `./install/superagent.sh screen` (backed by `install/setup_screen.py`). All mutating operations default to dry-run preview unless `--apply` is explicitly supplied.

### 4.1 Check Host Readiness (`doctor`)

Verifies the 9 required host binaries (`Xvnc`, `xfce4-panel`, `xfwm4`, `xfdesktop`, `xfce4-terminal`, `dbus-launch`, `xsetroot`, `xdpyinfo`, `xauth`), checks `/proc/meminfo` `MemAvailable`, and inspects the sanitized takeover lease state:

```bash
./install/superagent.sh screen doctor --hermes-home ~/.hermes --min-free-mb 1536
./install/superagent.sh screen doctor --hermes-home ~/.hermes --json
```

Returns exit code `0` when all required binaries and available memory meet thresholds, or `1` otherwise.

### 4.2 Install Minimal Host Dependencies (`install-deps`)

Detects the host distribution (`debian`, `fedora`, or `arch`) from `/etc/os-release` and builds the minimal Xvnc + XFCE package installation command. The full `xfce4` metapackage is intentionally excluded to prevent screensaver daemons and polkit prompts from locking headless sessions:

```bash
# Dry-run package plan
./install/superagent.sh screen install-deps

# Execute installation (uses non-interactive sudo -n when non-root)
./install/superagent.sh screen install-deps --apply
```

### 4.3 Configure Hermes Profile (`configure`)

Merges `bot_desktop`, `computer_use`, and `"computer_use"` under `platform_toolsets` into `<hermes-home>/config.yaml`:

```bash
# Preview unified diff without modifying disk
./install/superagent.sh screen configure \
  --hermes-home ~/.hermes \
  --geometry 1440x900 \
  --placement auto \
  --idle-stop-minutes 30 \
  --min-free-mb 1536

# Apply configuration atomically
./install/superagent.sh screen configure \
  --hermes-home ~/.hermes \
  --geometry 1440x900 \
  --placement auto \
  --apply
```

### 4.4 Lifecycle Inspection & Control (`status`, `start`, `stop`)

```bash
./install/superagent.sh screen status --hermes-home ~/.hermes
./install/superagent.sh screen start  --hermes-home ~/.hermes
./install/superagent.sh screen stop   --hermes-home ~/.hermes
```

`status` first prints the sanitized Super Agent lease snapshot from `<hermes-home>/bot-desktop/lease.json`, then delegates to `hermes computer-use screen status` with `HERMES_HOME` set in the child environment.

---

### 4.5 Browser Web Portal (`web`)

Upstream Hermes renders Bot Desktop inside the standalone Hermes Desktop application. For pure browser access without installing a desktop client, Super Agent provides a zero-dependency RFB-over-WebSocket portal (`install/screen_web_portal.py`):

```bash
# Run foreground portal on http://127.0.0.1:9120
./install/superagent.sh screen web --hermes-home ~/.hermes/profiles/<profile-id> --port 9120

# Install and start persistent systemd user unit (superagent-screen-portal.service)
./install/superagent.sh screen web --hermes-home ~/.hermes/profiles/<profile-id> --port 9120 --install-service --apply
```

---

## 5. Browser / Desktop Viewer & Human Takeover Lease Semantics

1. Operators open `http://127.0.0.1:9120` (Super Agent Web Portal) or the **Screen** pane in Hermes Desktop to watch the live virtual desktop stream.
2. When an operator clicks **Take Over** (`接管屏幕`, for example, to complete a multi-factor authentication prompt or inspect UI state interactively), `"holder": "human"` is written atomically under `lease.lock` to `<HERMES_HOME>/bot-desktop/lease.json`.
3. Super Agent workers observe this file via `agentcore.screen_lease.read_bot_desktop_lease()`:
   - While `holder == "human"`, the worker supervision loop transitions liveness stage to `human_takeover_wait` and **pauses execution wall-clock timeout accounting** so long-running jobs are not killed while waiting for the human operator.
   - Once the operator clicks **Hand Back** (`交还给 Bot`), normal execution and timeout accounting resume seamlessly.

---

## 6. Security Boundaries

- **Loopback-Only Binding**: `Xvnc` and internal VNC websocket bridges bind strictly to `127.0.0.1`; external access is gated exclusively through authenticated Hermes Dashboard sessions or SSH port forwarding.
- **Capability Token Redaction**: `agentcore.screen_lease` never exposes or logs raw `viewer_id` capability tokens from `lease.json`. Only a 12-hex-character SHA-256 prefix (`viewer_hash`) is emitted in diagnostics and CLI status output.
- **Fail-Closed Lease Observation**: If `<HERMES_HOME>/bot-desktop/lease.json` exists but is unreadable, contains malformed JSON, or specifies an unrecognized `holder`, `read_bot_desktop_lease()` fails closed to `holder="human"` (`fail_closed=True`). Neither autonomous agents nor timeout reapers preempt the desktop when lease state is ambiguous.
- **Standard Library Isolation**: All `agentcore` screen contract and lease observation modules use pure Python standard library imports only.
