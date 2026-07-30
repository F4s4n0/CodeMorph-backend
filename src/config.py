"""
Configurazione centralizzata della piattaforma di modernizzazione.

Tutti i nomi dei file di output sono definiti QUI e solo qui:
crew.py e tasks.py li importano, così un rename non può più
disallineare chi scrive il file da chi lo rilegge.
"""

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# Le variabili d'ambiente vanno caricate PRIMA di leggere WORKSPACE_DIR:
# questo modulo può essere importato prima che main.py chiami load_dotenv().
load_dotenv()

# --- Directory di lavoro condivisa (upload, output delle fasi, log live) ---
# Definita QUI e solo qui. Prima main.py leggeva l'env mentre graph_builder
# scriveva i log in una cartella relativa "workspace": chi scriveva i log
# live e chi li leggeva (endpoint /logs) guardavano DUE posti diversi, e il
# frontend non riceveva mai l'attività reale degli agenti.
WORKSPACE_DIR = Path(__file__).parent.parent / "workspace_sessioni"

# --- Nomi dei file di output (deliverable per fase) ---
FILE_ASSESSMENT       = "1_Assessment_Inventory.md"
FILE_DEPENDENCY_MAP   = "2_Map_Dependency.md"
FILE_TECH_DOC         = "3a_Technical_Documentation.md"
FILE_FUNCTIONAL_DOC   = "3b_Functional_Documentation.md"
FILE_DB_SCHEMA        = "3c_Database_Schema.sql"
FILE_TEST_BOOK        = "4_Test_Book_Generation.md"
FILE_MIGRATION_PLAN   = "5_Migration_Plan_ADR.md"
FILE_BACKEND_IMPL     = "6a_Backend_Project_Implementation.md"
FILE_FRONTEND_IMPL    = "6b_Frontend_Project_Implementation.md"
FILE_QUALITY_REPORT   = "7_Quality_Check_Report.md"
FILE_VALIDATION_FASE1 = "8_Validation_Report_Fase1.md"
FILE_VALIDATION_FASE2 = "9_Validation_Report_Fase2.md"
FILE_VALIDATION_FASE3 = "10_Validation_Report_Fase3.md"
# Tetto al contesto inviato al validatore. Deve reggere TUTTI i documenti
# di fase: con 40k il gate vedeva solo il primo e bocciava per assenza
# degli altri (che invece erano completi su disco).
VALIDAZIONE_MAX_CHARS = 250_000
# Quota massima per singolo documento: garantisce che ognuno sia
# rappresentato, invece di consumare tutto il budget sul primo.
VALIDAZIONE_MAX_CHARS_PER_DOC = 45_000
# Pausa tra l'analisi di un file e il successivo. Serve a non saturare i
# limiti per minuto del provider su progetti con molti file.
# 0 = nessuna pausa (progetti piccoli), 1-2s = prudente su progetti grandi.
DELAY_TRA_FILE_SEC = float(os.getenv("DELAY_TRA_FILE_SEC", "1"))

# File di checkpoint per la fase iterativa (permette il resume dopo un crash)
FILE_IMPL_CHECKPOINT  = "_implementation_checkpoint.json"


# Regole di intestazione comuni a TUTTI i documenti della Fase 1.
# Nascono da un rilievo del Quality Gate: ogni agente si numerava da sé
# ("Fase 2 — Dependency Discovery", due documenti entrambi "Fase 3") e i
# rinvii incrociati puntavano a documenti con nomi diversi da quelli reali.
CONVENZIONI_FASE1 = """
CONVENZIONI OBBLIGATORIE DI INTESTAZIONE:
1. Questo documento appartiene alla FASE 1 · UNDERSTANDING. Nell'header usa esattamente questa dicitura: non inventare numerazioni di fase diverse (mai 'Fase 2', 'Fase 3', 'Discovery' come numero di fase).
2. Identifica il documento con il proprio nome file, senza rinominarlo.
3. Per i rinvii ad altri deliverable della stessa fase usa ESATTAMENTE questi nomi:
   - 1_Assessment_Inventory.md (inventario degli asset)
   - 2_Map_Dependency.md (grafo delle dipendenze)
   - 3a_Technical_Documentation.md (documentazione tecnica)
   - 3b_Functional_Documentation.md (documentazione funzionale, strutturata come Product Backlog Agile con Epics e User Stories)
   - 4_Test_Book_Generation.md (piano di test)
4. Non citare sezioni o documenti che non esistono.
"""

# --- Regole di formattazione condivise (iniettate nei task, NON nei backstory) ---
# Nota: è una regola di formato output, non un tratto di personalità dell'agente.
# Metterla nei task che producono diagrammi evita di ripeterla in 4 backstory
# e di sprecare token ad ogni chiamata che non genera Mermaid.
MERMAID_RULES = (
    "\n\nREGOLE DIAGRAMMI MERMAID (OBBLIGATORIE se il documento include diagrammi):\n"
    "- Gli ID dei blocchi e dei subgraph non devono MAI contenere spazi, "
    "parentesi o caratteri speciali.\n"
    "- Usa i trattini bassi per gli ID (es. logica_business_hardcoded).\n"
    "- Racchiudi il testo descrittivo tra virgolette dentro parentesi quadre, "
    'es.: subgraph logica_business_hardcoded ["Logica di Business Cablata (Hardcoded)"].\n'
)

# --- Limiti operativi ---
# Dimensione massima (in caratteri) del codice generato passato al Quality Check
# in un singolo task, per non saturare la context window del modello.
QA_CHUNK_MAX_CHARS = 60_000

# =====================================================================
# Pagamenti: pass giornaliero e credito token
# =====================================================================

VALUTA_PAGAMENTI = "EUR"

# Quota giornaliera di accesso alla piattaforma.
PREZZO_PASS_GIORNALIERO_EUR = Decimal("299.00")

# Parte del pass accreditata come credito token spendibile: il consumo
# REALE di ogni fase viene addebitato su questo portafoglio.
QUOTA_TOKEN_PASS_EUR = Decimal("20.00")

DURATA_PASS_ORE = 24

# Limiti per la ricarica del credito token quando la quota si esaurisce.
RICARICA_MINIMA_EUR = Decimal("5.00")
RICARICA_MASSIMA_EUR = Decimal("1000.00")

# Listino di VENDITA dei token in EUR per 1 MILIONE di token (prompt e
# completion separati). Sono prezzi al cliente, margine incluso: vanno
# aggiornati liberamente qui. Il match è sul nome del modello senza il
# prefisso provider (es. "anthropic/claude-x" -> "claude-x"); i modelli
# non in lista usano la voce "default".
PREZZI_TOKEN_EUR_PER_1M = {
    "openai": {
        "gpt-5.4": {"prompt": Decimal("2.50"), "completion": Decimal("15.00")},
        "gpt-5.4-mini": {"prompt": Decimal("0.25"), "completion": Decimal("2.00")},
        "gpt-5.4-nano": {"prompt": Decimal("0.20"), "completion": Decimal("1.25")},
        "gpt-5.5": {"prompt": Decimal("5.00"), "completion": Decimal("30.00")},
    },
    "google": {
        "gemini-2.0-flash": {"prompt": Decimal("0.10"), "completion": Decimal("0.40")},
        "gemini-2.5-flash": {"prompt": Decimal("0.30"), "completion": Decimal("2.50")},
        "gemini-2.5-flash-lite": {"prompt": Decimal("0.10"), "completion": Decimal("0.40")},
        "gemini-2.5-pro": {"prompt": Decimal("1.25"), "completion": Decimal("10.00")},
        "gemini-3.1-flash-lite": {"prompt": Decimal("0.25"), "completion": Decimal("1.50")},
        "gemini-3.1-flash": {"prompt": Decimal("0.50"), "completion": Decimal("3.00")},
        "gemini-3.1-pro": {"prompt": Decimal("2.00"), "completion": Decimal("12.00")},
    },
    /
Claude ha terminato la risposta

Modifica in lib/api.js (frontend) e in UnderstandingPanel.jsx.

1. lib/api.js — commenta gli altri provider nella mappa:

js
export const MODELLI_PER_PROVIDER = {
  anthropic: [
    { valore: 'claude-sonnet-5', etichetta: 'Claude Sonnet 5 (equilibrato, consigliato)' },
    { valore: 'claude-opus-5', etichetta: 'Claude Opus 5 (massima capacità)' },
    { valore: 'claude-fable-5', etichetta: 'Claude Fable 5 (tier superiore)' },
    { valore: 'claude-haiku-4-5-20251001', etichetta: 'Claude Haiku 4.5 (veloce ed economico)' },
  ],
  // Temporaneamente disattivati:
  // - google: blocco geografico intermittente sulla regione del server (FAILED_PRECONDITION)
  // - openai / local: non ancora collaudati sulla pipeline completa
  // openai: [
  //   { valore: 'gpt-4o', etichetta: 'GPT-4o' },
  // ],
  // google: [ ... ],
  // local: [
  //   { valore: 'llama3', etichetta: 'Llama 3 (locale)' },
  // ],
};

2. UnderstandingPanel.jsx — nel <select> dei provider, lascia solo Anthropic:

jsx
        <select
          value={provider}
          onChange={(e) => cambiaProvider(e.target.value)}
          className="..."
        >
          <option value="anthropic">Anthropic (Claude)</option>
          {/* Temporaneamente disattivati: vedi MODELLI_PER_PROVIDER in lib/api.js
          <option value="openai">OpenAI</option>
          <option value="google">Google</option>
          <option value="local">Modello Locale On-Premise</option>
          */}
        </select>

3. Il default deve cambiare, altrimenti il pannello parte su un provider che non esiste più nella lista. Nello stato iniziale:

jsx
  const [provider, setProvider] = useState('anthropic');

(oggi è 'openai').

Il backend non va toccato: llm_config.py continua a supportare tutti i provider, così riattivarli è questione di togliere i commenti. E per i tuoi test puoi comunque forzare qualsiasi modello dal trial via TRIAL_PROVIDER/TRIAL_MODEL su Render.

Una cosa da verificare dopo il deploy: se un cliente ha una sessione salvata con provider_llm: "google", loadSession la ripristina e il selettore si trova un valore non presente tra le opzioni. Il <select> mostrerebbe vuoto e l'invio potrebbe fallire. Se hai sessioni vecchie in giro, vale la pena aggiungere una guardia in loadSession:

jsx
    if (sessionData.provider_llm && MODELLI_PER_PROVIDER[sessionData.provider_llm]) {
      setSelectedProvider(sessionData.provider_llm);
    }

così i provider non più disponibili vengono ignorati invece di rompere il pannello.

ora dammi i listini aggiornati

Ha pensato per 5s
Ha pensato per 5s

Cerco i prezzi ufficiali, perché su Opus 5 e Fable 5 non voglio andare a memoria:

Calibrated pricing conversions and flagged three critical updates
Calibrated pricing conversions and flagged three critical updates

Ecco i prezzi ufficiali. Le tariffe correnti sono 5
/
25
/25 per milione di token in ingresso/uscita per Opus 5, 2
/
10
/10 per Sonnet 5 fino al 31 agosto 2026, 1
/
5
/5 per Haiku 4.5 e 10
/
50
/50 per Fable 5.

Il listino aggiornato per config.py:

python
    "anthropic": {
        # Costi Anthropic (USD/1M): haiku 1/5 · sonnet-5 2/10 (intro) → 3/15 dal 01/09/2026
        #                            opus-5 5/25 · fable-5 10/50
        # Qui sono prezzi di VENDITA in EUR: usando i valori USD come EUR il
        # margine coincide col cambio (~8%). Alza i numeri per margine reale.
        "claude-haiku-4-5-20251001": {"prompt": Decimal("1.00"), "completion": Decimal("5.00")},
        "claude-haiku-4.5":          {"prompt": Decimal("1.00"), "completion": Decimal("5.00")},
        "claude-sonnet-4.6":         {"prompt": Decimal("3.00"), "completion": Decimal("15.00")},
        # Sonnet 5: già a listino post-introduttivo, per non rimetterci dal 1° settembre
        "claude-sonnet-5":           {"prompt": Decimal("3.00"), "completion": Decimal("15.00")},
        "claude-opus-4.6":           {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-opus-4.7":           {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-opus-4.8":           {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-opus-5":             {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-fable-5":            {"prompt": Decimal("10.00"), "completion": Decimal("50.00")},
    },
    "default": {
        # Prudenziale: un modello non a listino costa almeno quanto un Opus
        "prompt": Decimal("5.00"),
        "completion": Decimal("25.00"),
    },

Quattro cose importanti da sapere.

Il margine è quasi nullo. Mettendo i valori USD come EUR, guadagni solo la differenza di cambio (~8%). Se vuoi un margine reale, moltiplica per 1,25 o 1,30 — per Opus 5 diventerebbe 6,25/31,25.

Sonnet 5 costa più di quanto sembra oggi. Il prezzo introduttivo di 2
/
10
/10 vale fino al 31 agosto 2026, poi passa a 3
/
15
/15. L'ho messo già a 3/15: se ci lasciassi 2/10, dal 1° settembre venderesti sottocosto senza accorgertene.

Attenzione al tokenizer, che è il punto più insidioso: Anthropic segnala che Claude 4.7 e successivi tokenizzano lo stesso testo in circa il 30% di token in più rispetto a Sonnet 4.6 e modelli precedenti, quindi un confronto per milione di token sottostima la spesa reale. Il tuo conteggio si basa sui token effettivi riportati dall'API, quindi l'addebito resta corretto — ma se confronti mentalmente i costi con i modelli vecchi, la spesa reale sarà più alta di quanto ti aspetti.

Fable 5 costa il doppio di Opus 5 su entrambi i fronti. Vale la pena rifletterci sul selettore: Anthropic sostiene che Opus 5 si avvicini alle prestazioni di frontiera di Fable 5 a metà del prezzo per token. Se un cliente sceglie Fable per "prendere il migliore", spende il doppio per un guadagno che potrebbe non percepire — l'etichetta nel selettore dovrebbe dirlo chiaramente.

possiamo alzarli del 10% per avere un piccolo margine dai token

Orchestrated prezzi incrementati e margini calcolati strategicamente
Orchestrated prezzi incrementati e margini calcolati strategicamente

Ecco il listino con il 10% aggiunto:

python
    "anthropic": {
        # Costi Anthropic (USD/1M): haiku 1/5 · sonnet-5 3/15 (dal 01/09/26) ·
        # opus-5 5/25 · fable-5 10/50. Qui prezzi di VENDITA in EUR: valore USD +10%.
        # Margine complessivo ~18% (10% esplicito + differenza di cambio EUR/USD).
        "claude-haiku-4-5-20251001": {"prompt": Decimal("1.10"), "completion": Decimal("5.50")},
        "claude-haiku-4.5":          {"prompt": Decimal("1.10"), "completion": Decimal("5.50")},
        "claude-sonnet-4.6":         {"prompt": Decimal("3.30"), "completion": Decimal("16.50")},
        "claude-sonnet-5":           {"prompt": Decimal("3.30"), "completion": Decimal("16.50")},
        "claude-opus-4.6":           {"prompt": Decimal("5.50"), "completion": Decimal("27.50")},
        "claude-opus-4.7":           {"prompt": Decimal("5.50"), "completion": Decimal("27.50")},
        "claude-opus-4.8":           {"prompt": Decimal("5.50"), "completion": Decimal("27.50")},
        "claude-opus-5":             {"prompt": Decimal("5.50"), "completion": Decimal("27.50")},
        "claude-fable-5":            {"prompt": Decimal("11.00"), "completion": Decimal("55.00")},
    },
    "default": {
        # Prudenziale: un modello non a listino costa almeno quanto un Opus
        "prompt": Decimal("5.50"),
        "completion": Decimal("27.50"),
    },
}

# --- Pass multi-giorno -------------------------------------------------
# Prezzo per ogni giorno di accesso acquistato; ogni giorno PAGATO include
# QUOTA_TOKEN_GIORNO_EUR di credito token spendibile (ricaricabile a parte).
PREZZO_GIORNO_EUR = Decimal("299.00")
QUOTA_TOKEN_GIORNO_EUR = Decimal("20.00")

# Pacchetti proposti nel frontend (l'utente può anche digitare un numero libero)
PACCHETTI_GIORNI = [7, 30, 90, 180, 365]
GIORNI_MASSIMI_ACQUISTO = 365

# Bonus lineare sui giorni di accesso: alla soglia di 30 giorni acquistati
# scattano 2 giorni gratis, crescendo linearmente fino a 30 giorni gratis
# per un acquisto di 365 giorni. Sotto la soglia: nessun bonus.
# Verifica: 7->0, 30->2, 90->7, 180->14, 365->30.
BONUS_SOGLIA_GIORNI = 30
BONUS_GIORNI_MINIMO = 2
BONUS_GIORNI_MASSIMO = 30
# --- Soglia PayPal e bonifico -------------------------------------------
# Tetto per singolo pagamento PayPal (limite del circuito ~15.000 €:
# verifica il massimale REALE del tuo account Business e adegua).
# Con 299 €/giorno = 50 giorni esatti. Oltre: bonifico bancario.
SOGLIA_MASSIMA_PAYPAL_EUR = Decimal("14950.00")
# Le coordinate del bonifico NON vanno qui ma nelle variabili d'ambiente:
