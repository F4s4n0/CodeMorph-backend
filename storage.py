"""
Persistenza degli artefatti di sessione su Supabase Storage.

PERCHÉ ESISTE: il filesystem di Render è effimero — ogni deploy/riavvio
cancella WORKSPACE_DIR e con esso tutte le sessioni in corso. Questo modulo
salva lo zip di ogni fase su un bucket Supabase subito dopo la creazione e
lo ripristina automaticamente quando i file locali mancano, rendendo le
sessioni sopravvissute ai deploy senza richiedere un disco persistente.

Prerequisito (una tantum): creare su Supabase -> Storage un bucket PRIVATO
chiamato 'sessioni'. Nessuna variabile d'ambiente aggiuntiva: usa il client
già autenticato con la service role key (auth.supabase).

Tutte le funzioni sono best-effort: un errore di storage viene loggato ma
non interrompe mai la pipeline (meglio una fase completata senza backup che
una fase fallita per un problema di rete verso il bucket).
"""

import logging
import os
import zipfile

from auth import supabase

logger = logging.getLogger(__name__)

BUCKET_SESSIONI = "sessioni"

# Ordine di ripristino: si estraggono in sequenza sovrapponendo i contenuti.
# I SORGENTI stanno in un backup a parte (vedi sotto) e vengono ripristinati
# per primi: prima erano dentro lo zip di fase1, che per un applicativo reale
# arrivava a 50 MB — ricompressi e ricaricati a ogni esecuzione della fase, e
# consegnati al cliente che si riscaricava il proprio stesso codice.
FASI_IN_ORDINE = ("fase1", "fase2", "finale")

# Cartella dei sorgenti caricati dall'utente: serve alla Fase 3 dopo un
# riavvio di Render, ma NON va consegnata: il cliente ce l'ha gia'.
NOME_SORGENTI = "sorgenti_originali"


def _percorso_remoto(session_id, fase):
    return f"{session_id}/{fase}.zip"


def salva_zip_fase(session_id, fase, percorso_zip_locale):
    """
    Carica (con sovrascrittura) lo zip di una fase sul bucket.
    Da chiamare subito dopo la creazione dello zip in main.py.
    """
    try:
        with open(percorso_zip_locale, "rb") as f:
            contenuto = f.read()
        supabase.storage.from_(BUCKET_SESSIONI).upload(
            path=_percorso_remoto(session_id, fase),
            file=contenuto,
            file_options={"content-type": "application/zip", "upsert": "true"},
        )
        logger.info(
            "Backup su Storage: %s/%s.zip (%.1f KB)",
            session_id, fase, len(contenuto) / 1024,
        )
        return True
    except Exception as e:
        logger.warning("Backup su Storage fallito per %s/%s: %s", session_id, fase, e)
        return False


def scarica_zip_fase(session_id, fase, percorso_zip_locale):
    """Scarica lo zip di una fase dal bucket sul disco locale. True se riuscito."""
    try:
        contenuto = supabase.storage.from_(BUCKET_SESSIONI).download(
            _percorso_remoto(session_id, fase)
        )
        if not contenuto:
            return False
        os.makedirs(os.path.dirname(percorso_zip_locale), exist_ok=True)
        with open(percorso_zip_locale, "wb") as f:
            f.write(contenuto)
        return True
    except Exception:
        # Assenza del file sul bucket = caso normale (fase mai eseguita)
        return False


def _estrai_sicuro(percorso_zip, destinazione):
    """Estrazione con protezione zip-slip (stessa logica di main._estrai_zip_sicuro)."""
    destinazione_abs = os.path.abspath(destinazione)
    with zipfile.ZipFile(percorso_zip, "r") as zf:
        for membro in zf.infolist():
            finale = os.path.abspath(os.path.join(destinazione_abs, membro.filename))
            if not finale.startswith(destinazione_abs + os.sep) and finale != destinazione_abs:
                raise ValueError(f"Percorso non sicuro nello zip di backup: {membro.filename}")
        zf.extractall(destinazione_abs)


def _percorso_remoto_sorgenti(session_id):
    return f"{session_id}/sorgenti.zip"


def salva_sorgenti(session_id, cartella_sessione):
    """
    Carica i sorgenti dell'utente in un backup DEDICATO, una volta sola.

    Sono decine di megabyte che non cambiano piu' dopo l'upload: tenerli
    dentro lo zip di fase1 significava ricomprimerli e ricaricarli a ogni
    esecuzione, con minuti di attesa e banda sprecata a ogni giro.

    Se il backup esiste gia' non viene rifatto: e' immutabile.
    """
    cartella_sorgenti = os.path.join(cartella_sessione, NOME_SORGENTI)
    if not os.path.isdir(cartella_sorgenti):
        return False

    percorso_temporaneo = os.path.join(cartella_sessione, "_sorgenti_backup.zip")
    try:
        # Gia' su Storage: nulla da fare, i sorgenti non cambiano piu'.
        if _esiste_sorgenti(session_id):
            logger.info("Sorgenti gia' su Storage per %s: backup saltato.", session_id)
            return True

        with zipfile.ZipFile(percorso_temporaneo, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(cartella_sorgenti):
                for nome in files:
                    completo = os.path.join(root, nome)
                    # Percorsi relativi alla cartella di SESSIONE, così il
                    # ripristino ricrea 'sorgenti_originali/...' al posto giusto.
                    zf.write(completo, os.path.relpath(completo, cartella_sessione))

        with open(percorso_temporaneo, "rb") as f:
            contenuto = f.read()
        supabase.storage.from_(BUCKET_SESSIONI).upload(
            path=_percorso_remoto_sorgenti(session_id),
            file=contenuto,
            file_options={"content-type": "application/zip", "upsert": "true"},
        )
        logger.info("Backup sorgenti su Storage: %s (%.1f KB)", session_id, len(contenuto) / 1024)
        return True
    except Exception as e:
        logger.warning("Backup sorgenti fallito per %s: %s", session_id, e)
        return False
    finally:
        try:
            os.remove(percorso_temporaneo)
        except OSError:
            pass


def _esiste_sorgenti(session_id):
    """True se il backup dei sorgenti e' gia' sul bucket."""
    try:
        elenco = supabase.storage.from_(BUCKET_SESSIONI).list(session_id)
        return any((v.get("name") if isinstance(v, dict) else getattr(v, "name", "")) == "sorgenti.zip"
                   for v in (elenco or []))
    except Exception:
        return False


def ripristina_sorgenti(session_id, cartella_sessione):
    """Riporta su disco i sorgenti dal backup dedicato. True se riuscito."""
    zip_temporaneo = os.path.join(cartella_sessione, "_restore_sorgenti.zip")
    try:
        contenuto = supabase.storage.from_(BUCKET_SESSIONI).download(
            _percorso_remoto_sorgenti(session_id)
        )
        if not contenuto:
            return False
        os.makedirs(cartella_sessione, exist_ok=True)
        with open(zip_temporaneo, "wb") as f:
            f.write(contenuto)
        _estrai_sicuro(zip_temporaneo, cartella_sessione)
        logger.info("Sessione %s: sorgenti ripristinati da Storage.", session_id)
        return True
    except Exception:
        # Assenza = sessione creata prima di questa funzionalita', oppure
        # codice incollato invece di uno ZIP: nessun sorgente da ripristinare.
        return False
    finally:
        try:
            os.remove(zip_temporaneo)
        except OSError:
            pass


def ripristina_sessione(session_id, cartella_sessione):
    """
    Ricostruisce la cartella di sessione dai backup sul bucket, estraendo
    gli zip delle fasi in ordine (fase1 -> fase2 -> finale) e sovrapponendoli.
    Ritorna la lista delle fasi ripristinate (vuota se nessun backup esiste).
    """
    ripristinate = []
    os.makedirs(cartella_sessione, exist_ok=True)

    # I sorgenti PRIMA delle fasi: servono alla Fase 3 e stanno in un backup
    # dedicato, non piu' dentro lo zip di fase1.
    if ripristina_sorgenti(session_id, cartella_sessione):
        ripristinate.append("sorgenti")

    for fase in FASI_IN_ORDINE:
        zip_temporaneo = os.path.join(cartella_sessione, f"_restore_{fase}.zip")
        if not scarica_zip_fase(session_id, fase, zip_temporaneo):
            continue
        try:
            _estrai_sicuro(zip_temporaneo, cartella_sessione)
            ripristinate.append(fase)
            logger.info("Sessione %s: ripristinata %s da Storage.", session_id, fase)
        except Exception as e:
            logger.error("Ripristino %s fallito per la sessione %s: %s", fase, session_id, e)
        finally:
            try:
                os.remove(zip_temporaneo)
            except OSError:
                pass

    if not ripristinate:
        logger.info("Nessun backup su Storage per la sessione %s.", session_id)
    return ripristinate


def elimina_backup_sessione(session_id):
    """Rimuove i backup di una sessione dal bucket (per la cancellazione admin)."""
    try:
        percorsi = [_percorso_remoto(session_id, fase) for fase in FASI_IN_ORDINE]
        percorsi.append(_percorso_remoto_sorgenti(session_id))
        supabase.storage.from_(BUCKET_SESSIONI).remove(percorsi)
        logger.info("Backup su Storage rimossi per la sessione %s.", session_id)
        return True
    except Exception as e:
        logger.warning("Rimozione backup Storage fallita per %s: %s", session_id, e)
        return False
