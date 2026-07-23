from typing import List

def detect_signals(fast_vma: List[float], slow_vma: List[float], closes: List[float]) -> List[str]:
    """
    Returns a list of signals corresponding to each bar.
    'BUY', 'SELL', or 'NONE'
    """
    signals = []
    for i in range(len(closes)):
        if i == 0:
            signals.append("NONE")
            continue
            
        fast_curr = fast_vma[i]
        slow_curr = slow_vma[i]
        fast_prev = fast_vma[i-1]
        slow_prev = slow_vma[i-1]
        
        if fast_prev <= slow_prev and fast_curr > slow_curr:
            signals.append("BUY")
        elif fast_prev >= slow_prev and fast_curr < slow_curr:
            signals.append("SELL")
        else:
            signals.append("NONE")
            
    return signals
