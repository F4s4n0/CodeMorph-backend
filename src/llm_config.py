import os
from crewai import LLM

def get_llm(provider="openai", model_name="gpt-4o"):
    """
    Restituisce l'istanza corretta dell'LLM usando la classe nativa di CrewAI.
    Verifica prima la presenza della chiave API.
    """
    provider = provider.lower()
    temperature = 0.2 

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("Errore: La chiave OPENAI_API_KEY non è configurata o è vuota.")
        # CrewAI gestisce OpenAI nativamente, basta il nome del modello
        return LLM(model=model_name, api_key=api_key, temperature=temperature)
    
    elif provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Errore: La chiave ANTHROPIC_API_KEY non è configurata o è vuota.")
        # Usiamo il prefisso 'anthropic/' richiesto da LiteLLM/CrewAI
        return LLM(model=f"anthropic/{model_name}", api_key=api_key, temperature=temperature)
    
    elif provider == "google":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("Errore: La chiave GOOGLE_API_KEY non è configurata o è vuota.")
        # Usiamo il prefisso 'gemini/' richiesto da LiteLLM/CrewAI
        return LLM(model=f"gemini/{model_name}", api_key=api_key, temperature=temperature)
    
    elif provider == "local":
        # Per i modelli locali con Ollama
        return LLM(
            model=f"ollama/{model_name}", 
            base_url="http://localhost:11434", 
            temperature=temperature
        )
    
    else:
        raise ValueError(f"Provider LLM '{provider}' non supportato. Scegli tra: openai, anthropic, google, local.")