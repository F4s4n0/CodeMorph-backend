# Clausole per i Termini di Servizio

**BOZZA DA SOTTOPORRE A REVISIONE PROFESSIONALE**

Da integrare in `TermsOfService.jsx`, rinumerando le sezioni esistenti.
Le prime due sono quelle che ti proteggono dai comportamenti degli agenti;
le altre chiudono lacune emerse durante lo sviluppo.

---

## Rapporto contrattuale e soggetti terzi

> **Parti del contratto.** Il contratto di fornitura del servizio è concluso
> esclusivamente tra l'utente e CodeMorph.AI. Eventuali soggetti che abbiano
> segnalato o presentato il servizio operano in piena autonomia e **non sono
> autorizzati a rappresentare il Fornitore**, a concludere contratti in suo
> nome, a incassare somme, a concedere condizioni particolari né ad assumere
> impegni su prestazioni, tempistiche o risultati.
>
> Fanno fede esclusivamente le condizioni pubblicate sul sito e accettate
> dall'utente in fase di acquisto. Dichiarazioni difformi rese da terzi non
> sono opponibili al Fornitore.

## Durata e assenza di recesso

> **Durata dei pass.** I pass di accesso sono acquistati per la durata scelta
> dall'utente, decorrono dall'attivazione e non sono frazionabili né
> rimborsabili.
>
> Il servizio è disponibile in pass di durata giornaliera, che consente di
> valutare compiutamente la piattaforma prima di acquisti di durata
> superiore. L'utente riconosce di aver avuto tale facoltà.
>
> Il presente contratto è concluso tra soggetti che agiscono nell'esercizio
> della propria attività imprenditoriale o professionale: non trova pertanto
> applicazione la disciplina del diritto di recesso prevista dal Codice del
> Consumo.
>
> Resta impregiudicato il diritto dell'utente di ottenere il ripristino del
> servizio in caso di malfunzionamenti imputabili al Fornitore, secondo
> quanto previsto al paragrafo seguente.

## Malfunzionamenti e continuità del servizio

> **Interruzioni tecniche.** In caso di interruzione del servizio imputabile
> al Fornitore che impedisca l'utilizzo del pass acquistato, il Fornitore
> provvede al ripristino e, ove l'interruzione sia stata significativa,
> all'estensione della durata del pass per un periodo corrispondente.
>
> Il Fornitore non risponde di interruzioni dovute a cause esterne, ivi
> compresa l'indisponibilità dei servizi di intelligenza artificiale di
> terzi utilizzati dalla piattaforma.

## Credito per l'elaborazione (token)

> **Natura e consumo.** Il credito per l'elaborazione remunera l'utilizzo
> effettivo dei modelli di intelligenza artificiale. Viene scalato in base
> al volume di testo elaborato e al modello selezionato dall'utente, secondo
> il listino pubblicato nella piattaforma.
>
> Il credito consumato per elaborazioni effettivamente eseguite **non è
> rimborsabile**, anche in caso di interruzione volontaria della lavorazione
> da parte dell'utente o di esito ritenuto non soddisfacente. Le richieste
> già trasmesse ai fornitori di intelligenza artificiale sono da questi
> fatturate indipendentemente dall'interruzione.
>
> Il credito residuo non utilizzato alla scadenza del pass resta disponibile
> per acquisti successivi e non è convertibile in denaro.

## Trattamento del codice sorgente caricato

> **Titolarità e riservatezza.** L'utente mantiene la piena titolarità del
> codice sorgente caricato e degli elaborati prodotti. Il Fornitore non
> rivendica alcun diritto su di essi e non li utilizza per finalità diverse
> dall'erogazione del servizio richiesto.
>
> Il codice caricato è conservato per la durata della sessione di lavoro e
> per il tempo necessario a consentirne il download, ed è trasmesso ai
> fornitori di modelli di intelligenza artificiale al solo fine
> dell'elaborazione richiesta.
>
> L'utente garantisce di essere legittimo titolare del codice caricato o di
> disporre delle autorizzazioni necessarie al trattamento.

## Strumenti di monitoraggio

> **Servizi di terze parti.** Il Fornitore utilizza servizi terzi per il
> monitoraggio tecnico degli errori applicativi. Tali servizi possono
> trattare dati tecnici quali indirizzi IP, identificativi di sessione e
> messaggi di errore. **Il codice sorgente caricato dall'utente non è
> trasmesso a tali servizi.**

## Limiti tecnici del servizio

> *(già inserita — riportata qui per completezza)*
>
> Il servizio è dimensionato per l'analisi di applicativi fino a circa 500
> file di codice sorgente. I file di dimensione superiore a 250 KB non
> vengono elaborati e sono segnalati all'utente. Il tempo di elaborazione e
> il consumo di credito sono proporzionali alla dimensione e alla
> complessità del sistema analizzato.

---

## Nota tecnica: il consenso va tracciato

Le clausole valgono se il cliente le ha accettate in modo dimostrabile.
Oggi il flusso di acquisto non registra alcun consenso.

**Aggiungi nel PaymentPanel**, sopra i bottoni di acquisto:

```jsx
        <label className="flex items-start gap-3 mb-4 cursor-pointer">
          <input
            type="checkbox"
            checked={terminiAccettati}
            onChange={(e) => setTerminiAccettati(e.target.checked)}
            className="mt-1 w-4 h-4 accent-purple-500 cursor-pointer shrink-0"
          />
          <span className="text-xs text-slate-400 leading-relaxed">
            Dichiaro di aver letto e accettato i{' '}
            <button type="button" onClick={onTermsClick} className="text-purple-400 hover:text-purple-300 underline">
              Termini di Servizio
            </button>
            , con particolare riferimento alla durata dei pass, all'assenza di
            recesso e alle condizioni sul credito per l'elaborazione.
          </span>
        </label>
```

Con `disabled={... || !terminiAccettati}` sui bottoni di acquisto, e il
salvataggio del consenso nell'ordine:

```python
    "termini_accettati_at": datetime.now(timezone.utc).isoformat(),
```

più la colonna su Supabase:

```sql
ALTER TABLE payment_orders
  ADD COLUMN IF NOT EXISTS termini_accettati_at timestamptz;
```

Senza questo, in caso di contestazione non puoi dimostrare che il cliente
avesse accettato — e le clausole più importanti (assenza di recesso, non
rimborsabilità del credito) sono proprio quelle che vengono contestate.
