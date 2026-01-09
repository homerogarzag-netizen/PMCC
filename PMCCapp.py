import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant Pro", page_icon="🛡️")

# --- DISEÑO UI ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 8px; 
        border: 1px solid #30363d; text-align: center; height: 130px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 8px;}
    .roi-positive {color: #2ea043; font-size: 1.6rem; font-weight: bold;}
    .roi-negative {color: #f87171; font-size: 1.6rem; font-weight: bold;}
    .section-header {
        background: linear-gradient(90deg, #238636 0%, #2ea043 100%);
        color: white; padding: 10px 20px; border-radius: 8px; 
        margin: 30px 0 15px 0; font-size: 1.2rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ PMCC Master Accountant (V18.2)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Configuración")
    TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("Filtro Activo: LEAPS > 180 DTE & Delta > 0.60")

# --- FUNCIONES ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def decode_occ(symbol):
    """Extrae Símbolo, Tipo y Strike del símbolo OCC"""
    if not symbol or len(symbol) < 15: return symbol, "STOCK", 0
    try:
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
        if match:
            u_sym = match.group(1)
            o_type = "CALL" if match.group(3) == "C" else "PUT"
            strike = float(match.group(4)) / 1000
            return u_sym, o_type, strike
    except: pass
    return symbol, "UNKNOWN", 0

def run_pmcc_audit():
    # 1. Obtener Perfil y Cuenta
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    
    prof_data = r_p.json()['profile']['account']
    acct_id = prof_data[0]['account_number'] if isinstance(prof_data, list) else prof_data['account_number']

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. Ganancias Realizadas
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    # 4. Market Data Actual
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    report = {}

    # A. Identificar LEAPS REALES (>180 DTE)
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Calcular DTE
        dte = 0
        if q_data.get('expiration_date'):
            try:
                exp_dt = datetime.strptime(q_data['expiration_date'], '%Y-%m-%d')
                dte = (exp_dt - datetime.now()).days
            except: dte = 0

        # FILTRO PMCC: Long, Delta > 0.60 y DTE > 180
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.60 and dte > 180:
            if u_sym not in report:
                report[u_sym] = {
                    "leaps": [], "leaps_strikes": [], "realized_cc": 0.0, 
                    "history": [], "active_short": None, 
                    "spot": q_map.get(u_sym, {}).get('last', 0), "start_date": None
                }
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * q_data.get('last', 0) * 100
            
            try:
                acq_date = datetime.strptime(p.get('date_acquired', '2025-01-01')[:10], '%Y-%m-%d')
            except:
                acq_date = datetime.now()
            
            if report[u_sym]["start_date"] is None or acq_date < report[u_sym]["start_date"]:
                report[u_sym]["start_date"] = acq_date
            
            report[u_sym]['leaps_strikes'].append(q_data.get('strike', 0))
            report[u_sym]['leaps'].append({
                "Adquirido": acq_date.strftime('%Y-%m-%d'), "Exp": q_data.get('expiration_date'),
                "Strike": q_data.get('strike'), "Qty": int(float(p['quantity'])),
                "Cost": cost, "Value": val, "P/L": val - cost
            })

    # B. Auditoría de trades cerrados (Sincronizado con nombres de UI)
    for gl in gl_data:
        sym = gl.get('symbol', '')
        u_sym, o_type, strike = decode_occ(sym)
        
        if u_sym in report and o_type == "CALL":
            try:
                close_dt = datetime.strptime(gl.get('close_date', '2000-01-01')[:10], '%Y-%m-%d')
                open_dt = datetime.strptime(gl.get('open_date', '2000-01-01')[:10], '%Y-%m-%d')
            except:
                close_dt = open_dt = datetime.now()

            if close_dt >= report[u_sym]["start_date"]:
                is_leap = any(abs(strike - ls) < 0.5 for ls in report[u_sym]['leaps_strikes'])
                if not is_leap:
                    gain = float(gl.get('gain_loss', 0))
                    report[u_sym]['realized_cc'] += gain
                    # Guardamos en la llave 'history' para que la UI lo encuentre
                    report[u_sym]['history'].append({
                        "Cerrado": close_dt.strftime('%Y-%m-%d'),
                        "Tipo": "CALL",
                        "Qty": abs(int(float(gl.get('quantity', 0)))),
                        "Strike": strike,
                        "P/L": gain,
                        "DIT": (close_dt - open_dt).days
                    })

    # C. Corto Activo (Monitor de Jugo)
    for p in positions:
        u_sym = get_underlying(p['symbol'])
        if u_sym in report and float(p['quantity']) < 0:
            q = q_map.get(p['symbol'], {})
            u_p = report[u_sym]['spot']
            strike = q.get('strike', 0)
            opt_p = q.get('last', 0)
            juice = max(0, opt_p - max(0, u_p - strike))
            
            try:
                exp_dt = datetime.strptime(q['expiration_date'], '%Y-%m-%d')
                dte_val = (exp_dt - datetime.now()).days
            except: dte_val = 0

            report[u_sym]['active_short'] = {
                "Strike": strike, "Price": opt_p, "Ext": juice, "DTE": dte_val
            }

    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 GENERAR REPORTE PMCC"):
        data = run_pmcc_audit()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                # KPIs
                tc = sum([l['Cost'] for l in d['leaps']])
                tv = sum([l['Value'] for l in d['leaps']])
                re = d['realized_cc']
                ni = (tv - tc) + re
                ro = (ni / tc * 100) if tc > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${tc:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${tv:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${re:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${ni:,.2f}</p></div>', unsafe_allow_html=True)
                
                r_style = "roi-positive" if ro >= 0 else "roi-negative"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="{r_style}">{ro:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 TIEMPO DE ROLEAR")

                # CORRECCIÓN DE KEYERROR: Usamos el nombre correcto 'history'
                if d.get('history') and len(d['history']) > 0:
                    with st.expander(f"📔 Ver Trades Cerrados de la Campaña"):
                        df_cl = pd.DataFrame(d['history']).sort_values("Cerrado", ascending=False)
                        st.dataframe(df_cl.style.format({"P/L": "${:,.2f}", "Strike": "{:.2f}"}), use_container_width=True)
                
                st.divider()
        else:
            st.warning("No hay campañas PMCC activas (LEAPS > 180 días).")
else:
    st.info("👈 Introduce tu Token.")
