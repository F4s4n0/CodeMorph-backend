import logging
import os

logger = logging.getLogger(__name__)

MARCATORE_FILEPATH = "/// FILEPATH:"


def _sanitize_relative_path(raw_path, project_dir):
    """
    Valida il percorso indicato dall'IA e lo risolve DENTRO project_dir.

    L'LLM potrebbe generare (per errore o per prompt injection nel codice
    legacy analizzato) percorsi assoluti o con '..' che scriverebbero file
    FUORI dalla cartella di progetto. Qui li blocchiamo.

    Ritorna il percorso assoluto sicuro, oppure None se il percorso è invalido.
    """
    pulito = raw_path.replace("`", "").strip().strip('"').strip("'")
    if not pulito:
        return None

    # Normalizza separatori Windows eventualmente generati dall'IA
    pulito = pulito.replace("\\", "/").lstrip("/")

    candidato = os.path.abspath(os.path.join(project_dir, pulito))
    radice = os.path.abspath(project_dir)

    # Il percorso risolto DEVE restare dentro la radice del progetto
    if os.path.commonpath([candidato, radice]) != radice:
        logger.warning("Percorso rifiutato (fuori dal progetto): %s", raw_path)
        return None

    return candidato


def _scrivi_file(full_path, righe):
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as out_f:
        out_f.write("".join(righe))


def unpack_markdown_to_files(md_file_path, base_output_dir):
    """
    Legge un file Markdown generato dall'IA e converte i blocchi di codice
    marcati con '/// FILEPATH:' in file fisici e cartelle su disco.

    Ritorna il numero di file estratti (0 se il Markdown non esiste),
    così il chiamante può verificare che l'estrazione abbia prodotto qualcosa.
    """
    if not os.path.exists(md_file_path):
        logger.warning("File non trovato: %s", md_file_path)
        return 0

    with open(md_file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    project_dir = os.path.join(base_output_dir, "Target_Project")
    os.makedirs(project_dir, exist_ok=True)

    current_filepath = None
    in_code_block = False
    file_content = []
    file_estratti = 0

    for line in lines:
        # 1. Intercetta il marcatore del percorso file
        if line.strip().startswith(MARCATORE_FILEPATH):
            # Se c'era un blocco ancora aperto (l'IA ha dimenticato il ```
            # di chiusura), salviamo comunque quanto accumulato finora
            # invece di perderlo silenziosamente.
            if in_code_block and current_filepath and file_content:
                _scrivi_file(current_filepath, file_content)
                logger.info("File salvato (blocco non chiuso): %s", current_filepath)
                file_estratti += 1

            raw = line.strip()[len(MARCATORE_FILEPATH):]
            current_filepath = _sanitize_relative_path(raw, project_dir)
            file_content = []
            in_code_block = False
            continue

        # 2. Intercetta apertura/chiusura dei blocchi di codice (```)
        if current_filepath and line.strip().startswith("```"):
            if not in_code_block:
                # Inizia il codice (la keyword del linguaggio, es. ```python,
                # sta su questa stessa riga e viene ignorata)
                in_code_block = True
            else:
                # Blocco chiuso: salviamo il file su disco
                in_code_block = False
                _scrivi_file(current_filepath, file_content)
                logger.info("Creato file sorgente: %s", current_filepath)
                file_estratti += 1
                current_filepath = None
            continue

        # 3. Dentro un blocco: accoda la riga di codice al buffer
        if in_code_block and current_filepath:
            file_content.append(line)

    # 4. EOF con blocco ancora aperto: salva il residuo
    if in_code_block and current_filepath and file_content:
        _scrivi_file(current_filepath, file_content)
        logger.info("File salvato a fine documento (blocco non chiuso): %s", current_filepath)
        file_estratti += 1

    logger.info(
        "Estrazione completata: %d file generati in %s", file_estratti, project_dir
    )
    return file_estratti
