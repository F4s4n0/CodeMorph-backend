"""
Mascheramento dei segreti nei deliverable generati.

Un'analisi di sicurezza corretta SEGNALA una credenziale hardcoded, non la
ricopia: senza questo filtro il valore reale finisce nei documenti scaricabili
dal cliente (in una sessione di test la stessa password e' comparsa 16 volte
su 6 file diversi, perche' ogni agente la citava a supporto del proprio
rilievo).

Il filtro NON impedisce agli agenti di segnalare il problema: sostituisce solo
il valore, lasciando intatto il contesto ("password hardcoded in CheckDB.cs"
resta, il valore diventa ***). Chi possiede il codice sorgente sa comunque
qual e': il documento non deve essere un secondo posto in cui il segreto vive.

Nota sui limiti: le espressioni regolari riconoscono le forme piu' comuni
(connection string, assegnazioni, chiavi API note). Un segreto scritto in un
formato inatteso puo' sfuggire, quindi questo e' un livello di riduzione del
rischio, non una garanzia.
"""

import logging
import re

logger = logging.getLogger(__name__)

MASCHERA = "***RIMOSSO_DA_CODEMORPH***"

# Ogni voce: (regex, gruppo_da_mascherare, descrizione)
# Le regex catturano il PREFISSO nel gruppo 1 e il VALORE nel gruppo 2, cosi'
# la sostituzione conserva la forma originale e resta leggibile.
_REGOLE = [
    # Connection string: Password=xxx; / Pwd=xxx;
    (re.compile(r"((?:password|pwd)\s*=\s*)([^;\"'\s,\)\]}]{3,})", re.IGNORECASE),
     "password in connection string"),
    # Assegnazioni in codice: password: "xxx" / Password = "xxx"
    (re.compile(r"((?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
                r"access[_-]?key|private[_-]?key|client[_-]?secret)\s*[:=]\s*[\"'])"
                r"([^\"']{3,})([\"'])", re.IGNORECASE),
     "assegnazione di credenziale"),
    # Chiavi note con prefisso riconoscibile
    (re.compile(r"(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{20,})"),
     "chiave API con prefisso noto"),
    # JWT in chiaro
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "token JWT"),
    # URL con credenziali incorporate: schema://utente:password@host
    (re.compile(r"(://[^\s:/@]+:)([^\s@]{3,})(@)"),
     "credenziali nell'URL"),
    # Valore fra backtick vicino a una parola-chiave di credenziale, sulla
    # stessa riga. E' la forma con cui gli agenti espongono i segreti nelle
    # TABELLE markdown dell'inventario:
    #   | **Password SQL** | `valorevero` | CheckDB.cs | CRITICO |
    (re.compile(r"((?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
                r"credenzial\w*|chiave)[^\n`]{0,80}?`)([^`\n]{3,})(`)", re.IGNORECASE),
     "valore fra backtick accanto a parola-chiave di credenziale"),
]

# Valori che NON sono segreti veri: mascherarli renderebbe il documento
# illeggibile senza alcun guadagno.
_PLACEHOLDER = {
    "password", "yourpassword", "your_password", "changeme", "esempio", "example",
    "xxx", "xxxx", "***", "placeholder", "tuapassword", "secret", "mysecret",
    "null", "none", "true", "false", "stringa", "value", "valore", "test",
    MASCHERA.lower(),
}


def _e_placeholder(valore):
    v = (valore or "").strip().lower()
    if v in _PLACEHOLDER:
        return True
    # Segnaposto tipo {{PASSWORD}}, ${DB_PASS}, <inserire password>, %PWD%.
    # Basta che INIZI con un delimitatore di template: la regex del chiamante
    # puo' aver troncato la chiusura (es. cattura '{{PASSWORD' senza '}}').
    if re.match(r"^[\{\$<%\[]", v):
        return True
    # Riferimento a variabile d'ambiente: non e' il valore
    if re.fullmatch(r"(os\.)?(getenv|environ).*", v):
        return True
    # Nomi di variabile o placeholder descrittivi, non valori veri
    if re.fullmatch(r"[a-z][a-z0-9_]*(password|pwd|secret|key|token)[a-z0-9_]*", v):
        return True
    # Gia' mascherato in un passaggio precedente
    if "rimosso_da_codemorph" in v or set(v) == {"*"}:
        return True
    return False


def _varianti_segreto(valore):
    """
    Forme alternative sotto cui un segreto puo' ricomparire negli output.

    Gli agenti non sempre copiano il valore alla lettera: dentro i diagrammi
    Mermaid il carattere '$' rompe la sintassi, e una password come
    'abc123XYZ$' e' stata riscritta come 'abc123XYZ dollar'. La sostituzione
    letterale non trovava piu' corrispondenza e il segreto sopravviveva.

    Si generano quindi anche: il nucleo alfanumerico (se abbastanza lungo da
    non essere ambiguo) e le versioni con i simboli finali sostituiti dal loro
    nome. Sotto gli 8 caratteri il nucleo NON viene usato: il rischio di
    cancellare parole innocue dai documenti supererebbe il beneficio.
    """
    varianti = {valore}

    nucleo = re.sub(r"[^A-Za-z0-9]", "", valore)
    if len(nucleo) >= 8 and nucleo != valore:
        varianti.add(nucleo)

    # Simbolo finale scritto per esteso ('$' -> ' dollar', '_' -> ' underscore')
    nomi = {
        "$": ["dollar", "dollaro"], "!": ["exclamation"], "#": ["hash", "cancelletto"],
        "%": ["percent", "percento"], "&": ["ampersand", "e commerciale"],
        "@": ["at", "chiocciola"], "*": ["asterisk", "asterisco"],
    }
    for simbolo, parole in nomi.items():
        if simbolo in valore:
            base = valore.replace(simbolo, "")
            for parola in parole:
                varianti.add(f"{base} {parola}")
                varianti.add(f"{base}{parola}")
                varianti.add(valore.replace(simbolo, f" {parola}"))
                varianti.add(valore.replace(simbolo, parola))

    return varianti


def estrai_segreti_da_sorgenti(testo):
    """
    Valori letterali di credenziali presenti nel CODICE SORGENTE caricato.

    E' la parte deterministica del filtro, e la piu' affidabile: una volta noto
    il valore reale lo si puo' rimuovere ovunque compaia negli output, anche in
    forme che nessuna regex generica intercetterebbe (nei test reali il segreto
    e' sopravvissuto dentro i diagrammi Mermaid come "utente / valore", e
    perfino nella raccomandazione a rimuoverlo).

    Restituisce un set di stringhe da trattare come segrete.
    """
    trovati = set()
    if not testo:
        return trovati

    estrattori = [
        re.compile(r"(?:password|pwd)\s*=\s*([^;\"'\s,\)\]}]{4,})", re.IGNORECASE),
        re.compile(r"(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
                   r"access[_-]?key|client[_-]?secret)\s*[:=]\s*[\"']([^\"']{4,})[\"']",
                   re.IGNORECASE),
        re.compile(r"://[^\s:/@]+:([^\s@]{4,})@"),
    ]
    for regex in estrattori:
        for valore in regex.findall(testo):
            valore = valore.strip()
            # Sotto i 4 caratteri il rischio di rimuovere testo innocuo dai
            # documenti supera il beneficio.
            if len(valore) >= 4 and not _e_placeholder(valore):
                trovati.update(_varianti_segreto(valore))

    if trovati:
        logger.info("Individuati %d valori di credenziale nei sorgenti: "
                    "saranno rimossi dai deliverable.", len(trovati))
    return trovati


def maschera_segreti(testo, valori_noti=None):
    """
    Restituisce (testo_mascherato, numero_sostituzioni).

    `valori_noti` sono i segreti estratti dai sorgenti con
    estrai_segreti_da_sorgenti(): vengono rimossi per corrispondenza esatta,
    prima delle regole generiche.

    Non solleva mai: se il filtro fallisce si preferisce restituire il testo
    originale piuttosto che perdere il deliverable. Il chiamante logga il
    conteggio, cosi' un fallimento silenzioso resta visibile.
    """
    if not testo:
        return testo, 0

    try:
        totale = 0
        risultato = testo

        # 1. Valori noti: sostituzione letterale, dal piu' lungo al piu' corto
        #    per non spezzare un segreto contenuto in un altro.
        for valore in sorted(valori_noti or (), key=len, reverse=True):
            if valore and valore in risultato:
                totale += risultato.count(valore)
                risultato = risultato.replace(valore, MASCHERA)

        # 2. Regole generiche: intercettano cio' che non era nei sorgenti
        #    (segreti inventati dagli agenti, esempi di configurazione).
        for regex, descrizione in _REGOLE:
            def _sostituisci(m):
                nonlocal totale
                gruppi = m.groups()
                # Regola a gruppo singolo: l'intero match e' il segreto
                if len(gruppi) <= 1:
                    totale += 1
                    return MASCHERA
                valore = gruppi[1]
                if _e_placeholder(valore):
                    return m.group(0)
                totale += 1
                coda = gruppi[2] if len(gruppi) > 2 else ""
                return f"{gruppi[0]}{MASCHERA}{coda}"

            risultato = regex.sub(_sostituisci, risultato)

        if totale:
            logger.info("Mascherati %d possibili segreti nel documento.", totale)
        return risultato, totale

    except Exception as e:
        logger.error("Mascheramento segreti fallito (%s: %s): documento lasciato invariato.",
                     type(e).__name__, e)
        return testo, 0
