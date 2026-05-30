# Architecture

This document describes the planned v0.2 architecture for Durex.

Durex is evolving from a simple Codex task queue into a local LLM task orchestrator with remote human approval through Telegram.

The main goal of v0.2 is to let Codex run unattended for long sessions while still allowing the user to approve sensitive terminal prompts from a phone.

---

## High-level architecture

```mermaid
flowchart TD
    User[User] -->|adds tasks| CLI[codex_queue.py CLI]
    CLI -->|insert/update| DB[(SQLite task database)]
    Worker[Worker loop] -->|select next task| DB
    Worker --> Runner{Runner mode}
    Runner -->|non-interactive mode| SubprocessRunner[subprocess runner]
    Runner -->|interactive approval mode| PtyRunner[PTY runner]
    SubprocessRunner --> CodexExec[Codex CLI]
    PtyRunner --> CodexInteractive[Codex CLI in pseudo-terminal]
    CodexInteractive --> Detector[approval_detector.py]
    Detector --> Policy[approval_policy.py]
    Policy -->|auto allow| PtyRunner
    Policy -->|auto deny| PtyRunner
    Policy -->|ask user| Telegram[telegram_bridge.py]
    Telegram -->|inline keyboard decision| User
    User -->|approve or deny| Telegram
    Telegram --> PtyRunner
    PtyRunner -->|write response into PTY stdin| CodexInteractive
    CodexExec -->|stdout stderr return code| Worker
    CodexInteractive -->|terminal output return code| Worker
    Worker -->|persist output status session reset time| DB
    Worker --> Logs[(logs directory)]
```

---

## Main components

```mermaid
flowchart LR
    subgraph QueueLayer[Queue layer]
        CLI[codex_queue.py]
        DB[(SQLite)]
    end

    subgraph RunnerLayer[Runner layer]
        BuildCommand[build_codex_command task]
        ClassicRun[run_codex task]
        PtyRun[run_codex_with_pty task config]
    end

    subgraph ApprovalLayer[Approval layer]
        Detector[detect_approval_request text]
        Policy[classify_command command]
        Decision[ApprovalDecision]
    end

    subgraph RemoteLayer[Remote approval layer]
        Bot[TelegramApprovalBridge]
        Request[send_approval_request payload]
        Wait[wait_for_decision request_id]
    end

    CLI --> DB
    DB --> BuildCommand
    BuildCommand --> ClassicRun
    BuildCommand --> PtyRun
    PtyRun --> Detector
    Detector --> Policy
    Policy --> Decision
    Decision --> Request
    Request --> Bot
    Bot --> Wait
    Wait --> PtyRun
    ClassicRun --> DB
    PtyRun --> DB
```

---

## Task lifecycle

```mermaid
stateDiagram-v2
    [*] --> PENDING
    PENDING --> RUNNING: worker selects task
    RUNNING --> COMPLETED: Codex exits with success
    RUNNING --> WAITING_LIMIT: usage limit detected
    WAITING_LIMIT --> RUNNING: reset_at passed
    RUNNING --> PENDING: retryable error and attempts left
    RUNNING --> FAILED: max attempts reached
    RUNNING --> FAILED: local runner error
    COMPLETED --> [*]
    FAILED --> [*]
```

---

## PTY approval pipeline

```mermaid
flowchart TD
    A[PTY receives terminal output chunk] --> B[Append chunk to rolling buffer]
    B --> C[strip_ansi text]
    C --> D[detect_approval_request buffer]
    D -->|no approval| E[keep reading PTY]
    D -->|approval detected| F[extract_command context]
    F --> G[classify command with policy]
    G -->|AUTO_ALLOW| H[write y newline to PTY]
    G -->|AUTO_DENY| I[write n newline to PTY]
    G -->|ASK_TELEGRAM| J[send Telegram approval request]
    J --> K[wait for Telegram callback]
    K -->|approve| H
    K -->|deny| I
    K -->|show context| L[send additional terminal context]
    L --> K
    K -->|timeout| M[apply timeout default decision]
    M --> H
    M --> I
```

---

## Data model overview

The current database table is intentionally simple. v0.2 can keep the same task table and add optional fields later.

```mermaid
erDiagram
    TASKS {
        integer id PK
        text title
        text prompt
        text workdir
        integer priority
        text status
        text session_id
        text next_step
        text reset_at
        integer attempts
        integer max_attempts
        text last_error
        text output
        text created_at
        text updated_at
    }

    APPROVAL_EVENTS {
        integer id PK
        integer task_id FK
        text command
        text policy_decision
        text user_decision
        text telegram_message_id
        text context_excerpt
        text created_at
        text decided_at
    }

    TASKS ||--o{ APPROVAL_EVENTS : produces
```

---

## Function-level data flow

```mermaid
flowchart TD
    A[worker_loop check_interval stop_when_empty] --> B[get_next_task]
    B --> C[run_codex task]
    C --> D[build_codex_command task]
    D --> E{approval bridge enabled?}
    E -->|no| F[subprocess.run cmd cwd]
    E -->|yes| G[run_pty_command cmd cwd bridge config]
    F --> H[combined_output returncode]
    G --> I[PtyRunResult output returncode approval_events]
    H --> J[extract_session_id output]
    H --> K[extract_reset_at output]
    I --> J
    I --> K
    J --> L[update_task status output session_id]
    K --> L
```

---

## Configuration flow

```mermaid
flowchart LR
    Env[Environment variables] --> ConfigLoader[load_config]
    File[config.yaml] --> ConfigLoader
    Defaults[Built-in defaults] --> ConfigLoader
    ConfigLoader --> RunnerConfig[Runner config]
    ConfigLoader --> TelegramConfig[Telegram config]
    ConfigLoader --> PolicyConfig[Approval policy config]
    RunnerConfig --> PtyRunner
    TelegramConfig --> TelegramBridge
    PolicyConfig --> ApprovalPolicy
```

---

## Security boundaries

Durex v0.2 should keep these boundaries clear:

1. The Telegram bot does not execute commands directly.
2. Telegram only returns a decision to the local PTY runner.
3. The local Codex process remains the only process receiving terminal input.
4. The policy engine decides whether a command can be auto-approved, denied, or sent to Telegram.
5. All approval decisions should be logged for auditing.

---

## Why PTY first

PTY is the practical v0.2 choice because it can operate with the existing interactive terminal behavior. It does not require Codex to expose every approval event as structured data.

The tradeoff is that PTY parsing is text-based and therefore less stable than structured events. For that reason, the roadmap keeps structured event support as the v0.3/v0.4 direction.
