import os
import jwt
from datetime import datetime
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client
from dotenv import load_dotenv # <-- AGGIUNGI QUESTO

# Carica le variabili dal file .env nel sistema
load_dotenv() # <-- AGGIUNGI QUESTO PRIMA DI LEGGERE LE VARIABILI

# Inizializzazione variabili
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")

# Client Supabase per fare query al DB
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

security = HTTPBearer()

def get_current_user_and_validate_license(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials
    try:
        # 1. Decodifica il JWT generato da Supabase Auth
        payload = jwt.decode(
            token, 
            SUPABASE_JWT_SECRET, 
            algorithms=["HS256"], 
            options={"verify_aud": False}
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token non valido: user_id mancante.")
        
        # 2. Controllo Licenza Giornaliera sul Database di Supabase
        response = supabase.table("user_licenses").select("expires_at").eq("user_id", user_id).execute()
        
        if not response.data:
            raise HTTPException(
                status_code=402, 
                detail="Nessuna licenza trovata per questo account. Acquista un pass giornaliero."
            )
            
        expires_at_str = response.data[0]["expires_at"]
        # Conversione timestamp ISO (es: 2026-07-13T23:59:59+00:00)
        # Nota: gestisce la rimozione del fuso orario per il confronto rapido
        expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        
        if datetime.now(expires_at.tzinfo) > expires_at:
            raise HTTPException(
                status_code=402, 
                detail="La tua licenza giornaliera è scaduta. Rinnovala per continuare a usare gli agenti."
            )
            
        # Se tutto è ok, restituisce l'ID dell'utente autenticato e autorizzato
        return user_id

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessione scaduta. Effettua nuovamente il login.")
    except jwt.InvalidTokenError as e:
        print(f"🚨 ERRORE JWT REALE: {str(e)}") # Lo vedrai nel terminale nero
        print(f"🔑 LUNGHEZZA SECRET: {len(SUPABASE_JWT_SECRET) if SUPABASE_JWT_SECRET else 'VUOTO!'}")
        raise HTTPException(status_code=401, detail=f"Errore Token Reale: {str(e)}")