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
# 45k era troppo stretta: un backend da 68k arrivava al validatore tagliato
# a meta' listato, e il gate bocciava il documento per un troncamento
# prodotto dal troncamento stesso.
VALIDAZIONE_MAX_CHARS_PER_DOC = 80_000
# Pausa tra l'analisi di un file e il successivo. Serve a non saturare i
# limiti per minuto del provider su progetti con molti file.
# 0 = nessuna pausa (progetti piccoli), 1-2s = prudente su progetti grandi.
DELAY_TRA_FILE_SEC = float(os.getenv("DELAY_TRA_FILE_SEC", "1"))

# File di checkpoint per la fase iterativa (permette il resume dopo un crash)
FILE_IMPL_CHECKPOINT  = "_implementation_checkpoint.json"

# Selezione dei file scelta dall'utente in fase di upload. Vive nella cartella
# di sessione così viene ripristinata insieme al resto dopo un riavvio, e vale
# per TUTTE le fasi: senza, la Fase 3 migrerebbe anche i file esclusi.
FILE_SELEZIONE = "_file_selezionati.json"

# Codice sorgente passato agli agenti della Fase 1 insieme al grafo.
# Senza, gli agenti documentano solo l'elenco delle dipendenze e non
# possono citare funzioni, variabili o logica reale.
MAX_CARATTERI_SORGENTI = 600_000

# File generati automaticamente o senza logica di business: esclusi PRIMA
# di arrivare al modello, quindi a costo zero. (pattern, motivo mostrato)
DEFAULT_ESCLUSIONI_PATTERN = [
    (".designer.cs",      "Codice generato dal designer di Visual Studio"),
    (".g.cs",             "Codice generato automaticamente"),
    (".g.i.cs",           "Codice generato automaticamente"),
    ("assemblyinfo.cs",   "Metadati di assembly"),
    (".min.js",           "JavaScript minificato"),
    (".min.css",          "CSS minificato"),
    ("package-lock.json", "Lock file delle dipendenze"),
    ("composer.lock",     "Lock file delle dipendenze"),
    (".pb.go",            "Codice generato da Protocol Buffers"),
    ("_pb2.py",           "Codice generato da Protocol Buffers"),
]

# Cartelle da ignorare del tutto (build, dipendenze, IDE)
ESCLUDI_CARTELLE_EXTRA = {
    '.vs', 'packages', 'TestResults', 'coverage', 'third_party', 'libs',
}

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
    'es.: subgraph logica_business_hardcoded["Logica di Business Cablata (Hardcoded)"].\n'
    "- Se i nodi sono più di 15, RAGGRUPPALI in subgraph tematici "
    "(presentazione, logica applicativa, accesso ai dati, sistemi esterni) "
    "invece di metterli tutti allo stesso livello: un diagramma che si allarga "
    "per centinaia di pixel a ogni nodo è illeggibile.\n"
    "- Preferisci `graph LR` (sinistra-destra) quando i nodi sono molti: "
    "produce un layout più compatto e leggibile su schermo.\n"
    "- Se il sistema ha molte componenti, meglio PIÙ diagrammi tematici "
    "(uno per area) che un unico diagramma con tutto: ogni diagramma deve "
    "restare leggibile a schermo intero senza zoom.\n"
    "- L'etichetta va SEMPRE attaccata all'ID, senza spazio prima della "
    "parentesi quadra: `nodo_id[\"Testo\"]` e NON `nodo_id [\"Testo\"]`. "
    "Con lo spazio il diagramma non viene renderizzato.\n"
    "- Nelle etichette non usare & < > \" : ne' il punto e virgola. "
    "Scrivi 'e' al posto di '&': a seconda della versione del renderer "
    "questi caratteri interrompono il parsing anche dentro le virgolette.\n"
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
        "gpt-5.4": {
            "prompt": Decimal("2.1250"),
            "completion": Decimal("12.7500"),
        },
        "gpt-5.4-mini": {
            "prompt": Decimal("0.6375"),
            "completion": Decimal("3.8250"),
        },
        "gpt-5.4-nano": {
            "prompt": Decimal("0.1700"),
            "completion": Decimal("1.0625"),
        },
        "gpt-5.5": {
            "prompt": Decimal("4.2500"),
            "completion": Decimal("25.5000"),
        },
    },
    "google": {
        "gemini-3.1-pro-preview": {
            "prompt": Decimal("1.7000"),
            "completion": Decimal("15.3000"),
        },
        "gemini-3.5-flash": {
            "prompt": Decimal("1.2750"),
            "completion": Decimal("7.6500"),
        },
        "gemini-3.6-flash": {
            "prompt": Decimal("1.2750"),
            "completion": Decimal("6.3750"),
        },
    },
    "anthropic": {
    "claude-fable-5": {
        "prompt": Decimal("8.50"),
        "completion": Decimal("42.50"),
    },
    "claude-opus-5": {
        "prompt": Decimal("4.25"),
        "completion": Decimal("21.25"),
    },
    "claude-sonnet-5": {
        "prompt": Decimal("1.70"),
        "completion": Decimal("8.50"),
    },
    "claude-haiku-4-5-20251001": {
        "prompt": Decimal("0.85"),
        "completion": Decimal("4.25"),
    },
    "claude-haiku-4.5": {
        "prompt": Decimal("0.85"),
        "completion": Decimal("4.25"),
    },
},
    "default": {
        "prompt": Decimal("4.6750"),
        "completion": Decimal("23.3750"),
    },
}

def _modello_predefinito():
    """
    Sceglie dal listino Anthropic il modello 'sonnet' più recente.
    Ricavarlo dal listino invece di scriverlo evita il disallineamento:
    se aggiungi o rinomini un modello, il default segue da solo.
    """
    modelli = PREZZI_TOKEN_EUR_PER_1M["anthropic"]
    candidati = [m for m in modelli if "sonnet" in m]
    if not candidati:
        raise RuntimeError("Nessun modello 'sonnet' a listino: controlla PREZZI_TOKEN_EUR_PER_1M.")
    # Il più recente in ordine alfabetico: sonnet-5 batte sonnet-4.6
    return sorted(candidati)[-1]


MODELLO_PREDEFINITO = _modello_predefinito()

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

# Numero di parti oltre il quale il Quality Check viene segnalato come
# frammentato: con troppe analisi separate il revisore perde il contesto e
# nessuno vede le interazioni fra le parti. Superarlo di norma significa che
# gli agenti stanno duplicando codice, non che il progetto e' grande.
QA_MAX_CHUNK_ATTESI = 5

# Numero massimo di PROGETTI attesi nella solution generata.
# La Fase 3 migra un file legacy per volta e ogni passata tende a inventare
# la propria architettura: su un applicativo da 129 file sono nati decine di
# progetti sovrapposti (RUM.API, RUM.Modernized.Api, RUM_Modernized.WebApi...)
# per le stesse funzionalita', rendendo la solution inapribile.
# L'architettura si divide per STRATO (Api, Application, Domain,
# Infrastructure), non per file migrato: il numero di progetti non deve
# dipendere da quanti file si migrano.
MAX_PROGETTI_ATTESI = 6

# --- Struttura della solution, decisa in FASE 2 e approvata dal cliente ---
# Nasce da un difetto reale: la Fase 3 migra un file per volta e ogni passata
# inventava la propria architettura. Su un applicativo da 129 file sono nati
# decine di progetti sovrapposti per le stesse funzionalita', e la solution
# non si apriva. La struttura va decisa UNA volta, da chi progetta, e il
# cliente la approva al Check Point 2 prima che si scriva codice.
STRUTTURA_SOLUTION_RULES = (
    "\n\nSTRUTTURA DELLA SOLUTION (SEZIONE OBBLIGATORIA DEL DOCUMENTO):\n"
    "Includi una sezione con questa intestazione ESATTA:\n"
    "### STRUTTURA SOLUTION\n"
    "seguita da un elenco puntato con un progetto per riga, nella forma\n"
    "`- Nome.Progetto` seguito da un trattino e una riga di descrizione.\n"
    "\n"
    "REGOLE (valide per qualsiasi sistema, di qualsiasi dimensione):\n"
    f"- MASSIMO {MAX_PROGETTI_ATTESI} progetti in tutto.\n"
    "- Dividi per STRATO, non per funzionalita': Api (endpoint), Application \n"
    "  (casi d'uso), Domain (entita' e regole), Infrastructure (dati e sistemi \n"
    "  esterni), Shared (contratti e DTO condivisi, solo se servono \n"
    "  davvero), piu' un progetto di test.\n"
    "- Le aree funzionali del legacy (magazzino, personale, contabilita'...) \n"
    "  diventano CARTELLE dentro questi progetti, MAI progetti separati: il \n"
    "  numero di progetti non deve dipendere da quanti file si migrano.\n"
    "- Una sola convenzione di separatori: usa il punto, mai l'underscore.\n"
    "- Nomi definitivi: saranno usati alla lettera nella generazione del codice.\n"
    "\n"
    "Esempio di formato atteso:\n"
    "### STRUTTURA SOLUTION\n"
    "- Contoso.Api - Endpoint REST e configurazione dell'applicazione\n"
    "- Contoso.Application - Casi d'uso e servizi applicativi\n"
    "- Contoso.Domain - Entita' di dominio e regole di business\n"
    "- Contoso.Infrastructure - Accesso ai dati e integrazioni esterne\n"
    "- Contoso.Shared - Contratti e DTO condivisi fra i progetti\n"
    "- Contoso.Tests - Test unitari e di integrazione\n"
)
