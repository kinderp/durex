# Roadmap

This document describes the planned evolution of Durex from a simple Codex queue into a general-purpose AI task orchestration platform.

The roadmap is intentionally incremental. Each version should remain usable on its own.

---

## Vision

Durex exists to solve three practical problems:

1. long-running engineering tasks;
2. usage limits that interrupt work;
3. approvals that require a human to be present.

The long-term goal is an orchestrator that can continue useful work while the user is away and request help only when necessary.

```mermaid
flowchart LR
    Queue[Persistent queue] --> Agents[LLM agents]
    Agents --> Policy[Decision policy]
    Policy --> User[Human approvals]
    Agents --> GitHub[Repositories]
    Agents --> Reports[Reports]
```

---

## v0.1

Current generation.

### Features

- SQLite task queue
- task persistence
- usage limit detection
- resume support
- retry support
- simple worker loop

### Architecture

```mermaid
flowchart LR
    Queue[SQLite queue] --> Worker[Worker loop]
    Worker --> Codex[Codex CLI]
```

### Goal

Provide a reliable queue that can survive usage-limit interruptions.

---

## v0.2

PTY bridge and Telegram approvals.

### Features

- PTY runner
- approval detector
- approval policy engine
- Telegram approval bridge
- approval audit trail
- configurable verbosity
- timeout policies

### Architecture

```mermaid
flowchart LR
    Queue[Queue] --> Worker[Worker]
    Worker --> PtyRunner[PTY runner]
    PtyRunner --> Codex[Codex CLI]
    PtyRunner --> Detector[Approval detector]
    Detector --> Policy[Approval policy]
    Policy --> Telegram[Telegram bridge]
    Telegram --> User[User]
```

### Goal

Allow unattended execution while still enabling human approval from a phone.

---

## v0.3

Structured events.

### Features

- event runner
- structured logging
- event audit trail
- unified runner interface

### Architecture

```mermaid
flowchart LR
    Worker --> Runner[Runner interface]
    Runner --> PtyRunner[PTY runner]
    Runner --> EventRunner[Event runner]
    EventRunner --> EventStream[Structured events]
```

### Goal

Reduce reliance on terminal text parsing.

---

## v0.4

Workflow engine.

### Features

- task dependencies
- conditional execution
- task chaining
- workflow graphs

Example:

```text
Run tests
  -> Generate report
      -> Create summary
```

### Architecture

```mermaid
flowchart TD
    TaskA[Run tests]
    TaskB[Generate report]
    TaskC[Create summary]

    TaskA --> TaskB
    TaskB --> TaskC
```

### Goal

Move from independent tasks to orchestrated workflows.

---

## v0.5

GitHub-native automation.

### Features

- GitHub integration
- pull-request workflows
- repository actions
- automated review pipelines

### Architecture

```mermaid
flowchart LR
    GitHub[GitHub repository]
    GitHub --> Queue[Durex queue]
    Queue --> Codex[Codex]
    Codex --> PullRequest[Pull request]
    PullRequest --> Telegram[Approval]
```

### Goal

Enable repository-centered automation.

---

## v0.6

Web dashboard.

### Features

- active tasks view
- completed tasks view
- approval history
- queue management
- retry controls

### Architecture

```mermaid
flowchart LR
    Browser[Web UI]
    Browser --> API[Durex API]
    API --> Queue[Queue]
    API --> Audit[Audit log]
```

### Goal

Make monitoring and control easier.

---

## v0.7

Multi-agent execution.

### Features

- multiple workers
- specialized agents
- distributed queues
- role-based execution

### Architecture

```mermaid
flowchart LR
    Queue[Queue]
    Queue --> AgentA[Agent A]
    Queue --> AgentB[Agent B]
    Queue --> AgentC[Agent C]
```

### Goal

Increase throughput and specialization.

---

## v1.0

Autonomous overnight engineer.

### Features

- persistent workflows
- approvals through Telegram
- GitHub integration
- audit logs
- workflow dependencies
- structured events
- multiple workers

### Architecture

```mermaid
flowchart TD
    Queue[Task queue]
    Queue --> Workers[Worker pool]
    Workers --> Agents[Codex sessions]
    Agents --> Policy[Decision policy]
    Policy --> Telegram[Telegram approvals]
    Agents --> GitHub[Repositories]
    Agents --> Reports[Reports]
    Reports --> Dashboard[Dashboard]
```

### Example overnight flow

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Durex
    participant Codex
    participant Telegram

    User->>Durex: add overnight tasks
    User->>Durex: start worker
    Durex->>Codex: execute tasks
    Codex-->>Durex: approval needed
    Durex->>Telegram: send approval request
    Telegram->>User: phone notification
    User-->>Telegram: decision
    Telegram-->>Durex: approval result
    Durex->>Codex: continue execution
    Codex-->>Durex: completed work
```

### Goal

The user should be able to prepare a queue before going offline and wake up to completed work, reports and reviewable results.

---

## Guiding principles

1. Keep the queue simple.
2. Prefer composable modules.
3. Separate runner logic from approval logic.
4. Keep approval decisions auditable.
5. Support both PTY and structured-event execution.
6. Allow human supervision without requiring continuous presence.
