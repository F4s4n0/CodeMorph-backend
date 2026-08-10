# Fatturazione: flusso e modelli email

## ⚠️ Premessa tecnica: cosa può fare l'email

In Italia, tra soggetti titolari di partita IVA, **la fattura deve
transitare dal Sistema di Interscambio (SDI)** dell'Agenzia delle Entrate
in formato XML. Un PDF inviato via email **non è una fattura fiscalmente
valida**.

Quello che invii via email è la **copia di cortesia**: il documento
leggibile che accompagna la fattura già trasmessa allo SDI. È prassi
comune e apprezzata dai clienti (che altrimenti ricevono solo un XML nel
proprio cassetto fiscale), ma non sostituisce l'adempimento.

**Come emettere la fattura vera**, in ordine di praticità:
- **software di fatturazione** (Fatture in Cloud, Aruba, Fattura24...):
  inserisci i dati e trasmette allo SDI. Molti hanno API, quindi un domani
  la piattaforma potrebbe generarla automaticamente dai dati che già
  raccogli in `dati_fatturazione`;
- **portale gratuito dell'Agenzia delle Entrate**: sufficiente per pochi
  documenti, laborioso oltre;
- **il tuo commercialista**, se già gestisce le tue fatture.

I dati che ti servono li hai già: `payment_orders.dati_fatturazione`
contiene lo snapshot con ragione sociale, P.IVA, indirizzo, SDI o PEC.

---

## 1. Email al cliente — copia di cortesia della fattura

**Oggetto:** `Fattura n. [NUMERO] - CodeMorph.AI`

```
Gentile [RAGIONE SOCIALE],

in allegato la copia di cortesia della fattura n. [NUMERO] del [DATA],
relativa all'acquisto effettuato sulla piattaforma CodeMorph.AI.

RIEPILOGO
Descrizione:      [Pass di accesso - N giorni]
Imponibile:       [___] €
IVA 22%:          [___] €
Totale:           [___] €
Modalità:         [PayPal / Bonifico bancario]
Data acquisto:    [___]

La fattura in formato elettronico è stata trasmessa al Sistema di
Interscambio e sarà disponibile nel Suo cassetto fiscale, oppure recapitata
al codice destinatario [SDI/PEC] da Lei indicato.

Il pass è già attivo: può accedere alla piattaforma da
https://www.codemorph.it

Per qualsiasi chiarimento amministrativo può rispondere a questa email.

Cordiali saluti,

[NOME]
CodeMorph.AI
[P.IVA] - www.codemorph.it
```

---

## 2. Email al cliente — conferma dell'ordine (subito dopo l'acquisto)

Diversa dalla precedente: questa parte **subito**, la fattura può seguire
nei giorni successivi.

**Oggetto:** `Il tuo pass CodeMorph.AI è attivo`

```
Gentile [RAGIONE SOCIALE],

il pagamento è stato registrato e il Suo pass è attivo.

COSA HA ACQUISTATO
Pass di accesso:  [N] giorni [+ N giorni bonus]
Attivo fino al:   [DATA E ORA]
Credito incluso:  [___] € per l'elaborazione

Può accedere subito: https://www.codemorph.it

COME INIZIARE
1. Carichi l'archivio .zip del sistema da analizzare
2. Verifichi i file che la piattaforma ha individuato come rilevanti
3. Avvii la Fase 1 e attenda i documenti di analisi

Tra una fase e l'altra la pipeline si ferma: potrà ispezionare i documenti,
correggerli e approvarli prima di proseguire.

La fattura elettronica sarà emessa entro [___] giorni e recapitata
all'indirizzo indicato in fase di acquisto.

Per assistenza: https://www.codemorph.it (sezione Contatti)

Cordiali saluti,

[NOME]
CodeMorph.AI
```

---

## 3. Email al cliente — bonifico ricevuto

**Oggetto:** `Bonifico ricevuto - pass CodeMorph.AI attivo`

```
Gentile [RAGIONE SOCIALE],

confermiamo la ricezione del bonifico relativo all'ordine [NUMERO].

Il Suo pass di [N] giorni è ora attivo e resterà valido fino al [DATA].
Può accedere alla piattaforma: https://www.codemorph.it

La fattura elettronica sarà emessa e trasmessa nei prossimi giorni.

Cordiali saluti,

[NOME]
CodeMorph.AI
```

---

## 4. Email al segnalatore — riepilogo compensi

**Oggetto:** `Riepilogo compensi [PERIODO] - CodeMorph.AI`

```
Gentile [NOME],

di seguito il riepilogo dei compensi maturati nel periodo [___].

CLIENTI ATTRIBUITI CON ACQUISTI NEL PERIODO
[Cliente]        [Ordine]      [Imponibile]    [%]    [Compenso]
...

TOTALE MATURATO NEL PERIODO: [___] €
TOTALE GIÀ LIQUIDATO:        [___] €
DA LIQUIDARE:                [___] €

Per procedere al pagamento La invitiamo a emettere fattura intestata a:

[RAGIONE SOCIALE]
[INDIRIZZO]
P.IVA [___]
Codice SDI [___]

con la causale: "Compenso per attività di segnalazione - periodo [___]".

Il pagamento sarà effettuato entro [___] giorni dal ricevimento della
fattura.

Cordiali saluti,

[NOME]
CodeMorph.AI
```

---

## Note operative

**Numerazione progressiva.** Le fatture devono avere numerazione
progressiva senza salti nell'anno solare. Se usi un software di
fatturazione se ne occupa lui; se emetti a mano, tienine traccia con
rigore.

**Termini di emissione.** La fattura immediata va emessa entro 12 giorni
dall'operazione. Nelle email sopra ho lasciato il campo `[___] giorni`:
allineало ai termini che concorderai col commercialista.

**Automazione futura.** Quando avrai volumi, l'integrazione con le API di
un software di fatturazione ti farebbe emettere la fattura automaticamente
all'erogazione dell'ordine — i dati sono già tutti in
`payment_orders.dati_fatturazione`. È un buon candidato per la lista, dopo
le notifiche email.
