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
    """Voce di listino per il modello (senza prefisso provider), o 'default'."""
    chiave = (modello or "").split("/")[-1].strip().lower()
    return PREZZI_TOKEN_EUR_PER_1M.get(chiave, PREZZI_TOKEN_EUR_PER_1M["default"])


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

    def aggiungi_metriche(self, metriche):
        if not metriche:
            return
        self.prompt_tokens += _leggi_metrica(metriche, "prompt_tokens")
        self.completion_tokens += _leggi_metrica(metriche, "completion_tokens")
        self._totale_dichiarato += _leggi_metrica(metriche, "total_tokens")
        self.richieste += _leggi_metrica(metriche, "successful_requests")

    def aggiungi_crew(self, crew, risultato=None):
        """
        Registra l'uso di una Crew DOPO il kickoff. Preferisce le metriche
        del CrewOutput e ripiega su crew.usage_metrics: leggerle entrambe
        conterebbe gli stessi token due volte.
        """
        metriche = getattr(risultato, "token_usage", None) \
            or getattr(crew, "usage_metrics", None)
        self.aggiungi_metriche(metriche)

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
