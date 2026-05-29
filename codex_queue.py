#!/usr/bin/env python3
"""
codex_queue.py

This script is a small "orchestrator" for Codex CLI.

What is it for?
---------------
It lets you create a queue of tasks to be executed by Codex, for example:

- grading student assignments;
- generating automated tests;
- producing reports;
- resuming interrupted work;
- suspending a task when a usage limit is reached;
- retrying automatically after the usage limit resets.

General idea
------------
Instead of launching Codex manually every time, this program stores tasks
in a local SQLite database.

Each task has a status:

- PENDING        -> the task is waiting to be executed;
- RUNNING        -> the task is currently running;
- WAITING_LIMIT  -> Codex reached a usage limit and the task is waiting for reset;
- COMPLETED      -> the task completed successfully;
- FAILED         -> the task failed permanently.

You can leave this script running overnight.
When Codex becomes available again, the script automatically resumes ready tasks.
"""

# argparse is used to create terminal commands, for example:
# python codex_queue.py init
# python codex_queue.py add ...
# python codex_queue.py run
import argparse

# datetime is used to manage timestamps, dates, and limit reset times.
import datetime as dt

# os is not strictly required here, but it can be useful if you later expand
# the script with environment variables.
import os

# re is used to search inside Codex output for words such as:
# "usage limit", "429", "resets_at", etc.
import re

# sqlite3 is used to store data in a local database without installing
# PostgreSQL or MySQL. The database file will be codex_tasks.db.
import sqlite3

# subprocess is used to launch Codex CLI from Python.
# In practice, Python runs commands like:
# codex exec "prompt..."
# codex exec resume "session_id" "continue..."
import subprocess

# time is used to pause between one check and the next.
import time

# pathlib makes filesystem path handling cleaner.
from pathlib import Path


# ---------------------------------------------------------------------------
# BASIC CONFIGURATION
# ---------------------------------------------------------------------------

# Name of the SQLite file where tasks are stored.
DB_PATH = "codex_tasks.db"

# Name of the Codex CLI command.
# If the command is not called "codex" on your system, edit this line.
CODEX_BIN = "codex"

# How many seconds the worker waits before checking for executable tasks again.
# 60 means: check once per minute.
DEFAULT_CHECK_INTERVAL = 60


# Allowed task statuses.
# Keeping them in a constant makes the code clearer.
STATUSES = {
    "PENDING",
    "RUNNING",
    "WAITING_LIMIT",
    "COMPLETED",
    "FAILED",
}


# ---------------------------------------------------------------------------
# DATE AND TIME HELPERS
# ---------------------------------------------------------------------------

def utc_now():
    """
    Return the current time in UTC.

    Why UTC?
    --------
    To avoid issues with time zones, daylight saving time, remote servers, etc.
    Internally, it is safer to store timestamps in UTC.
    """
    return dt.datetime.now(dt.timezone.utc)


def iso_now():
    """
    Return the current time in ISO format.

    Example:
    2026-05-29T22:13:00.123456+00:00

    This format is convenient to store in the database and compare.
    """
    return utc_now().isoformat()


def parse_datetime(value):
    """
    Convert a datetime string into a datetime object.

    It also accepts dates ending with Z, for example:
    2026-05-30T03:00:00Z

    The Z means UTC.
    Python prefers +00:00, so we replace it before parsing.
    """
    if not value:
        return None

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def connect():
    """
    Open a connection to the SQLite database.

    SQLite does not require a server.
    The database is just a local file.
    """
    return sqlite3.connect(DB_PATH)


def init_db():
    """
    Create the tasks table if it does not already exist.

    This function should be called at least once before using the script:

        python codex_queue.py init

    The table stores:
    - task title;
    - prompt to send to Codex;
    - working directory;
    - priority;
    - status;
    - session_id for possible resume;
    - reset_at to know when to retry;
    - output;
    - errors;
    - creation and update timestamps.
    """
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                -- Human-readable task name.
                title TEXT NOT NULL,

                -- Full prompt to send to Codex.
                prompt TEXT NOT NULL,

                -- Directory where Codex should work.
                -- Example: /home/antonio/projects/student_A
                workdir TEXT NOT NULL,

                -- Lower number means earlier execution.
                -- priority=1 runs before priority=100.
                priority INTEGER NOT NULL DEFAULT 100,

                -- Task status.
                status TEXT NOT NULL DEFAULT 'PENDING',

                -- Codex session ID, if available.
                -- Used to resume an interrupted task.
                session_id TEXT,

                -- Instruction to give Codex when resuming.
                next_step TEXT,

                -- Date/time after which the task can run again.
                -- Used when a usage limit is reached.
                reset_at TEXT,

                -- Number of attempts already made.
                attempts INTEGER NOT NULL DEFAULT 0,

                -- Maximum number of attempts before marking the task as FAILED.
                max_attempts INTEGER NOT NULL DEFAULT 3,

                -- Last short error message.
                last_error TEXT,

                -- Full output produced by Codex.
                output TEXT,

                -- Task creation timestamp.
                created_at TEXT NOT NULL,

                -- Last update timestamp.
                updated_at TEXT NOT NULL
            )
            """
        )


def add_task(title, prompt, workdir=".", priority=100, max_attempts=3):
    """
    Add a new task to the database.

    Parameters:
    -----------
    title:
        Human-readable task title.

    prompt:
        Full text to pass to Codex.

    workdir:
        Directory where Codex should perform the work.

    priority:
        Task priority.
        Lower number = higher priority.

    max_attempts:
        Maximum number of attempts for generic errors.
        Usage limits are not treated as permanent failures:
        the task moves to WAITING_LIMIT instead.
    """
    # Convert the path to an absolute path.
    # This avoids ambiguity if the script is started from another directory.
    workdir = str(Path(workdir).resolve())

    with connect() as con:
        con.execute(
            """
            INSERT INTO tasks (
                title, prompt, workdir, priority, status,
                attempts, max_attempts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
            """,
            (
                title,
                prompt,
                workdir,
                priority,
                max_attempts,
                iso_now(),
                iso_now(),
            ),
        )


def list_tasks():
    """
    Print the task list to the terminal.

    Tasks are ordered so active or important tasks appear first.
    """
    with connect() as con:
        rows = con.execute(
            """
            SELECT id, title, status, priority, attempts, reset_at, workdir
            FROM tasks
            ORDER BY
                CASE status
                    WHEN 'RUNNING' THEN 1
                    WHEN 'WAITING_LIMIT' THEN 2
                    WHEN 'PENDING' THEN 3
                    WHEN 'FAILED' THEN 4
                    WHEN 'COMPLETED' THEN 5
                    ELSE 6
                END,
                priority ASC,
                id ASC
            """
        ).fetchall()

    if not rows:
        print("No tasks found.")
        return

    for row in rows:
        print(
            f"[{row[0]}] {row[1]} | status={row[2]} | priority={row[3]} "
            f"| attempts={row[4]} | reset_at={row[5]} | workdir={row[6]}"
        )


def get_next_task():
    """
    Find the next executable task.

    A task is executable if:

    1. it is PENDING;

    or

    2. it is WAITING_LIMIT but the reset_at time has already passed.

    Example:
    - Codex reached the limit at 23:00;
    - reset_at is 04:00;
    - until 03:59, the task will not run;
    - from 04:00 onward, the task becomes executable again.
    """
    now = iso_now()

    with connect() as con:
        # row_factory lets us access fields with task["id"],
        # task["title"], etc., instead of numeric indexes.
        con.row_factory = sqlite3.Row

        row = con.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'PENDING'
               OR (
                    status = 'WAITING_LIMIT'
                    AND reset_at IS NOT NULL
                    AND reset_at <= ?
               )
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """,
            (now,),
        ).fetchone()

    return row


def update_task(task_id, **fields):
    """
    Update one or more fields of a task.

    Example:
        update_task(3, status="COMPLETED", output="...")

    This helper avoids writing different SQL queries every time.
    """
    if not fields:
        return

    # Every update also updates updated_at automatically.
    fields["updated_at"] = iso_now()

    # Dynamically build SQL such as:
    # status = ?, output = ?, updated_at = ?
    columns = ", ".join(f"{key} = ?" for key in fields.keys())

    # Values to insert into the placeholders.
    values = list(fields.values())

    # The task id is needed at the end for the WHERE clause.
    values.append(task_id)

    with connect() as con:
        con.execute(
            f"UPDATE tasks SET {columns} WHERE id = ?",
            values,
        )


# ---------------------------------------------------------------------------
# CODEX OUTPUT ANALYSIS
# ---------------------------------------------------------------------------

def extract_session_id(text):
    """
    Try to extract a session_id from Codex output.

    Important note:
    ---------------
    The exact output format may change.
    This function uses several regular expressions to search for common patterns.

    If Codex prints the session ID in a different format on your system,
    update or add a pattern here.
    """
    patterns = [
        # Example:
        # session_id: abcdef...
        r"session[_ -]?id[:=]\s*([0-9a-fA-F-]{20,})",

        # Example:
        # Session: abcdef...
        r"Session[: ]+([0-9a-fA-F-]{20,})",

        # UUID example:
        # resume ... 123e4567-e89b-12d3-a456-426614174000
        r"resum(?:e|ing).*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def extract_reset_at(text):
    """
    Try to extract the usage limit reset time from Codex output.

    It searches for strings such as:
    - resets_at: "2026-05-30T03:00:00Z"
    - reset_at: "2026-05-30T03:00:00Z"
    - try again after 2026-05-30T03:00:00Z

    If nothing is found, it returns None.
    In that case, the script will use a fallback such as +5 hours.
    """
    patterns = [
        r"resets_at[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"reset_at[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']",
        r"try again after\s+([0-9T:\-+.Z]+)",
        r"reset[s]?\s+(?:at|on)\s+([0-9T:\-+.Z]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            value = match.group(1)
            parsed = parse_datetime(value)

            if parsed:
                return parsed.isoformat()

    return None


def looks_like_usage_limit(text):
    """
    Detect whether an error looks like a usage limit issue.

    It is not perfect, because it depends on the message returned by Codex.
    However, it catches common cases:
    - usage limit;
    - rate limit;
    - quota;
    - too many requests;
    - 429 error;
    - limit reached.
    """
    markers = [
        "usage limit",
        "rate limit",
        "quota",
        "too many requests",
        "429",
        "limit reached",
    ]

    lower = text.lower()
    return any(marker in lower for marker in markers)


# ---------------------------------------------------------------------------
# CODEX EXECUTION
# ---------------------------------------------------------------------------

def build_codex_command(task):
    """
    Build the Codex command to execute.

    Case 1: new task, no session_id
    --------------------------------
    Use:

        codex exec "prompt"

    Case 2: task already started, with session_id
    ---------------------------------------------
    Use:

        codex exec resume SESSION_ID "continue..."

    This makes it possible to try resuming from the previous state.
    """
    prompt = task["prompt"]
    session_id = task["session_id"]
    next_step = task["next_step"]

    if session_id:
        followup = (
            next_step
            or "Continue from where you stopped. Keep the plan and complete the task."
        )

        return [CODEX_BIN, "exec", "resume", session_id, followup]

    return [CODEX_BIN, "exec", prompt]


def run_codex(task):
    """
    Execute a task with Codex.

    Flow:
    -----
    1. Load the task from the database.
    2. Build the Codex command.
    3. Mark the task as RUNNING.
    4. Launch Codex.
    5. Read stdout and stderr.
    6. If Codex exits successfully -> COMPLETED.
    7. If Codex reaches a usage limit -> WAITING_LIMIT.
    8. If it fails for another reason -> retry or FAILED.
    """
    task_id = task["id"]

    # Build the command to launch.
    cmd = build_codex_command(task)

    # Before starting Codex, mark the task as RUNNING.
    # Also increment the attempts counter.
    update_task(
        task_id,
        status="RUNNING",
        attempts=task["attempts"] + 1,
        last_error=None,
    )

    print(f"\nStarting task #{task_id}: {task['title']}")
    print("Working directory:", task["workdir"])
    print("Command:", " ".join(cmd))

    try:
        # subprocess.run launches the command.
        #
        # cwd=task["workdir"]:
        #     Codex works inside the project directory.
        #
        # text=True:
        #     stdout/stderr are returned as strings.
        #
        # capture_output=True:
        #     We capture output and errors instead of only printing them.
        #
        # timeout=None:
        #     No time limit is set.
        #     If you want to avoid endless tasks, set something like timeout=3600.
        result = subprocess.run(
            cmd,
            cwd=task["workdir"],
            text=True,
            capture_output=True,
            timeout=None,
        )

        # Merge stdout and stderr into a single text.
        # This lets us search for usage limit and reset_at in both.
        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")

        # Try to recover a session_id from the output.
        found_session_id = extract_session_id(combined_output) or task["session_id"]

        # Try to recover the reset time.
        reset_at = extract_reset_at(combined_output)

        # CASE A: Codex completed successfully.
        if result.returncode == 0:
            update_task(
                task_id,
                status="COMPLETED",
                output=combined_output,
                session_id=found_session_id,
                reset_at=None,
            )
            print(f"Task #{task_id} completed.")
            return

        # CASE B: Codex appears to have reached a usage limit.
        if looks_like_usage_limit(combined_output):
            # If Codex does not provide a readable reset_at,
            # use an estimate: retry in 5 hours.
            #
            # You can change this value depending on your actual plan/limit.
            if not reset_at:
                reset_at = (utc_now() + dt.timedelta(hours=5)).isoformat()

            update_task(
                task_id,
                status="WAITING_LIMIT",
                output=combined_output,
                session_id=found_session_id,
                reset_at=reset_at,
                next_step=(
                    "Resume the work from the exact point where you stopped "
                    "and complete the task."
                ),
                last_error="Usage limit reached",
            )

            print(f"Task #{task_id} suspended because a usage limit was reached.")
            print(f"It will resume after: {reset_at}")
            return

        # CASE C: generic error.
        # If attempts are still available, put the task back into PENDING.
        if task["attempts"] + 1 < task["max_attempts"]:
            update_task(
                task_id,
                status="PENDING",
                output=combined_output,
                session_id=found_session_id,
                last_error=combined_output[-3000:],
            )

            print(f"Task #{task_id} failed, but it will be retried.")
            return

        # CASE D: generic error and no attempts left.
        update_task(
            task_id,
            status="FAILED",
            output=combined_output,
            session_id=found_session_id,
            last_error=combined_output[-3000:],
        )

        print(f"Task #{task_id} failed permanently.")

    except Exception as exc:
        # This catches Python-level errors, not Codex-level errors.
        # Examples:
        # - codex is not installed;
        # - workdir does not exist;
        # - insufficient permissions.
        update_task(
            task_id,
            status="FAILED",
            last_error=str(exc),
        )
        print(f"Task #{task_id} error: {exc}")


# ---------------------------------------------------------------------------
# WORKER
# ---------------------------------------------------------------------------

def worker_loop(check_interval=DEFAULT_CHECK_INTERVAL, stop_when_empty=False):
    """
    Main worker loop.

    This is the core of the script.

    It works like this:
    -------------------
    - find the next executable task;
    - if found, execute it;
    - if not found, wait check_interval seconds;
    - repeat.

    If stop_when_empty=True:
    ------------------------
    the worker stops when no executable task is found.

    If stop_when_empty=False:
    -------------------------
    the worker stays active forever.
    This mode is useful overnight or on a server.
    """
    init_db()

    while True:
        task = get_next_task()

        if not task:
            if stop_when_empty:
                print("No executable tasks found. Exiting.")
                return

            print(f"No task ready. Checking again in {check_interval} seconds.")
            time.sleep(check_interval)
            continue

        run_codex(task)


# ---------------------------------------------------------------------------
# EXAMPLE TASKS
# ---------------------------------------------------------------------------

def seed_example_tasks(workdir="."):
    """
    Insert a few example tasks.

    They are useful for understanding how the system works.

    You can modify them for your real project:
    - student grading;
    - test generation;
    - final report;
    - pull request creation;
    - code refactoring.
    """
    add_task(
        title="Grade student A assignment",
        prompt="""
You are an impartial grader.

Read the project in the current directory.
Run the available tests.
Evaluate the assignment using this rubric:

- functional correctness: 0-4
- code quality: 0-2
- error handling: 0-2
- clarity and style: 0-2

Produce:
1. grade out of 10;
2. reasoning for each criterion;
3. short final evaluation;
4. possible improvement suggestions.

Save the result in report_student_A.md.
""",
        workdir=workdir,
        priority=1,
    )

    add_task(
        title="Generate missing automated tests",
        prompt="""
Analyze the project in the current directory.
Identify cases not covered by tests.
Add clear and repeatable automated tests.
Do not change the application logic.
At the end, run the tests and write a summary in test_report.md.
""",
        workdir=workdir,
        priority=2,
    )

    add_task(
        title="Create final report",
        prompt="""
Read all markdown reports in the current directory.
Create a report_final.md file with:
- student list;
- grade;
- strengths;
- issues;
- teaching suggestions.
""",
        workdir=workdir,
        priority=3,
    )


# ---------------------------------------------------------------------------
# COMMAND-LINE INTERFACE
# ---------------------------------------------------------------------------

def main():
    """
    Define the commands available from the terminal.

    Available commands:
    -------------------

    1. init
       Create the database.

    2. add
       Add a task manually.

    3. seed
       Add example tasks.

    4. list
       Show tasks.

    5. run
       Start the worker.
    """
    parser = argparse.ArgumentParser(
        description="Simple Codex CLI task orchestrator with a SQLite queue."
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # Command:
    # python codex_queue.py init
    sub.add_parser("init", help="Initialize the SQLite database.")

    # Command:
    # python codex_queue.py add --title ... --prompt ... --workdir ... --priority ...
    add = sub.add_parser("add", help="Add a task to the queue.")
    add.add_argument("--title", required=True, help="Task title.")
    add.add_argument("--prompt", required=True, help="Prompt to pass to Codex.")
    add.add_argument("--workdir", default=".", help="Working directory.")
    add.add_argument("--priority", type=int, default=100, help="Priority: lower number = earlier.")
    add.add_argument("--max-attempts", type=int, default=3, help="Maximum attempts.")

    # Command:
    # python codex_queue.py seed --workdir /path/to/project
    seed = sub.add_parser("seed", help="Add example tasks.")
    seed.add_argument("--workdir", default=".", help="Working directory for example tasks.")

    # Command:
    # python codex_queue.py list
    sub.add_parser("list", help="Show tasks stored in the database.")

    # Command:
    # python codex_queue.py run
    run = sub.add_parser("run", help="Start the worker.")
    run.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_CHECK_INTERVAL,
        help="Seconds between checks.",
    )
    run.add_argument(
        "--stop-when-empty",
        action="store_true",
        help="Stop the worker if no task is ready.",
    )

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("Database initialized.")

    elif args.command == "add":
        init_db()
        add_task(
            title=args.title,
            prompt=args.prompt,
            workdir=args.workdir,
            priority=args.priority,
            max_attempts=args.max_attempts,
        )
        print("Task added.")

    elif args.command == "seed":
        init_db()
        seed_example_tasks(args.workdir)
        print("Example tasks added.")

    elif args.command == "list":
        init_db()
        list_tasks()

    elif args.command == "run":
        worker_loop(
            check_interval=args.interval,
            stop_when_empty=args.stop_when_empty,
        )


# Script entry point.
# When you run:
#
#     python codex_queue.py init
#
# Python enters here and calls main().
if __name__ == "__main__":
    main()
