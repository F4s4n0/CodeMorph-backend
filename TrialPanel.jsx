import { useCallback, useEffect, useRef, useState } from 'react';
import { BACKEND_URL, getAccessToken } from '../lib/api';

const MAX_RIGHE = 50;

export default function TrialPanel({ onBack, onGoToPayments, onContact }) {
  const [stato, setStato] = useState(null);
  const [codice, setCodice] = useState('');
  const [documento, setDocumento] = useState(null);
  const [logs, setLogs] = useState('');
  const [busy, setBusy] = useState(false);
  const [errore, setErrore] = useState(null);
  const pollRef = useRef(null);
  const fineLogRef = useRef(null);

  const chiama = useCallback(async (percorso, opzioni = {}) => {
    const token = await getAccessToken();
    if (!token) throw new Error('Sessione scaduta.');
    const r = await fetch(`${BACKEND_URL}/api/v1/trial${percorso}`, {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      ...opzioni,
    });
    let corpo = null;
    try { corpo = await r.json(); } catch { /* vuoto */ }
    if (!r.ok) throw new Error(corpo?.detail || `Errore ${r.status}`);
    return corpo;
  }, []);

  useEffect(() => {
    chiama('/stato').then(setStato).catch(() => setStato({ concesso: false }));
  }, [chiama]);

  // Ferma il polling se il componente viene smontato a metà analisi
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  // Tiene la console agganciata all'ultima riga
  useEffect(() => { fineLogRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [logs]);

  const righe = codice ? codice.split('\n').length : 0;
  const troppeRighe = righe > MAX_RIGHE;

  const fermaPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  };

  const esegui = async () => {
    setBusy(true);
    setErrore(null);
    setLogs('Avvio in corso...');

    // Log REALI dell'agente: il backend li scrive durante l'elaborazione
    pollRef.current = setInterval(async () => {
      try {
        const dati = await chiama('/logs');
        if (dati?.logs) setLogs(dati.logs);
      } catch { /* il polling non deve mai rompere la UI */ }
    }, 2000);

    try {
      const esito = await chiama('/esegui', { method: 'POST', body: JSON.stringify({ codice }) });
      // Ultima lettura per non perdere le righe finali
      try {
        const ultimi = await chiama('/logs');
        if (ultimi?.logs) setLogs(ultimi.logs);
      } catch { /* ignora */ }
      setDocumento(esito.documento);
      setStato((s) => ({ ...s, disponibile: false, usato: true }));
    } catch (e) {
      setErrore(e.message);
    } finally {
      fermaPolling();
      setBusy(false);
    }
  };

  const scaricaDocumento = () => {
    const blob = new Blob([documento], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `CodeMorph_Assessment_${new Date().toISOString().slice(0, 10)}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="bg-[#1e293b] border border-[#475569] rounded-xl p-5 md:p-8 shadow-lg animate-[fadeIn_0.3s_ease_forwards]">
      <div className="flex justify-between items-center mb-6 border-b border-slate-700 pb-4">
        <h2 className="text-xl md:text-2xl font-bold text-white">🎁 Prova Gratuita</h2>
        {onBack && (
          <button type="button" onClick={onBack} className="text-slate-400 hover:text-white text-sm underline">
            &larr; Indietro
          </button>
        )}
      </div>

      {errore && (
        <div className="mb-4 bg-red-900/30 border border-red-500/30 text-red-400 text-sm rounded-lg px-4 py-3">
          {errore}
        </div>
      )}

      {!stato ? (
        <p className="text-slate-400">Verifica disponibilità...</p>
      ) : documento ? (
        <>
          <div className="bg-green-900/30 border border-green-500/30 text-green-400 text-sm rounded-lg px-4 py-3 mb-4">
            ✓ Ecco l'analisi del tuo codice — è solo il primo dei 10 documenti che la pipeline completa produce.
          </div>

          <div className="flex flex-wrap gap-3 mb-4">
            <button
              type="button"
              onClick={scaricaDocumento}
              className="bg-slate-700 hover:bg-slate-600 text-white font-semibold py-2.5 px-5 rounded-lg transition-colors flex items-center gap-2"
            >
              ⬇️ Scarica il documento (.md)
            </button>
            {onGoToPayments && (
              <button
                type="button"
                onClick={onGoToPayments}
                className="bg-purple-600 hover:bg-purple-500 text-white font-bold py-2.5 px-5 rounded-lg transition-colors shadow-lg shadow-purple-500/20"
              >
                🚀 Sblocca la migrazione completa
              </button>
            )}
          </div>

          <pre className="bg-[#0f172a] border border-slate-700 rounded-xl p-4 text-xs md:text-sm text-slate-300 whitespace-pre-wrap max-h-[60vh] overflow-y-auto">
            {documento}
          </pre>
        </>
      ) : !stato.concesso ? (
        <div className="text-center py-6">
          <p className="text-slate-400 mb-4">
            La prova gratuita viene attivata su richiesta per le aziende interessate a valutare la piattaforma.
          </p>
          {onContact && (
            <button
              type="button"
              onClick={onContact}
              className="bg-purple-600 hover:bg-purple-500 text-white font-bold py-3 px-6 rounded-xl transition-colors shadow-lg shadow-purple-500/20"
            >
              💬 Richiedi la tua prova gratuita
            </button>
          )}
        </div>
      ) : stato.usato ? (
        <p className="text-slate-400">Hai già utilizzato la tua prova gratuita. Per l'analisi completa, attiva un pass.</p>
      ) : (
        <>
          <p className="text-slate-400 text-sm mb-4">
            Incolla fino a <span className="text-white font-bold">{MAX_RIGHE} righe</span> del tuo codice legacy
            (FoxPro, VB6, COBOL...) e ricevi gratis il documento di Assessment generato dai nostri agenti.
            Utilizzabile una sola volta.
          </p>

          <textarea
            rows="12"
            value={codice}
            onChange={(e) => setCodice(e.target.value)}
            disabled={busy}
            placeholder="Incolla qui il tuo codice legacy..."
            className="w-full bg-[#0f172a] border border-[#475569] rounded-lg p-3 text-slate-200 font-mono text-xs md:text-sm focus:outline-none focus:border-purple-500 resize-y disabled:opacity-60"
          />
          <div className={`text-xs mt-1 font-mono ${troppeRighe ? 'text-red-400 font-bold' : 'text-slate-500'}`}>
            {righe}/{MAX_RIGHE} righe{troppeRighe && ' — riduci il codice per procedere'}
          </div>

          <button
            type="button"
            onClick={esegui}
            disabled={busy || !codice.trim() || troppeRighe}
            className="mt-4 bg-purple-600 hover:bg-purple-500 text-white font-bold py-3 px-6 rounded-xl transition-colors shadow-lg shadow-purple-500/20 disabled:opacity-50 flex items-center gap-2"
          >
            {busy && <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />}
            {busy ? 'Analisi in corso...' : '⚡ Genera l\'Assessment gratuito'}
          </button>

          {/* CONSOLE DEI LOG: attività reale degli agenti durante l'analisi */}
          {(busy || logs) && (
            <div className="mt-6">
              <div className="text-slate-500 text-xs uppercase tracking-wider mb-2">Attività degli agenti</div>
              <div className="bg-black border border-slate-700 rounded-lg p-4 h-48 overflow-y-auto font-mono text-xs text-sky-400">
                {logs
                  ? logs.split('\n').filter(Boolean).map((riga, i) => (
                      <div key={i} className="mb-1">
                        <span className="text-purple-500 mr-2">&gt;</span>{riga}
                      </div>
                    ))
                  : <div className="text-slate-600">In attesa dei primi messaggi...</div>}
                {busy && (
                  <div className="animate-pulse opacity-70">
                    <span className="text-purple-500 mr-2">&gt;</span>_
                  </div>
                )}
                <div ref={fineLogRef} />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
