"""
Agenti e provvigioni.

L'attribuzione del cliente all'agente è PERMANENTE e avviene una sola
volta, alla registrazione. Ogni acquisto successivo — rinnovi compresi —
genera una provvigione: se il cliente continua a comprare, l'agente
continua a incassare.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user, supabase
from payments import _verifica_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/affiliazione")


class InputAttribuzione(BaseModel):
    codice: str


@router.post("/attribuisci")
def attribuisci_agente(dati: InputAttribuzione, user_id: str = Depends(get_current_user)):
    """
    Collega l'account all'agente che l'ha segnalato. Irreversibile: se il
    profilo ha già un agente, la richiesta viene ignorata (nessuno può
    "rubare" un cliente già attribuito riproponendo il proprio codice).
    """
    codice = (dati.codice or "").strip()
    if not codice:
        raise HTTPException(status_code=400, detail="Codice mancante.")

    try:
        # .limit(1) e non .single(): subito dopo il signUp la riga di profiles
        # potrebbe non esistere ancora (creata da trigger su auth.users) e
        # .single() solleverebbe, restituendo un 500 al posto di uno stato utile.
        p = (supabase.table("profiles").select("agente_id,role")
             .eq("id", user_id).limit(1).execute())
        if not p.data:
            logger.warning("Profilo %s non ancora presente: attribuzione da ritentare.", user_id)
            return {"status": "profilo_non_pronto"}
        # Un admin non e' un cliente segnalabile: senza questo, basta aprire
        # un link ?ref= da loggati per attribuire il proprio account.
        if p.data[0].get("role") == "admin":
            logger.info("Attribuzione ignorata: %s e' un account admin.", user_id)
            return {"status": "non_attribuibile"}
        if p.data[0].get("agente_id"):
            return {"status": "gia_attribuito"}

        a = (supabase.table("agenti").select("id,attivo")
             .eq("codice", codice).limit(1).execute())
        if not a.data or not a.data[0].get("attivo"):
            logger.info("Codice agente '%s' inesistente o disattivato.", codice)
            return {"status": "codice_non_valido"}

        upd = supabase.table("profiles").update({
            "agente_id": a.data[0]["id"],
            "agente_attribuito_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_id).execute()
        # Senza questo controllo un UPDATE bloccato da RLS torna 200 con data
        # vuoto: il log direbbe "attribuito" e il database resterebbe invariato.
        if not upd.data:
            logger.error(
                "Update di profiles a vuoto per %s (codice %s): probabile RLS "
                "o client Supabase con chiave anon anziche' service role.",
                user_id, codice,
            )
            raise HTTPException(status_code=500, detail="Attribuzione non salvata.")

        logger.info("Utente %s attribuito all'agente %s.", user_id, codice)
        return {"status": "attribuito"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Attribuzione agente fallita per %s: %s: %s",
                     user_id, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Attribuzione non riuscita.")


def registra_provvigione(user_id, order_id, importo_ordine_eur):
    """
    Da chiamare quando un ordine viene EROGATO (non quando viene creato).

    Best-effort: un problema qui non deve mai bloccare l'erogazione al
    cliente — il compenso si può sempre ricostruire dagli ordini.
    """
    try:
        # agente_attribuito_at DEVE stare nella select, altrimenti la finestra
        # dei 12 mesi non si applica mai e le provvigioni maturano all'infinito.
        p = (supabase.table("profiles").select("agente_id,agente_attribuito_at")
             .eq("id", user_id).limit(1).execute())
        riga = p.data[0] if p.data else {}

        agente_id = riga.get("agente_id")
        if not agente_id:
            return None                      # cliente diretto: nessuna provvigione

        # Il compenso spetta solo sugli acquisti entro 12 mesi dalla
        # registrazione del cliente: oltre, l'attribuzione resta ma non paga.
        attribuito = riga.get("agente_attribuito_at")
        if attribuito:
            data_attr = datetime.fromisoformat(str(attribuito).replace("Z", "+00:00"))
            if data_attr.tzinfo is None:
                # Timestamp senza offset: senza questo, la sottrazione fra naive
                # e aware solleva TypeError e la provvigione si perde in silenzio.
                data_attr = data_attr.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - data_attr).days > 365:
                logger.info("Ordine %s oltre i 12 mesi dall'attribuzione: nessuna provvigione.", order_id)
                return None

        a = (supabase.table("agenti").select("percentuale,attivo")
             .eq("id", agente_id).single().execute())
        if not a.data or not a.data.get("attivo"):
            return None

        percentuale = Decimal(str(a.data["percentuale"]))
        importo = (Decimal(str(importo_ordine_eur)) * percentuale / Decimal(100)).quantize(Decimal("0.01"))

        supabase.table("provvigioni").insert({
            "agente_id": agente_id,
            "user_id": user_id,
            "order_id": order_id,
            "importo_ordine_eur": float(importo_ordine_eur),
            "percentuale": float(percentuale),
            "importo_eur": float(importo),
            "stato": "maturata",
        }).execute()
        logger.info("Provvigione %.2f EUR maturata per l'agente %s (ordine %s).",
                    importo, agente_id, order_id)
        return importo
    except Exception as e:
        # UNIQUE su order_id: se scatta, la provvigione era già registrata.
        # Il nome dell'eccezione distingue quel caso innocuo da un errore vero.
        logger.warning("Provvigione non registrata per l'ordine %s: %s: %s",
                       order_id, type(e).__name__, e)
        return None

import re


class InputAgente(BaseModel):
    nome: str
    email: str
    codice: str = ""              # se vuoto viene generato dal nome
    percentuale: float = 30.0
    partita_iva: str = ""
    note: str = ""


def _genera_codice(nome):
    """
    Codice univoco per il link di segnalazione: base leggibile ricavata dal
    nome più un suffisso casuale. Il suffisso evita le omonimie (due Mario
    Rossi) senza rendere il codice illeggibile.
    """
    import secrets
    base = re.sub(r"[^A-Za-z0-9]", "", (nome or "").title())[:16] or "Agente"
    for _ in range(10):
        codice = f"{base}-{secrets.token_hex(2).upper()}"      # es. MarioRossi-7F3A
        esistente = supabase.table("agenti").select("id").eq("codice", codice).execute()
        if not esistente.data:
            return codice
    raise HTTPException(status_code=500, detail="Generazione del codice non riuscita: riprova.")

@router.delete("/admin/agenti/{agente_id}")
def elimina_agente(agente_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Elimina un agente, solo se non ha clienti attribuiti né
    provvigioni.

    Un agente che ha prodotto qualcosa NON va cancellato: le provvigioni
    sono documenti contabili e i clienti resterebbero orfani di
    attribuzione. In quel caso si usa la disattivazione.
    """
    _verifica_admin(user_id)

    try:
        prov = supabase.table("provvigioni").select("id").eq("agente_id", agente_id).limit(1).execute()
        if prov.data:
            raise HTTPException(
                status_code=400,
                detail="L'agente ha provvigioni registrate e non può essere eliminato: disattivalo.",
            )
        clienti = supabase.table("profiles").select("id").eq("agente_id", agente_id).limit(1).execute()
        if clienti.data:
            raise HTTPException(
                status_code=400,
                detail="L'agente ha clienti attribuiti e non può essere eliminato: disattivalo.",
            )

        r = supabase.table("agenti").delete().eq("id", agente_id).execute()
        if not r.data:
            raise HTTPException(status_code=404, detail="Agente non trovato.")
        logger.info("Agente %s eliminato (nessun cliente, nessuna provvigione).", agente_id)
        return {"status": "eliminato"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Eliminazione agente %s fallita: %s", agente_id, e)
        raise HTTPException(status_code=500, detail="Eliminazione non riuscita.")

@router.post("/admin/agenti")
def crea_agente(dati: InputAgente, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Registra un nuovo agente dopo la firma del contratto.
    Il codice è la chiave del link di segnalazione: deve essere unico,
    leggibile e stabile nel tempo (cambiarlo invaliderebbe i link già
    distribuiti).
    """
    _verifica_admin(user_id)

    nome = (dati.nome or "").strip()
    email = (dati.email or "").strip().lower()
    if len(nome) < 3:
        raise HTTPException(status_code=400, detail="Indica il nome completo dell'agente.")
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Indirizzo email non valido.")
    if not (0 < dati.percentuale <= 100):
        raise HTTPException(status_code=400, detail="La percentuale deve essere tra 0 e 100.")

    codice = _genera_codice(nome)
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", codice):
        raise HTTPException(
            status_code=400,
            detail="Il codice può contenere solo lettere, numeri, trattini e underscore (3-32 caratteri).",
        )

    try:
        esistente = supabase.table("agenti").select("id").eq("codice", codice).execute()
        if esistente.data:
            raise HTTPException(status_code=400, detail=f"Il codice '{codice}' è già assegnato a un altro agente.")

        r = supabase.table("agenti").insert({
            "codice": codice,
            "nome": nome,
            "email": email,
            "percentuale": float(dati.percentuale),
            "partita_iva": (dati.partita_iva or "").strip() or None,
            "note": (dati.note or "").strip() or None,
            "attivo": True,
        }).execute()
        logger.info("Agente '%s' creato con codice %s.", nome, codice)
        return r.data[0] if r.data else {"codice": codice}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Creazione agente fallita: %s", e)
        raise HTTPException(status_code=500, detail="Creazione non riuscita.")


@router.post("/admin/agenti/{agente_id}/stato")
def cambia_stato_agente(agente_id: str, attivo: bool, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Attiva o disattiva un agente.

    Disattivare NON toglie i clienti già attribuiti né le provvigioni
    maturate: impedisce solo nuove attribuzioni con quel codice e blocca
    la maturazione di nuovi compensi.
    """
    _verifica_admin(user_id)
    r = supabase.table("agenti").update({"attivo": attivo}).eq("id", agente_id).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Agente non trovato.")
    return {"status": "aggiornato", "attivo": attivo}

@router.get("/admin/agenti")
def elenco_agenti(user_id: str = Depends(get_current_user)):
    """[ADMIN] Agenti con clienti attribuiti e riepilogo compensi."""
    _verifica_admin(user_id)
    agenti = supabase.table("agenti").select("*").order("nome").execute().data or []
    prov = supabase.table("provvigioni").select("agente_id,user_id,importo_eur,stato").execute().data or []
    # I clienti attribuiti si contano dai PROFILI: un cliente è attribuito
    # dalla registrazione, anche se non ha ancora acquistato nulla.
    profili = supabase.table("profiles").select("agente_id").not_.is_("agente_id", "null").execute().data or []

    for ag in agenti:
        mie = [p for p in prov if p["agente_id"] == ag["id"]]
        ag["clienti_attribuiti"] = sum(1 for p in profili if p["agente_id"] == ag["id"])
        ag["clienti_paganti"] = len({p["user_id"] for p in mie})
        ag["maturato_eur"] = round(sum(float(p["importo_eur"]) for p in mie if p["stato"] == "maturata"), 2)
        ag["liquidato_eur"] = round(sum(float(p["importo_eur"]) for p in mie if p["stato"] == "liquidata"), 2)
    return agenti


def _mappa_email_utenti():
    """
    id -> (email, data_registrazione) dagli utenti Auth.

    L'email potrebbe non essere replicata in profiles: in quel caso l'unica
    fonte e' auth.users, raggiungibile solo con la service role. Se la
    chiamata non e' disponibile si degrada a un dizionario vuoto: il
    dettaglio mostrera' l'id al posto dell'indirizzo, senza rompersi.
    """
    try:
        risposta = supabase.auth.admin.list_users(page=1, per_page=1000)
        utenti = getattr(risposta, "users", risposta) or []
        mappa = {}
        for u in utenti:
            uid = getattr(u, "id", None) or (u.get("id") if isinstance(u, dict) else None)
            if not uid:
                continue
            email = getattr(u, "email", None) or (u.get("email") if isinstance(u, dict) else None)
            creato = getattr(u, "created_at", None) or (u.get("created_at") if isinstance(u, dict) else None)
            mappa[str(uid)] = (email, str(creato) if creato else None)
        return mappa
    except Exception as e:
        logger.warning("Elenco utenti Auth non disponibile: %s: %s", type(e).__name__, e)
        return {}


@router.get("/admin/agenti/{agente_id}/clienti")
def clienti_agente(agente_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] I singoli clienti attribuiti a un agente, con quanto hanno
    acquistato e quanto manca alla scadenza della finestra provvigionabile.

    La finestra parte da agente_attribuito_at: e' quella la data che conta
    per il compenso, non la registrazione dell'account.
    """
    _verifica_admin(user_id)

    try:
        profili = (supabase.table("profiles").select("*")
                   .eq("agente_id", agente_id).execute().data or [])
        if not profili:
            return []

        ids = [str(p["id"]) for p in profili]
        prov = (supabase.table("provvigioni")
                .select("user_id,importo_ordine_eur,importo_eur,stato,created_at")
                .eq("agente_id", agente_id).execute().data or [])

        # L'email sta in profiles solo se e' stata replicata li': altrimenti
        # si interroga Auth una volta sola per tutti i clienti.
        serve_auth = any(not p.get("email") for p in profili)
        da_auth = _mappa_email_utenti() if serve_auth else {}

        adesso = datetime.now(timezone.utc)
        clienti = []
        for p in profili:
            pid = str(p["id"])
            email_auth, creato_auth = da_auth.get(pid, (None, None))
            mie = [x for x in prov if str(x["user_id"]) == pid]

            giorni_residui = None
            scaduto = False
            attribuito = p.get("agente_attribuito_at")
            if attribuito:
                data_attr = datetime.fromisoformat(str(attribuito).replace("Z", "+00:00"))
                if data_attr.tzinfo is None:
                    data_attr = data_attr.replace(tzinfo=timezone.utc)
                giorni_residui = 365 - (adesso - data_attr).days
                scaduto = giorni_residui <= 0

            clienti.append({
                "user_id": pid,
                "email": p.get("email") or email_auth,
                "registrato_at": p.get("created_at") or creato_auth,
                "attribuito_at": attribuito,
                "giorni_residui": giorni_residui,
                "finestra_scaduta": scaduto,
                "ordini": len(mie),
                # Solo i pass: le ricariche token non generano provvigione e
                # quindi non compaiono qui.
                "acquistato_eur": round(sum(float(x.get("importo_ordine_eur") or 0) for x in mie), 2),
                "maturato_eur": round(sum(float(x["importo_eur"]) for x in mie if x["stato"] == "maturata"), 2),
                "liquidato_eur": round(sum(float(x["importo_eur"]) for x in mie if x["stato"] == "liquidata"), 2),
                "ultimo_ordine_at": max((x.get("created_at") for x in mie if x.get("created_at")), default=None),
            })

        # Prima chi ha speso di piu': e' il dato che serve leggere per primo.
        clienti.sort(key=lambda c: (-c["acquistato_eur"], c["email"] or ""))
        return clienti
    except Exception as e:
        logger.error("Dettaglio clienti agente %s fallito: %s: %s",
                     agente_id, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Dettaglio clienti non disponibile.")


@router.delete("/admin/clienti/{cliente_id}/attribuzione")
def rimuovi_attribuzione(cliente_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Sgancia un cliente dal suo agente.

    Serve per correggere le attribuzioni sbagliate (un test, un link aperto
    per errore). Se il cliente ha gia' generato provvigioni non si tocca
    nulla: quelle sono documenti contabili e vanno annullate una per una.
    """
    _verifica_admin(user_id)

    prov = (supabase.table("provvigioni").select("id")
            .eq("user_id", cliente_id).limit(1).execute())
    if prov.data:
        raise HTTPException(
            status_code=400,
            detail="Il cliente ha gia' provvigioni registrate: annullale prima di rimuovere l'attribuzione.",
        )

    r = (supabase.table("profiles")
         .update({"agente_id": None, "agente_attribuito_at": None})
         .eq("id", cliente_id).execute())
    if not r.data:
        raise HTTPException(status_code=404, detail="Cliente non trovato.")
    logger.info("Attribuzione rimossa per il cliente %s (admin %s).", cliente_id, user_id)
    return {"status": "rimossa"}


@router.get("/admin/provvigioni")
def elenco_provvigioni(user_id: str = Depends(get_current_user)):
    """[ADMIN] Tutte le provvigioni, dalla più recente."""
    _verifica_admin(user_id)
    r = (supabase.table("provvigioni").select("*")
         .order("created_at", desc=True).limit(200).execute())
    return r.data or []


@router.post("/admin/provvigioni/{provvigione_id}/liquida")
def liquida_provvigione(provvigione_id: str, user_id: str = Depends(get_current_user)):
    """[ADMIN] Segna una provvigione come pagata all'agente."""
    _verifica_admin(user_id)
    r = (supabase.table("provvigioni")
         .update({"stato": "liquidata", "liquidata_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", provvigione_id).eq("stato", "maturata").execute())
    if not r.data:
        raise HTTPException(status_code=400, detail="Provvigione inesistente o già liquidata.")
    return {"status": "liquidata"}