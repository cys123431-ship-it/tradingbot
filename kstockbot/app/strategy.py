from .logger import get_logger
from .universe import Universe
from .settings import Settings

logger = get_logger("strategy")

async def scan_watchlist(kis_client, universe: Universe) -> list[dict]:
    symbols = universe.get_symbols()[:Settings.MAX_SCAN_SYMBOLS]
    logger.info(f"Scanning {len(symbols)} symbols (max={Settings.MAX_SCAN_SYMBOLS})")
    results = []
    
    for symbol in symbols:
        try:
            res = await kis_client.get_price(symbol)
            output = res.get("output", {})
            
            # Defensive fetching
            price_str = output.get("stck_prpr", "0")
            vol_str = output.get("acml_vol", "0")
            change_rate_str = output.get("prdy_ctrt", "0")
            
            price = int(price_str) if price_str.lstrip('-').isdigit() else 0
            vol = int(vol_str) if vol_str.isdigit() else 0
            
            try:
                change_rate = float(change_rate_str)
            except ValueError:
                change_rate = 0.0

            if price < 1000:
                results.append({"symbol": symbol, "score": 0, "reason": "Price < 1000"})
                continue
                
            if price == 0 or vol == 0:
                results.append({"symbol": symbol, "score": 0, "reason": "Insufficient data"})
                continue
                
            score = 0
            reasons = []
            
            score += 10
            reasons.append("price_valid")
            
            score += 10
            reasons.append("volume_available")
            
            if -5.0 <= change_rate <= 10.0:
                score += 10
                reasons.append("change_rate_ok")
            
            if change_rate > 15.0:
                score -= 20
                reasons.append("too_hot")
                
            results.append({
                "symbol": symbol,
                "score": score,
                "reason": ", ".join(reasons)
            })
            
        except Exception as e:
            logger.error(f"Error scanning {symbol}: {e}")
            err = str(e)
            if len(err) > 120:
                err = err[:120] + "..."
            results.append({
                "symbol": symbol,
                "score": -999,
                "reason": f"price_fetch_failed: {err}"
            })
            
    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results
