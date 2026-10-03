<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Neutral Agent (Base delta-neutral)

<br clear="left">

[English](README.md) · **Italiano**

> ⚠️ **Software sperimentale, non consulenza finanziaria.** Il bot opera con denaro reale su Base e può perdere in parte o del tutto il capitale che gli affidi. Parti in paper trading o dry-run; in live usa un wallet dedicato e solo importi che puoi permetterti di perdere. Dettagli nella sezione **Avvertenza** in fondo.

Agente **delta-neutral su Base**: per ogni asset compra lo spot su Uniswap V3
e apre uno short della stessa quantità sul perpetual SynFutures V3. Il prezzo
si annulla fra le due gambe; resta il **funding**, che gli short incassano
quando il mercato è affollato di long.

È il quarto fratello della famiglia:

| Bot | Cosa fa |
|---|---|
| [intelligent-trading-agent](https://github.com/scobru/intelligent-trading-agent) | perpetual su SynFutures V3 (direzionale) |
| [intelligent-trading-agent-degen](https://github.com/scobru/intelligent-trading-agent-degen) | spot di token su Uniswap V3 (direzionale) |
| [intelligent-trading-agent-yield](https://github.com/scobru/intelligent-trading-agent-yield) | rendimento passivo su lending e vault |
| **intelligent-trading-agent-neutral** | funding carry: spot + short, senza esposizione al prezzo |

Struttura, dashboard, notifiche Telegram, modalità dry-run/paper e ciclo
decisionale LLM sono gli stessi. Riusa lo swap del bot degen/yield e il
microservizio SynFutures del bot principale, con in più l'endpoint `/funding`.

---

## 💸 Da dove viene il rendimento

In SynFutures V3 il funding è **continuo**. Quando il prezzo *fair*
dell'AMM supera il *mark* (index spot) i long pagano gli short
`|fair − mark|` per unità al giorno, e il totale pagato si divide fra
tutti gli short. Uno short incassa quindi:

```
tasso giornaliero = (fair − mark) / mark × (totale long / totale short)
```

Se il fair è sotto il mark, lo short paga `(fair − mark) / mark` al giorno.
Il fattore long/short conta: su un mercato sbilanciato verso i long lo
short incassa più del premio, **e il nostro stesso short diluisce
l'incasso**: il bot lo include prima di decidere.

---

## 🧠 Come funziona un ciclo

1. **Funding** — da `/funding` del microservizio: spot, mark, fair e open
   interest long/short di ogni perpetual in USDC. Ogni osservazione finisce
   nel database: si decide sulla **media** delle ultime
   `FUNDING_LOOKBACK_HOURS` (default 24h), mai su una lettura sola.
2. **Stato** — spot nel wallet, short e saldo sul Gate di SynFutures, e il
   registro `positions.json` che sa quali coppie appartengono alla strategia.
3. **Uscite automatiche**, prima di sentire il modello:
   - **hedge rotto**: short chiuso/liquidato o sbilanciato oltre `MAX_HEDGE_DRIFT_PCT`;
   - **margine**: se il prezzo sale lo short perde margine (mentre lo spot
     guadagna nel wallet); oltre `MAX_EFFECTIVE_LEVERAGE` si chiude;
   - **funding**: media sotto `EXIT_APR` (dopo `MIN_HOLD_HOURS`) o valore
     istantaneo sotto `PANIC_EXIT_APR`.
4. **Decisione** — l'LLM sceglie `open` / `close` / `hold` su ETH o BTC.
5. **Esecuzione**, con i limiti controllati nel codice (non nel prompt):
   - storico di almeno `FUNDING_MIN_OBSERVATIONS` osservazioni;
   - APR medio, già diluito dal nostro short, sopra `MIN_ENTRY_APR`;
   - funding istantaneo positivo;
   - costi di apertura + chiusura (swap, fee perp, gas) ripagati entro
     `MAX_BREAKEVEN_DAYS`;
   - capitale fra `MIN_POSITION_USD` e `MAX_POSITION_USD`, max
     `MAX_ASSET_PCT` su un asset, nozionale ≥ minimo SynFutures.

### Apertura e chiusura di una coppia

Con capitale `C` e leva dello short `L` (default 2):

1. swap USDC → spot per il nozionale `N = C × L / (L + 1)`;
2. deposito del margine `M = C − N` sul Gate SynFutures;
3. short della **stessa quantità** comprata.

Se lo short fallisce, lo spot appena comprato viene **rivenduto subito**: una
gamba sola è esattamente il rischio che la strategia vuole evitare. La
chiusura fa il percorso inverso (chiude lo short, ritira dal Gate, vende lo
spot); se un passo fallisce la coppia resta nel registro e il ciclo
successivo la vede come hedge rotto e riprova.

### 🚪 Collaterale sul Gate di SynFutures

Su SynFutures V3 i fondi a garanzia dei contratti perpetual risiedono nel contratto **Gate** (`0x208B443983D8BcC8578e9D86Db23FbA547071270` su Base).

#### Quanti USDC servono sul Gate?
La quantita' necessaria dipende dai parametri di dimensionamento (`MIN_POSITION_USD = $150`, `PERP_LEVERAGE = 2`):
- Per una posizione minima da $150: nozionale spot $100, margine short $50.
- Il protocollo/microservizio accetta ordini solo fino al **90%** del saldo Gate (`availableMargin × 0.9`), a cui si somma un cuscinetto del **+20%** (`GATE_MARGIN_BUFFER = 1.20`) per assorbire funding negativo o variazioni di prezzo senza rischiare la chiusura anticipata.
- **Minimo per 1 coppia (es. ETH):** **~60-67 USDC** sul Gate (+ 100 USDC nel wallet per lo spot).
- **Minimo per 2 coppie (ETH + BTC):** **~120-135 USDC** sul Gate (+ 200 USDC nel wallet per lo spot).
- **Portafoglio da $1.000:** **~350-400 USDC** sul Gate e il resto nel wallet.

#### Strumento di gestione Gate:
```bash
# Verifica saldi e fabbisogno calcolato per la strategia
python tools/deposit_gate.py --status

# Deposita automaticamente l'importo raccomandato sul Gate
python tools/deposit_gate.py --deposit

# Deposita o ritira un importo personalizzato
python tools/deposit_gate.py --deposit --amount 100
python tools/deposit_gate.py --withdraw --amount 50
```

---

## 🚀 Avvio

```bash
cp .env.example .env        # chiavi e parametri
docker compose up -d --build
```

In locale senza Docker servono Python 3.11 e Node 20:

```bash
pip install -r requirements.txt
(cd synfutures-service && npm install && npm run build && node dist/index.js &)
python dashboard.py &       # dashboard su http://localhost:3000
python main.py              # un ciclo
```

### Modalità

| Modalità | Variabile | Cosa succede |
|---|---|---|
| **Paper** | `PAPER_TRADING=true` | Portafoglio virtuale (`PAPER_START_USDC`, default $1000) con **prezzi e funding reali** di SynFutures: a ogni ciclo lo short matura il funding osservato. Non servono wallet né chiave. |
| **Dry-run** (default) | `DRY_RUN=true` | Legge il wallet vero, decide, valida i limiti e mostra il piano delle transazioni senza firmarle. |
| **Live** | `DRY_RUN=false` | Firma swap, deposito sul Gate e short. |

Il microservizio parte sempre (il funding si legge da lì) ma in paper e
dry-run gira **senza chiave**, in sola lettura.

**Parti dal paper**: il bot non entra finché non ha almeno
`FUNDING_MIN_OBSERVATIONS` osservazioni, quindi per le prime ore resta in
hold per costruzione. In live usa un wallet dedicato.

### ⛽ Rifornimento Automatico (Auto-Refuel ETH -> USDC)

Se il wallet ha USDC insufficienti (< `USDC_AUTO_SWAP_THRESHOLD`, default $5.0) ma possiede ETH nativo, l'agente converte in automatico l'ETH in eccesso in USDC tramite Uniswap V3 all'inizio del ciclo, riservando sempre l'ETH per pagare le fee (`ETH_GAS_RESERVE`, default 0.003 ETH).
In questo modo è sufficiente inviare solo ETH al wallet per rendere il bot operativo, senza dover inviare separatamente anche USDC.

```bash
# Controllo rapido o esecuzione manuale refuel
python main.py --refuel

# Ispezione saldi wallet con il tool dedicato
python tools/refuel.py --status
```

### CapRover

App con volume persistente su `/app/data` (database, registro coppie, stato
paper), porta HTTP 3000, variabili da `.env.example`.

---

## 📱 Telegram e dashboard

Comandi: `/status`, `/positions`, `/funding`, `/last`, `/run`, accettati
**solo** dalla chat `TELEGRAM_CHAT_ID`.

La dashboard ha lo stesso design system dei tre bot fratelli
(`static/dashboard.css` e `static/dashboard.js`, identici nei quattro
repository): badge di modalità, pannello paper, pannello wallet con l'ETH per
il gas, andamento del capitale, **storico del funding** per asset, coppie
aperte con leva effettiva, storico operazioni ed errori. Il pulsante "Esegui
ciclo ora" è attivo solo con `DASHBOARD_RUN_TOKEN`.

---

## 📁 Struttura

| File | Ruolo |
|---|---|
| `main.py` | un ciclo: funding, stato, uscite di rischio, decisione, esecuzione |
| `funding.py` | calcolo del funding dello short, diluizione, costi e breakeven |
| `neutral_manager.py` | stato delle coppie, limiti, apertura/chiusura delle due gambe |
| `neutral_agent.py` | chiamata LLM, schema JSON, fallback su hold |
| `synfutures_client.py` | client REST del microservizio |
| `synfutures-service/` | microservizio Node (Oyster SDK), con l'endpoint `/funding` |
| `base_client.py`, `uniswap.py` | chain Base e swap su Uniswap V3 (come nel bot yield) |
| `positions.py` | registro delle coppie aperte |
| `paper.py` | portafoglio virtuale che matura il funding reale |
| `db_utils.py` | SQLite: snapshot, osservazioni del funding, operazioni, errori |
| `dashboard.py`, `telegram_bot.py` | interfacce |
| `tests/` | test offline (`python -m pytest tests`) |

---

## ⚠️ Avvertenza

Questo software è sperimentale ed è fornito "così com'è", senza garanzie di alcun tipo
(vedi la licenza MIT). Non è consulenza finanziaria né un invito a investire.

- **Puoi perdere denaro.** Bug, decisioni sbagliate del modello, slippage, exploit dei protocolli,
  oracoli manipolati e liquidazioni possono far perdere in parte o del tutto il capitale.
- **Le decisioni le prende un LLM.** Può sbagliare o comportarsi in modo imprevedibile: i limiti
  dell'esecutore riducono il danno, non lo azzerano. I rendimenti passati, anche in paper, non
  garantiscono quelli futuri.
- **Parti in paper o dry-run.** In live usa un wallet dedicato al bot, con importi che puoi
  permetterti di perdere, e non riutilizzare quella chiave privata altrove.
- **Proteggi le chiavi.** La chiave privata va solo nelle variabili d'ambiente del deploy: non
  committarla mai. Senza `DASHBOARD_RUN_TOKEN` i comandi della dashboard restano disattivati:
  impostalo con un valore lungo e casuale prima di esporla su Internet.
- **Leggi e tasse.** Sei responsabile del rispetto delle norme e degli obblighi fiscali del tuo paese.

**Rischi specifici di questo bot.** Delta-neutral non vuol dire senza rischio: il funding può girare e restare
negativo, un rialzo violento mette sotto pressione il margine dello short,
l'esecuzione delle due gambe non è atomica e smart contract e oracoli possono
fallire. Nessuna garanzia, nessuna promessa di rendimento. Software fornito
così com'è.

## 📜 Licenza

MIT.
