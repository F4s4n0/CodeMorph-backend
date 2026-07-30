import os

from crewai import LLM

# Mappa provider -> (variabile d'ambiente della chiave, prefisso LiteLLM)
# Un provider nuovo si aggiunge qui con una riga, senza toccare la logica.
_PROVIDERS = {
    "openai":    ("OPENAI_API_KEY",    ""),           # CrewAI/LiteLLM gestisce OpenAI senza prefisso
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic/"),
    "google":    ("GOOGLE_API_KEY",    "gemini/"),
}



DEFAULT_TEMPERATURE = 0.2
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Modelli che NON accettano più il parametro temperature (deprecato lato API).
# Confronto per sottostringa: copre anche le varianti con data.
MODELLI_SENZA_TEMPERATURE = ("opus-5", "sonnet-5", "fable-5", "mythos-5")


def _accetta_temperature(model_name):
    nome = (model_name or "").lower()
    return not any(m in nome for m in MODELLI_SENZA_TEMPERATURE)

def get_llm(provider="openai", model_name="gpt-4o", temperature=DEFAULT_TEMPERATURE, max_tokens=None):
    """
    Restituisce l'istanza corretta dell'LLM usando la classe nativa di CrewAI.
    Verifica prima la presenza della chiave API.

    La temperatura è parametrizzabile: 0.2 di default (buon compromesso per
    generazione codice), ma il chiamante può abbassarla per task che richiedono
    massimo determinismo (es. estrazione JSON delle dipendenze).
    """
    provider = provider.lower().strip()

    if provider == "local":
        # Modelli locali via Ollama; l'URL è configurabile per deploy
        # dove Ollama non gira sulla stessa macchina del backend.
        return LLM(
            model=f"ollama/{model_name}",
            base_url=os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL),
            temperature=temperature,
            max_tokens=max_tokens,
            
        )

    if provider not in _PROVIDERS:
        supportati = ", ".join([*_PROVIDERS, "local"])
        raise ValueError(
            f"Provider LLM '{provider}' non supportato. Scegli tra: {supportati}."
        )

    env_var, prefisso = _PROVIDERS[provider]
    api_key = os.getenv(env_var)
    if not api_key:
        raise ValueError(f"Errore: la chiave {env_var} non è configurata o è vuota.")

    parametri = {
        "model": f"{prefisso}{model_name}",
        "api_key": api_key,
          # LiteLLM ritenta automaticamente gli errori transitori (529 overloaded,
        # 429 rate limit, timeout) con attesa progressiva: copre TUTTE le chiamate
        # della piattaforma, non solo un punto specifico.
        "num_retries": int(os.getenv("LLM_NUM_RETRIES", "4")),
    }
    # I modelli più recenti rifiutano temperature: va omesso, non azzerato
    if _accetta_temperature(model_name):
        parametri["temperature"] = temperature
    if max_tokens:
        parametri["max_tokens"] = max_tokens

    return LLM(**parametri)
