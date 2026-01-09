import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
import yfinance as yf
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC CEO Command Center Pro", page_icon="🛡️")

# --- DISEÑO UI PREMIUM (CSS) ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    /* Tarjetas de KPI */
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 12px; 
        border: 1px solid #374151; text-align: center; height: 140px;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.3);
    }
    .metric-label {color: #9ca3af; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; margin-bottom: 8px;}
    .metric-value {font-size: 1.5rem; font-weight: bold; margin: 0;}
    
    /* Encabezados de Sección */
    .section-header {
        background: linear-gradient(90deg, #238636 0%, #2ea043 100%);
        color: white; padding: 10px 20px; 
        border-radius: 8px; margin: 30px 0 15px 0; font-size: 1.2rem; font-weight: bold;
    }
    .summary-card-pmcc {
        background-color: #161b22; border: 1px solid #30363d;
        padding: 15px; border-radius: 8px; text-align: center; height: 125px;
    }
    .roi-val {font-size: 1.6rem; font-weight: bold;}
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ PMCC CEO Command Center")

# --- INICIALIZAR HISTORIAL ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])

# --- SIDEBAR ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("v19.2.0 | Clean Strikes & Qty Added")

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW']: return '^GSPC'
    if s in ['NDX', 'NDXW']: return '^NDX'
    if s in ['RUT', 'RUTW']: return '^RUT'
    return s.replace('/', '-')

def get_underlying_symbol(symbol):
    if not symbol: return ""
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def decode_occ_symbol(symbol):
    if not symbol or len(symbol) < 15: return symbol, "STOCK", 0
    try:
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
        if match:
            o_type = "CALL" if match.group(3) == "C" else "PUT"
            strike = float(match.group(4)) / 1000
            return match.group(1), o_type, strike
    except: pass
    return symbol, "UNKNOWN", 0

def clean_df_finance(df):
    if df.empty: return pd.Series()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    col = 'Adj Close' if 'Adj Close' in df.columns else ('Close' if 'Close' in df.columns else df.columns[0])
    series = df[col]
    series.index = series.index.tz_localize(None)
    return series

@st.cache_data(ttl=3600)
def get_beta(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV']: return 0.0
    try:
        data = yf.download(map_to_yahoo(ticker), period="1y", progress=False)
        stock_series = clean_df_finance(data)
        if stock_series.empty: return 1.0
        ret = stock_series.pct_change().dropna()
        aligned = pd.concat([ret, spy_returns], axis=1, join='inner').dropna()
        if len(aligned) < 10: return 1.0
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- MOTOR DE ANÁLISIS ---
def run_master_analysis():
    # 1. Obtener Datos Cuenta
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    prof = r_p.json()['profile']['account']
    acct_id = prof['account_number'] if isinstance(prof, dict) else prof[0]['account_number']
    
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    # 3. Ganancias Realizadas
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    # 4. Market Data
    all_syms = list(set([p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # 5. Yahoo para Beta
    spy_df_raw = yf.download("SPY", period="1y", progress=False)
    spy_ret = clean_df_finance(spy_df_raw).pct_change().dropna()

    report = {}
    
    # A. Identificar Leaps
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        q_data = m_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Calcular DTE
        dte_val = 0
        if q_data.get('expiration_date'):
            try:
                exp_dt = datetime.strptime(q_data['expiration_date'], '%Y-%m-%d')
                dte_val = (exp_dt - datetime.now()).days
            except: dte_val = 0

        if float(p['quantity']) > 0 and delta and abs(delta) > 0.55 and dte_val > 150:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "leaps_strikes": [], "realized_cc": 0.0, "closed_list": [], "active_short": None, "spot": m_map.get(u_sym, {}).get('last', 0)}
            
            cost_basis = abs(float(p.get('cost_basis', 0)))
            market_val = float(p['quantity']) * float(q_data.get('last', 0)) * 100
            
            report[u_sym]['leaps_strikes'].append(q_data.get('strike', 0))
            report[u_sym]['leaps'].append({
                "Adquirido": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'), "Strike": q_data.get('strike'),
                "Qty": int(float(p['quantity'])), "Cost": cost_basis, "Value": market_val, "P_L": market_val - cost_basis
            })

    # B. Auditoría de Trades Cerrados
    for gl in gl_data:
        sym = gl.get('symbol', '')
        u_sym, o_type, strike = decode_occ_symbol(sym)
        if u_sym in report and o_type == "CALL":
            try:
                close_dt = datetime.strptime(gl.get('close_date','2000-01-01')[:10], '%Y-%m-%d')
                leap_acq_dt = datetime.strptime(report[u_sym]['leaps'][0]['Adquirido'], '%Y-%m-%d')
                if close_dt >= leap_acq_dt:
                    is_leap_strike = any(abs(strike - ls) < 0.5 for ls in report[u_sym]['leaps_strikes'])
                    if not is_leap:
                        gain = float(gl.get('gain_loss', 0))
                        report[u_sym]['realized_cc'] += gain
                        report[u_sym]['closed_list'].append({"Cerrado": close_dt.strftime('%Y-%m-%d'), "Strike": strike, "P/L": gain, "DIT": gl.get('term', '-')})
            except: pass

    # C. Identificar Corto Activo
    total_rd, total_th, total_exp, total_bwd = 0, 0, 0, 0
    # Recalculamos riesgos globales aquí para unificar motor
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        q = m_map.get(sym, {})
        u_p = float(m_map.get(u_sym, {}).get('last', 0))
        qty = float(p['quantity'])
        is_opt = len(sym) > 5
        mult = 100 if is_opt else 1
        
        d = float(q.get('greeks', {}).get('delta', 1.0 if not is_opt else 0))
        th = float(q.get('greeks', {}).get('theta', 0))
        
        total_rd += (qty * d * mult)
        total_th += (qty * th * mult)
        
        if u_sym in report and qty < 0:
            strike = q.get('strike', 0)
            last_p = q.get('last', 0)
            init_p = abs(float(p.get('cost_basis', 0))) / (abs(qty) * 100)
            intr = max(0, u_p - strike)
            extr = last_p - intr
            
            try:
                exp_dt = datetime.strptime(q['expiration_date'], '%Y-%m-%d')
                dte_val = (exp_dt - datetime.now()).days
            except: dte_val = 0

            report[u_sym]['active_short'] = {
                "Qty": abs(int(qty)), "Strike": strike, "Price_Init": init_p, "Price_Last": last_p,
                "Intrinsic": intr, "Extrinsic": extr,
                "PL_Ext": init_p - extr, "PL_Total": (init_p - last_p) * 100 * abs(qty),
                "DTE": dte_val
            }
        
        # Para BWD Global
        beta = get_beta(u_sym, spy_ret)
        d_usd = qty * d * mult * u_p
        if spy_p > 0:
            total_bwd += (d_usd * beta) / spy_p
        total_exp += abs(d_usd)

    return {"nl": net_liq, "report": report, "rd": total_rd, "bwd": total_bwd, "th": total_th, "lev": total_exp, "spy_p": spy_p}

# --- UI ---
tab_risk, tab_ceo = st.tabs(["📊 Riesgo & Gráficos", "🏗️ CEO PMCC Accountant"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR COMMAND CENTER"):
        data = run_master_analysis()
        if data:
            new_h = {"Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": data['nl'], "Delta_Neto": data['rd'], "BWD_SPY": data['bwd'], "Theta_Diario": data['th'], "Apalancamiento": data['lev']/data['nl']}
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_h])], ignore_index=True)

            with tab_risk:
                st.markdown(f"### 🏦 Balance Neto: ${data['nl']:,.2f}")
                k1, k2, k3, k4 = st.columns(4)
                k1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value">{data["rd"]:.1f}</div></div>', unsafe_allow_html=True)
                k2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value">{data["bwd"]:.1f}</div></div>', unsafe_allow_html=True)
                k3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:#4ade80">${data["th"]:.2f}</div></div>', unsafe_allow_html=True)
                k4.markdown(f'<div class="card" style="border-bottom: 5px solid #00d4ff"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{data["lev"]/data["nl"]:.2f}x</div></div>', unsafe_allow_html=True)

                st.divider()
                h = st.session_state.history_df
                if len(h) > 1:
                    g1, g2 = st.columns(2)
                    with g1: st.write("**Capital ($)**"); st.area_chart(h, x="Timestamp", y="Net_Liq")
                    with g2: st.write("**Riesgo BWD**"); st.line_chart(h, x="Timestamp", y="BWD_SPY")

            with tab_ceo:
                report = data['report']
                # --- TABLA RESUMEN VENTAS (SHORTS) CON QTY Y STRIKE LIMPIO ---
                st.markdown('<div class="section-header">📑 RESUMEN DE VENTAS ACTIVAS (COVERED CALLS)</div>', unsafe_allow_html=True)
                
                summary_list = []
                for ticker, d in report.items():
                    if d['active_short']:
                        a = d['active_short']
                        summary_list.append({
                            "Activo": ticker,
                            "Contratos": a['Qty'],
                            "Strike": float(a['Strike']),
                            "Pr. Inicial": a['Price_Init'],
                            "Pr. Actual": a['Price_Last'],
                            "Intrínseco": a['Intrinsic'],
                            "Extrínseco (Jugo)": a['Extrinsic'],
                            "P/L Jugo": a['PL_Ext'],
                            "P/L Total ($)": a['PL_Total'],
                            "DTE": a['DTE']
                        })
                
                if summary_list:
                    df_sum = pd.DataFrame(summary_list)
                    st.dataframe(df_sum.style.format({
                        "Strike": "{:g}", # Elimina ceros finales
                        "Pr. Inicial": "${:.2f}", "Pr. Actual": "${:.2f}",
                        "Intrínseco": "${:.2f}", "Extrínseco (Jugo)": "${:.2f}",
                        "P/L Jugo": "${:.2f}", "P/L Total ($)": "${:,.2f}"
                    }), use_container_width=True)

                for ticker, d in report.items():
                    st.markdown(f'<div class="section-header">DETALLE CAMPAÑA: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                    tc, tv, re = sum([l['Cost'] for l in d['leaps']]), sum([l['Value'] for l in d['leaps']]), d['realized_cc']
                    ni = (tv - tc) + re
                    ro = (ni / tc * 100) if tc > 0 else 0
                    
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${tc:,.2f}</p></div>', unsafe_allow_html=True)
                    c2.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${tv:,.2f}</p></div>', unsafe_allow_html=True)
                    c3.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${re:,.2f}</p></div>', unsafe_allow_html=True)
                    c4.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${ni:,.2f}</p></div>', unsafe_allow_html=True)
                    c5.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">ROI TOTAL</p><p class="roi-val" style="color:{"#4ade80" if ro > 0 else "#f87171"}">{ro:.1f}%</p></div>', unsafe_allow_html=True)

                    st.write("### 🏛️ CORE POSITION (LEAPS)")
                    core_t = pd.DataFrame(d['leaps'])[['Adquirido', 'Exp', 'Strike', 'Qty', 'Cost', 'Value', 'P_L']]
                    st.table(core_t.style.format({"Strike": "{:g}", "Cost": "${:,.2f}", "Value": "${:,.2f}", "P_L": "${:,.2f}"}))

                    if d['active_short']:
                        ash = d['active_short']
                        st.write(f"### 🥤 MONITOR DE JUGO: Strike {ash['Strike']:g} | DTE: {ash['DTE']} | **Extrínseco: ${ash['Extrinsic']:.2f}**")
                        if ash['Extrinsic'] < 0.20: st.error("🚨 TIEMPO DE ROLEAR")
                    st.divider()
else:
    st.info("👈 Ingresa tu Token de Tradier.")
