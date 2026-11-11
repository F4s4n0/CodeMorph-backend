"""
Mascheramento delle chiavi API in tutto cio' che finisce nei log.

Perche' esiste questo modulo: Google riceve la chiave API in query string
(?key=...), e httpx registra l'URL completo di ogni richiesta. Il filtro
precedente, in main.py, non bastava per due motivi, entrambi riprodotti:

1. httpx passa l'URL al logger come OGGETTO `httpx.URL`, non come stringa.
   Il filtro mascherava solo gli argomenti di tipo `str`, quindi l'URL - con
   la chiave dentro - passava intatto a OGNI richiesta.
2. Quando una chiamata fallisce, l'URL compare nel testo dell'eccezione
   ("... for url 'https://...?key=...'"). Il traceback viene composto dal
   formatter DOPO i filtri, a partire da `exc_info`: il filtro non lo vedeva.

Qui il mascheramento avviene alla CREAZIONE di ogni record di log (record
factory), quindi vale per qualunque logger e qualunque handler, anche quelli
installati dopo l'avvio, e copre messaggio, argomenti e traceback.

Oltre ai parametri in URL si mascherano i VALORI delle chiavi configurate
nell'ambiente, ovunque compaiano: e' la difesa che non dipende dalla forma
in cui la chiave viene scritta.
"""

import logging
import os
import re

MASCHERA = "***"

# Variabili d'ambiente il cui VALORE non deve mai comparire in un log.
_VARIABILI_SEGRETE = (
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWT_SECRET",
    "PAYPAL_CLIENT_SECRET",
)

# Sotto questa lunghezza un valore non e' una chiave credibile, e sostituirlo
# rischierebbe di mascherare testo normale.
_LUNGHEZZA_MINIMA = 12

_PATTERN = (
    # Parametri in query string: ?key=..., &api_key=..., &token=...
    (re.compile(r"([?&](?:key|api_key|apikey|access_token|token)=)[^&\s\"'<>]+",
                re.IGNORECASE), r"\1" + MASCHERA),
    # Intestazioni di autenticazione riportate in un messaggio o in un errore
    (re.compile(r"((?:x-goog-api-key|x-api-key|api-key)['\"]?\s*[:=]\s*['\"]?)"
                r"[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE), r"\1" + MASCHERA),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{20,}"), r"\1" + MASCHERA),
    # Forme riconoscibili delle chiavi dei provider, anche fuori da un URL
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), MASCHERA),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), MASCHERA),
)


def _valori_segreti():
    # Letti a ogni chiamata: il .env puo' essere caricato dopo l'import di
    # questo modulo, e costa pochi microsecondi.
    valori = []
    for nome in _VARIABILI_SEGRETE:
        valore = (os.environ.get(nome) or "").strip()
        if len(valore) >= _LUNGHEZZA_MINIMA:
            valori.append(valore)
    return valori


def maschera_testo(testo):
    """Restituisce `testo` con chiavi e token sostituiti da ***."""
    if not testo:
        return testo
    risultato = str(testo)
    for valore in _valori_segreti():
        if valore in risultato:
            risultato = risultato.replace(valore, MASCHERA)
    for pattern, sostituto in _PATTERN:
        risultato = pattern.sub(sostituto, risultato)
    return risultato


def _maschera_argomento(arg):
    """
    Maschera un argomento del record lasciandone intatto il tipo quando non
    contiene nulla da nascondere: i numeri devono restare numeri (%d), e
    alcuni formatter (uvicorn.access) leggono gli argomenti per posizione.
    """
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    if isinstance(arg, str):
        return maschera_testo(arg)
    if isinstance(arg, (dict, list, tuple, set)):
        # Contenitori: si interviene solo se la loro rappresentazione
        # contiene davvero un segreto, per non alterarne il tipo inutilmente.
        testo = str(arg)
        mascherato = maschera_testo(testo)
        return mascherato if mascherato != testo else arg
    # Oggetti (es. httpx.URL): contano per come vengono STAMPATI.
    testo = str(arg)
    mascherato = maschera_testo(testo)
    return mascherato if mascherato != testo else arg


_formatter_traceback = logging.Formatter()


def maschera_record(record):
    """Maschera messaggio, argomenti e traceback di un record. Non solleva mai."""
    try:
        if isinstance(record.msg, str):
            record.msg = maschera_testo(record.msg)
        else:
            testo = str(record.msg)
            mascherato = maschera_testo(testo)
            if mascherato != testo:
                record.msg = mascherato

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _maschera_argomento(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_maschera_argomento(a) for a in record.args)

        # Il traceback viene composto dal formatter a partire da exc_info, ma
        # solo se exc_text e' vuoto: lo componiamo qui, gia' mascherato.
        if record.exc_info and not record.exc_text:
            if isinstance(record.exc_info, tuple) and record.exc_info[0] is not None:
                record.exc_text = _formatter_traceback.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = maschera_testo(record.exc_text)
        if record.stack_info:
            record.stack_info = maschera_testo(record.stack_info)
    except Exception:
        pass          # il logging non deve mai far fallire una richiesta
    return record


class MascheraChiaviURL(logging.Filter):
    """Seconda linea di difesa, per handler che ricevono record gia' creati."""

    def filter(self, record):
        maschera_record(record)
        return True


_installata = False


def installa_maschera_log():
    """
    Attiva il mascheramento per TUTTI i log del processo. Idempotente.

    La record factory e' il punto in cui nasce ogni record, qualunque sia il
    logger: a differenza dei filtri non dipende da quali handler esistono al
    momento della chiamata, ne' dalla propagazione fra logger padre e figlio.
    """
    global _installata
    if not _installata:
        factory_precedente = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            return maschera_record(factory_precedente(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _installata = True

    # I filtri sugli handler restano: coprono i record costruiti senza
    # passare dalla factory (logging.makeLogRecord) o modificati dopo.
    filtro = MascheraChiaviURL()

    def _aggiungi(destinazione):
        if not any(isinstance(f, MascheraChiaviURL) for f in destinazione.filters):
            destinazione.addFilter(filtro)

    root = logging.getLogger()
    for handler in root.handlers:
        _aggiungi(handler)
    for nome in ("httpx", "httpcore", "uvicorn", "uvicorn.error",
                 "uvicorn.access", "LiteLLM", "litellm"):
        for handler in logging.getLogger(nome).handlers:
            _aggiungi(handler)
