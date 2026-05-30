# Sequence Diagrams

This document describes the planned v0.2 runtime flows for Durex.

The diagrams intentionally include function names and the main data passed between components. They are meant to be used as an implementation guide for the PTY bridge and Telegram approval features.

---

## 1. Normal non-interactive task execution

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant CLI as codex_queue.py CLI
    participant DB as SQLite tasks table
    participant Worker as worker_loop()
    participant Runner as run_codex()
    participant Codex as Codex CLI

    User->>CLI: python3 codex_queue.py add --title --prompt --workdir --priority
    CLI->>DB: add_task(title, prompt, workdir, priority, max_attempts)
    User->>CLI: python3 codex_queue.py run
    CLI->>Worker: worker_loop(check_interval, stop_when_empty)
    Worker->>DB: get_next_task()
    DB-->>Worker: task{id,title,prompt,workdir,status,attempts}
    Worker->>Runner: run_codex(task)
    Runner->>Runner: build_codex_command(task)
    Runner->>DB: update_task(id, status='RUNNING', attempts=attempts+1)
    Runner->>Codex: subprocess.run(cmd, cwd=workdir)
    Codex-->>Runner: stdout, stderr, returncode
    Runner->>Runner: extract_session_id(output)
    Runner->>Runner: extract_reset_at(output)
    Runner->>DB: update_task(id, status='COMPLETED', output, session_id)
```

Data passed:

```text
task = {
  id,
  title,
  prompt,
  workdir,
  priority,
  status,
  session_id,
  next_step,
  attempts,
  max_attempts
}
```

---

## 2. Usage limit reached

```mermaid
sequenceDiagram
    autonumber
    participant Worker as worker_loop()
    participant Runner as run_codex()
    participant Codex as Codex CLI
    participant DB as SQLite tasks table

    Worker->>Runner: run_codex(task)
    Runner->>Codex: subprocess.run(cmd, cwd=task.workdir)
    Codex-->>Runner: stderr includes usage-limit text, returncode != 0
    Runner->>Runner: looks_like_usage_limit(output)
    Runner->>Runner: extract_reset_at(output)
    alt reset_at found
        Runner->>DB: update_task(status='WAITING_LIMIT', reset_at=parsed_reset_at)
    else reset_at not found
        Runner->>Runner: fallback reset_at = utc_now() + default_retry_hours
        Runner->>DB: update_task(status='WAITING_LIMIT', reset_at=fallback_reset_at)
    end
    Runner->>DB: update_task(output, last_error='Usage limit reached', next_step)
```

Important fields saved:

```text
status='WAITING_LIMIT'
reset_at='ISO-8601 UTC timestamp'
next_step='Resume from where you stopped and complete the task.'
```

---

## 3. Automatic resume after reset_at

```mermaid
sequenceDiagram
    autonumber
    participant Worker as worker_loop()
    participant DB as SQLite tasks table
    participant Runner as run_codex()
    participant Codex as Codex CLI

    Worker->>DB: get_next_task()
    DB-->>Worker: task where status='WAITING_LIMIT' and reset_at <= now
    Worker->>Runner: run_codex(task)
    Runner->>Runner: build_codex_command(task)
    alt task.session_id exists
        Runner->>Codex: codex exec resume session_id next_step
    else no session_id
        Runner->>Codex: codex exec original_prompt
    end
    Codex-->>Runner: output, returncode
    Runner->>DB: update_task(status, output, session_id, reset_at=None)
```

---

## 4. PTY task execution with Telegram approval

```mermaid
sequenceDiagram
    autonumber
    participant Worker as worker_loop()
    participant Pty as run_pty_command()
    participant Codex as Codex CLI in PTY
    participant Detector as detect_approval_request()
    participant Policy as classify_command()
    participant Telegram as TelegramApprovalBridge
    participant User
    participant DB as SQLite tasks table

    Worker->>Pty: run_pty_command(cmd, cwd, task, config)
    Pty->>Codex: spawn process in pseudo-terminal
    Codex-->>Pty: terminal output chunk
    Pty->>Pty: append chunk to rolling_buffer
    Pty->>Detector: detect_approval_request(rolling_buffer)
    Detector-->>Pty: ApprovalRequest(command, reason, context)
    Pty->>Policy: classify_command(command)
    Policy-->>Pty: ASK_TELEGRAM
    Pty->>Telegram: send_approval_request(task, command, context, verbosity)
    Telegram->>User: message with inline buttons
    User-->>Telegram: approve
    Telegram-->>Pty: ApprovalDecision(action='approve')
    Pty->>Codex: write 'y' plus newline to PTY stdin
    Codex-->>Pty: continues execution
    Codex-->>Pty: final output and exit status
    Pty-->>Worker: PtyRunResult(returncode, output, approval_events)
    Worker->>DB: update_task(status='COMPLETED', output)
```

---

## 5. Auto-allow policy flow

```mermaid
sequenceDiagram
    autonumber
    participant Pty as run_pty_command()
    participant Detector as detect_approval_request()
    participant Policy as ApprovalPolicy
    participant Codex as Codex CLI in PTY
    participant Audit as approval log

    Pty->>Detector: detect_approval_request(buffer)
    Detector-->>Pty: ApprovalRequest(command='test command', context)
    Pty->>Policy: classify_command(command)
    Policy-->>Pty: AUTO_ALLOW
    Pty->>Audit: record decision {source:'policy', action:'approve'}
    Pty->>Codex: write 'y' plus newline
```

The policy engine should only auto-allow commands that are explicitly configured as safe for the local project.

---

## 6. Auto-deny policy flow

```mermaid
sequenceDiagram
    autonumber
    participant Pty as run_pty_command()
    participant Detector as detect_approval_request()
    participant Policy as ApprovalPolicy
    participant Codex as Codex CLI in PTY
    participant Audit as approval log

    Pty->>Detector: detect_approval_request(buffer)
    Detector-->>Pty: ApprovalRequest(command, context)
    Pty->>Policy: classify_command(command)
    Policy-->>Pty: AUTO_DENY
    Pty->>Audit: record decision {source:'policy', action:'deny'}
    Pty->>Codex: write 'n' plus newline
```

---

## 7. Telegram timeout flow

```mermaid
sequenceDiagram
    autonumber
    participant Pty as run_pty_command()
    participant Telegram as TelegramApprovalBridge
    participant User
    participant Codex as Codex CLI in PTY
    participant Audit as approval log

    Pty->>Telegram: send_approval_request(request_id, task, command, context)
    Telegram->>User: inline keyboard
    Telegram-->>Pty: no callback received before timeout
    Pty->>Pty: apply timeout_default_decision
    alt timeout default is approve
        Pty->>Codex: write 'y' plus newline
        Pty->>Audit: record timeout approval
    else timeout default is deny
        Pty->>Codex: write 'n' plus newline
        Pty->>Audit: record timeout denial
    end
```

For safety, the recommended timeout default is denial.

---

## 8. Show more context flow

```mermaid
sequenceDiagram
    autonumber
    participant Pty as run_pty_command()
    participant Telegram as TelegramApprovalBridge
    participant User

    Pty->>Telegram: send_approval_request(context_excerpt)
    Telegram->>User: compact approval message
    User-->>Telegram: show_context
    Telegram->>User: send longer terminal context
    User-->>Telegram: approve or deny
    Telegram-->>Pty: ApprovalDecision(action)
```

---

## 9. Future structured event runner

```mermaid
sequenceDiagram
    autonumber
    participant Worker as worker_loop()
    participant EventRunner as run_event_command()
    participant Codex as Codex CLI JSON events
    participant Policy as ApprovalPolicy
    participant Telegram as TelegramApprovalBridge
    participant User
    participant DB as SQLite

    Worker->>EventRunner: run_event_command(cmd, cwd, task, config)
    EventRunner->>Codex: codex exec --json prompt
    Codex-->>EventRunner: {type:'command_request', command, cwd, reason}
    EventRunner->>Policy: classify_command(command)
    Policy-->>EventRunner: ASK_TELEGRAM
    EventRunner->>Telegram: send_approval_request(structured_payload)
    Telegram->>User: inline keyboard
    User-->>Telegram: approve
    Telegram-->>EventRunner: ApprovalDecision('approve')
    EventRunner->>Codex: send structured approval response if supported
    Codex-->>EventRunner: {type:'completed', output, session_id}
    EventRunner->>DB: update_task(status='COMPLETED', output, session_id)
```

---

## 10. Overnight unattended workflow

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant Queue as Durex queue
    participant Worker as worker_loop()
    participant Codex as Codex CLI
    participant Telegram as Telegram bridge
    participant DB as SQLite

    User->>Queue: add many tasks before sleeping
    User->>Worker: start overnight worker
    loop while tasks are available
        Worker->>DB: get_next_task()
        DB-->>Worker: next task
        Worker->>Codex: execute task
        alt approval needed
            Codex-->>Telegram: via PTY bridge approval request
            Telegram->>User: phone notification
            User-->>Telegram: approve or deny
            Telegram-->>Codex: via PTY bridge y or n
        end
        alt usage limit reached
            Worker->>DB: save WAITING_LIMIT and reset_at
            Worker->>Worker: wait until a task is ready
        else task completed
            Worker->>DB: save COMPLETED and output
        end
    end
```
