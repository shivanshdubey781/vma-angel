const API_BASE = "/api";

// ── AUDIO NOTIFICATION SYSTEM ──
let audioCtx = null;
function getAudioContext() {
    if (!audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (AudioContext) {
            audioCtx = new AudioContext();
        }
    }
    return audioCtx;
}

// Unlock browser audio security on first click
document.addEventListener('click', () => {
    const ctx = getAudioContext();
    if (ctx && ctx.state === 'suspended') {
        ctx.resume();
    }
}, { once: true });

function playDingSound() {
    try {
        const ctx = getAudioContext();
        if (!ctx) return;
        
        // Force resume in case browser suspended it
        if (ctx.state === 'suspended') {
            ctx.resume();
        }
        
        const now = ctx.currentTime;
        
        // Primary oscillator (pure tone)
        const osc1 = ctx.createOscillator();
        const gain1 = ctx.createGain();
        osc1.type = "sine";
        osc1.frequency.setValueAtTime(1600, now); // High-pitched chime frequency
        gain1.gain.setValueAtTime(0.7, now); // Loud volume
        gain1.gain.exponentialRampToValueAtTime(0.001, now + 1.8); // Chime rings for 1.8 seconds
        osc1.connect(gain1);
        gain1.connect(ctx.destination);
        
        // Secondary harmonic oscillator for metallic sheen
        const osc2 = ctx.createOscillator();
        const gain2 = ctx.createGain();
        osc2.type = "sine";
        osc2.frequency.setValueAtTime(2200, now);
        gain2.gain.setValueAtTime(0.3, now);
        gain2.gain.exponentialRampToValueAtTime(0.001, now + 1.2);
        osc2.connect(gain2);
        gain2.connect(ctx.destination);
        
        osc1.start(now);
        osc1.stop(now + 1.8);
        osc2.start(now);
        osc2.stop(now + 1.2);
        
    } catch (e) {
        console.error("Failed to play ding sound:", e);
    }
}

let previousActiveTradeIds = null;


function login() {
    const btn = document.getElementById("login-btn");
    const originalText = btn.innerText;
    btn.innerText = "Connecting...";
    btn.disabled = true;

    fetch(API_BASE + "/auth/login", { method: "POST" })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                alert("✅ " + (data.message || "AngelOne login successful!"));
            } else {
                alert("❌ Login failed: " + (data.message || "Unknown error"));
            }
            checkUser();
        })
        .catch(e => {
            console.error("Login failed", e);
            alert("Login request failed. Check backend logs.");
        })
        .finally(() => {
            btn.innerText = originalText;
            btn.disabled = false;
        });
}


function triggerFakeSignal(signalType) {
    const btn = document.querySelector(signalType === 'CE' ? '.fake-ce-btn' : '.fake-pe-btn');
    const originalText = btn.innerText;
    btn.innerText = "Triggering...";
    btn.disabled = true;

    fetch(API_BASE + "/algo/fake_signal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ signal: signalType })
    })
    .then(res => res.json())
    .then(data => {
        alert(data.message || `Fake ${signalType} Entry Scheduled`);
    })
    .catch(e => {
        console.error(`Fake signal ${signalType} failed`, e);
        alert(`Failed to trigger fake signal. Check if Algo is enabled.`);
    })
    .finally(() => {
        btn.innerText = originalText;
        btn.disabled = false;
    });
}

function checkUser() {
    fetch(API_BASE + "/auth/user")
        .then(res => res.json())
        .then(data => {
            const display = document.getElementById("user-display");
            const timeDisplay = document.getElementById("last-login-display");
            const btn = document.getElementById("login-btn");

            if (data.logged_in) {
                display.innerText = "✅ Connected: " + data.client_id + " (AngelOne)";
                display.style.background = "var(--success)";
                btn.style.display = "none";
                timeDisplay.innerText = data.last_login ? "Last Login: " + data.last_login : "Last Login: Unknown";
            } else {
                display.innerText = "Status: Disconnected";
                display.style.background = "var(--danger)";
                btn.style.display = "inline-block";
                timeDisplay.innerText = "Last Login: --";
            }
        })
        .catch(e => console.error("checkUser failed:", e));
}

async function loadConfig() {
    try {
        const res = await fetch(API_BASE + "/config");
        const data = await res.json();
        
        document.getElementById("cfg-fast").value = data.fast_period || 5;
        document.getElementById("cfg-slow").value = data.slow_period || 9;
        document.getElementById("cfg-target").value = data.target_pts || 50;
        document.getElementById("cfg-sl").value = data.sl_pts || 30;
        document.getElementById("cfg-trigger").value = data.trail_trigger || 20;
        document.getElementById("cfg-trail").value = data.trail_pts || 15;
        document.getElementById("cfg-qty").value = data.quantity || 1;
        document.getElementById("cfg-buffer").value = data.limit_buffer || 2.0;
        
        // Load Entry Mode
        const entryModeEl = document.getElementById("cfg-entry-mode");
        if (entryModeEl) {
            entryModeEl.value = data.immediate_entry ? "immediate" : "confirmed";
        }
        
        // Load Strike Selection
        const strikeSelEl = document.getElementById("cfg-strike-selection");
        if (strikeSelEl) {
            strikeSelEl.value = data.strike_selection || "A";
        }
        
        if (data.interval) {
            document.getElementById("interval-select").value = data.interval;
        }
        
        const toggle = document.getElementById("algo-toggle");
        const statusText = document.getElementById("algo-status-text");
        toggle.checked = data.algo_enabled || false;
        
        if(toggle.checked) {
            statusText.innerText = "ON";
            statusText.style.color = "var(--success)";
        } else {
            statusText.innerText = "OFF";
            statusText.style.color = "var(--danger)";
        }
    } catch(e) {
        console.error("Config load error", e);
    }
}

async function saveConfig() {
    const btn = document.querySelector(".save-btn");
    btn.innerText = "Saving...";
    
    const config = {
        fast_period: parseInt(document.getElementById("cfg-fast").value) || 5,
        slow_period: parseInt(document.getElementById("cfg-slow").value) || 9,
        target_pts: parseInt(document.getElementById("cfg-target").value) || 50,
        sl_pts: parseInt(document.getElementById("cfg-sl").value) || 30,
        trail_trigger: parseInt(document.getElementById("cfg-trigger").value) || 20,
        trail_pts: parseInt(document.getElementById("cfg-trail").value) || 15,
        quantity: parseInt(document.getElementById("cfg-qty").value) || 1,
        limit_buffer: parseFloat(document.getElementById("cfg-buffer").value) || 2.0,
        symbol: "NIFTY",
        exchange: "NFO",
        interval: document.getElementById("interval-select").value,
        immediate_entry: document.getElementById("cfg-entry-mode").value === "immediate",
        strike_selection: document.getElementById("cfg-strike-selection").value || "A"
    };
    
    try {
        const res = await fetch(API_BASE + "/config", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(config)
        });
        const data = await res.json();
        if(data.success) {
            btn.innerText = "Saved!";
            setTimeout(() => { btn.innerText = "Save Configuration"; }, 2000);
        }
    } catch(e) {
        alert("Error saving configuration.");
        btn.innerText = "Save Configuration";
    }
}

async function toggleAlgo() {
    const toggle = document.getElementById("algo-toggle");
    const statusText = document.getElementById("algo-status-text");
    
    try {
        const res = await fetch(API_BASE + "/algo/toggle", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({enabled: toggle.checked})
        });
        const data = await res.json();
        
        if(data.algo_enabled) {
            statusText.innerText = "ON";
            statusText.style.color = "var(--success)";
        } else {
            statusText.innerText = "OFF";
            statusText.style.color = "var(--danger)";
        }
    } catch(e) {
        alert("Error toggling algo.");
        toggle.checked = !toggle.checked;
    }
}

function updateInterval() {
    saveConfig();
}

async function fetchStatus() {
    try {
        const res = await fetch(API_BASE + "/algo/status");
        const data = await res.json();
        
        document.getElementById("mon-signal").innerText = data.last_signal || "NONE";
        document.getElementById("mon-trade").innerText = data.last_trade_at ? new Date(data.last_trade_at).toLocaleString() : "NONE";
        
        document.getElementById("mon-fast").innerText = data.fast_vma ? data.fast_vma.toFixed(2) : "0.00";
        document.getElementById("mon-slow").innerText = data.slow_vma ? data.slow_vma.toFixed(2) : "0.00";
        
        renderActiveTrades(data.open_trades || []);
        
    } catch(e) {
        console.error("Status check failed");
    }
}

function renderActiveTrades(trades) {
    const container = document.getElementById("active-trades-container");
    
    // Extract current trade IDs
    const currentTradeIds = new Set((trades || []).map(t => t.trade_id || t._id));
    
    // Compare with previous state to detect entries or exits
    if (previousActiveTradeIds !== null) {
        let playSound = false;
        
        // 1. Detect new entries
        for (let id of currentTradeIds) {
            if (!previousActiveTradeIds.has(id)) {
                console.log("[AUDIO] New trade entry detected:", id);
                playSound = true;
            }
        }
        
        // 2. Detect trade exits (TP / SL / Trial SL / Manual Exit)
        for (let id of previousActiveTradeIds) {
            if (!currentTradeIds.has(id)) {
                console.log("[AUDIO] Trade exit detected:", id);
                playSound = true;
            }
        }
        
        if (playSound) {
            playDingSound();
        }
    }
    
    previousActiveTradeIds = currentTradeIds;

    if(!trades || trades.length === 0) {
        container.innerHTML = '<p style="color: #888; font-size: 0.9rem; text-align: center;">No active trades</p>';
        return;
    }
    
    // Remove "No active trades" message if present
    if(container.querySelector('p')) {
        container.innerHTML = '';
    }
    
    // Track existing trade IDs to remove closed ones
    Array.from(container.children).forEach(child => {
        if(!currentTradeIds.has(child.id)) {
            child.remove();
        }
    });
    
    trades.forEach(t => {
        const id = t.trade_id || t._id;
        const ltp = t.current_ltp || t.entry_price;
        const tp = t.target_price || (t.entry_price + 50);
        const sl = t.current_sl || (t.entry_price - 30);
        
        // Calculate percentages for the bar (0% is SL, 100% is TP)
        const totalRange = tp - sl;
        let ltpPercent = ((ltp - sl) / totalRange) * 100;
        ltpPercent = Math.max(0, Math.min(100, ltpPercent)); // clamp between 0-100
        
        const isTrailActive = t.trail_active;
        const badgeClass = isTrailActive ? 'bg-success' : 'bg-warning';
        const badgeText = isTrailActive ? 'Trailing ACTIVE' : 'Trailing Waiting';
        const barColor = ltp >= t.entry_price ? 'var(--success)' : 'var(--danger)';
        const entryPercent = ((t.entry_price - sl) / totalRange) * 100;
        
        let tradeDiv = document.getElementById(id);
        
        if(!tradeDiv) {
            tradeDiv = document.createElement('div');
            tradeDiv.id = id;
            tradeDiv.style.cssText = "background: #f8fafc; border-radius: 8px; padding: 12px; margin-bottom: 10px; border: 1px solid #e2e8f0;";
            
            tradeDiv.innerHTML = `
                <div style="display:flex; justify-content:space-between; margin-bottom: 8px;">
                    <strong style="color:var(--primary)" class="trade-symbol"></strong>
                    <div>
                        <span class="badge trade-badge" style="font-size:0.75rem; margin-right: 5px;"></span>
                        <button class="btn btn-danger exit-btn" style="padding: 2px 8px; font-size: 0.75rem;" onclick="exitTrade('${id}')">Exit</button>
                    </div>
                </div>
                <div style="display:flex; justify-content:space-between; font-size: 0.85rem; margin-bottom: 5px; color:#64748b;">
                    <span class="trade-sl"></span>
                    <span style="color:#0f172a; font-weight:bold;" class="trade-ltp"></span>
                    <span class="trade-tp"></span>
                </div>
                <div style="width: 100%; height: 8px; background: #e2e8f0; border-radius: 4px; position: relative; overflow: hidden;">
                    <div class="trade-bar" style="position: absolute; left: 0; top: 0; height: 100%; transition: width 0.3s ease, background-color 0.3s ease;"></div>
                    <div class="trade-entry-marker" style="position: absolute; top: 0; bottom: 0; width: 2px; background: #000; z-index: 10;" title="Entry Price"></div>
                </div>
                <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-top: 5px;">
                    <span class="trade-qty"></span>
                    <span class="trade-entry"></span>
                </div>
            `;
            container.appendChild(tradeDiv);
        }
        
        // Update DOM elements dynamically to allow CSS transitions
        tradeDiv.querySelector('.trade-symbol').innerText = t.symbol;
        tradeDiv.querySelector('.trade-badge').className = `badge trade-badge ${badgeClass}`;
        tradeDiv.querySelector('.trade-badge').innerText = badgeText;
        
        tradeDiv.querySelector('.trade-sl').innerText = `SL: ${sl.toFixed(2)}`;
        tradeDiv.querySelector('.trade-ltp').innerText = `LTP: ${ltp.toFixed(2)}`;
        tradeDiv.querySelector('.trade-tp').innerText = `TP: ${tp.toFixed(2)}`;
        
        tradeDiv.querySelector('.trade-bar').style.width = `${ltpPercent}%`;
        tradeDiv.querySelector('.trade-bar').style.backgroundColor = barColor;
        tradeDiv.querySelector('.trade-entry-marker').style.left = `${entryPercent}%`;
        
        tradeDiv.querySelector('.trade-qty').innerText = `Qty: ${t.quantity}`;
        tradeDiv.querySelector('.trade-entry').innerText = `Entry: ${t.entry_price.toFixed(2)}`;
    });
}

let nearestStrikeData = null;

async function updateManualStrikePreview() {
    const side = document.getElementById("man-side").value;
    const nameEl = document.getElementById("man-strike-name");
    const priceEl = document.getElementById("man-strike-price");
    
    if (!nearestStrikeData) {
        nameEl.innerText = "Loading...";
        priceEl.innerText = "--";
        try {
            const res = await fetch(API_BASE + "/algo/nearest_strike");
            nearestStrikeData = await res.json();
        } catch(e) {
            console.error("Failed to fetch nearest strike", e);
            nameEl.innerText = "Error loading";
            return;
        }
    }
    
    if (nearestStrikeData && nearestStrikeData.success) {
        if (side === "BUY") { // CE
            nameEl.innerText = nearestStrikeData.ce_symbol;
            priceEl.innerText = `₹ ${nearestStrikeData.ce_price.toFixed(2)}`;
        } else { // PE
            nameEl.innerText = nearestStrikeData.pe_symbol;
            priceEl.innerText = `₹ ${nearestStrikeData.pe_price.toFixed(2)}`;
        }
    }
}

function openManualTrade() {
    nearestStrikeData = null;
    document.getElementById("manual-modal").style.display = "block";
    updateManualStrikePreview();
}

function closeManualTrade() {
    document.getElementById("manual-modal").style.display = "none";
}

async function executeManual() {
    const side = document.getElementById("man-side").value;
    const qty = parseInt(document.getElementById("man-qty").value) || 1;
    const bufferVal = document.getElementById("man-price").value;
    const buffer = bufferVal === "" ? 2.0 : parseFloat(bufferVal);
    
    const req = {
        direction: side,
        quantity: qty,
        buffer: buffer
    };
    
    const btn = document.querySelector("#manual-modal .btn-success");
    btn.innerText = "Executing...";
    btn.disabled = true;
    
    try {
        const res = await fetch(API_BASE + "/algo/manual_trade", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(req)
        });
        const data = await res.json();
        alert(data.message);
        if(data.success) {
            closeManualTrade();
        }
    } catch(e) {
        alert("Error executing trade.");
    } finally {
        btn.innerText = "Execute Trade";
        btn.disabled = false;
    }
}

async function exitTrade(tradeId) {
    if(!confirm("Are you sure you want to manually exit this trade at the current market price?")) return;
    
    try {
        const res = await fetch(API_BASE + "/algo/manual_exit", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({trade_id: tradeId})
        });
        const data = await res.json();
        alert(data.message);
        if(data.success) fetchStatus();
    } catch(e) {
        alert("Error exiting trade manually.");
    }
}

// Fetch VMA signals for history table
async function fetchSignals() {
    try {
        const res = await fetch(API_BASE + "/algo/signals_history?limit=30");
        if (!res.ok) {
            console.warn("Signals endpoint not ready");
            return;
        }
        const data = await res.json();
        renderSignalsTable(data.signals || []);
    } catch(e) {
        console.error("Failed to fetch signals", e);
    }
}

// Render signals in table format
function renderSignalsTable(signals) {
    const tbody = document.getElementById("signals-tbody");
    if (!tbody) return;
    
    if (!signals || !Array.isArray(signals) || signals.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align: center; color: #888; padding: 20px;">No signals recorded yet...</td></tr>';
        return;
    }
    
    // Clear and rebuild
    tbody.innerHTML = '';
    
    // Render newest signals first (which matches the database descending order)
    signals.forEach(sig => {
        const tr = document.createElement("tr");
        
        // Format timestamp
        let timeStr = "-";
        if (sig.timestamp) {
            try {
                const dt = new Date(sig.timestamp);
                if (!isNaN(dt.getTime())) {
                    timeStr = dt.toLocaleTimeString('en-IN', { hour12: false });
                }
            } catch (e) {
                console.error("Error formatting time:", e);
            }
        }
        
        // Close price
        const close = (sig.close !== undefined && sig.close !== null) ? parseFloat(sig.close).toFixed(2) : "-";
        
        // Short VMA
        const shortVma = (sig.short_vma !== undefined && sig.short_vma !== null) ? parseFloat(sig.short_vma).toFixed(2) : "-";
        
        // Long VMA
        const longVma = (sig.long_vma !== undefined && sig.long_vma !== null) ? parseFloat(sig.long_vma).toFixed(2) : "-";
        
        // Signal
        let signalClass = "signal-none";
        let signalText = sig.signal || "-";
        if (sig.signal === "BUY" || sig.signal === "CE") signalClass = "signal-buy";
        else if (sig.signal === "SELL" || sig.signal === "PE") signalClass = "signal-sell";
        
        // Confirm signal
        let confirmClass = "signal-none";
        let confirmText = sig.confirm_signal || "-";
        if (sig.confirm_signal === "BUY" || sig.confirm_signal === "CE") confirmClass = "signal-buy";
        else if (sig.confirm_signal === "SELL" || sig.confirm_signal === "PE") confirmClass = "signal-sell";
        
        // Quality
        let qualityClass = "quality-0";
        const quality = sig.quality !== undefined ? sig.quality : 0;
        if (quality === 5) qualityClass = "quality-5";
        else if (quality === 4) qualityClass = "quality-4";
        else if (quality === 3) qualityClass = "quality-3";
        else if (quality === 2) qualityClass = "quality-2";
        else if (quality === 1) qualityClass = "quality-1";
        const qualityText = (quality !== null && quality !== undefined) ? quality : "0";
        
        // Trend
        let trendClass = "trend-none";
        let trendText = sig.svma_trend || "-";
        if (sig.svma_trend === "UP") trendClass = "trend-up";
        else if (sig.svma_trend === "DOWN") trendClass = "trend-down";
        
        // Skip reason
        const skipReason = sig.skip_reason || "-";
        
        tr.innerHTML = `
            <td>${timeStr}</td>
            <td style="font-weight: 600;">${close}</td>
            <td>${shortVma}</td>
            <td>${longVma}</td>
            <td><span class="${signalClass}">${signalText}</span></td>
            <td><span class="${confirmClass}">${confirmText}</span></td>
            <td><span class="${qualityClass}">${qualityText}</span></td>
            <td><span class="${trendClass}">${trendText}</span></td>
            <td><span class="skip-reason" title="${skipReason}">${skipReason}</span></td>
        `;
        tbody.appendChild(tr);
    });
}

// Initialization
document.addEventListener("DOMContentLoaded", () => {
    checkUser();
    loadConfig();
    
    // Fetch immediately on load
    fetchStatus();
    fetchSignals();
    
    // Periodically fetch status and signals
    setInterval(fetchStatus, 1500);
    setInterval(fetchSignals, 3000);
});

// Order History
function showHistory() {
    const modal = document.getElementById("history-modal");
    modal.style.display = "block";
    fetchHistory();
}

function closeHistory() {
    document.getElementById("history-modal").style.display = "none";
}

function fetchHistory() {
    fetch(API_BASE + "/history")
        .then(res => res.json())
        .then(data => {
            if(data.success) {
                const tbody = document.getElementById("history-tbody");
                tbody.innerHTML = "";
                data.trades.forEach(t => {
                    const badgeClass = t.status === 'OPEN' ? 'bg-warning' : (t.status === 'REJECTED' || t.status === 'FAILED' ? 'bg-danger' : 'bg-success');
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td>${t.entry_time || '-'}</td>
                        <td>${t.symbol}</td>
                        <td style="color:${t.direction==='BUY'?'var(--success)':'var(--danger)'}">${t.direction}</td>
                        <td>${t.quantity}</td>
                        <td>${t.entry_price ? t.entry_price.toFixed(2) : '-'}</td>
                        <td>${t.exit_price ? t.exit_price.toFixed(2) : '-'}</td>
                        <td><span class="badge ${badgeClass}">${t.status}</span></td>
                        <td style="font-size:0.8rem; color:${t.status==='REJECTED'?'var(--danger)':'inherit'}">${t.close_reason || '-'}</td>
                    `;
                    tbody.appendChild(tr);
                });
            }
        });
}

window.onclick = function(event) {
    const modal = document.getElementById("history-modal");
    if (event.target == modal) {
        modal.style.display = "none";
    }
}
