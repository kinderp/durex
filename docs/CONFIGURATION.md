# Configuration

This document describes the planned configuration model for Durex v0.2.

The objective is to move runtime settings out of the source code and into a configuration file while still allowing environment-variable overrides for secrets.

---

## Configuration sources

The recommended precedence order is:

```mermaid
%%{init: {"theme": "base", "themeVariables": {"primaryColor": "#ffffff", "primaryTextColor": "#111827", "primaryBorderColor": "#374151", "lineColor": "#374151", "secondaryColor": "#f3f4f6", "tertiaryColor": "#ffffff", "textColor": "#111827", "mainBkg": "#ffffff", "nodeBorder": "#374151", "clusterBkg": "#f9fafb", "clusterBorder": "#9ca3af", "edgeLabelBackground": "#ffffff", "actorBkg": "#ffffff", "actorBorder": "#374151", "actorTextColor": "#111827", "activationBkgColor": "#e5e7eb", "activationBorderColor": "#374151", "signalColor": "#111827", "signalTextColor": "#111827", "noteBkgColor": "#fef3c7", "noteTextColor": "#111827", "noteBorderColor": "#92400e"}}}%%
flowchart TD
    Defaults[Built-in defaults]
    File[config.yaml]
    Env[Environment variables]

    Defaults --> Final[Effective configuration]
    File --> Final
    Env --> Final
```

Priority:

```text
Environment variables
    > config.yaml
        > built-in defaults
```

---

## Configuration file

Example:

```yaml
runner:
  mode: pty
  check_interval_seconds: 60
  default_retry_hours: 5

telegram:
  enabled: true
  bot_token_env: DUREX_TELEGRAM_BOT_TOKEN
  allowed_chat_id_env: DUREX_TELEGRAM_CHAT_ID
  verbosity: normal
  approval_timeout_seconds: 900
  timeout_default_decision: deny

policy:
  default_decision: ask

logging:
  directory: logs
  approval_audit_enabled: true
```

---

## Runner section

```yaml
runner:
  mode: pty
  check_interval_seconds: 60
  default_retry_hours: 5
```

### mode

Allowed values:

```text
pty
events
```

Meaning:

| Value | Meaning |
|---|---|
| `pty` | Run Codex inside a pseudo-terminal |
| `events` | Use structured event mode when available |

Recommended v0.2 value:

```yaml
mode: pty
```

---

### check_interval_seconds

How often the worker checks for runnable tasks.

Example:

```yaml
check_interval_seconds: 60
```

Equivalent to:

```python
worker_loop(check_interval=60)
```

---

### default_retry_hours

Fallback delay when a usage-limit reset timestamp cannot be extracted.

Example:

```yaml
default_retry_hours: 5
```

Behavior:

```text
reset_at = utc_now + default_retry_hours
```

---

## Telegram section

```yaml
telegram:
  enabled: true
  bot_token_env: DUREX_TELEGRAM_BOT_TOKEN
  allowed_chat_id_env: DUREX_TELEGRAM_CHAT_ID
  verbosity: normal
  approval_timeout_seconds: 900
  timeout_default_decision: deny
```

### enabled

Enable or disable Telegram approvals.

```yaml
enabled: true
```

---

### bot_token_env

Environment variable containing the bot token.

```yaml
bot_token_env: DUREX_TELEGRAM_BOT_TOKEN
```

Example:

```bash
export DUREX_TELEGRAM_BOT_TOKEN="example-token"
```

---

### allowed_chat_id_env

Environment variable containing the authorized Telegram chat id.

```yaml
allowed_chat_id_env: DUREX_TELEGRAM_CHAT_ID
```

Example:

```bash
export DUREX_TELEGRAM_CHAT_ID="123456789"
```

---

### verbosity

Controls the amount of information sent to Telegram.

Allowed values:

```text
compact
normal
verbose
```

Comparison:

| Mode | Content |
|---|---|
| compact | task and command |
| normal | task, command, reason |
| verbose | task, command, reason and terminal context |

---

### approval_timeout_seconds

Maximum time to wait for a Telegram decision.

Example:

```yaml
approval_timeout_seconds: 900
```

Meaning:

```text
15 minutes
```

---

### timeout_default_decision

Decision applied when the timeout expires.

Allowed values:

```text
approve
deny
stop
```

Recommended value:

```yaml
timeout_default_decision: deny
```

---

## Policy section

```yaml
policy:
  default_decision: ask
```

The policy engine decides whether an approval request:

```text
is automatically approved
is automatically denied
must be sent to Telegram
```

---

### default_decision

Allowed values:

```text
ask
approve
deny
```

Recommended value:

```yaml
default_decision: ask
```

---

### Future policy rules

A future version can support explicit rule lists.

Example:

```yaml
policy:
  default_decision: ask

  auto_allow:
    - test commands
    - static analysis commands

  ask_telegram:
    - repository modification commands
    - package installation commands

  auto_deny:
    - explicitly forbidden operations
```

The exact syntax can evolve without changing the overall architecture.

---

## Logging section

```yaml
logging:
  directory: logs
  approval_audit_enabled: true
```

### directory

Directory used for log files.

```yaml
directory: logs
```

Future layout:

```text
logs/
  task_1.log
  task_2.log
  task_3.log
```

---

### approval_audit_enabled

Enable audit logging of approval decisions.

```yaml
approval_audit_enabled: true
```

Future output:

```text
approval_audit.log
```

Example event:

```text
2026-05-30T22:15:00Z
request_id=abc123
source=telegram
action=approve
```

---

## Configuration loader

Recommended architecture:

```mermaid
%%{init: {"theme": "base", "themeVariables": {"primaryColor": "#ffffff", "primaryTextColor": "#111827", "primaryBorderColor": "#374151", "lineColor": "#374151", "secondaryColor": "#f3f4f6", "tertiaryColor": "#ffffff", "textColor": "#111827", "mainBkg": "#ffffff", "nodeBorder": "#374151", "clusterBkg": "#f9fafb", "clusterBorder": "#9ca3af", "edgeLabelBackground": "#ffffff", "actorBkg": "#ffffff", "actorBorder": "#374151", "actorTextColor": "#111827", "activationBkgColor": "#e5e7eb", "activationBorderColor": "#374151", "signalColor": "#111827", "signalTextColor": "#111827", "noteBkgColor": "#fef3c7", "noteTextColor": "#111827", "noteBorderColor": "#92400e"}}}%%
flowchart LR
    Defaults[Defaults]
    ConfigFile[config.yaml]
    Environment[Environment variables]
    Loader[load_config]
    Effective[EffectiveConfig]

    Defaults --> Loader
    ConfigFile --> Loader
    Environment --> Loader
    Loader --> Effective
```

The rest of the application should depend only on:

```text
EffectiveConfig
```

and not directly on environment variables or YAML parsing.

---

## Future configuration areas

Future versions may add:

```yaml
github:
  enabled: true

workflow:
  enabled: true

web:
  enabled: true

workers:
  count: 4
```

The v0.2 design should keep configuration modular enough to support these additions without redesigning the entire file.
