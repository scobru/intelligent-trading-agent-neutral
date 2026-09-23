from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import re
import time
import logging

load_dotenv()

logger = logging.getLogger(__name__)

# Modello configurabile: i router "free" possono restituire risposte vuote,
# in quel caso basta puntare OPENROUTER_MODEL a un modello stabile.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
MAX_LLM_ATTEMPTS = int(os.getenv("OPENROUTER_MAX_ATTEMPTS", "3"))

VALID_OPERATIONS = ("open", "close", "hold")


def get_openrouter_client():
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY mancante nel .env")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
    )


SYSTEM_RULES = """You are a delta-neutral funding-carry allocator operating a wallet on Base.

For each asset you can hold a hedged PAIR: spot bought on Uniswap V3 plus a
short of the same size on the SynFutures V3 perpetual. Price moves cancel
out between the two legs; what remains is the funding paid to shorts when
longs are crowded. You never bet on direction.

Hard rules:
- "open" only on an asset listed in the funding table, not already held.
- "close" only on an asset you currently hold as a pair.
- Position sizing is a fraction of the TOTAL portfolio value (spot + margin).

You MUST output ONLY a valid, raw JSON object (no extra commentary) adhering
strictly to this schema:
{
    "operation": "open" | "close" | "hold",
    "asset": "ETH" | "BTC",
    "target_portion_of_portfolio": float (0.0 to 1.0, only for open),
    "reason": "Brief explanation of the decision (max 300 chars)"
}
"""


def _clean_and_parse_json(text: str) -> dict:
    """Extract and parse JSON safely from model response."""
    if not text or not text.strip():
        raise ValueError("Risposta del modello vuota o nulla (content=None)")

    text = text.strip()

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end + 1])
        else:
            raise ValueError(f"Could not parse valid JSON from response: {text}")

    if not isinstance(data, dict):
        raise ValueError("Il modello non ha restituito un oggetto JSON")

    data["operation"] = str(data.get("operation", "hold")).lower().strip()
    if data["operation"] not in VALID_OPERATIONS:
        data["operation"] = "hold"

    # il modello a volte usa nomi alternativi o minuscole
    asset = data.get("asset") or data.get("symbol") or data.get("market") or ""
    asset = str(asset).strip().upper().split("-")[0].split("/")[0]
    data["asset"] = {"WETH": "ETH", "CBBTC": "BTC", "WBTC": "BTC"}.get(asset, asset)

    data.setdefault("target_portion_of_portfolio", 0.0)
    data.setdefault("reason", "Default signal")
    try:
        data["target_portion_of_portfolio"] = max(0.0, min(1.0, float(data["target_portion_of_portfolio"])))
    except (ValueError, TypeError):
        data["target_portion_of_portfolio"] = 0.0
    data["reason"] = str(data["reason"])[:300]

    return data


def _safe_hold_signal(reason: str) -> dict:
    """Segnale neutro usato quando l'LLM non produce una risposta utilizzabile."""
    return {
        "operation": "hold",
        "asset": "",
        "target_portion_of_portfolio": 0.0,
        "reason": reason[:300],
        "fallback": True,
    }


def _extract_message_text(response) -> str:
    """
    Estrae il testo dalla risposta OpenRouter gestendo i casi in cui
    `message.content` è None (tipico dei modelli free) o una lista di parti.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", None)

    # Alcuni provider restituiscono il contenuto come lista di blocchi
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            else:
                parts.append(getattr(part, "text", "") or "")
        content = "".join(parts)

    if isinstance(content, str) and content.strip():
        return content

    # Fallback: i modelli con reasoning a volte lasciano il JSON solo lì
    extra = getattr(message, "model_extra", None) or {}
    for key in ("reasoning", "reasoning_content"):
        value = getattr(message, key, None) or extra.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return ""


def _api_error(response):
    """Restituisce l'errore applicativo eventualmente incapsulato nella risposta."""
    err = getattr(response, "error", None)
    if err:
        return err
    extra = getattr(response, "model_extra", None) or {}
    return extra.get("error")


def decide_action(prompt: str, max_attempts: int = None) -> dict:
    """
    Invia il prompt all'LLM tramite OpenRouter e restituisce la decisione (open/close/hold).

    Se il modello risponde vuoto o con JSON non valido riprova; esaurititi i
    tentativi restituisce un segnale 'hold' invece di far fallire l'intero ciclo.
    """
    cl = get_openrouter_client()
    attempts = max_attempts or MAX_LLM_ATTEMPTS
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = cl.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_RULES},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
        except Exception as exc:
            last_error = f"Chiamata OpenRouter fallita: {type(exc).__name__}: {exc}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        api_error = _api_error(response)
        if api_error:
            last_error = f"OpenRouter ha restituito un errore: {api_error}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        output_text = _extract_message_text(response)
        if not output_text.strip():
            finish_reason = None
            try:
                finish_reason = response.choices[0].finish_reason
            except Exception:
                pass
            last_error = (
                f"Risposta vuota dal modello {OPENROUTER_MODEL} "
                f"(finish_reason={finish_reason})"
            )
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        try:
            return _clean_and_parse_json(output_text)
        except Exception as exc:
            last_error = f"JSON non valido: {type(exc).__name__}: {exc}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)

    print(f"❌ [LLM] nessuna risposta valida dopo {attempts} tentativi: {last_error}")
    logger.error("[LLM] fallback su 'hold' — %s", last_error)
    return _safe_hold_signal(f"Fallback automatico (nessuna decisione AI): {last_error}")
