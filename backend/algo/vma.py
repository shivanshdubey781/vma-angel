def _vma_series(rows: list[dict], length: int) -> list[float]:
    """
    LazyBear Variable Moving Average - exact TradingView match.
    """
    if length <= 0:
        raise ValueError("length must be > 0")
    k = 1.0 / length
    pdmS = mdmS = pdiS = mdiS = iS_val = 0.0
    vma_prev = rows[0]["close"]          # warm-up seed — CRITICAL, do not remove
    iS_arr: list[float] = []
    out: list[float] = []
    
    for i, r in enumerate(rows):
        src = r["close"]
        prev = rows[i-1]["close"]  if i > 0  else src

        pdm = max(src - prev, 0.0)
        mdm = max(prev - src, 0.0)

        pdmS = (1 - k) * pdmS + k * pdm
        mdmS = (1 - k) * mdmS + k * mdm

        s = pdmS + mdmS
        pdi = pdmS / s   if s else 0.0
        mdi = mdmS / s   if s else 0.0

        pdiS = (1 - k) * pdiS + k * pdi
        mdiS = (1 - k) * mdiS + k * mdi

        d = abs(pdiS - mdiS)
        s1 = pdiS + mdiS
        ratio = d / s1   if s1 else 0.0
        iS_val = (1 - k) * iS_val + k * ratio
        iS_arr.append(iS_val)

        win = iS_arr[max(0, i - length + 1) : i + 1]
        hhv, llv = max(win), min(win)
        rng = hhv - llv
        vI = (iS_val - llv) / rng   if rng else 0.0

        vma_val = (1 - k * vI) * vma_prev + k * vI * src
        vma_prev = vma_val
        out.append(round(vma_val, 4))
        
    return out

def _atr_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder ATR - same as TradingView ta.atr()"""
    trs, atrs = [], []
    prev_close = None
    atr = 0.0
    for r in rows:
        hi, lo, cl = r["high"], r["low"], r["close"]
        if prev_close is None:
            tr = hi - lo
        else:
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        n = len(trs)
        if n < period:
            atr = sum(trs) / n
        elif n == period:
            atr = sum(trs) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        atrs.append(round(atr, 4))
        prev_close = cl
    return atrs

def _rsi_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder RSI - same as TradingView ta.rsi()"""
    closes = [r["close"] for r in rows]
    gains, losses = [], []
    rsis: list[float] = []
    avg_gain = avg_loss = 0.0
    for i, cl in enumerate(closes):
        if i == 0:
            rsis.append(50.0)
            continue
        chg = cl - closes[i - 1]
        gain = max(chg, 0.0)
        loss = max(-chg, 0.0)
        if i < period:
            gains.append(gain)
            losses.append(loss)
            avg_gain = sum(gains) / len(gains)
            avg_loss = sum(losses) / len(losses)
        elif i == period:
            gains.append(gain)
            losses.append(loss)
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
            
        if avg_gain == 0.0 and avg_loss == 0.0:
            rsis.append(50.0)
        elif avg_loss == 0.0:
            rsis.append(100.0)
        else:
            rsis.append(round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2))
    return rsis

def compute_dual_vma(rows: list[dict], short_len: int = 5, long_len: int = 9) -> list[dict]:
    """
    Compute Short-VMA and Long-VMA for every bar and tag crossover signals.
    """
    if short_len >= long_len:
        raise ValueError("short_len must be < long_len")
    
    short_vma = _vma_series(rows, short_len)
    long_vma = _vma_series(rows, long_len)
    atr_vals = _atr_series(rows, 14)
    rsi_vals = _rsi_series(rows, 14)
    
    results: list[dict] = []
    for i, r in enumerate(rows):
        sv = short_vma[i]
        lv = long_vma[i]
        atr = atr_vals[i]
        rsi = rsi_vals[i]
        
        # --- Crossover signal ---
        if i == 0:
            signal = "NONE"
        else:
            prev_sv = short_vma[i-1]
            prev_lv = long_vma[i-1]
            if prev_sv <= prev_lv and sv > lv:
                signal = "CE"    # short crosses above long → bullish
            elif prev_sv >= prev_lv and sv < lv:
                signal = "PE"    # short crosses below long → bearish
            else:
                signal = "NONE"
                
        # --- Slopes (3-bar lookback) ---
        short_slope = round(sv - short_vma[i-3], 4)   if i >= 3  else 0.0
        long_slope  = round(lv - long_vma[i-3],  4)   if i >= 3  else 0.0
        
        # --- Spread & sideways flag ---
        vma_spread  = round(abs(sv - lv), 4)
        is_sideways = bool(vma_spread < round(atr * 0.3, 4))
        
        # --- ATR bands (around short VMA) ---
        upper_band = round(sv + atr * 1.5, 4)
        lower_band = round(sv - atr * 1.5, 4)
        
        # --- Short-VMA trend direction (for UI colouring) ---
        if i == 0:              svma_trend = "FLAT"
        elif sv > short_vma[i-1]: svma_trend = "UP"
        elif sv < short_vma[i-1]: svma_trend = "DOWN"
        else:                   svma_trend = "FLAT"
        
        # --- Relative position of short vs long ---
        position = "ABOVE" if sv > lv else ("BELOW" if sv < lv else "CROSS")
        
        # --- Confirm signal = previous bar's signal ---
        confirm_signal = results[i-1]["signal"]   if i > 0  else "NONE"
        
        # --- Quality score 0–5 ---
        active_signal = signal if signal != "NONE" else confirm_signal
        quality = 0
        if active_signal != "NONE":
            quality += 1                                   # point 1: crossover exists

            if active_signal == "CE" and short_slope > 0:
                quality += 1                               # point 2: short slope up
            elif active_signal == "PE" and short_slope < 0:
                quality += 1

            if active_signal == "CE" and long_slope > 0:
                quality += 1                               # point 3: long slope up
            elif active_signal == "PE" and long_slope < 0:
                quality += 1

            if vma_spread >= round(atr * 0.5, 4):
                quality += 1                               # point 4: lines separating

            if active_signal == "CE" and rsi > 55:
                quality += 1                               # point 5: RSI confirms
            elif active_signal == "PE" and rsi < 45:
                quality += 1
                
        results.append({
            "timestamp": r["timestamp"],
            "open": round(r["open"], 4),
            "high": round(r["high"], 4),
            "low": round(r["low"], 4),
            "close": round(r["close"], 4),
            "short_vma": sv,
            "long_vma": lv,
            "signal": signal,
            "confirm_signal": confirm_signal,
            "svma_trend": svma_trend,
            "position": position,
            "atr": atr,
            "rsi": rsi,
            "upper_band": upper_band,
            "lower_band": lower_band,
            "is_sideways": is_sideways,
            "short_slope": short_slope,
            "long_slope": long_slope,
            "quality": quality
        })
    return results
