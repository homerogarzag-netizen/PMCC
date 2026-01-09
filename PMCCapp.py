import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant Pro", page_icon="🛡️")

# --- DISEÑO UI PREMIUM (ESTILO TOM KING) ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card-pmcc {
        background-color: #161b22; border: 1px solid #30363d;
        padding: 15px; border-radius: 8px; text-align: center; height: 125px;
        box-shadow: 2px 2px 8px rgba(0,0,0,0.2);
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-val {font-size: 1.6rem; font-weight: bold;}
    .section-header {
        background: linear-gradient(90deg, #238636 0%, #2ea043 100%);
        color: white; padding: 10px 20px; 
        border-radius: 8px; margin: 30px 0 15px 0; font-size: 1.2rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ PMCC CEO Command Center")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("v19.4.0 | Detailed Audit Restored")

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

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

# --- MOTOR DE ANÁLISIS ---
def run_pmcc_audit():
    # 1. Datos Cuenta
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    prof = r_p.json()['profile']['account']
    acct_id = prof['account_number'] if isinstance(prof, dict) else prof[0]['account_number']

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    # 3. Ganancias Realizadas (Official Gain/Loss)
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    # 4. Market Data
    all_syms = list(set([p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    report = {}
    
    # A. Identificar Leaps (>150 DTE para seguridad)
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        q_data = m_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        dte_val = 0
        if q_data.get('expiration_date'):
            try:
                exp_dt = datetime.strptime(q_data['expiration_date'], '%Y-%m-%d')
                dte_val = (exp_dt - datetime.now()).days
            except: dte_val = 0

        # Identificamos el CORE (LEAPS)
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.55 and dte_val > 150:
            if u_sym not in report:
                report[u_sym] = {
                    "leaps": [], "leaps_strikes": [], "realized_cc": 0.0, 
                    "closed_list": [], "active_short": None, 
                    "spot": m_map.get(u_sym, {}).get('last', 0),
                    "campaign_start": None
                }
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * float(q_data.get('last', 0)) * 100
            acq_dt = datetime.strptime(p.get('date_acquired', '2025-01-01')[:10], '%Y-%m-%d')
            
            # La campaña empieza con el primer LEAPS
            if report[u_sym]["campaign_start"] is None or acq_dt < report[u_sym]["campaign_start"]:
                report[u_sym]["campaign_start"] = acq_dt
            
            report[u_sym]['leaps_strikes'].append(q_data.get('strike', 0))
            report[u_sym]['leaps'].append({
                "Adquirido": acq_dt.strftime('%Y-%m-%d'),
                "Exp": q_data.get('expiration_date'), "Strike": q_data.get('strike'),
                "Qty": int(float(p['quantity'])), "Cost": cost, "Value": val, "P_L": val - cost
            })

    # B. Auditoría de Trades Cerrados (Filtro por fecha de campaña y tipo CALL)
    for gl in gl_data:
        sym = gl.get('symbol', '')
        u_sym, o_type, strike = decode_occ_symbol(sym)
        if u_sym in report and o_type == "CALL":
            try:
                close_dt = datetime.strptime(gl.get('close_date','2000-01-01')[:10], '%Y-%m-%d')
                if close_dt >= report[u_sym]["campaign_start"]:
                    # Verificar si es un cierre de LEAPS o una venta de renta
                    is_leap_strike = any(abs(strike - ls) < 0.5 for ls in report[u_sym]['leaps_strikes'])
                    if not is_leap_strike:
                        gain = float(gl.get('gain_loss', 0))
                        report[u_sym]['realized_cc'] += gain
                        report[u_sym]['closed_list'].append({
                            "Cerrado": close_dt.strftime('%m/%d/%y'),
                            "Contrato": sym[-8:],
                            "Strike": strike,
                            "P/L": gain,
                            "DIT": gl.get('term', '-')
                        })
            except: pass

    # C. Identificar Corto Activo con cálculos de "Jugo"
    for p in raw_pos:
        sym = p['symbol']
        u_sym = get_underlying_symbol(sym)
        if u_sym in report and float(p['quantity']) < 0:
            q = m_map.get(sym, {})
            u_p = report[u_sym]['spot']
            strike = q.get('strike', 0)
            last_p = q.get('last', 0)
            qty = abs(int(float(p['quantity'])))
            
            init_p = abs(float(p.get('cost_basis', 0))) / (qty * 100)
            intr = max(0, u_p - strike)
            extr = last_p - intr
            
            report[u_sym]['active_short'] = {
                "Qty": qty, "Strike": strike, "Price_Init": init_p, "Price_Last": last_p,
                "Intrinsic": intr, "Extrinsic": extr,
                "PL_Juice_USD": (init_p - extr) * 100 * qty, 
                "PL_Total_USD": (init_p - last_p) * 100 * qty,
                "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
            }

    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE CONTABLE"):
        data = run_pmcc_audit()
        if data:
            # --- TABLA RESUMEN VENTAS ACTIVAS ---
            st.markdown('<div class="section-header">📑 RESUMEN DE VENTAS ACTIVAS (COVERED CALLS)</div>', unsafe_allow_html=True)
            summary_list = []
            for ticker, d in data.items():
                if d['active_short']:
                    a = d['active_short']
                    summary_list.append({
                        "Activo": ticker, "Cant": a['Qty'], "Strike": float(a['Strike']),
                        "Pr. Inicial": a['Price_Init'], "Pr. Actual": a['Price_Last'],
                        "Intrínseco": a['Intrinsic'], "Extrínseco": a['Extrinsic'],
                        "P/L Jugo ($)": a['PL_Juice_USD'], "P/L Total ($)": a['PL_Total_USD'],
                        "DTE": a['DTE']
                    })
            
            if summary_list:
                st.dataframe(pd.DataFrame(summary_list).style.format({
                    "Strike": "{:g}", "Pr. Inicial": "${:.2f}", "Pr. Actual": "${:.2f}",
                    "Intrínseco": "${:.2f}", "Extrínseco": "${:.2f}",
                    "P/L Jugo ($)": "${:,.2f}", "P/L Total ($)": "${:,.2f}"
                }), use_container_width=True)

            # --- DETALLE POR ACTIVO ---
            for ticker, d in data.items():
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
                c4.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${ni:,.2f}</p></div>', unsafe_allow_html=True)
                r_color = "#4ade80" if ro >= 0 else "#f87171"
                c5.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">ROI TOTAL</p><p class="roi-val" style="color:{r_color}">{ro:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Strike": "{:g}", "Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    ash = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {ash['Strike']:g} | DTE: {ash['DTE']} | **Extrínseco: ${ash['Extrinsic']:.2f}**")
                    if ash['Extrinsic'] < 0.20: st.error("🚨 TIEMPO DE ROLEAR")

                if d['closed_list']:
                    with st.expander(f"📔 Ver Historial Cerrado de la Campaña ({ticker})"):
                        st.dataframe(pd.DataFrame(d['closed_list']).style.format({"P/L": "${:,.2f}", "Strike": "{:g}"}), use_container_width=True)
                st.divider()
        else:
            st.error("No se detectaron campañas PMCC activas.")
else:
    st.info("👈 Ingresa tu Token de Tradier.")
