# Codex Queue

`codex_queue.py` è un piccolo orchestratore Python per creare una coda di task da eseguire con **Codex CLI**.

Serve soprattutto quando vuoi lasciare una lista di lavori pronti, ad esempio di notte, e vuoi che il sistema provi a riprendere i task quando il limite d'uso torna disponibile.

---

## Cosa fa

- Salva i task in SQLite.
- Esegue i task con `codex exec`.
- Gestisce priorità.
- Riconosce errori tipo usage limit/rate limit.
- Mette il task in pausa se il limite è raggiunto.
- Riprova dopo `reset_at`.
- Prova a usare `codex exec resume` se trova un `session_id`.
- Salva output ed errori nel database.

---

## Stati dei task

| Stato | Significato |
|---|---|
| `PENDING` | Task pronto da eseguire |
| `RUNNING` | Task in esecuzione |
| `WAITING_LIMIT` | Task sospeso perché Codex ha raggiunto il limite |
| `COMPLETED` | Task completato |
| `FAILED` | Task fallito definitivamente |

---

## Requisiti

Servono:

- Python 3.10 o superiore;
- Codex CLI installato;
- autenticazione Codex già configurata;
- una cartella di progetto su cui Codex può lavorare.

Controlla Python:

```bash
python3 --version
```

Controlla Codex:

```bash
codex --help
```

Se il comando Codex non si chiama `codex`, modifica nel file Python:

```python
CODEX_BIN = "codex"
```

Esempio:

```python
CODEX_BIN = "/usr/local/bin/codex"
```

---

## File inclusi

```text
codex_queue.py
README.md
```

Quando avvii lo script, verrà creato anche:

```text
codex_tasks.db
```

---

## Inizializzazione

Da terminale:

```bash
python3 codex_queue.py init
```

Risultato atteso:

```text
Database inizializzato.
```

---

## Aggiungere task di esempio

```bash
python3 codex_queue.py seed --workdir /percorso/del/progetto
```

Esempio su macOS:

```bash
python3 codex_queue.py seed --workdir /Users/antonio/progetti/studente_A
```

Questo crea tre task:

1. correzione esercizio studente A;
2. generazione test automatici mancanti;
3. creazione report finale.

---

## Vedere la lista dei task

```bash
python3 codex_queue.py list
```

Esempio:

```text
[1] Correggi esercizio studente A | status=PENDING | priority=1 | attempts=0 | reset_at=None | workdir=/Users/antonio/progetti/studente_A
```

---

## Avviare il worker

```bash
python3 codex_queue.py run
```

Il worker resta acceso e controlla periodicamente se ci sono task pronti.

---

## Avvio notturno

Per lasciarlo lavorare di notte:

```bash
nohup python3 codex_queue.py run > codex_queue.log 2>&1 &
```

Per vedere cosa sta facendo:

```bash
tail -f codex_queue.log
```

---

## Aggiungere un task manuale

```bash
python3 codex_queue.py add \
  --title "Correggi studente B" \
  --workdir /Users/antonio/progetti/studente_B \
  --priority 1 \
  --prompt "Esegui i test, correggi l'elaborato e genera report_studente_B.md"
```

---

## Esempio per correzione studenti

```bash
python3 codex_queue.py add \
  --title "Correzione Mario Rossi" \
  --workdir /Users/antonio/correzioni/mario_rossi \
  --priority 1 \
  --prompt "Sei un correttore imparziale. Leggi il codice nella cartella corrente. Esegui i test. Valuta secondo questa griglia: correttezza 0-4, qualità codice 0-2, gestione errori 0-2, chiarezza 0-2. Crea report_mario_rossi.md con voto, motivazioni per ogni indicatore e giudizio finale."
```

---

## Priorità

Numero più basso = eseguito prima.

Esempio:

```text
priority=1    molto importante
priority=10   importante
priority=100  normale
priority=999  bassa priorità
```

---

## Worker che si ferma quando non ci sono task

```bash
python3 codex_queue.py run --stop-when-empty
```

Utile per fare prove.

---

## Cambiare intervallo di controllo

Default: 60 secondi.

Ogni 5 minuti:

```bash
python3 codex_queue.py run --interval 300
```

---

## Come gestisce il limite d'uso

Lo script cerca nell'output parole come:

```text
usage limit
rate limit
quota
too many requests
429
limit reached
```

Se le trova, mette il task in:

```text
WAITING_LIMIT
```

Poi cerca un orario di reset, ad esempio:

```text
resets_at: "2026-05-30T03:00:00Z"
```

Se non trova un orario leggibile, usa un fallback:

```python
reset_at = utc_now() + dt.timedelta(hours=5)
```

Puoi cambiare le 5 ore dentro `run_codex()`.

---

## Ripresa con session_id

Se lo script trova un `session_id`, prova a riprendere con:

```bash
codex exec resume SESSION_ID "continua..."
```

Se non trova un `session_id`, il task può essere rieseguito dal prompt originale.

La funzione da modificare se il formato cambia è:

```python
extract_session_id()
```

---

## Controllare SQLite manualmente

Apri il database:

```bash
sqlite3 codex_tasks.db
```

Query utile:

```sql
SELECT id, title, status, priority, reset_at FROM tasks;
```

Per uscire:

```sql
.quit
```

---

## Struttura consigliata per correzioni

```text
correzioni/
  studente_A/
    soluzione.py
    test.py
  studente_B/
    soluzione.py
    test.py
  studente_C/
    soluzione.py
    test.py
```

Poi aggiungi un task per ogni studente.

---

## Workflow consigliato

```bash
python3 codex_queue.py init

python3 codex_queue.py add \
  --title "Correggi studente A" \
  --workdir /Users/antonio/correzioni/studente_A \
  --priority 1 \
  --prompt "Esegui test, valuta secondo griglia e crea report_A.md"

python3 codex_queue.py add \
  --title "Correggi studente B" \
  --workdir /Users/antonio/correzioni/studente_B \
  --priority 1 \
  --prompt "Esegui test, valuta secondo griglia e crea report_B.md"

python3 codex_queue.py add \
  --title "Report finale classe" \
  --workdir /Users/antonio/correzioni \
  --priority 3 \
  --prompt "Leggi tutti i report degli studenti e crea report_finale_classe.md"

nohup python3 codex_queue.py run > codex_queue.log 2>&1 &
```

---

## Limiti di questa versione

È una versione semplice e locale.

Non include ancora:

- dashboard web;
- PostgreSQL;
- più worker paralleli;
- esportazione automatica degli output in file separati;
- notifiche email/Telegram;
- integrazione GitHub Actions;
- caricamento griglie da YAML/JSON.

---

## Evoluzione consigliata

Per un sistema più professionale aggiungerei:

1. una cartella `tasks/` con prompt salvati in file;
2. una cartella `reports/` con output Markdown;
3. una tabella `students`;
4. una tabella `rubrics`;
5. integrazione GitHub Actions;
6. generazione automatica PDF dei report;
7. notifica quando la correzione della classe è finita.
