# CodeMorph.AI

Piattaforma agentica di migrazione software legacy (FastAPI + CrewAI + Supabase).

## Fasi della pipeline

| Fase | Endpoint | Descrizione |
|------|----------|-------------|
| 1 · Understanding | `POST /api/v1/modernize/understand` | Assessment, mappa dipendenze, documentazione, test book |
| 2 · Design | `POST /api/v1/modernize/design` | Migration Plan, ADR, schema DB target |
| 3 · Implementation | `POST /api/v1/modernize/implement` | Generazione codice backend/frontend + Quality Check |

Tra una fase e l'altra ci sono i Checkpoint umani (HITL): download dei report
(`GET /api/v1/modernize/download/{session_id}/{fase}`) e ri-upload dei file
corretti (`POST /api/v1/modernize/upload/{session_id}`).

## Pagamenti: pass giornaliero (299 €) con PayPal e Google Pay

L'accesso alla piattaforma richiede un **pass giornaliero da 299,00 €**
(24 ore), **di cui 20,00 € accreditati come credito token** spendibile.

- `GET  /api/v1/payments/config` — dati per i bottoni di pagamento (client id PayPal, prezzi, valuta).
- `POST /api/v1/payments/ordini` — crea l'ordine: `{"tipo": "pass_giornaliero" | "ricarica_token", "metodo": "paypal" | "googlepay", "importo_eur": <solo per ricariche>}`.
- `POST /api/v1/payments/cattura` — dopo l'approvazione del cliente: cattura il pagamento, verifica l'importo e attiva licenza + credito token. Idempotente.

Entrambi i metodi si regolano tramite le **PayPal Orders API v2**: il bottone
Google Pay del JS SDK PayPal (`components=buttons,googlepay`) approva lo stesso
ordine creato dal backend, quindi il flusso server è identico e cambia solo il
campo `metodo` registrato. Alla cattura l'importo pagato viene verificato
contro l'ordine registrato lato server (mai contro valori inviati dal client).

Se l'utente ha già un pass attivo, le 24 ore del nuovo pass si accodano alla
scadenza corrente.

## Credito token a consumo

- Ogni fase misura i token **realmente consumati** dalle Crew (metriche CrewAI,
  micro-agenti del grafo dipendenze inclusi) e li converte in EUR con il
  listino `PREZZI_TOKEN_EUR_PER_1M` di `src/config.py` (prezzi di vendita,
  modificabili liberamente).
- L'addebito avviene a fine fase sul portafoglio dell'utente; il dettaglio
  (token prompt/completion, costo, saldo residuo) è nel campo `token` della
  risposta e nel log live.
- A credito esaurito le fasi rispondono **402** finché l'utente non ricarica
  (`ricarica_token`, minimo 5 €) o compra un nuovo pass.
- `GET /api/v1/tokens/saldo` — saldo e ultimi movimenti.

Gli endpoint di pagamento richiedono solo il login (JWT Supabase), **non** la
licenza: chi deve ancora comprare il pass non ne avrebbe una.

## Log live sincronizzati con l'attività reale

`GET /api/v1/modernize/logs/{session_id}` restituisce i log scritti **dagli
agenti mentre lavorano** (callback CrewAI su ogni task: agente al lavoro, task
completato, file migrato N/M, Quality Check, consumo token), ogni riga con
timestamp. Niente più avanzamento simulato a tempo lato frontend.

Polling incrementale: passare `?da_riga=<righe_totali dell'ultima risposta>`
per ricevere solo le righe nuove (`righe_totali` è nel payload).

## Setup

1. **Dipendenze**: `pip install -r requirements.txt`
2. **Database**: eseguire `db/schema_pagamenti_token.sql` nell'SQL Editor di
   Supabase (idempotente). Finché non viene eseguito, il controllo del credito
   token è disattivato (fail-open con warning nei log) per non bloccare le
   installazioni esistenti.
3. **Variabili d'ambiente** (`.env`):

   | Variabile | Descrizione |
   |-----------|-------------|
   | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET` | Connessione e verifica JWT Supabase |
   | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Credenziali REST PayPal (le stesse per Google Pay) |
   | `PAYPAL_ENV` | `sandbox` (default) o `live` |
   | `WORKSPACE_DIR` | Cartella di lavoro per sessioni e log live (default `/tmp/workspace_sessioni`) |
   | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | Chiave del provider LLM scelto |
   | `CORS_ORIGINS` | Lista domini frontend separati da virgola |

4. **Avvio**: `uvicorn main:app --reload`
