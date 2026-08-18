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


def _email_offuscata(email):
    """
    Email ridotta per l'agente: `ga…re@azienda.it`.

    L'agente NON deve ricevere il recapito dei clienti: dopo la segnalazione
    il cliente e' un contatto della piattaforma, e trasmettergli l'indirizzo
    completo sarebbe una comunicazione di dato personale a un terzo senza
    base giuridica. Restano i due caratteri iniziali e finali, quanto basta
    perche' l'agente riconosca un'azienda che ha segnalato lui.
    """
    if not email or "@" not in str(email):
        return None
    locale, _, dominio = str(email).partition("@")
    if len(locale) <= 4:
        # Troppo corto per mascherare in modo utile: si tiene solo l'iniziale.
        locale_mascherato = (locale[:1] or "?") + "…"
    else:
        locale_mascherato = f"{locale[:2]}…{locale[-2:]}"
    return f"{locale_mascherato}@{dominio}"


def _agente_del_chiamante(user_id):
    """
    Riga `agenti` collegata a chi sta chiamando, o 404.

    E' il perno del pannello agente: ogni endpoint filtra su QUESTO id, mai su
    un parametro ricevuto dal client. Cosi' un agente non puo' leggere i dati
    di un altro cambiando un valore nella richiesta.
    """
    r = (supabase.table("agenti").select("id,nome,codice,percentuale,attivo")
         .eq("user_id", user_id).limit(1).execute())
    if not r.data:
        raise HTTPException(status_code=403, detail="Questo account non è collegato a nessun agente.")
    return r.data[0]


@router.get("/mio-riepilogo")
def mio_riepilogo(user_id: str = Depends(get_current_user)):
    """
    [AGENTE] Il proprio riepilogo: clienti segnalati e compensi.

    Sola lettura. L'agente NON vede i progetti dei clienti, il codice caricato
    o i loro consumi, e non puo' liquidare le proprie provvigioni.
    """
    ag = _agente_del_chiamante(user_id)

    try:
        prov = (supabase.table("provvigioni")
                .select("user_id,importo_ordine_eur,importo_eur,percentuale,stato,created_at,liquidata_at")
                .eq("agente_id", ag["id"]).order("created_at", desc=True).execute().data or [])
        profili = (supabase.table("profiles").select("id,email,agente_attribuito_at")
                   .eq("agente_id", ag["id"]).execute().data or [])

        adesso = datetime.now(timezone.utc)
        clienti = []
        for p in profili:
            pid = str(p["id"])
            mie = [x for x in prov if str(x["user_id"]) == pid]
            giorni = None
            attribuito = p.get("agente_attribuito_at")
            if attribuito:
                d = datetime.fromisoformat(str(attribuito).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                giorni = 365 - (adesso - d).days
            clienti.append({
                "email": _email_offuscata(p.get("email")),
                "attribuito_at": attribuito,
                "giorni_residui": giorni,
                "ordini": len(mie),
                "acquistato_eur": round(sum(float(x.get("importo_ordine_eur") or 0) for x in mie), 2),
                "compenso_eur": round(sum(float(x["importo_eur"]) for x in mie
                                          if x["stato"] in ("maturata", "liquidata")), 2),
            })
        clienti.sort(key=lambda c: -c["acquistato_eur"])

        # Le righe annullate NON entrano nei totali: sono ordini rimborsati
        # o registrazioni di prova.
        return {
            "agente": {"nome": ag["nome"], "codice": ag["codice"],
                       "percentuale": ag["percentuale"], "attivo": ag["attivo"]},
            "clienti": clienti,
            "movimenti": [
                {k: m.get(k) for k in
                 ("importo_ordine_eur", "importo_eur", "percentuale", "stato", "created_at", "liquidata_at")}
                for m in prov
            ],
            "da_liquidare_eur": round(sum(float(m["importo_eur"]) for m in prov
                                          if m["stato"] == "maturata"), 2),
            "liquidato_eur": round(sum(float(m["importo_eur"]) for m in prov
                                       if m["stato"] == "liquidata"), 2),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Riepilogo agente fallito per %s: %s: %s", user_id, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Riepilogo non disponibile.")


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


def _normalizza_iban(iban):
    """
    IBAN ripulito e verificato, o None se vuoto.

    Il controllo mod-97 (ISO 13616) intercetta refusi e cifre invertite: un
    IBAN sbagliato significa un bonifico rifiutato o, peggio, inviato a un
    altro conto. Vale la pena bloccarlo in inserimento invece di scoprirlo
    al momento del pagamento.
    """
    if not iban:
        return None
    pulito = re.sub(r"\s", "", str(iban)).upper()
    if not pulito:
        return None

    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", pulito):
        raise HTTPException(
            status_code=400,
            detail="IBAN non valido: deve iniziare con due lettere del paese, due cifre di controllo e proseguire con lettere o numeri.",
        )

    # mod-97: si spostano i primi 4 caratteri in coda e le lettere diventano
    # numeri (A=10 ... Z=35); il resto della divisione per 97 deve dare 1.
    riordinato = pulito[4:] + pulito[:4]
    numerico = "".join(str(int(c, 36)) for c in riordinato)
    if int(numerico) % 97 != 1:
        raise HTTPException(
            status_code=400,
            detail="IBAN non valido: le cifre di controllo non corrispondono. Verifica di averlo copiato correttamente.",
        )
    return pulito


class InputAgente(BaseModel):
    nome: str
    email: str = ""               # ricavata dall'account se si passa user_id
    user_id: str = ""             # account registrato da promuovere ad agente
    codice: str = ""              # se vuoto viene generato dal nome
    percentuale: float = 30.0
    partita_iva: str = ""
    iban: str = ""                # conto su cui liquidare le provvigioni
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

@router.get("/admin/utenti-selezionabili")
def utenti_selezionabili(user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Account registrati che possono diventare agenti.

    L'agente deve prima registrarsi normalmente sulla piattaforma: qui si
    sceglie il suo account e lo si promuove. Sono esclusi gli admin e chi e'
    gia' collegato a un agente, cosi' l'elenco mostra solo scelte valide.
    """
    _verifica_admin(user_id)

    try:
        # select("*") e non un elenco di colonne: se una di queste non esiste
        # nello schema, Supabase risponde con un errore e l'elenco arriva vuoto
        # al frontend, che mostra "nessun account" invece del guasto reale.
        profili = (supabase.table("profiles").select("*").execute().data or [])
        gia_agenti = {str(a.get("user_id")) for a in
                      (supabase.table("agenti").select("user_id").execute().data or [])
                      if a.get("user_id")}

        elenco = []
        for p in profili:
            pid = str(p.get("id"))
            if p.get("role") == "admin" or pid in gia_agenti:
                continue
            elenco.append({
                "user_id": pid,
                "email": p.get("email"),
                "registrato_at": p.get("created_at"),
                # Chi e' gia' cliente di un agente crea un conflitto di ruolo:
                # va segnalato a chi sceglie, non escluso a priori.
                "gia_cliente_di_un_agente": bool(p.get("agente_id")),
            })
        elenco.sort(key=lambda u: (u["email"] or "").lower())
        logger.info("Account selezionabili come agente: %d su %d profili.",
                    len(elenco), len(profili))
        return elenco
    except Exception as e:
        logger.error("Elenco utenti selezionabili fallito: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Elenco utenti non disponibile.")


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
    account = (dati.user_id or "").strip()

    # L'agente deve avere un account: e' cosi' che accede al proprio riepilogo.
    # L'email si ricava dal profilo, non da quanto digitato: evita il caso in
    # cui l'agente riceva il link su un indirizzo e acceda con un altro.
    if account:
        p = (supabase.table("profiles").select("id,email,role")
             .eq("id", account).limit(1).execute())
        if not p.data:
            raise HTTPException(status_code=400, detail="Account selezionato inesistente.")
        if p.data[0].get("role") == "admin":
            raise HTTPException(status_code=400, detail="Un account admin non può essere reso agente.")
        gia = (supabase.table("agenti").select("id,nome")
               .eq("user_id", account).limit(1).execute())
        if gia.data:
            raise HTTPException(
                status_code=400,
                detail=f"Questo account è già collegato all'agente '{gia.data[0].get('nome')}'.",
            )
        email = (p.data[0].get("email") or email).strip().lower()

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
            "user_id": account or None,
            "percentuale": float(dati.percentuale),
            "partita_iva": (dati.partita_iva or "").strip() or None,
            "iban": _normalizza_iban(dati.iban),
            "note": (dati.note or "").strip() or None,
            "attivo": True,
        }).execute()

        if account:
            # Il ruolo abilita il pannello agente. Non tocca l'attribuzione
            # come cliente: se ha gia' acquistato, quegli ordini restano suoi.
            try:
                supabase.table("profiles").update({"role": "agente"}).eq("id", account).execute()
            except Exception as e:
                # L'agente e' creato: senza il ruolo non vedra' il proprio
                # pannello, ma il compenso matura lo stesso. Va sistemato a mano.
                logger.error("Ruolo 'agente' non assegnato a %s (%s): assegnarlo manualmente.",
                             account, type(e).__name__)

        logger.info("Agente '%s' creato con codice %s (account: %s).",
                    nome, codice, account or "nessuno")
        return r.data[0] if r.data else {"codice": codice}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Creazione agente fallita: %s", e)
        raise HTTPException(status_code=500, detail="Creazione non riuscita.")


class InputDatiPagamento(BaseModel):
    iban: str = ""
    partita_iva: str = ""


@router.post("/admin/agenti/{agente_id}/dati-pagamento")
def aggiorna_dati_pagamento(agente_id: str, dati: InputDatiPagamento,
                            user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Aggiorna IBAN e partita IVA di un agente.

    Sono dati che cambiano nel tempo (cambio banca, apertura partita IVA) e
    devono poter essere corretti senza ricreare l'agente, che invaliderebbe
    il codice e quindi tutti i link gia' distribuiti.
    """
    _verifica_admin(user_id)

    aggiornamento = {
        "iban": _normalizza_iban(dati.iban),
        "partita_iva": (dati.partita_iva or "").strip() or None,
    }
    r = supabase.table("agenti").update(aggiornamento).eq("id", agente_id).execute()
    if not r.data:
        raise HTTPException(status_code=404, detail="Agente non trovato.")
    logger.info("Dati di pagamento aggiornati per l'agente %s.", agente_id)
    return {"status": "aggiornato"}


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
def elenco_provvigioni(
    agente_id: str = "",
    cliente_id: str = "",
    stato: str = "",
    user_id: str = Depends(get_current_user),
):
    """
    [ADMIN] Provvigioni con nome agente ed email cliente, dalla più recente.

    Senza l'arricchimento la tabella mostrerebbe solo UUID: inutilizzabile per
    capire chi ha comprato cosa e a quale agente spetta il compenso.
    I filtri sono opzionali e si combinano fra loro.
    """
    _verifica_admin(user_id)

    try:
        q = supabase.table("provvigioni").select("*")
        if agente_id:
            q = q.eq("agente_id", agente_id)
        if cliente_id:
            q = q.eq("user_id", cliente_id)
        if stato:
            q = q.eq("stato", stato)
        righe = q.order("created_at", desc=True).limit(500).execute().data or []
        if not righe:
            return []

        agenti = {a["id"]: a for a in
                  (supabase.table("agenti").select("id,nome,codice").execute().data or [])}

        # L'email puo' non essere replicata in profiles: in quel caso l'unica
        # fonte e' auth.users, gia' interrogata da _mappa_email_utenti.
        profili = {str(p["id"]): p for p in
                   (supabase.table("profiles").select("*").execute().data or [])}
        serve_auth = any(not profili.get(str(r["user_id"]), {}).get("email") for r in righe)
        da_auth = _mappa_email_utenti() if serve_auth else {}

        for r in righe:
            ag = agenti.get(r.get("agente_id")) or {}
            r["agente_nome"] = ag.get("nome")
            r["agente_codice"] = ag.get("codice")
            pid = str(r.get("user_id"))
            email_auth, _ = da_auth.get(pid, (None, None))
            r["cliente_email"] = (profili.get(pid) or {}).get("email") or email_auth
        return righe
    except Exception as e:
        logger.error("Elenco provvigioni fallito: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Elenco provvigioni non disponibile.")


@router.post("/admin/provvigioni/{provvigione_id}/annulla")
def annulla_provvigione(provvigione_id: str, user_id: str = Depends(get_current_user)):
    """
    [ADMIN] Annulla una provvigione (ordine rimborsato, test, errore).

    Non si elimina la riga: resta a storico con stato 'annullata', perche' e'
    un documento contabile. Una provvigione gia' liquidata NON si annulla:
    il denaro e' uscito, va gestito con una nota di credito.
    """
    _verifica_admin(user_id)
    r = (supabase.table("provvigioni")
         .update({"stato": "annullata"})
         .eq("id", provvigione_id).eq("stato", "maturata").execute())
    if not r.data:
        raise HTTPException(
            status_code=400,
            detail="Provvigione inesistente, già liquidata o già annullata.",
        )
    logger.info("Provvigione %s annullata dall'admin %s.", provvigione_id, user_id)
    return {"status": "annullata"}


@router.post("/admin/provvigioni/{provvigione_id}/liquida")
def liquida_provvigione(provvigione_id: str, user_id: str = Depends(get_current_user)):
    """[ADMIN] Segna una provvigione come pagata all'agente."""
    _verifica_admin(user_id)

    # Senza IBAN il bonifico non e' stato fatto: segnare "liquidata" creerebbe
    # una discrepanza fra quanto risulta pagato e quanto e' uscito davvero.
    p = (supabase.table("provvigioni").select("agente_id")
         .eq("id", provvigione_id).limit(1).execute())
    if p.data:
        a = (supabase.table("agenti").select("iban,nome")
             .eq("id", p.data[0]["agente_id"]).limit(1).execute())
        if a.data and not a.data[0].get("iban"):
            raise HTTPException(
                status_code=400,
                detail=f"Manca l'IBAN di {a.data[0].get('nome')}: inseriscilo prima di registrare il pagamento.",
            )

    r = (supabase.table("provvigioni")
         .update({"stato": "liquidata", "liquidata_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", provvigione_id).eq("stato", "maturata").execute())
    if not r.data:
        raise HTTPException(status_code=400, detail="Provvigione inesistente o già liquidata.")
    return {"status": "liquidata"}