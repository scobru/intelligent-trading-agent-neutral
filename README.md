<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Neutral Agent (Base delta-neutral)

<br clear="left">

**English** · [Italiano](README.it.md)

> ⚠️ **Experimental software, not financial advice.** The bot trades real money on Base and can lose some or all of the capital you give it. Start with paper trading or dry-run; when you go live, use a dedicated wallet and only amounts you can afford to lose. See the **Disclaimer** section at the bottom.

A **delta-neutral agent on Base**: for each asset it buys spot on Uniswap V3
and opens a short of the same size on the SynFutures V3 perpetual. The price
cancels out between the two legs; what is left is the **funding**, which shorts
collect when the market is crowded with longs.

It is part of the [Intelligent Trading](https://github.com/scobru/intelligent-trading)
suite of agents for Base. Structure, dashboard, Telegram notifications,
dry-run/paper modes and the LLM decision cycle are shared with its siblings.
It reuses the swap code of the degen/yield bots and the SynFutures
microservice of the perp bot, with an extra `/funding` endpoint.

---

## 💸 Where the yield comes from

On SynFutures V3 funding is **continuous**. When the AMM's *fair* price is
above the *mark* (spot index), longs pay shorts `|fair − mark|` per unit per
day, and the total paid is shared among all the shorts. A short therefore
collects:

```
daily rate = (fair − mark) / mark × (total long / total short)
```

If fair is below mark, the short pays `(fair − mark) / mark` per day. The
long/short ratio matters: on a market skewed toward longs the short collects
more than the premium, **and our own short dilutes the income**: the bot
accounts for it before deciding.

---

## 🧠 How a cycle works

1. **Funding** — from the microservice's `/funding`: spot, mark, fair and
   long/short open interest of every USDC perpetual. Every observation is
   stored in the database: decisions use the **average** of the last
   `FUNDING_LOOKBACK_HOURS` (default 24h), never a single reading.
2. **State** — spot in the wallet, short and balance on the SynFutures Gate,
   and the `positions.json` registry that knows which pairs belong to the
   strategy.
3. **Automatic exits**, before consulting the model:
   - **broken hedge**: short closed/liquidated or off balance by more than
     `MAX_HEDGE_DRIFT_PCT`;
   - **margin**: if the price rises the short loses margin (while the spot
     gains in the wallet); beyond `MAX_EFFECTIVE_LEVERAGE` the pair is closed;
   - **funding**: average below `EXIT_APR` (after `MIN_HOLD_HOURS`) or current
     value below `PANIC_EXIT_APR`.
4. **Decision** — the LLM picks `open` / `close` / `hold` on ETH or BTC.
5. **Execution**, with limits enforced in the code (not in the prompt):
   - history of at least `FUNDING_MIN_OBSERVATIONS` observations;
   - average APR, already diluted by our short, above `MIN_ENTRY_APR`;
   - positive current funding;
   - opening + closing costs (swaps, perp fees, gas) paid back within
     `MAX_BREAKEVEN_DAYS`;
   - capital between `MIN_POSITION_USD` and `MAX_POSITION_USD`, at most
     `MAX_ASSET_PCT` on one asset, notional ≥ the SynFutures minimum.

### Opening and closing a pair

With capital `C` and short leverage `L` (default 2):

1. swap USDC → spot for the notional `N = C × L / (L + 1)`;
2. deposit the margin `M = C − N` on the SynFutures Gate;
3. short **the same quantity** that was bought.

If the short fails, the spot just bought is **sold right away**: a single leg
is exactly the risk the strategy wants to avoid. Closing goes the other way
(close the short, withdraw from the Gate, sell the spot); if a step fails the
pair stays in the registry and the next cycle sees it as a broken hedge and
retries.

### 🚪 Collateral on the SynFutures Gate

On SynFutures V3 the collateral for perpetual contracts sits in the **Gate**
contract (`0x208B443983D8BcC8578e9D86Db23FbA547071270` on Base).

#### How much USDC is needed on the Gate?

It depends on the sizing parameters (`MIN_POSITION_USD = $150`,
`PERP_LEVERAGE = 2`):
- For a minimum $150 position: $100 spot notional, $50 short margin.
- The protocol/microservice accepts orders only up to **90%** of the Gate
  balance (`availableMargin × 0.9`), plus a **+20%** buffer
  (`GATE_MARGIN_BUFFER = 1.20`) to absorb negative funding or price moves
  without risking an early close.
- **Minimum for 1 pair (e.g. ETH):** **~60-67 USDC** on the Gate (+ 100 USDC
  in the wallet for the spot).
- **Minimum for 2 pairs (ETH + BTC):** **~120-135 USDC** on the Gate (+ 200
  USDC in the wallet for the spot).
- **$1,000 portfolio:** **~350-400 USDC** on the Gate and the rest in the
  wallet.

#### Gate management tool

```bash
# Check balances and the amount the strategy needs
python tools/deposit_gate.py --status

# Deposit the recommended amount on the Gate automatically
python tools/deposit_gate.py --deposit

# Deposit or withdraw a custom amount
python tools/deposit_gate.py --deposit --amount 100
python tools/deposit_gate.py --withdraw --amount 50
```

---

## 🚀 Getting started

```bash
cp .env.example .env        # keys and parameters
docker compose up -d --build
```

Locally without Docker you need Python 3.11 and Node 20:

```bash
pip install -r requirements.txt
(cd synfutures-service && npm install && npm run build && node dist/index.js &)
python dashboard.py &       # dashboard at http://localhost:3000
python main.py              # one cycle
```

### Modes

| Mode | Variable | What happens |
|---|---|---|
| **Paper** | `PAPER_TRADING=true` | Virtual portfolio (`PAPER_START_USDC`, default $1000) with **real SynFutures prices and funding**: every cycle the short earns the observed funding. No wallet or key needed. |
| **Dry-run** (default) | `DRY_RUN=true` | Reads the real wallet, decides, validates the limits and shows the transaction plan without signing it. |
| **Live** | `DRY_RUN=false` | Signs swaps, the Gate deposit and the short. |

The microservice always starts (funding is read from it), but in paper and
dry-run it runs **without a key**, read-only.

**Start with paper**: the bot does not enter until it has at least
`FUNDING_MIN_OBSERVATIONS` observations, so for the first hours it holds by
design. When live, use a dedicated wallet.

### ⛽ Auto-refuel (ETH → USDC)

If the wallet holds too little USDC (< `USDC_AUTO_SWAP_THRESHOLD`, default
$5.0) but has native ETH, the agent automatically converts the excess ETH into
USDC on Uniswap V3 at the start of the cycle, always keeping the ETH needed for
fees (`ETH_GAS_RESERVE`, default 0.003 ETH). Sending only ETH to the wallet is
enough to make the bot operational.

```bash
# Quick check or manual refuel
python main.py --refuel

# Inspect wallet balances with the dedicated tool
python tools/refuel.py --status
```

### CapRover

An app with a persistent volume on `/app/data` (database, pair registry, paper
state), HTTP port 3000, variables from `.env.example`.

---

## 📱 Telegram and dashboard

Commands: `/status`, `/positions`, `/funding`, `/last`, `/run`, accepted
**only** from the `TELEGRAM_CHAT_ID` chat.

The dashboard uses the design system shared by every agent in the suite
(`static/dashboard.css` and `static/dashboard.js`, identical in every
repository): mode badge, paper panel, wallet panel with ETH for gas, equity
curve, **funding history** per asset, open pairs with effective leverage,
operation history and errors. The "Run cycle now" button is enabled only with
`DASHBOARD_RUN_TOKEN`.

---

## 📁 Structure

| File | Role |
|---|---|
| `main.py` | one cycle: funding, state, risk exits, decision, execution |
| `funding.py` | short funding math, dilution, costs and break-even |
| `neutral_manager.py` | pair state, limits, opening/closing both legs |
| `neutral_agent.py` | LLM call, JSON schema, fallback to hold |
| `synfutures_client.py` | REST client for the microservice |
| `synfutures-service/` | Node microservice (Oyster SDK), with the `/funding` endpoint |
| `base_client.py`, `uniswap.py` | Base chain and Uniswap V3 swaps (as in the yield bot) |
| `positions.py` | registry of open pairs |
| `paper.py` | virtual portfolio that earns the real funding |
| `db_utils.py` | SQLite: snapshots, funding observations, operations, errors |
| `dashboard.py`, `dashboard_auth.py`, `telegram_bot.py` | interfaces and token check for dashboard commands |
| `tests/` | offline tests (`python -m pytest tests`) |

---

## ⚠️ Disclaimer

This software is experimental and provided "as is", without warranty of any
kind (see the MIT license). It is not financial advice nor an invitation to
invest.

- **You can lose money.** Bugs, wrong model decisions, slippage, protocol
  exploits, manipulated oracles and liquidations can cause the loss of some or
  all of your capital.
- **Decisions are made by an LLM.** It can be wrong or behave unpredictably:
  the executor's limits reduce the damage, they do not eliminate it. Past
  results, paper ones included, do not guarantee future ones.
- **Start with paper or dry-run.** When live, use a wallet dedicated to the
  bot, with amounts you can afford to lose, and never reuse that private key
  elsewhere.
- **Protect your keys.** The private key belongs only in the deployment's
  environment variables: never commit it. Without `DASHBOARD_RUN_TOKEN` the
  dashboard commands stay disabled: set it to a long random value before
  exposing the dashboard to the Internet.
- **Laws and taxes.** You are responsible for complying with the rules and tax
  obligations of your country.

**Risks specific to this bot.** Delta-neutral does not mean risk-free: funding
can flip and stay negative, a violent rally puts pressure on the short's
margin, the two legs are not executed atomically, and smart contracts and
oracles can fail. No guarantees, no promised returns.

## 📜 License

MIT.
