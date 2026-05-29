#!/usr/bin/env python3
"""
codex_queue.py

Piccolo orchestratore per Codex CLI.

Obiettivo:
- salvare una lista di task in SQLite;
- eseguirli con `codex exec`;
- sospenderli se viene raggiunto un usage limit;
- riprenderli dopo l'orario di reset;
- provare a usare `codex exec resume` se è disponibile un session_id.

Nota importante:
Il formato preciso degli errori e dei session_id può cambiare in base alla versione
CLI/servizio. Per questo le funzioni `extract_session_id()` e `extract_reset_at()`
usano pattern generici e potrebbero richiedere piccoli adattamenti.
"""

import argparse
import datetime as dt
import re
import sqlite3
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIGURAZIONE BASE
# ---------------------------------------------------------------------------

# File SQLite locale in cui salviamo tutti i task.
DB_PATH = "codex_tasks.db"

# Comando Codex CLI. Se sul tuo sistema il binario ha un altro nome/percorso,
# modifica questa costante, ad esempio: CODEX_BIN = "/usr/local/bin/codex".
CODEX_BIN = "codex"

# Ogni quanti secondi il worker controlla se ci sono task pronti.
DEFAULT_CHECK_INTERVAL = 60

# Stati logici usati nel database.
STATUS_PENDING = "PENDING"          # Task pronto ma non ancora eseguito.
STATUS_RUNNING = "RUNNING"          # Task in esecuzione.
STATUS_WAITING_LIMIT = "WAITING_LIMIT"  # Task sospeso per limite d'uso.
STATUS_COMPLETED = "COMPLETED"      # Task completato con successo.
STATUS_FAILED = "FAILED"            # Task fallito definitivamente.


# ---------------------------------------------------------------------------
# DATE E ORARI
# ---------------------------------------------------------------------------

def utc_now() -> dt.datetime:
    """Restituisce l'ora attuale in UTC, con timezone esplicita."""
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    """Restituisce l'ora attuale in formato ISO, pronta da salvare in SQLite."""
    return utc_now().isoformat()


def parse_datetime(value: str | None) -> dt.datetime | None:
    """
    Converte una stringa data/ora in datetime.

    Accetta anche il formato con Z finale, ad esempio:
        2026-05-30T03:00:00Z

    Se la stringa non è valida, restituisce None.
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

def connect() -> sqlite3.Connection:
    """Apre una connessione SQLite al database locale."""
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """
    Crea la tabella dei task, se non esiste.

    Da eseguire almeno una volta:
        python3 codex_queue.py init
    """
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                workdir TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL DEFAULT 'PENDING',
                session_id TEXT,
                next_step TEXT,
                reset_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error TEXT,
                output TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def add_task(
    title: str,
    prompt: str,
    workdir: str = ".",
    priority: int = 100,
    max_attempts: int = 3,
) -> None:
    """
    Aggiunge un task alla coda.

    title:
        Nome leggibile del task.
    prompt:
        Prompt completo da passare a Codex.
    workdir:
        Cartella in cui Codex deve lavorare.
    priority:
        Numero più basso = task più importante.
    max_attempts:
        Tentativi massimi in caso di errore generico.
    """
    workdir = str(Path(workdir).resolve())

    with connect() as con:
        con.execute(
            """
            INSERT INTO tasks (
                title, prompt, workdir, priority, status,
                attempts, max_attempts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                title,
                prompt,
                workdir,
                priority,
                STATUS_PENDING,
                max_attempts,
                iso_now(),
                iso_now(),
            ),
        )


def list_tasks() -> None:
    """Stampa una lista sintetica dei task presenti nel database."""
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
        print("Nessun task presente.")
        return

    for row in rows:
        print(
            f"[{row[0]}] {row[1]} | status={row[2]} | priority={row[3]} "
            f"| attempts={row[4]} | reset_at={row[5]} | workdir={row[6]}"
        )


def get_next_task() -> sqlite3.Row | None:
    """
    Restituisce il prossimo task eseguibile.

    Un task è eseguibile quando:
    - è PENDING;
    - oppure è WAITING_LIMIT e reset_at è passato.
    """
    now = iso_now()

    with connect() as con:
        con.row_factory = sqlite3.Row
        return con.execute(
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


def update_task(task_id: int, **fields) -> None:
    """
    Aggiorna uno o più campi di un task.

    Esempio:
        update_task(1, status="COMPLETED", output="...")
    """
    if not fields:
        return

    fields["updated_at"] = iso_now()
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [task_id]

    with connect() as con:
        con.execute(f"UPDATE tasks SET {columns} WHERE id = ?", values)


# ---------------------------------------------------------------------------
# LETTURA OUTPUT CODEX
# ---------------------------------------------------------------------------

def extract_session_id(text: str) -> str | None:
    """
    Prova a estrarre un session_id dall'output di Codex.

    Se la tua versione di Codex stampa la sessione in un modo diverso,
    aggiungi qui un nuovo pattern regex.
    """
    patterns = [
        r"session[_ -]?id[:=]\s*([0-9a-fA-F-]{20,})",
        r"Session[: ]+([0-9a-fA-F-]{20,})",
        r"resum(?:e|ing).*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def extract_reset_at(text: str) -> str | None:
    """
    Prova a estrarre l'orario di reset del limite dall'output di Codex.

    Cerca forme come:
    - resets_at: "2026-05-30T03:00:00Z"
    - reset_at: "2026-05-30T03:00:00Z"
    - try again after 2026-05-30T03:00:00Z
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
            parsed = parse_datetime(match.group(1))
            if parsed:
                return parsed.isoformat()

    return None


def looks_like_usage_limit(text: str) -> bool:
    """Riconosce in modo euristico un errore di limite d'uso/rate limit."""
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
# ESECUZIONE CODEX
# ---------------------------------------------------------------------------

def build_codex_command(task: sqlite3.Row) -> list[str]:
    """
    Costruisce il comando Codex.

    Task nuovo:
        codex exec PROMPT

    Task con session_id:
        codex exec resume SESSION_ID FOLLOWUP
    """
    if task["session_id"]:
        followup = task["next_step"] or (
            "Continua dal punto in cui ti eri interrotto. "
            "Mantieni il piano e completa il task."
        )
        return [CODEX_BIN, "exec", "resume", task["session_id"], followup]

    return [CODEX_BIN, "exec", task["prompt"]]


def run_codex(task: sqlite3.Row) -> None:
    """
    Esegue un singolo task con Codex e aggiorna lo stato nel database.
    """
    task_id = task["id"]
    cmd = build_codex_command(task)

    update_task(
        task_id,
        status=STATUS_RUNNING,
        attempts=task["attempts"] + 1,
        last_error=None,
    )

    print(f"\nAvvio task #{task_id}: {task['title']}")
    print("Cartella di lavoro:", task["workdir"])
    print("Comando:", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=task["workdir"],
            text=True,
            capture_output=True,
            timeout=None,
        )

        combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
        found_session_id = extract_session_id(combined_output) or task["session_id"]
        reset_at = extract_reset_at(combined_output)

        if result.returncode == 0:
            update_task(
                task_id,
                status=STATUS_COMPLETED,
                output=combined_output,
                session_id=found_session_id,
                reset_at=None,
            )
            print(f"Task #{task_id} completato.")
            return

        if looks_like_usage_limit(combined_output):
            # Fallback: se Codex non comunica un reset leggibile, riprova tra 5 ore.
            if not reset_at:
                reset_at = (utc_now() + dt.timedelta(hours=5)).isoformat()

            update_task(
                task_id,
                status=STATUS_WAITING_LIMIT,
                output=combined_output,
                session_id=found_session_id,
                reset_at=reset_at,
                next_step=(
                    "Riprendi il lavoro dal punto esatto in cui eri arrivato "
                    "e completa il task."
                ),
                last_error="Usage limit reached",
            )
            print(f"Task #{task_id} sospeso per limite. Riprenderà dopo: {reset_at}")
            return

        if task["attempts"] + 1 < task["max_attempts"]:
            update_task(
                task_id,
                status=STATUS_PENDING,
                output=combined_output,
                session_id=found_session_id,
                last_error=combined_output[-3000:],
            )
            print(f"Task #{task_id} fallito, ma verrà ritentato.")
            return

        update_task(
            task_id,
            status=STATUS_FAILED,
            output=combined_output,
            session_id=found_session_id,
            last_error=combined_output[-3000:],
        )
        print(f"Task #{task_id} fallito definitivamente.")

    except Exception as exc:
        update_task(task_id, status=STATUS_FAILED, last_error=str(exc))
        print(f"Errore task #{task_id}: {exc}")


# ---------------------------------------------------------------------------
# WORKER
# ---------------------------------------------------------------------------

def worker_loop(check_interval: int = DEFAULT_CHECK_INTERVAL, stop_when_empty: bool = False) -> None:
    """
    Ciclo principale.

    Cerca task pronti, li esegue e resta in ascolto.
    """
    init_db()

    while True:
        task = get_next_task()

        if not task:
            if stop_when_empty:
                print("Nessun task eseguibile. Uscita.")
                return

            print(f"Nessun task pronto. Ricontrollo tra {check_interval} secondi.")
            time.sleep(check_interval)
            continue

        run_codex(task)


# ---------------------------------------------------------------------------
# TASK DI ESEMPIO
# ---------------------------------------------------------------------------

def seed_example_tasks(workdir: str = ".") -> None:
    """Aggiunge task di esempio per provare subito il sistema."""
    add_task(
        title="Correggi esercizio studente A",
        prompt="""
Sei un correttore imparziale.

Leggi il progetto nella cartella corrente.
Esegui i test disponibili.
Valuta l'esercizio secondo questa griglia:

- correttezza funzionale: 0-4
- qualità codice: 0-2
- gestione errori: 0-2
- chiarezza e stile: 0-2

Produci:
1. voto su 10;
2. motivazione per ogni indicatore;
3. giudizio finale sintetico;
4. eventuali suggerimenti di miglioramento.

Salva il risultato in report_studente_A.md.
""",
        workdir=workdir,
        priority=1,
    )

    add_task(
        title="Genera test automatici mancanti",
        prompt="""
Analizza il progetto nella cartella corrente.
Individua i casi non coperti dai test.
Aggiungi test automatici chiari e ripetibili.
Non cambiare la logica dell'applicazione.
Alla fine esegui i test e scrivi un riepilogo in test_report.md.
""",
        workdir=workdir,
        priority=2,
    )

    add_task(
        title="Crea report finale",
        prompt="""
Leggi tutti i report markdown presenti nella cartella corrente.
Crea un report_finale.md con:
- elenco studenti;
- voto;
- punti di forza;
- criticità;
- suggerimenti didattici.
""",
        workdir=workdir,
        priority=3,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Definisce i comandi utilizzabili da terminale."""
    parser = argparse.ArgumentParser(
        description="Orchestratore semplice per task Codex CLI con coda SQLite."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Inizializza il database SQLite.")

    add = sub.add_parser("add", help="Aggiunge un task alla coda.")
    add.add_argument("--title", required=True, help="Titolo del task.")
    add.add_argument("--prompt", required=True, help="Prompt da passare a Codex.")
    add.add_argument("--workdir", default=".", help="Cartella di lavoro.")
    add.add_argument("--priority", type=int, default=100, help="Numero più basso = prima.")
    add.add_argument("--max-attempts", type=int, default=3, help="Tentativi massimi.")

    seed = sub.add_parser("seed", help="Aggiunge task di esempio.")
    seed.add_argument("--workdir", default=".", help="Cartella di lavoro dei task di esempio.")

    sub.add_parser("list", help="Mostra i task presenti nel database.")

    run = sub.add_parser("run", help="Avvia il worker.")
    run.add_argument("--interval", type=int, default=DEFAULT_CHECK_INTERVAL)
    run.add_argument("--stop-when-empty", action="store_true")

    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("Database inizializzato.")

    elif args.command == "add":
        init_db()
        add_task(
            title=args.title,
            prompt=args.prompt,
            workdir=args.workdir,
            priority=args.priority,
            max_attempts=args.max_attempts,
        )
        print("Task aggiunto.")

    elif args.command == "seed":
        init_db()
        seed_example_tasks(args.workdir)
        print("Task di esempio aggiunti.")

    elif args.command == "list":
        init_db()
        list_tasks()

    elif args.command == "run":
        worker_loop(check_interval=args.interval, stop_when_empty=args.stop_when_empty)


if __name__ == "__main__":
    main()
