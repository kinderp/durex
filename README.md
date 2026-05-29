# Codex Queue

`codex_queue.py` is a small Python orchestrator for running a list of tasks with **Codex CLI**, storing their state, and automatically resuming them after a usage limit resets.

The idea is simple: instead of launching Codex manually every time, you prepare a queue of jobs and leave the program running, even overnight.

## What it is for

It can be used to:

- grade student assignments;
- run automated tests;
- generate reports;
- create feedback;
- resume interrupted work;
- make use of overnight usage windows;
- suspend a task when Codex reaches a usage limit;
- automatically retry after the reset.

## General structure

The program uses a local SQLite database:

```text
codex_tasks.db
```

The database stores tasks with these statuses:

```text
PENDING
RUNNING
WAITING_LIMIT
COMPLETED
FAILED
```

Meaning:

| Status | Meaning |
|---|---|
| `PENDING` | The task is waiting |
| `RUNNING` | The task is running |
| `WAITING_LIMIT` | Codex reached a usage limit and the task is waiting for reset |
| `COMPLETED` | The task completed successfully |
| `FAILED` | The task failed permanently |

## Requirements

You need:

- Python 3.10 or newer;
- Codex CLI installed;
- Codex access already configured;
- a project directory where Codex can work.

Check Python:

```bash
python3 --version
```

Check Codex:

```bash
codex --help
```

If the command is not called `codex`, open `codex_queue.py` and edit this line:

```python
CODEX_BIN = "codex"
```

## Installation

Download or copy the file:

```text
codex_queue.py
```

Optionally make it executable:

```bash
chmod +x codex_queue.py
```

You can still use it like this:

```bash
python3 codex_queue.py
```

## First run

Initialize the database:

```bash
python3 codex_queue.py init
```

Expected result:

```text
Database initialized.
```

This will create the file:

```text
codex_tasks.db
```

## Add example tasks

You can load three example tasks:

```bash
python3 codex_queue.py seed --workdir /path/to/project
```

Example:

```bash
python3 codex_queue.py seed --workdir /Users/antonio/projects/student_A
```

This adds tasks such as:

1. grade a student assignment;
2. generate tests;
3. create a final report.

## Show the task list

```bash
python3 codex_queue.py list
```

Example output:

```text
[1] Grade student A assignment | status=PENDING | priority=1 | attempts=0 | reset_at=None | workdir=/Users/antonio/projects/student_A
[2] Generate missing automated tests | status=PENDING | priority=2 | attempts=0 | reset_at=None | workdir=/Users/antonio/projects/student_A
[3] Create final report | status=PENDING | priority=3 | attempts=0 | reset_at=None | workdir=/Users/antonio/projects/student_A
```

## Start the worker

To start executing tasks:

```bash
python3 codex_queue.py run
```

The program:

1. takes the first available task;
2. runs it with Codex;
3. stores the output;
4. moves to the next task;
5. if it detects a usage limit, it pauses the task;
6. retries after `reset_at`.

## Overnight run

To let it work overnight:

```bash
nohup python3 codex_queue.py run > codex_queue.log 2>&1 &
```

Meaning:

- `nohup` keeps the process alive even if you close the terminal;
- `>` saves the output to a file;
- `2>&1` saves errors to the same file;
- `&` runs the process in the background.

To watch the log:

```bash
tail -f codex_queue.log
```

## Add a task manually

Simple example:

```bash
python3 codex_queue.py add \
  --title "Grade student B" \
  --workdir /Users/antonio/projects/student_B \
  --priority 1 \
  --prompt "Run the tests, grade the assignment, and generate report_student_B.md"
```

## More complete grading example

```bash
python3 codex_queue.py add \
  --title "Grade Mario Rossi assignment" \
  --workdir /Users/antonio/grading/mario_rossi \
  --priority 1 \
  --prompt "You are an impartial grader. Read the code in the current directory. Run the tests. Evaluate using this rubric: correctness 0-4, code quality 0-2, error handling 0-2, clarity 0-2. Create a file report_mario_rossi.md with the grade, reasoning for each criterion, and final evaluation."
```

## Priority

Priority works like this:

```text
priority=1    very urgent
priority=10   important
priority=100  normal
priority=999  low priority
```

The worker runs lower numerical priority first.

Example:

```bash
python3 codex_queue.py add \
  --title "Urgent task" \
  --prompt "Do this before the others" \
  --priority 1
```

## Stop when there are no tasks

By default, the worker keeps running forever.

If you want it to stop when no task is ready:

```bash
python3 codex_queue.py run --stop-when-empty
```

Useful for quick tests.

## Change the check interval

By default, it checks every 60 seconds.

To check every 5 minutes:

```bash
python3 codex_queue.py run --interval 300
```

## How it handles usage limits

When Codex fails, the script checks whether the output contains words such as:

```text
usage limit
rate limit
quota
too many requests
429
limit reached
```

If it finds them, the task moves to:

```text
WAITING_LIMIT
```

Then it tries to read a reset time, for example:

```text
resets_at: "2026-05-30T03:00:00Z"
```

If it cannot read one, it uses an estimate:

```text
current time + 5 hours
```

This part is in the code:

```python
if not reset_at:
    reset_at = (utc_now() + dt.timedelta(hours=5)).isoformat()
```

You can change `5` to another value.

## Task resume

If the script finds a `session_id`, it tries to resume using:

```bash
codex exec resume SESSION_ID "continue..."
```

If it does not find a `session_id`, the task can be launched again with the original prompt.

Note: `session_id` detection depends on Codex output format. If Codex prints the session ID differently, edit this function:

```python
extract_session_id()
```

## Where the output is stored

Codex output is saved in the database field:

```text
output
```

For a more advanced version, you can add a function that exports outputs into `.md` files.

Future example:

```text
reports/task_1.md
reports/task_2.md
reports/task_3.md
```

## Inspect the database manually

You can open the database with SQLite:

```bash
sqlite3 codex_tasks.db
```

Then run:

```sql
SELECT id, title, status, priority, reset_at FROM tasks;
```

To exit:

```sql
.quit
```

## Recommended structure for assignment grading

A good overnight queue could be:

```text
1. run tests for student A
2. grade student A
3. generate student A report
4. run tests for student B
5. grade student B
6. generate student B report
7. create general summary
```

With priorities:

```text
priority=1   student grading
priority=2   individual reports
priority=3   final report
priority=4   software improvements
```

## Example workflow

```bash
python3 codex_queue.py init

python3 codex_queue.py add \
  --title "Grade student A" \
  --workdir /Users/antonio/grading/student_A \
  --priority 1 \
  --prompt "Run tests, evaluate using the rubric, and create report_A.md"

python3 codex_queue.py add \
  --title "Grade student B" \
  --workdir /Users/antonio/grading/student_B \
  --priority 1 \
  --prompt "Run tests, evaluate using the rubric, and create report_B.md"

python3 codex_queue.py add \
  --title "Final class report" \
  --workdir /Users/antonio/grading \
  --priority 3 \
  --prompt "Read all student reports and create report_final_class.md"

nohup python3 codex_queue.py run > codex_queue.log 2>&1 &
```

## Limits of this version

This is a simple local version.

Limits:

- it uses SQLite, not PostgreSQL;
- it has no web interface;
- it does not automatically export outputs to separate files;
- it does not manage multiple parallel workers;
- `reset_at` detection depends on the text returned by Codex;
- resume depends on the availability of a `session_id`.

For a professional version, you could add:

- PostgreSQL;
- Redis Queue;
- web dashboard;
- per-task log files;
- Telegram/email notifications;
- GitHub Actions integration;
- automatic Markdown/PDF reports;
- management of multiple classes and students;
- grading rubrics loaded from YAML/JSON files.


## Common issues

### 1. `codex: command not found`

Codex CLI is not installed or is not in your PATH.

Check:

```bash
which codex
```

If the command has a different path, you can edit:

```python
CODEX_BIN = "codex"
```

with something like:

```python
CODEX_BIN = "/usr/local/bin/codex"
```

### 2. Working directory does not exist

Possible error:

```text
No such file or directory
```

Check `--workdir`.

Correct example:

```bash
python3 codex_queue.py add \
  --title "Test" \
  --workdir /Users/antonio/projects/test \
  --prompt "Analyze the project"
```

### 3. The task stays in WAITING_LIMIT

Check the date:

```bash
python3 codex_queue.py list
```

If `reset_at` is in the future, this is normal.

### 4. The task fails immediately

Check the log:

```bash
tail -f codex_queue.log
```

Or open SQLite and inspect `last_error`.

## Practical advice

For assignment grading, the best structure is to create one folder per student:

```text
grading/
  student_A/
    solution.py
    test.py
  student_B/
    solution.py
    test.py
  student_C/
    solution.py
    test.py
```

Then add one task per folder.

This way Codex works in isolation and does not mix files from different students.
