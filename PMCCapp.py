import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC CEO Command Center", page_icon="🛡️")

# --- DISEÑO UI PREMIUM ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 12px; 
        border: 1px solid #374151; text-align: center; height: 140px;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.3);
    }
    .metric-label {color: #9ca3af; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; margin-bottom: 8px;}
    .metric-value {font-size: 1.5rem; font-weight: bold; margin: 0;}
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
    st.caption("v19.0.0 | Active Shorts Summary")

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

def get_underlying_symbol(symbol):
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
    all_syms = list(set([p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    report = {}
    detailed_positions = []

    # Identificar Leaps (>180 DTE)
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        q_data = m_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Calcular DTE
        dte_val = 0
        if q_data.get('expiration_date'):
            exp_dt = datetime.strptime(q_data['expiration_date'], '%Y-%m-%d')
            dte_val = (exp_dt - datetime.now()).days

        # Solo PMCC Leaps
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.60 and dte_val > 180:
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

    # Auditoría de Trades Cerrados
    for gl in gl_data:
        sym = gl.get('symbol', '')
        u_sym, o_type, strike = decode_occ_symbol(sym)
        if u_sym in report and o_type == "CALL":
            close_dt = datetime.strptime(gl.get('close_date','2000-01-01')[:10], '%Y-%m-%d')
            open_dt = datetime.strptime(gl.get('open_date','2000-01-01')[:10], '%Y-%m-%d')
            if close_dt >= datetime.strptime(report[u_sym]['leaps'][0]['Adquirido'], '%Y-%m-%d'):
                is_leap_strike = any(abs(strike - ls) < 0.5 for ls in report[u_sym]['leaps_strikes'])
                if not is_leap_strike:
                    gain = float(gl.get('gain_loss', 0))
                    report[u_sym]['realized_cc'] += gain
                    report[u_sym]['closed_list'].append({"Cerrado": close_dt.strftime('%Y-%m-%d'), "Strike": strike, "P/L": gain, "DIT": (close_dt-open_dt).days})

    # Identificar Corto Activo con cálculos de "Jugo"
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        if u_sym in report and float(p['quantity']) < 0:
            q = m_map.get(sym, {})
            u_p = report[u_sym]['spot']
            strike = q.get('strike', 0)
            last_price = q.get('last', 0)
            
            # Matrícula de Precios
            initial_price = abs(float(p.get('cost_basis', 0))) / (abs(float(p['quantity'])) * 100)
            intrinsic = max(0, u_p - strike)
            extrinsic = last_price - intrinsic
            
            report[u_sym]['active_short'] = {
                "Symbol": sym, "Strike": strike, "Price_Init": initial_price, "Price_Last": last_price,
                "Intrinsic": intrinsic, "Extrinsic": extrinsic,
                "PL_Ext": initial_price - extrinsic, "PL_Total": (initial_price - last_price) * 100 * abs(float(p['quantity'])),
                "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
            }

    return {"nl": net_liq, "report": report}

# --- UI ---

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR TODO EL SISTEMA"):
        data = run_master_analysis()
        if data:
            nl = data['nl']
            report = data['report']

            # --- NUEVA SECCIÓN: TABLA RESUMEN DE VENTAS (SHORTS) ---
            st.markdown('<div class="section-header">📑 RESUMEN DE VENTAS ACTIVAS (COVERED CALLS)</div>', unsafe_allow_html=True)
            
            summary_list = []
            for ticker, d in report.items():
                if d['active_short']:
                    a = d['active_short']
                    summary_list.append({
                        "Activo": ticker,
                        "Strike": a['Strike'],
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
                    "Pr. Inicial": "${:.2f}", "Pr. Actual": "${:.2f}",
                    "Intrínseco": "${:.2f}", "Extrínseco (Jugo)": "${:.2f}",
                    "P/L Jugo": "${:.2f}", "P/L Total ($)": "${:,.2f}"
                }), use_container_width=True)
            else:
                st.info("No hay ventas cortas activas en las campañas PMCC.")

            # --- SECCIÓN POR ACTIVO (TOM KING STYLE) ---
            for ticker, d in report.items():
                st.markdown(f'<div class="section-header">DETALLE CAMPAÑA: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                tc = sum([l['Cost'] for l in d['leaps']])
                tv = sum([l['Value'] for l in d['leaps']])
                re = d['realized_cc']
                ni = (tv - tc) + re
                ro = (ni / tc * 100) if tc > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${tc:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${tv:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${re:,.2f}</p></div>', unsafe_allow_html=True)
                cc4_val = f'<p class="kpi-value">${ni:,.2f}</p>' if ni >= 0 else f'<p class="kpi-value" style="color:#f87171">-${abs(ni):,.2f}</p>'
                c4.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">NET INCOME</p>{cc4_val}</div>', unsafe_allow_html=True)
                
                r_c = "#4ade80" if ro > 0 else "#f87171"
                c5.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">ROI TOTAL</p><p class="roi-val" style="color:{r_c}">{ro:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    ash = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {ash['Strike']} | DTE: {ash['DTE']} | **Extrínseco: ${ash['Ext']:.2f}**")
                    if ash['Ext'] < 0.20: st.error("🚨 TIEMPO DE ROLEAR")

                if d['closed_list']:
                    with st.expander("📔 Ver Historial de Trades Cerrados"):
                        st.dataframe(pd.DataFrame(d['closed_list']).sort_values("Cerrado", ascending=False))
                st.divider()

else:
    st.info("👈 Ingresa tu Token de Tradier.")
