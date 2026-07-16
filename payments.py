"""
Pagamenti e credito token di CodeMorph.AI.

- Pass giornaliero: 299,00 € per 24 ore di accesso, di cui 20,00 € vengono
  accreditati come CREDITO TOKEN spendibile (vedi src/config.py).
- Pass multi-giorno (pass_giorni): pacchetti 7/30/90/180/365 giorni o numero
  personalizzato. Prezzo = giorni × PREZZO_GIORNO_EUR; ogni giorno PAGATO
  include QUOTA_TOKEN_GIORNO_EUR di credito token. Giorni BONUS gratuiti con
  crescita lineare: 30 giorni → 2 gratis … 365 giorni → 30 gratis (vedi
  calcola_giorni_bonus). I giorni bonus estendono la licenza ma non
  accreditano quota token.
- Metodi supportati: PayPal e Google Pay. Entrambi si regolano tramite le
  PayPal Orders API v2: il bottone Google Pay del JS SDK di PayPal
  (components=buttons,googlepay) approva lo STESSO ordine creato qui, quindi
  il flusso server (crea ordine -> cattura -> erogazione) è identico e cambia
  solo il campo `metodo` registrato a fini contabili.
- Flusso REDIRECT (checkout gestito dal backend): se FRONTEND_URL è
  configurata, gli ordini includono return_url/cancel_url; il frontend
  reindirizza l'utente su approve_url e al rientro chiama /payments/cattura
  con l'order_id (parametro `token` aggiunto da PayPal all'URL di ritorno).
- Credito token: portafoglio per utente (tabella token_wallets). Ogni fase
  addebita il costo REALE dei token consumati (vedi src/token_tracker.py);
  a saldo esaurito le fasi rispondono 402 finché l'utente non ricarica o
  acquista un nuovo pass.

Variabili d'ambiente: PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET,
PAYPAL_ENV (sandbox | live, default sandbox), FRONTEND_URL (per il redirect).
Schema database: db/schema_pagamenti_token.sql + db/migrazione_pass_giorni.sql.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import _parse_expiry, get_current_user, supabase
from src.config import (
    DURATA_PASS_ORE,
    PREZZO_PASS_GIORNALIERO_EUR,
    QUOTA_TOKEN_PASS_EUR,
    RICARICA_MASSIMA_EUR,
    RICARICA_MINIMA_EUR,
    VALUTA_PAGAMENTI,
)

# --- Costanti del pass multi-giorno ---------------------------------------
# Importate da src.config se presenti (fonte di verità preferita); i default
# qui sotto permettono al modulo di funzionare anche prima di aggiornare
# config.py, ma è consigliato spostare i valori lì (vedi blocco fornito).
try:
    from src.config import (
        PREZZO_GIORNO_EUR,
        QUOTA_TOKEN_GIORNO_EUR,
        PACCHETTI_GIORNI,
        GIORNI_MASSIMI_ACQUISTO,
        BONUS_SOGLIA_GIORNI,
        BONUS_GIORNI_MINIMO,
        BONUS_GIORNI_MASSIMO,
    )
except ImportError:  # config.py non ancora aggiornato: default a listino
    PREZZO_GIORNO_EUR = Decimal("299.00")
    QUOTA_TOKEN_GIORNO_EUR = Decimal("20.00")
    PACCHETTI_GIORNI = [7, 30, 90, 180, 365]
    GIORNI_MASSIMI_ACQUISTO = 365
    BONUS_SOGLIA_GIORNI = 30    # sotto questa soglia: nessun bonus
    BONUS_GIORNI_MINIMO = 2     # bonus alla soglia (30 giorni -> 2 gratis)
    BONUS_GIORNI_MASSIMO = 30   # bonus al massimo (365 giorni -> 30 gratis)

# Tetto massimo per un singolo pagamento PayPal: oltre questa cifra il
# checkout PayPal viene rifiutato dal circuito (limite ~15.000 € — verifica
# il massimale REALE del tuo account Business e adegua il valore in config).
# Con il listino a 299 €/giorno corrisponde a 50 giorni (14.950 €).
# Sopra soglia resta disponibile il BONIFICO BANCARIO.
try:
    from src.config import SOGLIA_MASSIMA_PAYPAL_EUR
except ImportError:
    SOGLIA_MASSIMA_PAYPAL_EUR = Decimal("14950.00")

# Coordinate per i pagamenti via bonifico (da variabili d'ambiente, MAI nel codice)
IBAN_BONIFICI = os.getenv("IBAN_BONIFICI", "").strip()
INTESTATARIO_BONIFICI = os.getenv("INTESTATARIO_BONIFICI", "").strip()


def calcola_giorni_bonus(giorni_pagati: int) -> int:
    """
    Giorni di accesso GRATUITI con crescita lineare tra la soglia e il
    massimo acquistabile: 30 giorni -> 2 gratis ... 365 giorni -> 30 gratis
    (con i default). Sotto la soglia: nessun bonus. Arrotondamento per
    difetto, così il totale non supera mai la retta di crescita.

    Verifica con i pacchetti standard: 7->0, 30->2, 90->7, 180->14, 365->30.
    """
    if giorni_pagati < BONUS_SOGLIA_GIORNI:
        return 0
    ampiezza = GIORNI_MASSIMI_ACQUISTO - BONUS_SOGLIA_GIORNI
    crescita = BONUS_GIORNI_MASSIMO - BONUS_GIORNI_MINIMO
    bonus = BONUS_GIORNI_MINIMO + (giorni_pagati - BONUS_SOGLIA_GIORNI) * crescita / ampiezza
    return min(int(bonus), BONUS_GIORNI_MASSIMO)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["Pagamenti & Token"])

_DUE_DECIMALI = Decimal("0.01")


def _dec(valore, default=Decimal("0")):
    """Converte in Decimal qualunque valore numerico arrivi da JSON/DB."""
    try:
        return Decimal(str(valore))
    except (InvalidOperation, TypeError, ValueError):
        return default


# =====================================================================
# Client PayPal (Orders API v2) — usato sia per PayPal sia per Google Pay
# =====================================================================

class _PayPalClient:
    """Client minimale per le Orders API v2 con cache del token OAuth."""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = None
        self._token_scadenza = 0.0

    # Le credenziali sono lette a ogni accesso (non congelate all'import):
    # così un backend avviato senza chiavi risponde 503 SOLO sugli endpoint
    # di pagamento invece di rifiutarsi di partire.
    @property
    def client_id(self):
        return os.getenv("PAYPAL_CLIENT_ID", "").strip()

    @property
    def _client_secret(self):
        return os.getenv("PAYPAL_CLIENT_SECRET", "").strip()

    @property
    def ambiente(self):
        return "live" if os.getenv("PAYPAL_ENV", "sandbox").strip().lower() == "live" else "sandbox"

    @property
    def base_url(self):
        if self.ambiente == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def configurato(self):
        return bool(self.client_id and self._client_secret)

    def _access_token(self, forza=False):
        with self._lock:
            if not forza and self._token and time.time() < self._token_scadenza - 60:
                return self._token

            risposta = httpx.post(
                f"{self.base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self._client_secret),
                timeout=30.0,
            )
            risposta.raise_for_status()
            dati = risposta.json()
            self._token = dati["access_token"]
            self._token_scadenza = time.time() + int(dati.get("expires_in", 300))
            return self._token

    def _chiama(self, metodo, percorso, json_body=None):
        if not self.configurato:
            raise HTTPException(
                status_code=503,
                detail="Pagamenti non configurati sul server (PAYPAL_CLIENT_ID/PAYPAL_CLIENT_SECRET mancanti).",
            )
        try:
            token = self._access_token()
            risposta = httpx.request(
                metodo,
                f"{self.base_url}{percorso}",
                json=json_body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=30.0,
            )
            if risposta.status_code == 401:
                # Token OAuth scaduto lato PayPal: un solo retry con token fresco
                token = self._access_token(forza=True)
                risposta = httpx.request(
                    metodo,
                    f"{self.base_url}{percorso}",
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=30.0,
                )
            return risposta
        except httpx.HTTPError as e:
            logger.error("Errore di rete verso PayPal: %s", e)
            raise HTTPException(
                status_code=502,
                detail="Comunicazione con PayPal non riuscita. Riprova tra qualche istante.",
            )

    def crea_ordine(self, importo: Decimal, descrizione: str, user_id: str) -> dict:
        corpo = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": VALUTA_PAGAMENTI, "value": f"{importo:.2f}"},
                "description": descrizione[:127],  # limite PayPal
                "custom_id": user_id,
            }],
        }

        # Flusso REDIRECT: con FRONTEND_URL configurata, PayPal sa dove
        # rimandare l'utente dopo l'approvazione (aggiunge ?token=ORDER_ID
        # all'URL di ritorno). Il flusso a bottoni JS SDK non ne è toccato:
        # con i bottoni l'approvazione avviene in-page e il redirect non scatta.
        frontend_url = os.getenv("FRONTEND_URL", "").strip().rstrip("/")
        if frontend_url:
            corpo["application_context"] = {
                "brand_name": "CodeMorph.AI",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": f"{frontend_url}/?paypal=success",
                "cancel_url": f"{frontend_url}/?paypal=cancel",
            }
        else:
            logger.warning(
                "FRONTEND_URL non configurata: il checkout con redirect non "
                "potrà rientrare nell'app (i bottoni JS SDK funzionano comunque)."
            )

        risposta = self._chiama("POST", "/v2/checkout/orders", corpo)
        if risposta.status_code not in (200, 201):
            logger.error(
                "Creazione ordine PayPal fallita (%s): %s",
                risposta.status_code, risposta.text[:500],
            )
            raise HTTPException(status_code=502, detail="PayPal ha rifiutato la creazione dell'ordine.")
        return risposta.json()

    def cattura_ordine(self, order_id: str) -> dict:
        risposta = self._chiama("POST", f"/v2/checkout/orders/{order_id}/capture", {})
        if risposta.status_code in (200, 201):
            return risposta.json()

        # Retry del client dopo una cattura già riuscita: recuperiamo lo
        # stato reale dell'ordine invece di fallire.
        if risposta.status_code == 422 and "ORDER_ALREADY_CAPTURED" in risposta.text:
            return self.ottieni_ordine(order_id)

        logger.error(
            "Cattura PayPal fallita (%s): %s", risposta.status_code, risposta.text[:500]
        )
        if risposta.status_code == 404:
            raise HTTPException(status_code=404, detail="Ordine inesistente su PayPal.")
        if "ORDER_NOT_APPROVED" in risposta.text:
            raise HTTPException(
                status_code=402,
                detail="L'ordine non è stato ancora approvato: completa il pagamento su PayPal/Google Pay e riprova.",
            )
        raise HTTPException(status_code=402, detail="Il pagamento non risulta completabile su PayPal.")

    def ottieni_ordine(self, order_id: str) -> dict:
        risposta = self._chiama("GET", f"/v2/checkout/orders/{order_id}")
        if risposta.status_code != 200:
            raise HTTPException(status_code=502, detail="Impossibile verificare l'ordine su PayPal.")
        return risposta.json()


paypal = _PayPalClient()


def _estrai_esito_cattura(ordine: dict):
    """
    Estrae (stato_ordine, stato_cattura, importo, valuta) dalla risposta di
    cattura (o dal GET ordine, per gli ordini già catturati in precedenza).
    """
    unita = (ordine.get("purchase_units") or [{}])[0]
    catture = ((unita.get("payments") or {}).get("captures") or [])
    if catture:
        cattura = catture[0]
        amount = cattura.get("amount") or {}
        return (
            ordine.get("status"),
            cattura.get("status"),
            _dec(amount.get("value"), None),
            amount.get("currency_code"),
        )
    amount = unita.get("amount") or {}
    return ordine.get("status"), None, _dec(amount.get("value"), None), amount.get("currency_code")


# =====================================================================
# Portafoglio del credito token
# =====================================================================

def _inserisci_movimento(user_id, tipo, importo_eur, descrizione="", session_id=None, tracker=None):
    """Registra il movimento nell'audit trail (best effort: non blocca il flusso)."""
    riga = {
        "user_id": user_id,
        "tipo": tipo,
        "importo_eur": float(importo_eur),
        "descrizione": descrizione,
        "session_id": session_id,
    }
    if tracker is not None:
        riga.update({
            "tokens_prompt": tracker.prompt_tokens,
            "tokens_completion": tracker.completion_tokens,
            "tokens_totali": tracker.tokens_totali,
            "modello": tracker.modello,
        })
    try:
        supabase.table("token_transactions").insert(riga).execute()
    except Exception as e:
        logger.warning("Movimento token non registrato (%s, %s €): %s", tipo, importo_eur, e)


def _modifica_saldo(user_id, delta: Decimal) -> Decimal:
    """
    Aggiornamento ATOMICO del saldo tramite la funzione Postgres
    `modifica_saldo_token` (vedi db/schema_pagamenti_token.sql): evita le
    race condition dei read-modify-write concorrenti.
    """
    risposta = supabase.rpc(
        "modifica_saldo_token",
        {"p_user_id": user_id, "p_delta": float(delta)},
    ).execute()
    return _dec(risposta.data)


def leggi_saldo_token(user_id):
    """
    Ritorna (saldo, wallet_esiste). `saldo` è None se l'infrastruttura token
    non è disponibile (migrazione SQL non ancora applicata).
    """
    try:
        risposta = (
            supabase.table("token_wallets")
            .select("saldo_eur")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "Tabelle del credito token non disponibili (%s): eseguire db/schema_pagamenti_token.sql.", e
        )
        return None, False

    if not risposta.data:
        return Decimal("0"), False
    return _dec(risposta.data[0]["saldo_eur"]), True


def verifica_credito_token(user_id) -> Optional[Decimal]:
    """
    Da chiamare all'inizio di ogni fase: blocca con 402 chi ha esaurito il
    credito token, PRIMA di consumare LLM.

    - Wallet inesistente ma licenza valida (utenti attivati prima di questa
      funzionalità): bootstrap una tantum con la quota inclusa nel pass.
    - Infrastruttura token assente (migrazione SQL non applicata): fail-open
      con warning nei log, per non bloccare le installazioni esistenti.
    """
    
    # --- INIZIO VIP PASS ADMIN ---
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        if utente.data and utente.data.get("role") == "admin":
            # Decimal per coerenza con il resto del modulo: per l'admin è "infinito"
            return Decimal("9999.00")
    except Exception:
        # Se c'è un errore nella lettura dell'admin, passiamo sotto al controllo standard
        pass
    # --- FINE VIP PASS ADMIN ---

    saldo, wallet_esiste = leggi_saldo_token(user_id)
    if saldo is None:
        return None

    if not wallet_esiste:
        try:
            saldo = _modifica_saldo(user_id, QUOTA_TOKEN_PASS_EUR)
            _inserisci_movimento(
                user_id, "accredito_pass", QUOTA_TOKEN_PASS_EUR,
                descrizione="Attivazione credito token (quota inclusa nel pass attivo)",
            )
        except Exception as e:
            logger.warning("Bootstrap del credito token fallito per %s: %s", user_id, e)
            return None

    if saldo <= 0:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Credito token esaurito (saldo {saldo:.2f} €). "
                "Ricarica il credito oppure acquista un nuovo pass giornaliero per continuare."
            ),
        )
    return saldo


def addebita_consumo_token(user_id, tracker, session_id=None):
    """
    Addebita il costo REALE dei token consumati nella fase appena conclusa.
    Ritorna (costo, saldo_dopo); saldo_dopo è None se l'addebito non è
    riuscito (infrastruttura assente o errore DB, loggato).
    """
    
    # --- INIZIO VIP PASS ADMIN ---
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        if utente.data and utente.data.get("role") == "admin":
            # Per l'admin il costo è sempre 0 e il saldo rimane "infinito"
            return Decimal("0.00"), Decimal("9999.00")
    except Exception:
        pass
    # --- FINE VIP PASS ADMIN ---

    costo = tracker.costo_eur()
    if costo <= 0 and tracker.tokens_totali == 0:
        return Decimal("0"), None

    try:
        saldo = _modifica_saldo(user_id, -costo)
    except Exception as e:
        logger.error("Addebito token non riuscito (%s €) per %s: %s", costo, user_id, e)
        return costo, None

    _inserisci_movimento(
        user_id, "consumo", -costo,
        descrizione="Consumo token della fase di modernizzazione",
        session_id=session_id,
        tracker=tracker,
    )
    return costo, saldo


# =====================================================================
# Licenze (pass giornaliero)
# =====================================================================

def _attiva_pass(user_id, ore_totali: float) -> datetime:
    """
    Crea/estende la licenza per il pass appena pagato. Se l'utente ha già un
    pass attivo, le ore si ACCODANO alla scadenza corrente (non si perde
    quanto già pagato).
    """
    base = datetime.now(timezone.utc)
    try:
        risposta = (
            supabase.table("user_licenses")
            .select("expires_at")
            .eq("user_id", user_id)
            .order("expires_at", desc=True)
            .limit(1)
            .execute()
        )
        if risposta.data:
            scadenza_attuale = _parse_expiry(risposta.data[0]["expires_at"])
            if scadenza_attuale > base:
                base = scadenza_attuale
    except Exception as e:
        logger.warning("Lettura licenza esistente fallita per %s: %s (parto da adesso).", user_id, e)

    scadenza = base + timedelta(hours=ore_totali)
    # UPSERT, non insert: user_licenses ha la primary key su user_id (una
    # sola riga per utente), quindi al rinnovo l'insert violerebbe la PK
    # (errore 23505). La scadenza di partenza è già stata letta e accodata
    # sopra, quindi sovrascrivere la riga con la nuova scadenza è corretto.
    supabase.table("user_licenses").upsert(
        {"user_id": user_id, "expires_at": scadenza.isoformat()}
    ).execute()
    return scadenza


def _attiva_pass_giornaliero(user_id) -> datetime:
    """Pass 24h classico: caso particolare di _attiva_pass (retrocompatibilità)."""
    return _attiva_pass(user_id, DURATA_PASS_ORE)


def _marca_stato_ordine(order_id, stato):
    try:
        supabase.table("payment_orders").update({"stato": stato}).eq("id", order_id).execute()
    except Exception as e:
        logger.error("Impossibile marcare l'ordine %s come '%s': %s", order_id, stato, e)


def _eroga_acquisto(user_id, ordine_db, order_id) -> dict:
    """
    Attiva quanto acquistato in base al tipo dell'ordine registrato su DB.
    UNICA implementazione dell'erogazione, condivisa tra la cattura PayPal e
    la conferma admin dei bonifici: qualunque metodo di pagamento passa da qui.
    NON gestisce le transizioni di stato: il chiamante deve aver già vinto la
    transizione condizionata (creato/in_attesa_bonifico -> completato).
    """
    if ordine_db["tipo"] == "pass_giornaliero":
        scadenza = _attiva_pass_giornaliero(user_id)
        saldo = _modifica_saldo(user_id, QUOTA_TOKEN_PASS_EUR)
        _inserisci_movimento(
            user_id, "accredito_pass", QUOTA_TOKEN_PASS_EUR,
            descrizione=f"Credito token incluso nel pass giornaliero (ordine {order_id})",
        )
        return {
            "status": "success",
            "tipo": "pass_giornaliero",
            "metodo": ordine_db["metodo"],
            "licenza_scade_il": scadenza.isoformat(),
            "token_accreditati_eur": float(QUOTA_TOKEN_PASS_EUR),
            "saldo_token_eur": float(saldo),
        }

    if ordine_db["tipo"] == "pass_giorni":
        # I giorni vengono letti dall'ordine registrato alla creazione
        # (calcolati lato server), MAI da input del client.
        giorni_pagati = int(ordine_db.get("giorni_pagati") or 0)
        giorni_bonus = int(ordine_db.get("giorni_bonus") or 0)
        if giorni_pagati <= 0:
            # Ordine registrato prima della migrazione SQL o corrotto
            raise ValueError(f"Ordine pass_giorni senza giorni registrati: {order_id}")

        giorni_totali = giorni_pagati + giorni_bonus
        scadenza = _attiva_pass(user_id, ore_totali=giorni_totali * 24)

        quota_token = (QUOTA_TOKEN_GIORNO_EUR * giorni_pagati).quantize(_DUE_DECIMALI)
        saldo = _modifica_saldo(user_id, quota_token)
        _inserisci_movimento(
            user_id, "accredito_pass", quota_token,
            descrizione=(
                f"Credito token incluso nel pass {giorni_pagati} giorni "
                f"(+{giorni_bonus} bonus) — ordine {order_id}"
            ),
        )
        return {
            "status": "success",
            "tipo": "pass_giorni",
            "metodo": ordine_db["metodo"],
            "giorni_pagati": giorni_pagati,
            "giorni_bonus": giorni_bonus,
            "giorni_totali": giorni_totali,
            "licenza_scade_il": scadenza.isoformat(),
            "token_accreditati_eur": float(quota_token),
            "saldo_token_eur": float(saldo),
        }

    # ricarica_token
    importo = _dec(ordine_db["importo_eur"])
    saldo = _modifica_saldo(user_id, importo)
    _inserisci_movimento(
        user_id, "ricarica", importo,
        descrizione=f"Ricarica credito token (ordine {order_id})",
    )
    return {
        "status": "success",
        "tipo": "ricarica_token",
        "metodo": ordine_db["metodo"],
        "token_accreditati_eur": float(importo),
        "saldo_token_eur": float(saldo),
    }


def _verifica_admin(user_id):
    """403 se l'utente autenticato non ha ruolo admin (per la conferma bonifici)."""
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
    except Exception as e:
        logger.error("Verifica ruolo admin fallita per %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Impossibile verificare i privilegi.")
    if not utente.data or utente.data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Operazione riservata agli amministratori.")


# =====================================================================
# Modelli di input
# =====================================================================

class InputOrdine(BaseModel):
    tipo: Literal["pass_giornaliero", "pass_giorni", "ricarica_token"]
    metodo: Literal["paypal", "googlepay", "bonifico"] = "paypal"
    # Solo per le ricariche: per i pass l'importo è calcolato a listino
    importo_eur: Optional[float] = None
    # Solo per pass_giorni: numero di giorni da acquistare (pacchetto o libero)
    giorni: Optional[int] = None


class InputCattura(BaseModel):
    order_id: str


# =====================================================================
# Endpoint
# =====================================================================

@router.get("/payments/config")
def configurazione_pagamenti(user_id: str = Depends(get_current_user)):
    """Dati per inizializzare i bottoni PayPal / Google Pay nel frontend."""
    return {
        "configurato": paypal.configurato,
        "ambiente": paypal.ambiente,
        "paypal_client_id": paypal.client_id or None,  # ID pubblico, serve al JS SDK
        "valuta": VALUTA_PAGAMENTI,
        "metodi": ["paypal", "googlepay", "bonifico"],
        # PayPal è utilizzabile solo fino a questa cifra per singolo ordine;
        # oltre, il frontend deve proporre il bonifico.
        "soglia_massima_paypal_eur": float(SOGLIA_MASSIMA_PAYPAL_EUR),
        "bonifico_disponibile": bool(IBAN_BONIFICI),
        "prezzo_pass_giornaliero_eur": float(PREZZO_PASS_GIORNALIERO_EUR),
        "quota_token_inclusa_eur": float(QUOTA_TOKEN_PASS_EUR),
        "durata_pass_ore": DURATA_PASS_ORE,
        "ricarica_minima_eur": float(RICARICA_MINIMA_EUR),
        "ricarica_massima_eur": float(RICARICA_MASSIMA_EUR),
        # Pass multi-giorno: parametri per l'anteprima nel frontend.
        # Il calcolo AUTORITATIVO resta comunque lato server alla creazione ordine.
        "pass_giorni": {
            "prezzo_giorno_eur": float(PREZZO_GIORNO_EUR),
            "quota_token_giorno_eur": float(QUOTA_TOKEN_GIORNO_EUR),
            "pacchetti": PACCHETTI_GIORNI,
            "giorni_massimi": GIORNI_MASSIMI_ACQUISTO,
            "bonus": {
                "soglia_giorni": BONUS_SOGLIA_GIORNI,
                "minimo": BONUS_GIORNI_MINIMO,
                "massimo": BONUS_GIORNI_MASSIMO,
            },
        },
    }


@router.post("/payments/ordini")
def crea_ordine_pagamento(richiesta: InputOrdine, user_id: str = Depends(get_current_user)):
    """
    Crea l'ordine di pagamento (pass giornaliero o ricarica token).
    Il frontend fa approvare l'ordine con il bottone PayPal o Google Pay
    del JS SDK PayPal e poi chiama /payments/cattura.
    """
    giorni_pagati = None
    giorni_bonus = None

    if richiesta.tipo == "pass_giornaliero":
        importo = PREZZO_PASS_GIORNALIERO_EUR
        descrizione = (
            f"CodeMorph.AI — Pass giornaliero {DURATA_PASS_ORE}h "
            f"(include {QUOTA_TOKEN_PASS_EUR:.0f} € di credito token)"
        )
    elif richiesta.tipo == "pass_giorni":
        if richiesta.giorni is None:
            raise HTTPException(status_code=400, detail="Indica il numero di giorni da acquistare (giorni).")
        giorni_pagati = int(richiesta.giorni)
        if not (1 <= giorni_pagati <= GIORNI_MASSIMI_ACQUISTO):
            raise HTTPException(
                status_code=400,
                detail=f"Il numero di giorni deve essere compreso tra 1 e {GIORNI_MASSIMI_ACQUISTO}.",
            )
        # Prezzo e bonus calcolati QUI, mai fidandosi di valori dal client
        giorni_bonus = calcola_giorni_bonus(giorni_pagati)
        importo = (PREZZO_GIORNO_EUR * giorni_pagati).quantize(_DUE_DECIMALI)
        quota_token = (QUOTA_TOKEN_GIORNO_EUR * giorni_pagati).quantize(_DUE_DECIMALI)
        etichetta_bonus = f" + {giorni_bonus} giorni bonus" if giorni_bonus else ""
        descrizione = (
            f"CodeMorph.AI — Pass {giorni_pagati} giorni{etichetta_bonus} "
            f"(include {quota_token:.0f} € di credito token)"
        )
    else:
        if richiesta.importo_eur is None:
            raise HTTPException(status_code=400, detail="Indica l'importo della ricarica (importo_eur).")
        importo = _dec(richiesta.importo_eur, None)
        if importo is None:
            raise HTTPException(status_code=400, detail="Importo della ricarica non valido.")
        importo = importo.quantize(_DUE_DECIMALI)
        if not (RICARICA_MINIMA_EUR <= importo <= RICARICA_MASSIMA_EUR):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"La ricarica deve essere compresa tra {RICARICA_MINIMA_EUR:.2f} "
                    f"e {RICARICA_MASSIMA_EUR:.2f} €."
                ),
            )
        descrizione = f"CodeMorph.AI — Ricarica credito token {importo:.2f} €"

    # ------------------------- BONIFICO BANCARIO -------------------------
    # Nessuna chiamata a PayPal: l'ordine nasce "in_attesa_bonifico" e viene
    # erogato dall'endpoint admin di conferma quando l'incasso arriva.
    if richiesta.metodo == "bonifico":
        if not IBAN_BONIFICI:
            raise HTTPException(
                status_code=503,
                detail="Pagamento con bonifico non disponibile (IBAN_BONIFICI non configurato sul server).",
            )
        order_id = f"BON-{uuid.uuid4().hex[:12].upper()}"
        try:
            supabase.table("payment_orders").insert({
                "id": order_id,
                "user_id": user_id,
                "tipo": richiesta.tipo,
                "metodo": "bonifico",
                "importo_eur": float(importo),
                "valuta": VALUTA_PAGAMENTI,
                "stato": "in_attesa_bonifico",
                "giorni_pagati": giorni_pagati,
                "giorni_bonus": giorni_bonus,
            }).execute()
        except Exception as e:
            logger.error("Ordine bonifico %s non registrato su DB: %s", order_id, e)
            raise HTTPException(status_code=500, detail="Ordine non registrato: riprova.")

        return {
            "order_id": order_id,
            "stato": "in_attesa_bonifico",
            "tipo": richiesta.tipo,
            "metodo": "bonifico",
            "importo_eur": float(importo),
            "valuta": VALUTA_PAGAMENTI,
            "giorni_pagati": giorni_pagati,
            "giorni_bonus": giorni_bonus,
            "istruzioni_bonifico": {
                "iban": IBAN_BONIFICI,
                "intestatario": INTESTATARIO_BONIFICI or None,
                "importo_eur": float(importo),
                # La causale È l'ID ordine: permette di abbinare l'incasso
                "causale": order_id,
                "nota": (
                    "L'attivazione avviene alla conferma dell'incasso "
                    "(tipicamente 1-2 giorni lavorativi dalla ricezione del bonifico)."
                ),
            },
        }

    # ------------------------- PAYPAL / GOOGLE PAY -------------------------
    # Tetto per singolo pagamento sul circuito PayPal: sopra soglia il
    # cliente deve usare il bonifico.
    if importo > SOGLIA_MASSIMA_PAYPAL_EUR:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Gli ordini oltre {SOGLIA_MASSIMA_PAYPAL_EUR:.2f} € non sono pagabili "
                "con PayPal: seleziona il pagamento con bonifico bancario."
            ),
        )

    ordine = paypal.crea_ordine(importo, descrizione, user_id)
    order_id = ordine.get("id")
    if not order_id:
        raise HTTPException(status_code=502, detail="Risposta PayPal senza ID ordine.")

    # L'ordine va registrato PRIMA dell'approvazione: in cattura l'importo
    # pagato viene verificato contro quello registrato QUI, non contro
    # valori arrivati dal client.
    try:
        supabase.table("payment_orders").insert({
            "id": order_id,
            "user_id": user_id,
            "tipo": richiesta.tipo,
            "metodo": richiesta.metodo,
            "importo_eur": float(importo),
            "valuta": VALUTA_PAGAMENTI,
            "stato": "creato",
            # Solo per pass_giorni (None altrove): l'erogazione in cattura
            # legge QUESTI valori, non ricalcola da input del client.
            "giorni_pagati": giorni_pagati,
            "giorni_bonus": giorni_bonus,
        }).execute()
    except Exception as e:
        logger.error("Ordine PayPal %s non registrato su DB: %s", order_id, e)
        raise HTTPException(
            status_code=500,
            detail="Ordine non registrato: riprova. Nessun importo è stato addebitato.",
        )

    url_approvazione = next(
        (l.get("href") for l in ordine.get("links", []) if l.get("rel") in ("approve", "payer-action")),
        None,
    )
    return {
        "order_id": order_id,
        "stato": ordine.get("status"),
        "approve_url": url_approvazione,
        "tipo": richiesta.tipo,
        "metodo": richiesta.metodo,
        "importo_eur": float(importo),
        "valuta": VALUTA_PAGAMENTI,
        "giorni_pagati": giorni_pagati,
        "giorni_bonus": giorni_bonus,
    }


@router.post("/payments/cattura")
def cattura_pagamento(richiesta: InputCattura, user_id: str = Depends(get_current_user)):
    """
    Cattura l'ordine approvato, verifica importo e valuta contro quanto
    registrato alla creazione e attiva l'acquisto:
    - pass_giornaliero: licenza 24h + accredito della quota token inclusa;
    - ricarica_token: accredito dell'importo sul portafoglio.
    Idempotente: un ordine già completato non viene accreditato due volte.
    """
    try:
        risposta = supabase.table("payment_orders").select("*").eq("id", richiesta.order_id).execute()
    except Exception as e:
        logger.error("Lettura ordine %s fallita: %s", richiesta.order_id, e)
        raise HTTPException(status_code=503, detail="Servizio ordini non disponibile.")

    if not risposta.data:
        raise HTTPException(status_code=404, detail="Ordine sconosciuto: crealo prima da /payments/ordini.")
    ordine_db = risposta.data[0]

    if ordine_db["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Questo ordine appartiene a un altro account.")

    if ordine_db["metodo"] == "bonifico":
        raise HTTPException(
            status_code=400,
            detail="Questo ordine è un bonifico: viene attivato alla conferma dell'incasso, non tramite PayPal.",
        )

    if ordine_db["stato"] == "completato":
        saldo, _ = leggi_saldo_token(user_id)
        return {
            "status": "gia_completato",
            "tipo": ordine_db["tipo"],
            "messaggio": "Ordine già elaborato in precedenza: nessun nuovo accredito.",
            "saldo_token_eur": float(saldo) if saldo is not None else None,
        }
    if ordine_db["stato"] == "anomalo":
        raise HTTPException(
            status_code=409,
            detail=f"Ordine in stato anomalo: contatta il supporto citando l'ordine {richiesta.order_id}.",
        )

    # 1. Cattura su PayPal e verifica dell'esito
    esito = paypal.cattura_ordine(richiesta.order_id)
    stato_ordine, stato_cattura, importo_pagato, valuta = _estrai_esito_cattura(esito)

    if stato_ordine != "COMPLETED" or stato_cattura not in (None, "COMPLETED"):
        raise HTTPException(
            status_code=402,
            detail=f"Pagamento non completato su PayPal (stato: {stato_ordine}/{stato_cattura}). Riprova.",
        )

    atteso = _dec(ordine_db["importo_eur"]).quantize(_DUE_DECIMALI)
    if (
        importo_pagato is None
        or importo_pagato.quantize(_DUE_DECIMALI) != atteso
        or valuta != ordine_db["valuta"]
    ):
        _marca_stato_ordine(richiesta.order_id, "anomalo")
        logger.error(
            "Importo catturato NON corrispondente per %s: atteso %s %s, pagato %s %s",
            richiesta.order_id, atteso, ordine_db["valuta"], importo_pagato, valuta,
        )
        raise HTTPException(
            status_code=400,
            detail="L'importo pagato non corrisponde all'ordine: anomalia segnalata al supporto.",
        )

    # 2. Transizione di stato CONDIZIONATA (creato -> completato): se due
    #    catture arrivano in parallelo, una sola vince e accredita.
    presa_in_carico = (
        supabase.table("payment_orders")
        .update({"stato": "completato", "completed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", richiesta.order_id)
        .eq("stato", "creato")
        .execute()
    )
    if not presa_in_carico.data:
        saldo, _ = leggi_saldo_token(user_id)
        return {
            "status": "gia_completato",
            "tipo": ordine_db["tipo"],
            "messaggio": "Ordine già elaborato in precedenza: nessun nuovo accredito.",
            "saldo_token_eur": float(saldo) if saldo is not None else None,
        }

    # 3. Erogazione di quanto acquistato (implementazione condivisa con i bonifici)
    try:
        return _eroga_acquisto(user_id, ordine_db, richiesta.order_id)
    except Exception:
        # Il pagamento è stato INCASSATO ma l'attivazione è fallita: non
        # lasciamo l'ordine su 'completato' (un retry sembrerebbe ok senza
        # aver erogato nulla) — lo marchiamo per l'intervento del supporto.
        _marca_stato_ordine(richiesta.order_id, "anomalo")
        logger.exception("Erogazione fallita per l'ordine %s", richiesta.order_id)
        raise HTTPException(
            status_code=500,
            detail=(
                "Pagamento ricevuto ma attivazione fallita: contatta il supporto "
                f"citando l'ordine {richiesta.order_id}."
            ),
        )


@router.post("/admin/payments/{order_id}/rieroga-anomalo")
def rieroga_ordine_anomalo(order_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Ri-eroga un ordine in stato 'anomalo' (pagamento incassato ma
    attivazione fallita, es. per un errore DB poi risolto). Transizione
    condizionata anomalo -> completato: una sola ri-erogazione può vincere.
    """
    _verifica_admin(user_id)

    try:
        risposta = supabase.table("payment_orders").select("*").eq("id", order_id).execute()
    except Exception as e:
        logger.error("Lettura ordine anomalo %s fallita: %s", order_id, e)
        raise HTTPException(status_code=503, detail="Servizio ordini non disponibile.")

    if not risposta.data:
        raise HTTPException(status_code=404, detail="Ordine inesistente.")
    ordine_db = risposta.data[0]

    if ordine_db["stato"] != "anomalo":
        raise HTTPException(
            status_code=409,
            detail=f"L'ordine è in stato '{ordine_db['stato']}', non 'anomalo': nessuna ri-erogazione necessaria.",
        )

    presa_in_carico = (
        supabase.table("payment_orders")
        .update({"stato": "completato", "completed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", order_id)
        .eq("stato", "anomalo")
        .execute()
    )
    if not presa_in_carico.data:
        return {"status": "gia_completato", "messaggio": "Ordine già ri-erogato da un'altra richiesta."}

    try:
        esito = _eroga_acquisto(ordine_db["user_id"], ordine_db, order_id)
        esito["rierogato_da_admin"] = user_id
        logger.info("Ordine anomalo %s ri-erogato con successo dall'admin %s", order_id, user_id)
        return esito
    except Exception:
        # Ancora fallito: torna anomalo per un nuovo tentativo dopo il fix
        _marca_stato_ordine(order_id, "anomalo")
        logger.exception("Ri-erogazione fallita per l'ordine %s", order_id)
        raise HTTPException(
            status_code=500,
            detail=f"Ri-erogazione fallita: controlla i log del server ({order_id}).",
        )


@router.get("/admin/payments/bonifici-in-attesa")
def bonifici_in_attesa(user_id: str = Depends(get_current_user)):
    """[ADMIN] Elenco degli ordini via bonifico in attesa di incasso."""
    _verifica_admin(user_id)
    try:
        risposta = (
            supabase.table("payment_orders")
            .select("*")
            .eq("metodo", "bonifico")
            .eq("stato", "in_attesa_bonifico")
            .order("created_at", desc=True)
            .execute()
        )
        return risposta.data or []
    except Exception as e:
        logger.error("Lettura bonifici in attesa fallita: %s", e)
        raise HTTPException(status_code=503, detail="Servizio ordini non disponibile.")


@router.post("/admin/payments/{order_id}/conferma-bonifico")
def conferma_bonifico(order_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Conferma l'incasso di un bonifico e attiva l'acquisto per il
    cliente. Stessa erogazione idempotente e stessa protezione dalla doppia
    conferma del flusso PayPal (transizione condizionata di stato).
    """
    _verifica_admin(user_id)

    try:
        risposta = supabase.table("payment_orders").select("*").eq("id", order_id).execute()
    except Exception as e:
        logger.error("Lettura ordine bonifico %s fallita: %s", order_id, e)
        raise HTTPException(status_code=503, detail="Servizio ordini non disponibile.")

    if not risposta.data:
        raise HTTPException(status_code=404, detail="Ordine inesistente.")
    ordine_db = risposta.data[0]

    if ordine_db["metodo"] != "bonifico":
        raise HTTPException(status_code=400, detail="Questo ordine non è un bonifico.")
    if ordine_db["stato"] == "completato":
        return {"status": "gia_completato", "messaggio": "Bonifico già confermato in precedenza: nessun nuovo accredito."}
    if ordine_db["stato"] == "anomalo":
        raise HTTPException(status_code=409, detail=f"Ordine in stato anomalo: verifica manuale richiesta ({order_id}).")

    # Transizione condizionata: in_attesa_bonifico -> completato.
    # Due conferme in parallelo (o un doppio click): una sola vince.
    presa_in_carico = (
        supabase.table("payment_orders")
        .update({"stato": "completato", "completed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", order_id)
        .eq("stato", "in_attesa_bonifico")
        .execute()
    )
    if not presa_in_carico.data:
        return {"status": "gia_completato", "messaggio": "Bonifico già confermato in precedenza: nessun nuovo accredito."}

    # Erogazione al PROPRIETARIO dell'ordine (non all'admin che conferma!)
    try:
        esito = _eroga_acquisto(ordine_db["user_id"], ordine_db, order_id)
        esito["confermato_da_admin"] = user_id
        return esito
    except Exception:
        _marca_stato_ordine(order_id, "anomalo")
        logger.exception("Erogazione fallita per il bonifico %s", order_id)
        raise HTTPException(
            status_code=500,
            detail=f"Incasso registrato ma attivazione fallita: verifica manuale richiesta ({order_id}).",
        )


@router.get("/tokens/saldo")
def saldo_token(user_id: str = Depends(get_current_user)):
    """Saldo del credito token e ultimi movimenti (consumi e ricariche)."""
    saldo, _ = leggi_saldo_token(user_id)

    movimenti = []
    if saldo is not None:
        try:
            risposta = (
                supabase.table("token_transactions")
                .select("tipo,importo_eur,tokens_totali,modello,session_id,descrizione,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(20)
                .execute()
            )
            movimenti = risposta.data or []
        except Exception as e:
            logger.warning("Lettura movimenti token fallita per %s: %s", user_id, e)

    return {
        "sistema_token_attivo": saldo is not None,
        "saldo_eur": float(saldo) if saldo is not None else None,
        "quota_token_pass_eur": float(QUOTA_TOKEN_PASS_EUR),
        "ricarica_minima_eur": float(RICARICA_MINIMA_EUR),
        "movimenti": movimenti,
    }
