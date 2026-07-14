import os

def unpack_markdown_to_files(md_file_path, base_output_dir):
    """
    Legge un file Markdown generato dall'IA e converte i blocchi di codice
    in file fisici e cartelle sul server.
    """
    if not os.path.exists(md_file_path):
        print(f"⚠️ File non trovato: {md_file_path}")
        return

    with open(md_file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    current_filepath = None
    in_code_block = False
    file_content = []
    file_estratti = 0

    # Creiamo una sottocartella specifica per il progetto generato
    project_dir = os.path.join(base_output_dir, "Target_Project")
    os.makedirs(project_dir, exist_ok=True)

    for line in lines:
        # 1. Intercetta il marcatore del percorso file
        if line.strip().startswith("/// FILEPATH:"):
            # Estrae il percorso e pulisce eventuali spazi o backtick
            current_filepath = line.replace("/// FILEPATH:", "").replace("`", "").strip()
            file_content = []  # Resetta il buffer per il nuovo file
            in_code_block = False
            continue

        # 2. Intercetta l'apertura e chiusura dei blocchi di codice (```)
        if current_filepath and line.strip().startswith("```"):
            if not in_code_block:
                # Inizia a leggere il codice (ignorando la parola chiave del linguaggio es. ```python)
                in_code_block = True
                continue
            else:
                # Il blocco di codice è finito: SALVIAMO IL FILE SU DISCO
                in_code_block = False
                
                # Calcola il percorso assoluto e crea le cartelle genitrici se mancano
                full_path = os.path.join(project_dir, current_filepath)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                
                # Scrive il contenuto fisicamente sul server
                with open(full_path, 'w', encoding='utf-8') as out_f:
                    out_f.write("".join(file_content))
                
                print(f"✅ Creato file sorgente: {current_filepath}")
                file_estratti += 1
                current_filepath = None  # Resetta in attesa del prossimo marcatore
                continue

        # 3. Se siamo dentro un blocco, accoda la riga di codice al buffer
        if in_code_block and current_filepath:
            file_content.append(line)
            
    print(f"📦 Estrazione completata: {file_estratti} file generati nella cartella Target_Project.")