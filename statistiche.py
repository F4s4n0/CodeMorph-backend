"""
Statistiche aggregate della piattaforma.

Servono a due cose diverse:
1. capire come sta andando (quante analisi, quanto costano, quanto rendono);
2. avere numeri REALI da usare un domani come prova sociale sul sito.

Il secondo punto e' il motivo per cui questo modulo esiste ora e non fra sei
mesi: pubblicare un contatore richiede di averlo misurato prima, e i numeri
vanno lasciati crescere finche' non sono presentabili. Finche' sono piccoli
restano qui, visibili solo all'admin.

Tutte le letture usano select("*") e .get(): lo schema e' cambiato piu' volte
e un nome di colonna sbagliato farebbe fallire l'intera pagina invece di
lasciare vuoto un singolo dato.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user, supabase
from payments import _verifica_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin")


def _righe(tabella):
    """Legge una tabella intera senza far fallire tutto se non esiste."""
    try:
        return supabase.table(tabella).select("*").execute().data or []
    except Exception as e:
        logger.warning("Tabella '%s' non leggibile (%s): statistica parziale.",
                       tabella, type(e).__name__)
        return []


def _data(valore):
    if not valore:
        return None
    try:
        d = datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


@router.get("/statistiche")
def statistiche(user_id: str = Depends(get_current_user)):
    """[ADMIN] Numeri aggregati su analisi, costi, clienti e rete agenti."""
    _verifica_admin(user_id)

    try:
        sessioni = _righe("migration_sessions")
        profili = _righe("profiles")
        movimenti = _righe("token_transactions")
        provvigioni = _righe("provvigioni")
        agenti = _righe("agenti")
        prove = _righe("trial_bonuses")

        adesso = datetime.now(timezone.utc)
        ultimi_30 = adesso - timedelta(days=30)

        # --- Analisi ----------------------------------------------------
        completate = 0
        file_migrati = 0
        file_generati = 0
        righe_legacy = 0
        caratteri_legacy = 0
        file_legacy = 0
        rilievi_critici = 0
        rilievi_alti = 0
        linguaggi = Counter()
        modelli = Counter()
        sessioni_30gg = 0

        for s in sessioni:
            aggiornata = _data(s.get("updated_at"))
            if aggiornata and aggiornata >= ultimi_30:
                sessioni_30gg += 1
            if s.get("current_step") == "final":
                completate += 1
            if s.get("provider_llm"):
                modelli[s["provider_llm"]] += 1

            righe_legacy += int(s.get("righe_legacy") or 0)
            caratteri_legacy += int(s.get("caratteri_legacy") or 0)
            file_legacy += int(s.get("file_legacy_analizzati") or 0)
            rilievi_critici += int(s.get("rilievi_critici") or 0)
            rilievi_alti += int(s.get("rilievi_alti") or 0)

            r = s.get("risultato") or {}
            if isinstance(r, dict):
                file_migrati += len(r.get("file_migrati") or [])
                estratti = r.get("file_sorgente_estratti") or {}
                if isinstance(estratti, dict):
                    file_generati += (estratti.get("backend") or 0) + (estratti.get("frontend") or 0)
                if r.get("linguaggio_target"):
                    linguaggi[r["linguaggio_target"]] += 1

        # --- Economia ---------------------------------------------------
        # I movimenti negativi sono consumi, i positivi ricariche e pass.
        costo_elaborazioni = sum(
            abs(float(m.get("importo_eur") or 0)) for m in movimenti
            if float(m.get("importo_eur") or 0) < 0
        )
        token_totali = sum(int(m.get("tokens_totali") or 0) for m in movimenti)

        maturato = sum(float(p.get("importo_eur") or 0) for p in provvigioni
                       if p.get("stato") == "maturata")
        liquidato = sum(float(p.get("importo_eur") or 0) for p in provvigioni
                        if p.get("stato") == "liquidata")
        venduto_via_agenti = sum(float(p.get("importo_ordine_eur") or 0) for p in provvigioni
                                 if p.get("stato") in ("maturata", "liquidata"))

        # --- Persone ----------------------------------------------------
        iscritti_30gg = sum(1 for p in profili
                            if (_data(p.get("created_at")) or adesso) >= ultimi_30)
        attribuiti = sum(1 for p in profili if p.get("agente_id"))
        prove_usate = sum(1 for p in prove if p.get("used_at"))

        return {
            "analisi": {
                "sessioni_totali": len(sessioni),
                "sessioni_completate": completate,
                "sessioni_ultimi_30gg": sessioni_30gg,
                "righe_legacy_analizzate": righe_legacy,
                "caratteri_legacy_analizzati": caratteri_legacy,
                "file_legacy_analizzati": file_legacy,
                "file_legacy_migrati": file_migrati,
                "file_progetto_generati": file_generati,
                "rilievi_critici": rilievi_critici,
                "rilievi_alti": rilievi_alti,
                "rilievi_totali": rilievi_critici + rilievi_alti,
                "linguaggi_target": dict(linguaggi),
                "provider_usati": dict(modelli),
            },
            "economia": {
                "costo_elaborazioni_eur": round(costo_elaborazioni, 2),
                "token_totali": token_totali,
                "venduto_via_agenti_eur": round(venduto_via_agenti, 2),
                "provvigioni_da_liquidare_eur": round(maturato, 2),
                "provvigioni_liquidate_eur": round(liquidato, 2),
            },
            "persone": {
                "utenti_totali": len(profili),
                "iscritti_ultimi_30gg": iscritti_30gg,
                "clienti_attribuiti_ad_agenti": attribuiti,
                "agenti_attivi": sum(1 for a in agenti if a.get("attivo")),
                "prove_concesse": len(prove),
                "prove_utilizzate": prove_usate,
            },
            # Da qui in avanti righe analizzate e rilievi vengono persistiti
            # (migrazione_metriche.sql + crew.py). Le sessioni precedenti a
            # quel deploy restano a zero: non sono recuperabili.
            "note": [
                "Righe e rilievi sono contati solo dalle sessioni successive all'attivazione del tracciamento.",
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Statistiche non calcolate: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Statistiche non disponibili.")


# ---------------------------------------------------------------------
# Le metriche di analisi (righe_legacy, rilievi_*) vengono scritte da crew.py
# su migration_sessions: la Fase 1 misura il sorgente, la Fase 3 conta i
# rilievi del Quality Check. Richiedono migrazione_metriche.sql.
# ---------------------------------------------------------------------
