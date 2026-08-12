import logging
import os
import threading
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

# --- Cache delle licenze valide -------------------------------------------
# Il frontend interroga gli endpoint protetti ogni pochi secondi durante le
# elaborazioni: senza cache, ogni polling e' una query a Supabase. Su piani
# con poche risorse questo satura le connessioni proprio mentre e' in corso
# una chiamata LLM, e la verifica fallisce con [Errno 11] bloccando endpoint
# di sola lettura come i log.
#
# La licenza e' un pass GIORNALIERO: rileggerla ogni minuto e' abbondante.
_cache_licenze = {}                  # user_id -> (scadenza_licenza, verificata_at)
_cache_lock = threading.Lock()
TTL_CACHE_LICENZA = 60               # secondi prima di riverificare su DB
TOLLERANZA_DB_NON_RAGGIUNGIBILE = 900   # 15 min di grazia se il DB non risponde


def _leggi_cache(user_id):
    with _cache_lock:
        return _cache_licenze.get(user_id)


def _scrivi_cache(user_id, scadenza):
    with _cache_lock:
        _cache_licenze[user_id] = (scadenza, datetime.now(timezone.utc))


def invalida_cache_licenza(user_id=None):
    """
    Da chiamare dopo un acquisto o un rinnovo, altrimenti l'utente resta
    bloccato fino alla scadenza del TTL pur avendo appena pagato.
    """
    with _cache_lock:
        if user_id is None:
            _cache_licenze.clear()
        else:
            _cache_licenze.pop(user_id, None)


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
    adesso = datetime.now(timezone.utc)

    # 1. Cache: se la licenza e' stata verificata di recente ed e' ancora
    #    valida, si evita del tutto la query.
    in_cache = _leggi_cache(user_id)
    if in_cache:
        scadenza, verificata_at = in_cache
        if (adesso - verificata_at).total_seconds() < TTL_CACHE_LICENZA:
            if adesso > scadenza:
                raise HTTPException(
                    status_code=402,
                    detail="La tua licenza giornaliera è scaduta. Rinnovala per continuare a usare gli agenti.",
                )
            return user_id

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
        # Il DB non risponde: NON significa che la licenza sia scaduta.
        # Se poco fa era valida si lascia passare, altrimenti chi sta pagando
        # un'elaborazione in corso si vedrebbe bloccare anche la sola lettura
        # dei log per un errore di risorse temporaneo.
        logger.error("Errore query licenze per utente %s: %s: %s", user_id, type(e).__name__, e)
        if in_cache:
            scadenza, verificata_at = in_cache
            entro_tolleranza = (adesso - verificata_at).total_seconds() < TOLLERANZA_DB_NON_RAGGIUNGIBILE
            if entro_tolleranza and adesso <= scadenza:
                logger.warning(
                    "Licenza di %s accettata dalla cache: database non raggiungibile.", user_id
                )
                return user_id
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

    _scrivi_cache(user_id, expires_at)

    if adesso > expires_at:
        raise HTTPException(
            status_code=402,
            detail="La tua licenza giornaliera è scaduta. Rinnovala per continuare a usare gli agenti.",
        )

    # Tutto ok: restituisce l'ID dell'utente autenticato e autorizzato
    return user_id
