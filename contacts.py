"""
Richieste di contatto.

L'endpoint è PUBBLICO (il form è raggiungibile anche da chi non è
registrato), quindi ha tre difese contro gli abusi:
  1. validazione stringente dei campi
  2. campo honeypot: i bot compilano tutto, gli umani non lo vedono
  3. rate limit per IP in memoria

Il salvataggio usa il client Supabase con service role, che bypassa RLS:
la tabella resta inaccessibile dall'esterno.
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import supabase

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_RICHIESTE_PER_IP = 3          # invii consentiti...
FINESTRA_SECONDI = 3600           # ...in un'ora
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_invii_recenti = {}               # ip -> [timestamp, ...]
_lock = threading.Lock()


def _rate_limit_superato(ip: str) -> bool:
    """True se questo IP ha già inviato troppe richieste nell'ultima ora."""
    adesso = time.time()
    with _lock:
        recenti = [t for t in _invii_recenti.get(ip, []) if adesso - t < FINESTRA_SECONDI]
        if len(recenti) >= MAX_RICHIESTE_PER_IP:
            _invii_recenti[ip] = recenti
            return True
        recenti.append(adesso)
        _invii_recenti[ip] = recenti
        # Pulizia occasionale per non far crescere il dizionario all'infinito
        if len(_invii_recenti) > 500:
            for chiave in [k for k, v in _invii_recenti.items()
                           if not any(adesso - t < FINESTRA_SECONDI for t in v)]:
                _invii_recenti.pop(chiave, None)
        return False


class InputContatto(BaseModel):
    name: str
    email: str
    message: str
    origine: Optional[str] = "landing"
    # Honeypot: campo nascosto via CSS, un umano non lo compila mai
    azienda_extra: Optional[str] = None


@router.post("/api/v1/contatti")
def crea_richiesta_contatto(dati: InputContatto, request: Request):
    """Registra una richiesta di contatto. Endpoint pubblico."""

    # 1. Honeypot: risposta di successo finta, così il bot non capisce
    if dati.azienda_extra:
        logger.info("Richiesta di contatto scartata (honeypot compilato).")
        return {"status": "success"}

    # 2. Rate limit per IP
    ip = request.client.host if request.client else "sconosciuto"
    if _rate_limit_superato(ip):
        raise HTTPException(
            status_code=429,
            detail="Hai già inviato diverse richieste. Riprova più tardi o scrivici direttamente via email.",
        )

    # 3. Validazione
    nome = (dati.name or "").strip()
    email = (dati.email or "").strip().lower()
    messaggio = (dati.message or "").strip()

    if len(nome) < 2 or len(nome) > 120:
        raise HTTPException(status_code=400, detail="Indica il tuo nome completo.")
    if not _EMAIL_RE.match(email) or len(email) > 200:
        raise HTTPException(status_code=400, detail="L'indirizzo email non sembra valido.")
    if len(messaggio) < 10:
        raise HTTPException(status_code=400, detail="Descrivi la tua richiesta in almeno qualche parola.")
    if len(messaggio) > 5000:
        raise HTTPException(status_code=400, detail="Il messaggio è troppo lungo (massimo 5000 caratteri).")

    try:
        supabase.table("contact_requests").insert({
            "name": nome,
            "email": email,
            "message": messaggio,
            "status": "nuova",
            "origine": dati.origine or "landing",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error("Salvataggio richiesta di contatto fallito: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Non siamo riusciti a registrare la richiesta: riprova tra poco.",
        )

    logger.info("Nuova richiesta di contatto da %s (%s)", nome, email)
    return {"status": "success"}
