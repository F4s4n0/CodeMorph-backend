import logging
import os
from datetime import datetime, timezone

import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client

# Carica le variabili dal file .env PRIMA di leggerle
load_dotenv()

logger = logging.getLogger(__name__)

# --- Bootstrap configurazione: fallire subito e con un messaggio chiaro ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")

_mancanti = [
    nome
    for nome, valore in (
        ("SUPABASE_URL", SUPABASE_URL),
        ("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY),
        ("SUPABASE_JWT_SECRET", SUPABASE_JWT_SECRET),
    )
    if not valore
]
if _mancanti:
    # Senza questo check, create_client(None, ...) esplode più avanti
    # con un errore criptico difficile da diagnosticare in produzione.
    raise RuntimeError(
        f"Configurazione incompleta: variabili d'ambiente mancanti: {', '.join(_mancanti)}. "
        "Verifica il file .env."
    )

# Client Supabase per le query al DB (usa la service role key: solo lato server!)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

security = HTTPBearer()


def _parse_expiry(expires_at_str):
    """
    Converte il timestamp ISO di Supabase in datetime timezone-aware.
    Se il timestamp è naive (senza fuso), lo assume UTC — che è quello
    che Supabase usa internamente — invece di confrontare date ambigue.
    """
    expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> str:
    """
    Autentica il JWT di Supabase e restituisce lo user_id SENZA verificare
    la licenza. Serve agli endpoint di pagamento: un utente senza licenza
    (o con pass scaduto) deve comunque poter acquistare il pass giornaliero
    o ricaricare i token — col controllo licenza sarebbe un circolo vizioso.
    """
    token = credentials.credentials
    try:
        # 1. Decodifica del JWT generato da Supabase Auth
        # TODO: valutare options={"verify_aud": True} con audience="authenticated"
        # (l'aud standard dei token Supabase) per una verifica più stretta.
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessione scaduta. Effettua nuovamente il login.")
    except jwt.InvalidTokenError as e:
        # Il dettaglio tecnico va SOLO nei log server: esporlo al client
        # (com'era prima, insieme alla lunghezza del secret) regala a un
        # attaccante informazioni utili a forgiare token.
        logger.warning("Token JWT non valido: %s", e)
        raise HTTPException(status_code=401, detail="Token di autenticazione non valido.")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token non valido: user_id mancante.")

    return user_id


def get_current_user_and_validate_license(user_id: str = Depends(get_current_user)):
    # 2. Controllo licenza sul database Supabase.
    #    Ordiniamo per scadenza decrescente: se l'utente ha rinnovato più volte
    #    e ha più righe, conta la licenza PIÙ RECENTE, non la prima trovata.
    try:
        response = (
            supabase.table("user_licenses")
            .select("expires_at")
            .eq("user_id", user_id)
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error("Errore query licenze per utente %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Servizio licenze temporaneamente non disponibile.")

    if not response.data:
        raise HTTPException(
            status_code=402,
            detail="Nessuna licenza trovata per questo account. Acquista un pass giornaliero.",
        )

    try:
        expires_at = _parse_expiry(response.data[0]["expires_at"])
    except (ValueError, TypeError, KeyError) as e:
        logger.error("Timestamp licenza malformato per utente %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Dati licenza non validi. Contatta il supporto.")

    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(
            status_code=402,
            detail="La tua licenza giornaliera è scaduta. Rinnovala per continuare a usare gli agenti.",
        )

    # Tutto ok: restituisce l'ID dell'utente autenticato e autorizzato
    return user_id
