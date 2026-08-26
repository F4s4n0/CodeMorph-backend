import json
import logging
import os
import re
import time                   # il modulo, NON datetime.time: quella e' una
                              # classe senza sleep() e mascherava il modulo.
import interruzione

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from crewai import Crew, Process
from src.agents import create_agents
from src.config import (
    FILE_ASSESSMENT,
    FILE_DEPENDENCY_MAP,
    FILE_SELEZIONE,
    FILE_TECH_DOC,
    FILE_FUNCTIONAL_DOC,
    FILE_DB_SCHEMA,
    FILE_MIGRATION_PLAN,
    FILE_BACKEND_IMPL,
    FILE_FRONTEND_IMPL,
    FILE_IMPL_CHECKPOINT,
    FILE_QUALITY_REPORT,
    FILE_TEST_BOOK,
    QA_CHUNK_MAX_CHARS,
    QA_MAX_CHUNK_ATTESI,
    MAX_PROGETTI_ATTESI,
    FILE_VALIDATION_FASE1,
    FILE_VALIDATION_FASE2,
    FILE_VALIDATION_FASE3,
    VALIDAZIONE_MAX_CHARS,
    DELAY_TRA_FILE_SEC,
    VALIDAZIONE_MAX_CHARS_PER_DOC
)
from src.graph_builder import raccogli_sorgenti
from src.live_log import crea_logger_attivita, log_message
from src.segreti import estrai_segreti_da_sorgenti, maschera_segreti

# Valori di credenziale trovati nei sorgenti del cliente, da rimuovere da OGNI
# deliverable. Popolato all'inizio di ogni fase da _registra_segreti(): e' a
# livello di modulo perche' _salva_output_su_disco viene chiamata da tutte le
# fasi e non riceve il codice sorgente fra i parametri.
_SEGRETI_SESSIONE = set()


def _registra_segreti(*sorgenti):
    """
    Aggiunge al registro i segreti trovati nel codice caricato.
    Da chiamare all'inizio di OGNI fase: le fasi 2 e 3 girano in processi che
    potrebbero non aver eseguito la fase 1 (riavvio di Render, resume).
    """
    for testo in sorgenti:
        if testo:
            _SEGRETI_SESSIONE.update(estrai_segreti_da_sorgenti(testo))


def _carica_segreti_sessione(output_dir):
    """
    Registro dei segreti persistito nella cartella di sessione.

    Il set in memoria si azzera a ogni riavvio del processo: senza questo file,
    una fase ripresa dopo un riavvio scriverebbe i deliverable con le
    credenziali in chiaro, pur avendo il filtro attivo.
    """
    percorso = os.path.join(output_dir, "_segreti.json")
    try:
        if os.path.exists(percorso):
            with open(percorso, "r", encoding="utf-8") as f:
                _SEGRETI_SESSIONE.update(json.load(f))
    except Exception as e:
        logger.warning("Registro segreti non leggibile (%s): si riparte dai sorgenti.",
                       type(e).__name__)


def _salva_segreti_sessione(output_dir):
    """Il file NON va incluso nello ZIP consegnato al cliente."""
    if not _SEGRETI_SESSIONE:
        return
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "_segreti.json"), "w", encoding="utf-8") as f:
            json.dump(sorted(_SEGRETI_SESSIONE), f)
    except Exception as e:
        logger.warning("Registro segreti non salvato (%s): dopo un riavvio andrà ricostruito.",
                       type(e).__name__)
from src.tasks import (
    get_understanding_tasks,
    get_design_tasks,
    get_iterative_implementation_tasks,
    get_quality_check_task,
    get_validation_task,
)

logger = logging.getLogger(__name__)


def _salva_metriche(session_id, **valori):
    """
    Scrive su DB le metriche della sessione (righe analizzate, rilievi trovati).

    Sono numeri che il sistema PRODUCE gia' ma non conserva: la dimensione del
    sorgente finisce solo in un log, i rilievi solo dentro un markdown. Senza
    questa scrittura andrebbero persi ad ogni sessione, e un domani non si
    potrebbe dire quante righe di legacy si sono analizzate finora.

    Best-effort: una metrica mancante non deve mai fermare la pipeline.
    """
    if not session_id:
        return
    puliti = {k: v for k, v in valori.items() if v is not None}
    if not puliti:
        return
    try:
        from auth import supabase          # import locale: evita cicli
        supabase.table("migration_sessions").update(puliti).eq("id", session_id).execute()
        logger.info("Metriche salvate per %s: %s", session_id, puliti)
    except Exception as e:
        # Colonna assente = migrazione non eseguita: va detto a voce alta,
        # altrimenti si scopre solo quando le statistiche restano a zero.
        logger.warning("Metriche non salvate per %s (%s: %s). "
                       "Hai eseguito migrazione_metriche.sql?",
                       session_id, type(e).__name__, e)


def _salva_consumo_parziale(session_id, tracker):
    """
    Scrive su DB il consumo accumulato FINORA, dopo ogni crew completata.

    Serve a non perdere i token già bruciati se il server viene riavviato a
    metà fase: il tracker vive in memoria e morirebbe con il processo, mentre
    Anthropic ha comunque fatturato quelle chiamate. All'avvio successivo il
    parziale viene addebitato al cliente invece di restare a carico nostro.

    Best-effort: un errore qui non deve mai fermare la pipeline.
    """
    if not session_id or tracker is None:
        return
    try:
        from auth import supabase          # import locale: evita cicli
        supabase.table("migration_sessions").update({
            "token_parziali": int(tracker.tokens_totali),
            "costo_parziale_eur": float(tracker.costo_eur()),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", session_id).execute()
    except Exception as e:
        logger.warning("Consumo parziale non salvato per %s: %s", session_id, e)


# =====================================================================
# Helper di basso livello
# =====================================================================
def _pulisci_output(contenuto):
    """
    Rimuove l'eventuale ragionamento interno dell'agente ("Thought: ...")
    che CrewAI a volte lascia trapelare in testa all'output finale.
    Il testo vero riparte dal primo titolo Markdown ('#'); in mancanza,
    dal primo doppio a-capo. Se non troviamo un confine sicuro, meglio
    lasciare tutto com'è che rischiare di tagliare contenuto buono.
    """
    if not contenuto.lstrip().startswith("Thought:"):
        return contenuto
    posizione_titolo = contenuto.find("#")
    if posizione_titolo != -1:
        return contenuto[posizione_titolo:]
    posizione_paragrafo = contenuto.find("\n\n")
    if posizione_paragrafo != -1:
        return contenuto[posizione_paragrafo:].lstrip()
    logger.warning("Output che inizia con 'Thought:' ma senza confine chiaro: lo lascio intatto.")
    return contenuto

_nomi_progetto = {}


def _nome_progetto(session_id):
    """
    Nome che il cliente ha dato al progetto al momento dell'upload.

    E' lo stesso che vede in "I Miei Progetti", nel selettore in alto e nel
    nome dello ZIP: metterlo anche nell'intestazione dei documenti chiude il
    cerchio, e a distanza di mesi permette di capire a quale sistema si
    riferisce un file trovato in una cartella.

    In cache: viene richiesto una volta per ogni documento salvato.
    """
    if not session_id:
        return ""
    if session_id in _nomi_progetto:
        return _nomi_progetto[session_id]
    nome = ""
    try:
        from auth import supabase          # import locale: evita cicli
        r = (supabase.table("migration_sessions").select("session_name")
             .eq("id", session_id).limit(1).execute())
        if r.data:
            nome = (r.data[0].get("session_name") or "").strip()
    except Exception as e:
        logger.warning("Nome progetto non leggibile per %s: %s", session_id, type(e).__name__)
    _nomi_progetto[session_id] = nome
    return nome


def _intestazione_documento(percorso, nome_progetto=""):
    """
    Intestazione standard dei deliverable markdown.

    In Markdown PURO: nessun file esterno referenziato, così il documento
    resta integro ovunque venga spostato o inviato.

    Estratta da _salva_output_su_disco perché i due file di implementazione
    (6a e 6b) NON passano di lì: vengono scritti in append, un blocco per ogni
    file legacy migrato, e senza questa funzione restavano gli unici
    deliverable senza intestazione.
    """
    nome_doc = os.path.splitext(os.path.basename(percorso))[0]
    nome_doc = nome_doc.split("_", 1)[-1].replace("_", " ")
    data_oggi = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y")
    return (
        "<div align=\"center\">\n\n"
        "# ⬢ CodeMorph`.AI`\n\n"
        "**Piattaforma di modernizzazione di sistemi legacy**\n\n"
        "</div>\n\n"
        "---\n\n"
        f"### {nome_doc}\n\n"
        f"| | |\n"
        f"|---|---|\n"
        # Il nome scelto dal cliente in fase di upload: lo stesso
        # che vede in "I Miei Progetti" e nel nome dello ZIP.
        + (f"| **Progetto** | {nome_progetto} |\n" if nome_progetto else "")
        + f"| **Generato il** | {data_oggi} |\n"
        f"| **Piattaforma** | CodeMorph.AI — www.codemorph.it |\n"
        f"| **Natura del documento** | Prodotto da modelli di intelligenza artificiale |\n\n"
        "> ⚠️ **Validazione richiesta.** Il contenuto di questo documento è generato "
        "automaticamente e può contenere imprecisioni o omissioni. Prima di ogni "
        "utilizzo operativo deve essere verificato da personale tecnico qualificato.\n\n"
        "---\n\n"
    )


def _salva_output_su_disco(tasks, output_dir, nome_progetto=""):
    """
    Scrive esplicitamente l'output di ogni task nella cartella di sessione.
    ATTENZIONE: non fidarsi di task.output_file per la CARTELLA — le versioni
    recenti di CrewAI sanificano i percorsi assoluti rimuovendo la '/' iniziale,
    trasformandoli in relativi (finirebbero dentro la cartella del progetto).
    Da output_file prendiamo SOLO il nome del file; la cartella è la nostra.
    """
    for task in tasks:
        percorso_task = getattr(task, "output_file", None)
        if not percorso_task:
            continue
        percorso = os.path.join(output_dir, os.path.basename(percorso_task))
        try:
            contenuto = _task_output_text(task)
            # Mascheramento PRIMA di qualunque scrittura: e' l'unico imbuto da
            # cui passano tutti i deliverable, quindi il posto giusto per
            # garantire che nessun segreto finisca in un file scaricabile.
            contenuto, n_mascherati = maschera_segreti(contenuto, valori_noti=_SEGRETI_SESSIONE)
            if n_mascherati:
                logger.info("%s: rimossi %d possibili segreti.",
                            os.path.basename(percorso), n_mascherati)
            contenuto, n_mermaid = _normalizza_mermaid(contenuto)
            if n_mermaid:
                logger.info("%s: corrette %d anomalie di sintassi Mermaid.",
                            os.path.basename(percorso), n_mermaid)
            if percorso.lower().endswith(".md"):
                contenuto = _intestazione_documento(percorso, nome_progetto) + contenuto
            os.makedirs(output_dir, exist_ok=True)
            with open(percorso, "w", encoding="utf-8") as f:
                f.write(contenuto)
            logger.info("Output salvato: %s (%d caratteri)", percorso, len(contenuto))
        except Exception:
            logger.exception("Impossibile salvare l'output del task su %s", percorso)


def _task_output_text(task):
    """Estrae il testo dall'output di un Task (compatibile tra versioni CrewAI)."""
    return _pulisci_output(getattr(task.output, "raw", None) or str(task.output))


def _load_checkpoint(path):
    """Carica l'elenco dei file legacy già processati (per il resume dopo crash)."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Checkpoint corrotto (%s), riparto da zero.", e)
    return set()


def _save_checkpoint(path, processed):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(processed), f, ensure_ascii=False, indent=2)


def _chunk_text(text, max_chars):
    """Divide un testo lungo in blocchi <= max_chars, spezzando su righe intere."""
    if len(text) <= max_chars:
        return [text]
    chunks, current, current_len = [], [], 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks

def _normalizza_percorsi(testo, registro_cartelle, e_frontend):
    """
    Riconduce i percorsi `/// FILEPATH:` a una struttura unica e coerente.

    Ogni file legacy viene migrato in una passata separata e l'agente ricostruisce
    ogni volta la propria idea di struttura: nella stessa sessione sono comparsi
    `src/backend/CognosMonitor.API/` e `src/CognosMonitor.API/` (due radici per lo
    stesso progetto), test sia sotto `src/` sia sotto `tests/`, e le cartelle
    `Dtos/` e `DTOs/` — che su un file system case-sensitive sono due cartelle
    diverse e impediscono la compilazione.

    Tre regole, tutte deterministiche:
    1. radice corretta: `src/backend`, `src/frontend` o `tests/...` per i progetti
       di test, dedotta dall'agente che ha prodotto il blocco;
    2. maiuscole coerenti: la PRIMA forma vista di una cartella diventa quella
       canonica per tutta la sessione (registro_cartelle);
    3. separatori normalizzati e prefissi spuri rimossi.

    `registro_cartelle` va condiviso fra tutti i file della fase, altrimenti la
    canonizzazione vale solo dentro un singolo file e il problema resta.
    """
    if not testo:
        return testo, 0

    radice_area = "frontend" if e_frontend else "backend"
    conteggio = 0

    def _canonica(segmento):
        """Prima forma vista di una cartella: quella vince, sempre."""
        chiave = segmento.lower()
        if chiave not in registro_cartelle:
            registro_cartelle[chiave] = segmento
        return registro_cartelle[chiave]

    def _canonica_progetto(segmento):
        """
        Nome del progetto (primo segmento dopo la radice), unificato anche
        quando cambia il SEPARATORE.

        Ogni file legacy viene migrato in una passata indipendente e l'agente
        reinventa ogni volta la propria convenzione: su un progetto reale sono
        comparsi `RUM.Modernized.Api`, `RUM_Modernized.Api` e
        `RUM_Modernized.WebApi` come tre progetti distinti per la stessa cosa.
        Punti e underscore vengono equiparati, cosi' la prima forma vista fa
        da riferimento per tutte le successive.
        """
        chiave = segmento.lower().replace("_", ".")
        if chiave not in registro_cartelle:
            registro_cartelle[chiave] = segmento
        return registro_cartelle[chiave]

    def _sistema(match):
        nonlocal conteggio
        originale = match.group(2).strip()

        percorso = originale.replace("\\", "/").lstrip("./").lstrip("/")
        parti = [p for p in percorso.split("/") if p and p != "."]
        if not parti:
            return match.group(0)

        # Via i prefissi di radice: vengono ricostruiti dopo, uniformi.
        while parti and parti[0].lower() in ("src", "tests", "test", "backend", "frontend"):
            parti.pop(0)
        if not parti:
            return match.group(0)

        # Un progetto di test va sotto tests/, ovunque l'agente l'abbia messo.
        e_test = any(p.lower().endswith((".tests", ".integrationtests", ".unittests"))
                     for p in parti)

        # Il PRIMO segmento e' il nome del progetto e va unificato con regole
        # piu' larghe (punti e underscore equivalenti); gli altri sono
        # cartelle interne, dove conta solo la differenza di maiuscole.
        if len(parti) > 1:
            cartelle = [_canonica_progetto(parti[0])] + [_canonica(p) for p in parti[1:-1]]
        else:
            cartelle = []
        nuovo = "/".join((["tests"] if e_test else ["src"]) + [radice_area] + cartelle + [parti[-1]])

        if nuovo != originale:
            conteggio += 1
        return f"{match.group(1)}{nuovo}"

    risultato = re.sub(r"(///\s*FILEPATH:\s*)(.+)", _sistema, testo)
    return risultato, conteggio


def _normalizza_mermaid(testo):
    """
    Corregge gli errori di sintassi Mermaid piu' frequenti PRIMA della scrittura.

    Le regole nel prompt riducono il problema ma non lo eliminano: basta uno
    spazio di troppo e il cliente apre un deliverable con un errore di parsing
    al posto del diagramma. Qui il controllo e' deterministico e vale per tutti
    i documenti, di tutte le fasi, con qualunque modello.

    Interviene SOLO dentro i blocchi ```mermaid: il resto del markdown non
    viene toccato.
    """
    if not testo or "```mermaid" not in testo:
        return testo, 0

    conteggio = 0

    def _corpo(m):
        nonlocal conteggio
        corpo = m.group(2)

        # 1. Spazio fra ID ed etichetta: `nodo_id ["Testo"]` non renderizza.
        corpo, n1 = re.subn(r'([A-Za-z0-9_]) +(\[")', r'\1\2', corpo)

        # 2. '&' dentro un'etichetta: ammessa in teoria, ma rompe il parsing
        #    in diverse versioni del renderer. Sostituita SOLO dentro le
        #    etichette fra virgolette, mai nella sintassi del grafo.
        n2 = 0

        def _senza_ampersand(match):
            nonlocal n2
            etichetta = match.group(0)
            if "&" not in etichetta:
                return etichetta
            n2 += etichetta.count("&")
            return etichetta.replace("&", "e")

        corpo = re.sub(r'"[^"\n]*"', _senza_ampersand, corpo)

        conteggio += n1 + n2
        return m.group(1) + corpo + m.group(3)

    risultato = re.sub(r'(```mermaid\n)(.*?)(```)', _corpo, testo, flags=re.DOTALL)
    return risultato, conteggio


def _tronca_su_sezioni(testo, max_char):
    """
    Taglia sui confini di sezione markdown invece che a metà frase.
    E' la rete di sicurezza di _sintetizza_contesto: se la sintesi non è
    disponibile, meglio un documento parziale ma leggibile che nessun contesto.
    """
    if not testo or len(testo) <= max_char:
        return testo
    pezzi, totale = [], 0
    for blocco in re.split(r"(?m)^(?=#{1,3} )", testo):
        if totale + len(blocco) > max_char:
            break
        pezzi.append(blocco)
        totale += len(blocco)
    ridotto = "".join(pezzi) or testo[:max_char]
    return ridotto + "\n\n[...documento troncato: sezioni successive omesse.]"


# La sintesi di un documento è la stessa per tutti i file della fase: si
# calcola una volta sola, altrimenti ogni retry pagherebbe di nuovo la stessa
# chiamata.
_cache_sintesi = {}


def _sintetizza_contesto(llm, testo, etichetta, max_char=8000):
    """
    Condensa un documento di contesto mantenendone la sostanza operativa.

    Serve nel secondo tentativo di migrazione: il prompt va alleggerito, ma
    eliminare del tutto la documentazione funzionale priverebbe l'agente del
    comportamento atteso. Meglio una sintesi che il vuoto.

    Non solleva mai: se la chiamata fallisce si ripiega sul troncamento. Questo
    codice gira DOPO un fallimento, quindi non deve introdurre un secondo modo
    di fallire.
    """
    if not testo or len(testo) <= max_char:
        return testo

    chiave = (etichetta, len(testo))
    if chiave in _cache_sintesi:
        return _cache_sintesi[chiave]

    prompt = (
        f"Sintetizza il seguente documento ({etichetta}) in meno di {max_char} caratteri, "
        "per un ingegnere che deve riscrivere il software in un'altra tecnologia.\n\n"
        "CONSERVA: regole di business, comportamenti attesi, vincoli, casi limite, "
        "nomi di entità/campi/funzioni citati, criteri di accettazione.\n"
        "ELIMINA: introduzioni, motivazioni, ripetizioni, project management, "
        "considerazioni di metodo.\n"
        "Rispondi SOLO con la sintesi, in elenchi puntati compatti, senza preamboli.\n\n"
        f"---\n{testo}"
    )

    try:
        risposta = llm.call([{"role": "user", "content": prompt}])
        sintesi = str(risposta or "").strip()
        if len(sintesi) < 200:
            # Risposta inutilizzabile: è esattamente il caso che stiamo
            # cercando di aggirare, quindi non insistiamo.
            raise ValueError(f"sintesi troppo breve ({len(sintesi)} caratteri)")
        sintesi = f"[SINTESI DI {etichetta} — documento completo disponibile nei deliverable]\n\n{sintesi}"
        logger.info("Contesto '%s' sintetizzato: %d -> %d caratteri.",
                    etichetta, len(testo), len(sintesi))
    except Exception as e:
        logger.warning("Sintesi di '%s' non riuscita (%s: %s): uso il troncamento.",
                       etichetta, type(e).__name__, e)
        sintesi = _tronca_su_sezioni(testo, max_char)

    _cache_sintesi[chiave] = sintesi
    return sintesi


def _vale_un_secondo_tentativo(errore):
    """
    True se il fallimento non ha una causa nel merito e puo' rientrare da solo.

    Ritentare ha senso quando il modello non ha prodotto nulla di utilizzabile
    (risposta vuota, troncata, timeout, sovraccarico): sono episodi che spesso
    non si ripetono. NON ha senso su chiave mancante, credito esaurito o
    prompt malformato, dove il secondo tentativo brucerebbe solo altri token.

    LiteLLM ritenta gia' da sé gli errori di rete (num_retries): qui si copre
    il livello sopra, cioe' la risposta arrivata ma inservibile.
    """
    testo = f"{type(errore).__name__}: {errore}".lower()

    inutile_ritentare = (
        "api_key", "api key", "authentication", "unauthorized", "permission",
        "not found", "notfounderror", "invalid model", "credito", "quota",
        "insufficient", "billing",
    )
    if any(s in testo for s in inutile_ritentare):
        return False

    vale_la_pena = (
        "none or empty",        # CrewAI: risposta senza testo utilizzabile
        "invalid response",
        "timeout", "timed out",
        "overloaded", "529",
        "max_tokens", "truncated", "length",
        "connection", "temporarily",
    )
    return any(s in testo for s in vale_la_pena)


def _progetti_dal_migration_plan(output_dir):
    """
    Progetti dichiarati dall'architetto nella sezione '### STRUTTURA SOLUTION'
    del migration plan, e APPROVATI dal cliente al Check Point 2.

    E' la fonte autorevole della struttura: la Fase 3 non deve inventarla,
    deve rispettarla. Se il cliente ha modificato il documento al checkpoint
    (rinominando un progetto, togliendone uno), qui si legge la sua versione
    — il documento e' la fonte, non una copia salvata a parte.

    Se la sezione manca si torna al comportamento precedente: il primo file
    migrato definisce la struttura per tutti gli altri.
    """
    testo = _read_if_exists(f"{output_dir}/{FILE_MIGRATION_PLAN}", "")
    if not testo:
        return set()

    # Dalla riga dell'intestazione fino alla prossima intestazione di pari
    # livello: si legge solo l'elenco, non il resto del documento.
    blocco = re.search(r"#+\s*STRUTTURA\s+SOLUTION\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
                       testo, re.IGNORECASE | re.DOTALL)
    if not blocco:
        logger.info("Migration plan senza sezione 'STRUTTURA SOLUTION': "
                    "la struttura sara' definita dal primo file migrato.")
        return set()

    progetti = set()
    for riga in blocco.group(1).splitlines():
        # `- Nome.Progetto - descrizione` oppure `- **Nome.Progetto**: ...`
        m = re.match(r"\s*[-*+]\s*\**\s*([A-Za-z][A-Za-z0-9._]{2,60})\**\s*[-:–]", riga)
        if m:
            progetti.add(m.group(1).strip(". "))
    if progetti:
        logger.info("Struttura approvata dal migration plan: %s", sorted(progetti))
    return progetti


def _progetti_dichiarati(testo):
    """
    Nomi dei progetti gia' creati, letti dai percorsi `/// FILEPATH:`.

    Servono a dire all'agente successivo quale architettura ESISTE gia'.
    Senza, ogni file migrato ne inventa una nuova: su un applicativo da 129
    file sono nati decine di progetti sovrapposti (`RUM.API`,
    `RUM.Modernized.Api`, `RUM_Modernized.WebApi`... per la stessa cosa),
    rendendo la solution inutilizzabile pur essendo il codice corretto.

    Il primo file fa da guida: chi viene dopo riusa, non reinventa.
    """
    progetti = set()
    for percorso in re.findall(r"///\s*FILEPATH:\s*(.+)", testo or ""):
        parti = [p for p in percorso.strip().replace("\\", "/").split("/") if p]
        # Si salta la radice (src/backend, tests/frontend...) e si prende il
        # segmento successivo, che e' il nome del progetto.
        while parti and parti[0].lower() in ("src", "tests", "test", "backend", "frontend"):
            parti.pop(0)
        if len(parti) > 1:
            progetti.add(parti[0])
    return progetti


def _tipi_dichiarati(testo):
    """
    Nomi di classi/interfacce/record dichiarati in un output generato.

    Serve a dire all'agente successivo cosa esiste già: senza, ogni file
    rigenera le classi condivise con varianti di nome e namespace (nella
    stessa sessione sono comparse sia Dtos/ sia DTOs/, due cartelle diverse
    per la stessa cosa).
    """
    return set(re.findall(
        r"\b(?:class|interface|record|struct|enum)\s+([A-Z][A-Za-z0-9_]*)", testo or ""
    ))


def _read_if_exists(path, fallback):
    """Legge un file di contesto se esiste, altrimenti restituisce il fallback."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    logger.warning("File di contesto non trovato: %s — uso fallback.", path)
    return fallback

# =====================================================================
# FASE 1 - UNDERSTANDING
# =====================================================================

def _verifica_copertura_inventario(output_dir, codice_legacy, session_id):
    """
    Controlla che OGNI file dato in pasto agli agenti compaia nei documenti,
    e aggiunge in coda all'inventario quelli mancanti.

    Un'istruzione nel prompt non basta: il modello puo' credere di aver citato
    tutto e saltarne qualcuno senza accorgersene. Qui il controllo e'
    deterministico — si confrontano i nomi presenti nel contesto con quelli
    presenti nei documenti — e il risultato e' visibile al cliente invece di
    restare un dato interno.

    Non riscrive il documento: aggiunge una sezione finale con l'elenco dei
    non citati. Meglio una nota onesta in fondo che un documento che sembra
    completo e non lo e'.
    """
    ricevuti = {n.strip() for n in re.findall(r"----- FILE: (.+?) -----", codice_legacy or "")}
    if not ricevuti:
        return

    nomi_brevi = {p.rsplit("/", 1)[-1].lower(): p for p in ricevuti}

    testo_documenti = ""
    for nome in (FILE_ASSESSMENT, FILE_DEPENDENCY_MAP, FILE_TECH_DOC,
                 FILE_FUNCTIONAL_DOC, FILE_TEST_BOOK):
        testo_documenti += _read_if_exists(f"{output_dir}/{nome}", "").lower()
    if not testo_documenti:
        return

    mancanti = sorted(percorso for breve, percorso in nomi_brevi.items()
                      if breve not in testo_documenti)
    if not mancanti:
        log_message(session_id, f"✅ Copertura inventario completa: tutti i {len(ricevuti)} file sono citati nei documenti.")
        return

    log_message(
        session_id,
        f"⚠️ {len(mancanti)} file su {len(ricevuti)} non risultano citati nei documenti: "
        "vengono elencati in coda all'inventario.",
    )
    logger.warning("Inventario incompleto: %d file non citati (%s).",
                   len(mancanti), ", ".join(mancanti[:10]))

    percorso = f"{output_dir}/{FILE_ASSESSMENT}"
    testo = _read_if_exists(percorso, "")
    if not testo:
        return
    coda = [
        "",
        "---",
        "",
        "## File analizzati ma non trattati nel dettaglio",
        "",
        f"I seguenti {len(mancanti)} file fanno parte dell'analisi ma non sono stati",
        "descritti singolarmente nelle sezioni precedenti. Sono elencati qui per",
        "completezza: se uno di essi e' rilevante per il tuo sistema, segnalalo",
        "prima di procedere alla fase successiva.",
        "",
    ]
    coda += [f"- `{p}`" for p in mancanti]
    coda.append("")
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(testo.rstrip() + "\n" + "\n".join(coda))


def run_understanding_phase(llm, codice_legacy, output_dir, session_id=None, tracker=None, quality_gate=False):
    """
    Esegue la FASE 1: Understanding.
    Crea l'inventario, la mappa delle dipendenze, la documentazione e il test book.

    `session_id` attiva il log live dell'attività REALE degli agenti
    (via callback CrewAI); `tracker` accumula i token consumati.
    """
    os.makedirs(output_dir, exist_ok=True)

    _carica_segreti_sessione(output_dir)
    _registra_segreti(codice_legacy)
    _salva_segreti_sessione(output_dir)

    # Dimensione del legacy analizzato: si misura QUI perche' e' l'unico
    # punto in cui il sorgente completo e' in memoria. Il marcatore usato dal
    # raccoglitore permette di contare anche quanti file lo compongono.
    testo_legacy = codice_legacy or ""
    numero_file = testo_legacy.count("----- FILE:")
    _salva_metriche(
        session_id,
        righe_legacy=testo_legacy.count("\n") + 1 if testo_legacy else 0,
        caratteri_legacy=len(testo_legacy),
        file_legacy_analizzati=numero_file or None,
    )

    agents = create_agents(llm)
    # Il numero di file regola l'ampiezza attesa dei documenti: su un sistema
    # da centinaia di file gli agenti devono coprire tutte le aree funzionali,
    # non comprimere tutto nello spazio pensato per un applicativo piccolo.
    tasks = get_understanding_tasks(agents, output_dir, numero_file=numero_file)
    annuncia_avvio, task_callback = crea_logger_attivita(
        session_id, tasks, etichetta="Fase 1 · Understanding"
    )

    # Selezioniamo solo gli agenti necessari per questa fase
    fase1_agents = [
        agents["legacy_system_analyzer"],
        agents["dependency_mapper"],
        agents["tech_business_documenter"],
        agents["functional_analyst"],
        agents["qa_test_planner"],
    ]

    def step_con_stop(step):
            interruzione.verifica_stop(session_id)   # solleva se richiesto

    crew = Crew(
        agents=fase1_agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
        memory=False,  # Disattivato per evitare errori di fuso orario/database locale
        step_callback=step_con_stop,
        task_callback=task_callback,  # Log live sincronizzato con l'attività reale

    )

    annuncia_avvio()
    risultato = crew.kickoff(inputs={"codice_legacy": codice_legacy})
    _salva_output_su_disco(tasks, output_dir, _nome_progetto(session_id))
    # Verifica deterministica: un'istruzione nel prompt non garantisce che gli
    # agenti abbiano davvero citato tutto.
    try:
        _verifica_copertura_inventario(output_dir, codice_legacy, session_id)
    except Exception as e:
        logger.warning("Verifica copertura inventario non riuscita: %s", e)
    if tracker is not None:
        tracker.aggiungi_crew(crew, risultato)
        _salva_consumo_parziale(session_id, tracker)
    if quality_gate:
        _valida_fase(
            llm, output_dir, "Fase 1 · Understanding",
            [FILE_ASSESSMENT, FILE_DEPENDENCY_MAP, FILE_TECH_DOC, FILE_FUNCTIONAL_DOC],
            FILE_VALIDATION_FASE1, session_id, tracker,
        )
    return risultato


# =====================================================================
# FASE 2 - DESIGN
# =====================================================================

def run_design_phase(llm, linguaggio_target, output_dir, session_id=None, tracker=None, quality_gate=False):
    """
    Esegue la FASE 2: Design.
    Rilegge da disco i documenti validati della fase 1 (le Crew sono isolate
    tra loro, quindi il contesto va reiniettato esplicitamente) e genera il
    Migration Plan, gli ADR e lo schema del database target.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Contesto della FASE 1: senza questo il Cloud Architect e il DBA
    # lavorerebbero "al buio", basandosi solo sul nome del linguaggio target.
    documenti_fase1 = []
    for nome in (FILE_ASSESSMENT, FILE_DEPENDENCY_MAP, FILE_TECH_DOC, FILE_FUNCTIONAL_DOC):
        contenuto = _read_if_exists(f"{output_dir}/{nome}", "")
        if contenuto:
            documenti_fase1.append(f"### {nome}\n{contenuto}")
    contesto_fase1 = "\n\n".join(documenti_fase1)

    # Evidenza primaria: architetto e DBA devono vedere il codice reale,
    # non solo la documentazione della fase precedente. Senza, lo schema
    # database nasce da una descrizione invece che dalle query vere.
    cartella_sorgenti = os.path.join(output_dir, "sorgenti_originali")
    contesto_sorgenti = ""
    if os.path.isdir(cartella_sorgenti):
        # Rispetta la selezione fatta in Fase 1: i file esclusi dall'utente
        # non devono rientrare dalla finestra e tornare a costare token.
        ammessi = None
        percorso_sel = os.path.join(output_dir, FILE_SELEZIONE)
        if os.path.exists(percorso_sel):
            try:
                with open(percorso_sel, "r", encoding="utf-8") as f:
                    ammessi = set(json.load(f))
            except Exception:
                logger.warning("Selezione file non leggibile: passo tutti i sorgenti.")
        contesto_sorgenti = raccogli_sorgenti(cartella_sorgenti, file_ammessi=ammessi)
    if not contesto_sorgenti:
        contesto_sorgenti = "Codice sorgente non disponibile in questa sessione."

    # ANCHE la Fase 2 deve registrare i segreti: puo' girare in un processo
    # che non ha eseguito la Fase 1 (riavvio di Render fra un checkpoint e
    # l'altro), e in quel caso il registro sarebbe vuoto proprio mentre si
    # scrive il migration plan — che cita le credenziali cablate da rimuovere.
    _carica_segreti_sessione(output_dir)
    _registra_segreti(contesto_sorgenti)
    _salva_segreti_sessione(output_dir)

    agents = create_agents(llm)
    tasks = get_design_tasks(agents, output_dir, contesto_fase1=contesto_fase1)
    annuncia_avvio, task_callback = crea_logger_attivita(
        session_id, tasks, etichetta="Fase 2 · Design"
    )

    def step_con_stop(step):
            interruzione.verifica_stop(session_id)   # solleva se richiesto

    crew = Crew(
        agents=[
            agents["cloud_solutions_architect"],
            agents["database_administrator"],
        ],
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
        memory=False,
        step_callback=step_con_stop,
        task_callback=task_callback,  # Log live sincronizzato con l'attività reale
    )

    annuncia_avvio()
    risultato = crew.kickoff(inputs={
        "linguaggio_target": linguaggio_target,
        "contesto_fase1": contesto_fase1 or "Nessun documento di Fase 1 disponibile.",
        "contesto_sorgenti": contesto_sorgenti,
    })
    _salva_output_su_disco(tasks, output_dir, _nome_progetto(session_id))
    if tracker is not None:
        tracker.aggiungi_crew(crew, risultato)
        _salva_consumo_parziale(session_id, tracker)
    if quality_gate:
        _valida_fase(
            llm, output_dir, "Fase 2 · Design",
            [FILE_MIGRATION_PLAN, FILE_DB_SCHEMA],
            FILE_VALIDATION_FASE2, session_id, tracker,
        )
    return risultato

# =====================================================================
# FASE 3 - IMPLEMENTATION (iterativa) + QUALITY CHECK
# =====================================================================

def run_implementation_phase(
    llm,
    linguaggio_target,
    output_dir,
    lista_file_legacy_estratti,
    session_id=None,
    tracker=None,
    quality_gate=False
):
    """
    Esegue la FASE 3: per ogni file legacy genera backend e frontend target,
    poi lancia il Quality Check finale sul codice prodotto.

    Ritorna un dizionario con l'esito per file e il verdetto QA, così il
    chiamante può loggare/notificare senza dover rileggere i file da disco.

    `session_id` attiva il log live per file e per task (attività reale);
    `tracker` accumula i token consumati da tutte le crew della fase.
    """
    os.makedirs(output_dir, exist_ok=True)
    agents = create_agents(llm)

    # 1. Caricamento del contesto (ADR e schema DB generati in FASE 2)
    contesto_adr = _read_if_exists(f"{output_dir}/{FILE_MIGRATION_PLAN}", "Nessun ADR.")
    contesto_sql = _read_if_exists(f"{output_dir}/{FILE_DB_SCHEMA}", "Nessun DB Schema.")
    # Requisiti funzionali e test attesi: senza questi il developer conosce
    # l'architettura ma non COSA deve fare il software per l'utente.
    contesto_funzionale = _read_if_exists(f"{output_dir}/{FILE_FUNCTIONAL_DOC}", "")
    contesto_test = _read_if_exists(f"{output_dir}/{FILE_TEST_BOOK}", "")

    # Fase 3 puo' girare in un processo che non ha eseguito la Fase 1 (riavvio
    # di Render, resume da checkpoint): i segreti vanno rilevati di nuovo qui.
    _carica_segreti_sessione(output_dir)
    _registra_segreti(*[f.get("codice", "") for f in lista_file_legacy_estratti])
    _salva_segreti_sessione(output_dir)

    percorso_backend = f"{output_dir}/{FILE_BACKEND_IMPL}"
    percorso_frontend = f"{output_dir}/{FILE_FRONTEND_IMPL}"
    percorso_checkpoint = f"{output_dir}/{FILE_IMPL_CHECKPOINT}"

    # 2. Checkpoint: se una run precedente è crashata a metà, riprendiamo
    #    da dove eravamo invece di rigenerare (e ripagare) tutto.
    processati = _load_checkpoint(percorso_checkpoint)
    if not processati:
        # Prima run (o restart pulito): svuota i file finali per evitare duplicati.
        # I due file di implementazione crescono in APPEND (un blocco per ogni
        # file legacy migrato), quindi l'intestazione va scritta ORA: aggiungerla
        # a ogni scrittura la ripeterebbe una volta per file.
        nome_prog = _nome_progetto(session_id)
        for percorso in (percorso_backend, percorso_frontend):
            with open(percorso, "w", encoding="utf-8") as f:
                f.write(_intestazione_documento(percorso, nome_prog))
    else:
        logger.info(
            "Resume: %d file già processati verranno saltati.", len(processati)
        )
        log_message(
            session_id,
            f"⏭️ Resume: {len(processati)} file già processati in una run precedente verranno saltati.",
        )

    esiti = {"completati": [], "falliti": [], "saltati": []}
    totale = len(lista_file_legacy_estratti)

    # Tipi gia' prodotti: iniettati nei task successivi perche' non vengano
    # ridefiniti. In caso di resume si rileggono dai file gia' scritti, cosi'
    # il riavvio non riparte "senza memoria" di cosa esiste.
    tipi_generati = set()
    # Progetti: la struttura DECISA in Fase 2 e approvata dal cliente al
    # Check Point 2 ha la precedenza. Solo se manca si torna al vecchio
    # comportamento, in cui il primo file migrato la definisce per tutti.
    progetti_generati = _progetti_dal_migration_plan(output_dir)
    if progetti_generati:
        log_message(
            session_id,
            f"🏛️ Struttura approvata al Check Point 2: {len(progetti_generati)} progetti "
            f"({', '.join(sorted(progetti_generati))}).",
        )
    if processati:
        tipi_generati |= _tipi_dichiarati(_read_if_exists(percorso_backend, ""))
        tipi_generati |= _tipi_dichiarati(_read_if_exists(percorso_frontend, ""))
        progetti_generati |= _progetti_dichiarati(_read_if_exists(percorso_backend, ""))
        progetti_generati |= _progetti_dichiarati(_read_if_exists(percorso_frontend, ""))

    # Forma canonica delle cartelle, condivisa fra TUTTI i file della fase:
    # senza, ogni passata sceglie le proprie maiuscole e nascono Dtos/ e DTOs/.
    # In caso di resume si ricostruisce dai percorsi gia' scritti.
    registro_cartelle = {}
    if processati:
        for gia_scritto in (_read_if_exists(percorso_backend, ""), _read_if_exists(percorso_frontend, "")):
            for percorso_noto in re.findall(r"///\s*FILEPATH:\s*(.+)", gia_scritto):
                for segmento in percorso_noto.strip().split("/")[:-1]:
                    registro_cartelle.setdefault(segmento.lower(), segmento)

    log_message(
        session_id,
        f"⚙️ Fase 3: {totale} file legacy in coda di migrazione verso {linguaggio_target}.",
    )

    # 3. IL CICLO ITERATIVO: un file legacy alla volta
    for indice, file_info in enumerate(lista_file_legacy_estratti, start=1):
        interruzione.verifica_stop(session_id)
        nome_file = file_info["nome"]

        if nome_file in processati:
            logger.info("Salto %s: già processato in una run precedente.", nome_file)
            log_message(session_id, f"⏭️ ({indice}/{totale}) Salto {nome_file}: già processato.")
            esiti["saltati"].append(nome_file)
            continue

        def _costruisci_crew(alleggerito=False):
            """
            Prepara i task per questo file. Con alleggerito=True i contesti
            DISCORSIVI (documentazione funzionale, test book) vengono sintetizzati
            invece che eliminati: l'agente perde la prosa ma conserva regole di
            business e criteri di accettazione.

            ADR e schema SQL restano INTEGRI: sono densi di informazione
            strutturale, e una sintesi produrrebbe codice che riferisce tabelle
            o colonne inesistenti.
            """
            if alleggerito:
                funzionale = _sintetizza_contesto(llm, contesto_funzionale, "documentazione funzionale")
                test = _sintetizza_contesto(llm, contesto_test, "test book")
            else:
                funzionale, test = contesto_funzionale, contesto_test

            tasks_file = get_iterative_implementation_tasks(
                agents=agents,
                linguaggio_target=linguaggio_target,
                nome_file_legacy=nome_file,
                contenuto_file_legacy=file_info["codice"],
                contesto_adr=contesto_adr,
                contesto_sql=contesto_sql,
                contesto_funzionale=funzionale,
                contesto_test=test,
                tipi_gia_generati=tipi_generati,
                progetti_esistenti=progetti_generati,
            )
            etichetta = f"file {indice}/{totale}: {nome_file}"
            if alleggerito:
                etichetta += " (2° tentativo)"
            avvio, callback = crea_logger_attivita(session_id, tasks_file.as_list(), etichetta=etichetta)

            def step_con_stop(step):
                interruzione.verifica_stop(session_id)   # solleva se richiesto

            crew_file = Crew(
                agents=[agents["senior_migration_developer"], agents["frontend_developer"]],
                tasks=tasks_file.as_list(),
                process=Process.sequential,
                verbose=False,  # Silenzioso per non inondare la console
                memory=False,
                step_callback=step_con_stop,
                task_callback=callback,  # Il log live segue il lavoro reale sul file
            )
            return tasks_file, crew_file, avvio

        impl_tasks, dev_crew, annuncia_avvio = _costruisci_crew()

        try:
            logger.info("Migrazione di %s in corso...", nome_file)
            log_message(session_id, f"📦 ({indice}/{totale}) Migrazione di {nome_file} avviata...")
            annuncia_avvio()
            try:
                dev_crew.kickoff()
            except Exception as primo_errore:
                # Un secondo tentativo ha senso solo per i fallimenti SENZA una
                # causa nel merito: risposta vuota, troncata, timeout. Un errore
                # di configurazione o di credito si ripeterebbe identico, e uno
                # stop richiesto dall'utente non va mai aggirato.
                if isinstance(primo_errore, interruzione.FaseInterrotta) or \
                        not _vale_un_secondo_tentativo(primo_errore):
                    raise
                logger.warning("Primo tentativo fallito su %s (%s): riprovo con contesto ridotto.",
                               nome_file, type(primo_errore).__name__)
                log_message(
                    session_id,
                    f"🔄 ({indice}/{totale}) {nome_file}: nessuna risposta utile dal modello "
                    f"({type(primo_errore).__name__}). Riprovo con la documentazione sintetizzata...",
                )
                if tracker is not None:
                    # Il tentativo fallito ha comunque consumato token: va
                    # contabilizzato, altrimenti l'addebito risulta piu' basso
                    # del consumo reale.
                    tracker.aggiungi_crew(dev_crew)
                impl_tasks, dev_crew, annuncia_avvio = _costruisci_crew(alleggerito=True)
                annuncia_avvio()
                dev_crew.kickoff()

            if tracker is not None:
                tracker.aggiungi_crew(dev_crew)
                _salva_consumo_parziale(session_id, tracker)

            # Accesso NOMINATO agli output: niente più tasks[0]/tasks[1]
            output_backend = _task_output_text(impl_tasks.backend)
            output_frontend = _task_output_text(impl_tasks.frontend)

            # Questi due file NON passano da _salva_output_su_disco: il
            # mascheramento va applicato anche qui, o le credenziali del
            # sorgente ricompaiono nel codice generato.
            output_backend, _n1 = maschera_segreti(output_backend, valori_noti=_SEGRETI_SESSIONE)
            output_frontend, _n2 = maschera_segreti(output_frontend, valori_noti=_SEGRETI_SESSIONE)
            if _n1 or _n2:
                logger.info("%s: rimossi %d possibili segreti dal codice generato.",
                            nome_file, _n1 + _n2)

            # Percorsi ricondotti a una struttura unica: il backend non puo'
            # finire in due radici diverse, i test devono stare sotto tests/,
            # e Dtos/ e DTOs/ non possono coesistere.
            output_backend, _p1 = _normalizza_percorsi(output_backend, registro_cartelle, e_frontend=False)
            output_frontend, _p2 = _normalizza_percorsi(output_frontend, registro_cartelle, e_frontend=True)
            if _p1 or _p2:
                logger.info("%s: normalizzati %d percorsi di file.", nome_file, _p1 + _p2)

            # Rete di sicurezza per il resume: un file scritto da una versione
            # precedente (o da una run interrotta) puo' esistere senza
            # intestazione. Si aggiunge qui, una volta sola.
            for percorso in (percorso_backend, percorso_frontend):
                if "CodeMorph" not in _read_if_exists(percorso, "")[:400]:
                    esistente = _read_if_exists(percorso, "")
                    with open(percorso, "w", encoding="utf-8") as f:
                        f.write(_intestazione_documento(percorso, _nome_progetto(session_id)))
                        f.write(esistente)

            with open(percorso_backend, "a", encoding="utf-8") as f:
                f.write(f"\n\n<!-- ===== ORIGINE LEGACY: {nome_file} ===== -->\n\n")
                f.write(output_backend)

            with open(percorso_frontend, "a", encoding="utf-8") as f:
                f.write(f"\n\n<!-- ===== ORIGINE LEGACY: {nome_file} ===== -->\n\n")
                f.write(output_frontend)

            # Checkpoint SOLO dopo la scrittura riuscita su entrambi i file
            processati.add(nome_file)
            _save_checkpoint(percorso_checkpoint, processati)

            # Il checkpoint su disco non basta: il file system di Render e'
            # effimero e il backup di fase avviene solo alla FINE. Su 200 file
            # un riavvio a meta' azzerava ore di lavoro e il credito consumato.
            # Qui si salvano su Storage checkpoint e documenti (poche decine di
            # KB) dopo OGNI file: un'interruzione costa un file, non duecento.
            try:
                import storage
                storage.salva_progresso_fase3(session_id, output_dir)
            except Exception as e:
                logger.warning("Progresso Fase 3 non salvato su Storage: %s", e)

            # Tipi e progetti appena prodotti diventano contesto per i file
            # successivi: e' cosi' che il primo file guida tutti gli altri.
            tipi_generati |= _tipi_dichiarati(output_backend)
            tipi_generati |= _tipi_dichiarati(output_frontend)
            progetti_generati |= _progetti_dichiarati(output_backend)
            progetti_generati |= _progetti_dichiarati(output_frontend)

            # Avviso una volta sola, quando la soglia viene superata: oltre
            # questo numero la solution non e' piu' apribile senza riordinarla,
            # e il numero di progetti NON deve crescere con i file migrati.
            if len(progetti_generati) == MAX_PROGETTI_ATTESI + 1:
                log_message(
                    session_id,
                    f"⚠️ La solution ha superato i {MAX_PROGETTI_ATTESI} progetti attesi "
                    f"({len(progetti_generati)}): gli agenti stanno creando una struttura "
                    "per funzionalita' invece che per strato. Il codice resta valido, "
                    "ma la struttura andra' riorganizzata.",
                )
                logger.warning("Proliferazione progetti: %s", sorted(progetti_generati))

            esiti["completati"].append(nome_file)
            log_message(
                session_id,
                f"✅ ({indice}/{totale}) {nome_file} migrato e salvato nei file di implementazione.",
            )
            if DELAY_TRA_FILE_SEC:
                time.sleep(DELAY_TRA_FILE_SEC)


        except Exception as e:
            # Un fallimento su un file (rate limit, timeout, errore LLM) non deve
            # bruciare il lavoro fatto sugli altri: logga e prosegui.
            logger.exception("Errore durante la migrazione di %s — proseguo.", nome_file)
            # Il tipo dell'eccezione va nel log LIVE, non solo in quello server:
            # senza, un errore banale dopo il salvataggio sembra un fallimento
            # della migrazione (e' cosi' che un AttributeError su time.sleep e'
            # rimasto nascosto marcando come "falliti" file gia' completi).
            gia_salvato = nome_file in processati
            nota = " (il file era gia' stato salvato)" if gia_salvato else ""
            log_message(
                session_id,
                f"❌ Migrazione di {nome_file} fallita — {type(e).__name__}: {e}{nota} "
                f"— proseguo con il file successivo.",
            )
            # Se il salvataggio era andato a buon fine, il file NON e' fallito:
            # marcarlo tale falserebbe il riepilogo finale.
            if gia_salvato:
                if nome_file not in esiti["completati"]:
                    esiti["completati"].append(nome_file)
            else:
                esiti["falliti"].append(nome_file)


    # 4. QUALITY CHECK finale, a blocchi per non saturare la context window
    codice_backend = _read_if_exists(percorso_backend, "")
    codice_frontend = _read_if_exists(percorso_frontend, "")
    codice_completo = (
        f"===== CODICE BACKEND GENERATO =====\n{codice_backend}\n\n"
        f"===== CODICE FRONTEND GENERATO =====\n{codice_frontend}"
    )

    chunks = _chunk_text(codice_completo, QA_CHUNK_MAX_CHARS)
    report_qa = []
    if len(chunks) > QA_MAX_CHUNK_ATTESI:
        # Oltre questa soglia l'analisi si frammenta: il revisore vede porzioni
        # di file senza il contesto delle altre e segnala falsi positivi, mentre
        # nessuno guarda le interazioni fra parti. Il numero alto di parti e'
        # quasi sempre sintomo di output sproporzionato, non di un progetto
        # grande: va detto a chi sta pagando le chiamate.
        log_message(
            session_id,
            f"⚠️ Il codice generato ({len(codice_completo):,} caratteri) richiede "
            f"{len(chunks)} analisi separate: l'esito sara' frammentato. "
            "Verifica che gli agenti non stiano duplicando lo stesso codice."
            .replace(",", "."),
        )
    log_message(
        session_id,
        "🕵️ Avvio Quality Check (OWASP/SonarQube) sul codice generato"
        + (f" in {len(chunks)} parti..." if len(chunks) > 1 else "..."),
    )

    for i, chunk in enumerate(chunks, start=1):
        etichetta = f"parte {i}/{len(chunks)}" if len(chunks) > 1 else ""
        nome_report = (
            FILE_QUALITY_REPORT
            if len(chunks) == 1
            else FILE_QUALITY_REPORT.replace(".md", f"_part{i}.md")
        )

        qa_tasks = get_quality_check_task(
            agents=agents,
            output_dir=output_dir,
            codice_da_analizzare=chunk,
            chunk_label=etichetta,
            output_filename=nome_report,
        )
        annuncia_qa, callback_qa = crea_logger_attivita(
            session_id, qa_tasks, etichetta=f"Quality Check {etichetta}".strip()
        )
        def step_con_stop(step):
            interruzione.verifica_stop(session_id)   # solleva se richiesto

        qa_crew = Crew(
            agents=[agents["security_quality_reviewer"]],
            tasks=qa_tasks,
            process=Process.sequential,
            verbose=False,
            memory=False,
            step_callback=step_con_stop,
            task_callback=callback_qa,
        )

        try:
            annuncia_qa()
            risultato = qa_crew.kickoff()
            _salva_output_su_disco(qa_tasks, output_dir, _nome_progetto(session_id))
            if tracker is not None:
                tracker.aggiungi_crew(qa_crew, risultato)
                _salva_consumo_parziale(session_id, tracker)
            report_qa.append(getattr(risultato, "raw", None) or str(risultato))
        except Exception:
            logger.exception("Errore durante il Quality Check (%s).", etichetta or "unico")
            log_message(session_id, f"❌ Quality Check fallito ({etichetta or 'unico'}).")
            report_qa.append(f"QUALITY CHECK FALLITO ({etichetta or 'unico'})")

    esiti["quality_report"] = "\n\n---\n\n".join(report_qa)

    # Rilievi per gravita': il Quality Check li produce come testo, quindi si
    # contano qui una volta sola, prima che il dato diventi solo markdown.
    # Il conteggio e' per righe, non per occorrenze: una riga di tabella o un
    # titolo valgono un rilievo, la stessa parola ripetuta in un paragrafo no.
    testo_qa = esiti["quality_report"] or ""
    critici = alti = 0
    for riga in testo_qa.splitlines():
        maiuscola = riga.upper()
        if "CRITICO" in maiuscola or "CRITICAL" in maiuscola:
            critici += 1
        elif "ALTO" in maiuscola or "ALTA" in maiuscola or "HIGH" in maiuscola:
            alti += 1
    _salva_metriche(
        session_id,
        rilievi_critici=critici,
        rilievi_alti=alti,
        rilievi_totali=critici + alti,
    )

    log_message(
        session_id,
        f"📈 Fase 3: pipeline completata — {len(esiti['completati'])} migrati, "
        f"{len(esiti['falliti'])} falliti, {len(esiti['saltati'])} saltati.",
    )
    if quality_gate:
        validazione = _valida_fase(
            llm, output_dir, "Fase 3 · Implementation",
            [FILE_BACKEND_IMPL, FILE_FRONTEND_IMPL, FILE_QUALITY_REPORT],
            FILE_VALIDATION_FASE3, session_id, tracker,
        )
        if validazione:
            esiti["validazione"] = validazione["esito"]
    return esiti


def _estratto_per_validatore(contenuto, max_char):
    """
    Riduce un documento per il Quality Gate SENZA farlo sembrare difettoso.

    Tagliare a carattere fisso spezza i blocchi di codice a meta': il validatore
    vede un ```csharp mai chiuso e segnala il documento come "troncato e non
    compilabile" — un difetto che esiste solo nel suo input, non su disco.
    E' successo davvero: un backend da 68k e' stato bocciato per un troncamento
    prodotto dal troncamento stesso.

    Qui il taglio cade sull'ultima chiusura di blocco utile, e l'avviso e'
    ripetuto IN TESTA oltre che in coda: in fondo a decine di migliaia di
    caratteri di codice una nota finale non regge contro l'evidenza visiva
    di una sintassi interrotta.
    """
    if not contenuto or len(contenuto) <= max_char:
        return contenuto

    testa = contenuto[:max_char]

    # Ultimo blocco di codice COMPLETO: si taglia li', mai a meta' listato.
    chiusure = [m.end() for m in re.finditer(r"(?m)^```\s*$", testa)]
    if chiusure and chiusure[-1] > max_char * 0.5:
        testa = testa[:chiusure[-1]]
    else:
        # Nessun blocco: si ripiega sull'ultimo paragrafo intero.
        ultimo = testa.rfind("\n\n")
        if ultimo > max_char * 0.5:
            testa = testa[:ultimo]

    avviso = (
        "> ATTENZIONE AL VALIDATORE: quello che segue e' un ESTRATTO PARZIALE.\n"
        "> Il documento su disco e' COMPLETO e integro: la parte non riportata\n"
        "> e' stata omessa solo per limiti di contesto di questa validazione.\n"
        "> NON segnalare il documento come troncato, incompleto o non\n"
        "> compilabile: sarebbe un rilievo sul mio estratto, non sul deliverable.\n\n"
    )
    coda = "\n\n[...fine dell'estratto. Il documento originale prosegue ed e' completo su disco.]"
    return avviso + testa + coda


def _valida_fase(llm, output_dir, nome_fase, file_da_validare, nome_report,
                 session_id=None, tracker=None):
    """
    Quality Gate: un revisore indipendente valuta i documenti appena prodotti
    ed emette un verdetto motivato, salvato come report nella cartella di sessione.
    NON blocca la pipeline e non fa rifare il lavoro: documenta.
    Ritorna {"esito": ..., "report": ...} oppure None se non c'è nulla da validare.
    """
    documenti = []
    for nome in file_da_validare:
        contenuto = _read_if_exists(f"{output_dir}/{nome}", "")
        if not contenuto:
            continue
        # Quota EQUA per documento: senza questo il primo documento lungo
        # consuma tutto il budget e gli altri non arrivano affatto al gate,
        # che li segnala come "mancanti" pur essendo completi su disco.
        contenuto = _estratto_per_validatore(contenuto, VALIDAZIONE_MAX_CHARS_PER_DOC)
        documenti.append(f"### {nome}\n{contenuto}")

    if not documenti:
        logger.warning("Validazione %s saltata: nessun documento trovato.", nome_fase)
        return None

    contenuto_fase = "\n\n".join(documenti)
    if len(contenuto_fase) > VALIDAZIONE_MAX_CHARS:
        contenuto_fase = _estratto_per_validatore(contenuto_fase, VALIDAZIONE_MAX_CHARS)

    agents = create_agents(llm)
    tasks = get_validation_task(
        agent=agents["quality_gate_auditor"],
        output_dir=output_dir,
        nome_fase=nome_fase,
        output_filename=nome_report,
    )
    annuncia, callback = crea_logger_attivita(
        session_id, tasks, etichetta=f"Quality Gate · {nome_fase}"
    )
    def step_con_stop(step):
            interruzione.verifica_stop(session_id)   # solleva se richiesto

    crew = Crew(
        agents=[agents["quality_gate_auditor"]],
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
        memory=False,
        step_callback=step_con_stop,
        task_callback=callback,
    )

    try:
        log_message(session_id, f"🔎 Quality Gate: validazione dei documenti della {nome_fase} in corso...")
        annuncia()
        # Il contenuto passa da inputs: mai concatenato nella description
        risultato = crew.kickoff(inputs={"contenuto_fase": contenuto_fase})
        _salva_output_su_disco(tasks, output_dir, _nome_progetto(session_id))
        if tracker is not None:
            tracker.aggiungi_crew(crew, risultato)
            _salva_consumo_parziale(session_id, tracker)

        report = _pulisci_output(getattr(risultato, "raw", None) or str(risultato))
        prima_riga = report.strip().splitlines()[0] if report.strip() else ""
        esito = prima_riga.replace("ESITO:", "").strip() if "ESITO:" in prima_riga.upper() else "NON DETERMINATO"
        log_message(session_id, f"🔎 Quality Gate {nome_fase}: {esito}")
        return {"esito": esito, "report": report}
    except Exception:
        # La validazione è un di più: se fallisce, la pipeline prosegue
        logger.exception("Quality Gate fallito per la %s.", nome_fase)
        log_message(session_id, f"⚠️ Quality Gate {nome_fase} non riuscito: la fase resta valida, verifica manualmente i documenti.")
        return None