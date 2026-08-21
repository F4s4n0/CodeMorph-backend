"""
Invio email via SMTP.

Volutamente minimale: la libreria standard basta, nessuna dipendenza e
nessuna chiave API in piu'. Le credenziali sono le stesse gia' configurate
su Supabase per il recupero password.

Due principi:
1. Una notifica NON deve mai bloccare o rallentare la richiesta che l'ha
   generata. L'invio parte in un thread e ogni errore viene solo loggato:
   se la posta non parte, il contatto e' comunque salvato.
2. Se l'SMTP non e' configurato il modulo resta silente. Cosi' in locale o
   in un ambiente senza credenziali non si rompe nulla.

Cosa NON si notifica, di proposito: il completamento delle fasi. La pipeline
si ferma comunque ai checkpoint e il cliente deve rientrare per validare, con
i log completi a disposizione. Mandare una mail per ogni fase di ogni sessione
significherebbe centinaia di messaggi che dicono una cosa gia' nota.
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# Con Gmail il mittente DEVE coincidere con l'utenza autenticata, altrimenti
# l'invio viene rifiutato: se non specificato si usa SMTP_USER.
SMTP_MITTENTE = os.getenv("SMTP_MITTENTE", SMTP_USER)
SMTP_NOME_MITTENTE = os.getenv("SMTP_NOME_MITTENTE", "CodeMorph.AI")
# Dove arrivano le notifiche interne (nuovo contatto, nuovo bonifico...).
EMAIL_ADMIN = os.getenv("EMAIL_ADMIN", SMTP_USER)


def smtp_configurato():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _invia_ora(destinatario, oggetto, testo, rispondi_a=None):
    """Invio sincrono: chiamato solo dal thread di invia_email()."""
    messaggio = EmailMessage()
    messaggio["From"] = formataddr((SMTP_NOME_MITTENTE, SMTP_MITTENTE))
    messaggio["To"] = destinatario
    messaggio["Subject"] = oggetto
    if rispondi_a:
        # Rispondendo alla notifica si scrive al cliente, non a se stessi.
        messaggio["Reply-To"] = rispondi_a
    messaggio.set_content(testo)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(messaggio)


def invia_email(destinatario, oggetto, testo, rispondi_a=None):
    """
    Invia in BACKGROUND. Non solleva mai e non attende: il chiamante prosegue
    subito, perche' un server di posta lento non deve far aspettare un utente
    che ha appena premuto "invia".
    """
    if not smtp_configurato():
        logger.info("SMTP non configurato: notifica '%s' non inviata.", oggetto)
        return
    if not destinatario:
        return

    def _lavoro():
        try:
            _invia_ora(destinatario, oggetto, testo, rispondi_a)
            logger.info("Email inviata a %s: %s", destinatario, oggetto)
        except Exception as e:
            # Best-effort: il dato e' gia' salvato, la mail e' un di piu'.
            logger.error("Invio email a %s fallito (%s: %s).",
                         destinatario, type(e).__name__, e)

    threading.Thread(target=_lavoro, daemon=True).start()


# =====================================================================
# Notifiche specifiche
# =====================================================================

def notifica_nuovo_contatto(dati):
    """
    [ADMIN] Nuova richiesta dal modulo contatti.

    E' la notifica piu' utile: senza, una richiesta resta invisibile finche'
    non si apre il pannello, e un lead che aspetta due giorni e' perso.
    Il corpo contiene tutta la qualificazione, cosi' si puo' rispondere dal
    telefono senza aprire il computer.
    """
    etichette = {
        "prova_gratuita": "Prova gratuita", "informazioni": "Informazioni",
        "preventivo": "Preventivo", "partnership": "Partnership", "supporto": "Supporto",
        "titolare": "Titolare", "responsabile_it": "Responsabile IT",
        "sviluppatore": "Sviluppatore", "consulente": "Consulente", "altro": "Altro",
        "visual_foxpro": "Visual FoxPro", "vb6": "VB6", "delphi": "Delphi",
        "cobol": "COBOL", "rpg": "RPG/AS400", "access": "Access",
        "powerbuilder": "PowerBuilder", "non_so": "Non sa",
        "piccolo": "Piccolo", "medio": "Medio", "grande": "Grande",
    }
    def et(v):
        return etichette.get(v, v) if v else "—"

    motivo = et(dati.get("motivo"))
    righe = [
        f"Nuova richiesta di contatto — {motivo}",
        "",
        f"Azienda:      {dati.get('azienda') or '—'}",
        f"Referente:    {dati.get('name') or '—'} ({et(dati.get('ruolo'))})",
        f"Email:        {dati.get('email') or '—'}",
        f"Telefono:     {dati.get('telefono') or '—'}",
        "",
        f"Tecnologia:   {et(dati.get('tecnologia_legacy'))}",
        f"Dimensione:   {et(dati.get('dimensione_progetto'))}",
        f"Provenienza:  {dati.get('origine') or '—'}",
        "",
        "Messaggio:",
        (dati.get("message") or "").strip(),
        "",
        "—",
        "Rispondendo a questa email scrivi direttamente al cliente.",
    ]
    invia_email(
        EMAIL_ADMIN,
        f"[CodeMorph] {motivo} — {dati.get('azienda') or dati.get('name') or 'nuovo contatto'}",
        "\n".join(righe),
        rispondi_a=dati.get("email"),
    )


def notifica_pagamento_confermato(email_cliente, descrizione, giorni=None):
    """[CLIENTE] Bonifico incassato: il pass è attivo."""
    righe = [
        "Abbiamo ricevuto il tuo pagamento e il pass è ora attivo.",
        "",
        f"Dettaglio: {descrizione}",
    ]
    if giorni:
        righe.append(f"Giorni di accesso: {giorni}")
    righe += [
        "",
        "Puoi accedere subito alla piattaforma:",
        "https://www.codemorph.it",
        "",
        "—",
        "CodeMorph.AI — modernizzazione di sistemi legacy",
    ]
    invia_email(email_cliente, "[CodeMorph] Pagamento confermato, pass attivo",
                "\n".join(righe))
