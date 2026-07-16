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

# File di checkpoint per la fase iterativa (permette il resume dopo un crash)
FILE_IMPL_CHECKPOINT  = "_implementation_checkpoint.json"

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
    "anthropic": {
        "claude-haiku-4.5": {"prompt": Decimal("1.00"), "completion": Decimal("5.00")},
        "claude-sonnet-4.6": {"prompt": Decimal("3.00"), "completion": Decimal("15.00")},
        "claude-sonnet-5": {"prompt": Decimal("3.00"), "completion": Decimal("15.00")},
        "claude-opus-4.6": {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-opus-4.7": {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
        "claude-opus-4.8": {"prompt": Decimal("5.00"), "completion": Decimal("25.00")},
    },
    "default": {
        "prompt": Decimal("3.00"),
        "completion": Decimal("12.00"),
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
