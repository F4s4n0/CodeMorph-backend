"""
Contabilità dei token consumati dalle Crew AI.

L'endpoint crea UN tracker per richiesta, lo passa alle fasi (crew.py e
graph_builder.py lo alimentano dopo ogni kickoff) e a fine fase converte
i token in EUR col listino di src/config.py, addebitando il portafoglio
dell'utente (vedi payments.py).
"""

import logging
from decimal import Decimal

from src.config import PREZZI_TOKEN_EUR_PER_1M

logger = logging.getLogger(__name__)

_UN_MILIONE = Decimal(1_000_000)
# Il saldo è tenuto a 4 decimali: i consumi delle singole fasi possono
# valere frazioni di centesimo e arrotondarli a 2 li azzererebbe.
_PRECISIONE_EUR = Decimal("0.0001")


def listino_modello(modello):
    """
    Voce di listino per il modello, cercata dentro TUTTI i provider.
    Il dizionario è annidato (provider -> modello -> prezzi): cercare la
    chiave al primo livello faceva ricadere ogni modello sul 'default',
    fatturando tutto alla tariffa più alta.
    """
    chiave = (modello or "").split("/")[-1].strip().lower()
    for nome_provider, modelli in PREZZI_TOKEN_EUR_PER_1M.items():
        if nome_provider == "default" or not isinstance(modelli, dict):
            continue
        # I listini dei provider hanno i modelli come chiavi
        if "prompt" in modelli:      # è la voce 'default', non un provider
            continue
        for nome_modello, prezzi in modelli.items():
            if nome_modello.lower() == chiave:
                return prezzi
    logger.warning("Modello '%s' non a listino: applico la tariffa di default.", modello)
    return PREZZI_TOKEN_EUR_PER_1M["default"]


def _leggi_metrica(metriche, nome):
    """Estrae un contatore dalle UsageMetrics di CrewAI (oggetto o dict)."""
    if isinstance(metriche, dict):
        valore = metriche.get(nome, 0)
    else:
        valore = getattr(metriche, nome, 0)
    try:
        return int(valore or 0)
    except (TypeError, ValueError):
        return 0


class TokenUsageTracker:
    """Accumula l'uso token di più Crew e lo converte in costo EUR."""

    def __init__(self, modello=""):
        self.modello = (modello or "").strip()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.richieste = 0
        self._totale_dichiarato = 0
        # Ultima lettura CUMULATIVA vista da aggiungi_crew(). Serve a sommare
        # solo l'incremento: vedi la spiegazione in aggiungi_crew().
        self._ultima_lettura = {}

    def aggiungi_metriche(self, metriche):
        if not metriche:
            return
        self.prompt_tokens += _leggi_metrica(metriche, "prompt_tokens")
        self.completion_tokens += _leggi_metrica(metriche, "completion_tokens")
        self._totale_dichiarato += _leggi_metrica(metriche, "total_tokens")
        self.richieste += _leggi_metrica(metriche, "successful_requests")

    def aggiungi_crew(self, crew, risultato=None):
        """
        Contabilizza il consumo di una crew appena eseguita.

        ATTENZIONE — le metriche di CrewAI sono CUMULATIVE, non per singola
        esecuzione: gli agenti accumulano il consumo al proprio interno e
        `crew.usage_metrics` somma quegli accumulatori. In Fase 3 gli agenti
        vengono creati UNA volta sola e riusati per ogni file, quindi ogni
        lettura contiene gia' tutto il consumo dei file precedenti.

        Sommare il valore assoluto a ogni file faceva crescere il conteggio in
        modo QUADRATICO: su 211 file il totale risultava ~106 volte quello
        reale, e un cliente si e' visto addebitare 6.417 EUR per un consumo di
        47 EUR.

        Qui si somma solo l'INCREMENTO rispetto all'ultima lettura. Se le
        metriche non fossero cumulative (crew con agenti nuovi ogni volta) il
        valore scenderebbe invece di salire: in quel caso si riparte da zero e
        si somma il valore pieno, che e' il comportamento corretto per quel
        caso.
        """
        da_risultato = getattr(risultato, "token_usage", None)
        da_crew = getattr(crew, "usage_metrics", None)
        metriche = da_risultato or da_crew
        if not metriche:
            return

        # Si decide UNA volta, alla seconda lettura, se questa sorgente e'
        # cumulativa. Farlo per singola metrica sarebbe fragile: un contatore
        # costante (due chiamate a file, sempre) verrebbe scambiato per
        # "nessun incremento" e il consumo sparirebbe dal conteggio.
        totale_letto = _leggi_metrica(metriche, "total_tokens") or (
            _leggi_metrica(metriche, "prompt_tokens")
            + _leggi_metrica(metriche, "completion_tokens")
        )
        # NOTA IMPORTANTE — le metriche di CrewAI sono CUMULATIVE per AGENTE:
        # ogni agente accumula il consumo al proprio interno e usage_metrics
        # somma quegli accumulatori. Riusando gli stessi agenti per piu' file,
        # ogni lettura conterrebbe gia' tutto il consumo precedente e sommarla
        # farebbe crescere il totale in modo QUADRATICO: su 211 file un cliente
        # si e' visto addebitare 6.417 EUR per un consumo reale di 47.
        #
        # La correzione e' in crew.py, che ora crea agenti NUOVI per ogni file:
        # i contatori ripartono da zero e ogni lettura vale per se'. Qui resta
        # un presidio: si segnala se una lettura appare cumulativa, cioe' se
        # supera il totale gia' contabilizzato quando ci sono gia' state
        # letture precedenti.
        if self._ultima_lettura and totale_letto >= max(1, self.tokens_totali):
            logger.warning(
                "Metriche possibilmente CUMULATIVE (lettura=%d, gia' contato=%d): "
                "se gli agenti vengono riusati il consumo risultera' sovrastimato.",
                totale_letto, self.tokens_totali,
            )
        self._ultima_lettura["_totale"] = totale_letto

        incremento = {n: _leggi_metrica(metriche, n) for n in
                      ("prompt_tokens", "completion_tokens", "total_tokens",
                       "successful_requests")}

        logger.debug("METRICHE | lette=%s | incremento=%s | accumulato=(p=%d c=%d t=%d)",
                     metriche, incremento,
                     self.prompt_tokens, self.completion_tokens, self._totale_dichiarato)
        self.aggiungi_metriche(incremento)

    @property
    def tokens_totali(self):
        return self._totale_dichiarato or (self.prompt_tokens + self.completion_tokens)

    def costo_eur(self):
        """Costo del consumo accumulato secondo il listino di vendita."""
        listino = listino_modello(self.modello)

        if not self.prompt_tokens and not self.completion_tokens and self._totale_dichiarato:
            # Metriche incomplete (solo il totale): prezziamo alla tariffa media
            tariffa_media = (listino["prompt"] + listino["completion"]) / 2
            costo = Decimal(self._totale_dichiarato) / _UN_MILIONE * tariffa_media
        else:
            costo = (
                Decimal(self.prompt_tokens) / _UN_MILIONE * listino["prompt"]
                + Decimal(self.completion_tokens) / _UN_MILIONE * listino["completion"]
            )
        return costo.quantize(_PRECISIONE_EUR)
