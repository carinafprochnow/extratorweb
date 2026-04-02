import streamlit as st
import requests
import pandas as pd
import time
from io import BytesIO

# --- CONFIGURAÇÕES FIXAS (Secrets) ---
URL_API_PROJURIS = "https://api.projurisadv.com.br/adv-service/consulta/central-captura-processo"

try:
    TOKEN_FORNECEDOR = st.secrets["TOKEN_FORNECEDOR"]
except:
    st.error("Erro: TOKEN_FORNECEDOR não configurado nos Secrets.")
    st.stop()

# --- MAPAS DE DADOS ORIGINAIS ---
MAPA_FILTROS = {
    'ERRO': 'ERRO',
    'EM_ANDAMENTO': 'FILTRO_EM_ANDAMENTO',
    'PENDENTE': 'FILTRO_PENDENTES',
    'VINCULADOS': 'VINCULADOS',
    'OUTROS (Segredo/Credencial)': 'ERRO'
}

MAPA_CNJ = {
    "TRF1": ".4.01.", "TRF2": ".4.02.", "TRF3": ".4.03.", "TRF4": ".4.04.", "TRF5": ".4.05.", "TRF6": ".4.06.",
    "TRT1": ".5.01.", "TRT2": ".5.02.", "TRT3": ".5.03.", "TRT4": ".5.04.", "TRT5": ".5.05.", "TRT6": ".5.06.",
    "TRT7": ".5.07.", "TRT8": ".5.08.", "TRT9": ".5.09.", "TRT10": ".5.10.", "TRT11": ".5.11.", "TRT12": ".5.12.",
    "TRT13": ".5.13.", "TRT14": ".5.14.", "TRT15": ".5.15.", "TRT16": ".5.16.", "TRT17": ".5.17.", "TRT18": ".5.18.",
    "TRT19": ".5.19.", "TRT20": ".5.20.", "TRT21": ".5.21.", "TRT22": ".5.22.", "TRT23": ".5.23.", "TRT24": ".5.24.",
    "TJAC": ".8.01.", "TJAL": ".8.02.", "TJAM": ".8.04.", "TJAP": ".8.03.", "TJBA": ".8.05.", "TJCE": ".8.06.",
    "TJDFT": ".8.07.", "TJES": ".8.08.", "TJGO": ".8.09.", "TJMA": ".8.10.", "TJMG": ".8.13.", "TJMS": ".8.12.",
    "TJMT": ".8.11.", "TJPA": ".8.14.", "TJPB": ".8.15.", "TJPE": ".8.17.", "TJPI": ".8.18.", "TJPR": ".8.16.",
    "TJRJ": ".8.19.", "TJRN": ".8.20.", "TJRO": ".8.22.", "TJRR": ".8.23.", "TJRS": ".8.21.", "TJSC": ".8.24.",
    "TJSE": ".8.25.", "TJSP": ".8.26.", "TJTO": ".8.27."
}

DIC_TRIBUNAIS = {
    'TODOS': ['TODOS'],
    'JUSTIÇA FEDERAL': ['TODOS'] + sorted([k for k in MAPA_CNJ if k.startswith("TRF")]),
    'JUSTIÇA DO TRABALHO': ['TODOS'] + sorted([k for k in MAPA_CNJ if k.startswith("TRT")]),
    'JUSTIÇA ESTADUAL': ['TODOS'] + sorted([k for k in MAPA_CNJ if k.startswith("TJ")])
}

# --- INTERFACE ---
st.set_page_config(page_title="Extrator Projuris Web", layout="wide")
st.title("📂 Captura de Processos - Projuris ADV")

with st.sidebar:
    st.header("Configurações")
    token_user_raw = st.text_input("Seu Token (com ou sem Bearer)", type="password")
    cd_arrendatario = st.text_input("Arrendatário", value="60470")
    status_usuario = st.selectbox("Status", list(MAPA_FILTROS.keys()), index=3)
    
    st.divider()
    st.header("Filtros")
    ambito = st.selectbox("Âmbito", list(DIC_TRIBUNAIS.keys()))
    tribunal_sigla = st.selectbox("Tribunal", DIC_TRIBUNAIS[ambito])

if st.button("🚀 Iniciar Extração"):
    if not token_user_raw:
        st.error("Insira o Token.")
    else:
        with st.status("Extraindo processos...", expanded=True) as status_box:
            try:
                # --- LÓGICA DE TOKEN FLEXÍVEL ---
                token_limpo = token_user_raw.strip()
                if not token_limpo.lower().startswith("bearer "):
                    token_final = f"Bearer {token_limpo}"
                else:
                    token_final = token_limpo

                headers = {
                    "Authorization": token_final,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0"
                }

                filtro_api = MAPA_FILTROS.get(status_usuario)
                filtros_api_lista = [filtro_api] if status_usuario != 'VINCULADOS' else ['VINCULADOS', 'PROCESSO_VINCULADO']
                
                dados_brutos = []
                
                for f in filtros_api_lista:
                    if dados_brutos: break
                    st.write(f"🛰️ Consultando {f}...")
                    pagina = 0
                    while True:
                        payload = {"habilitado": True, "tipoFiltroConsulta": f}
                        res = requests.post(URL_API_PROJURIS, headers=headers, params={"quan-registros": 100, "pagina": pagina}, json=payload, timeout=30)
                        
                        if res.status_code != 200:
                            if res.status_code == 412:
                                st.error(f"Erro 412: Verifique o Arrendatário ou o Token.")
                            break
                        
                        data = res.json()
                        itens = data.get('centralCapturaProcessoConsultaResultadoWs', [])
                        if not itens: break
                        
                        dados_brutos.extend(itens)
                        st.write(f"📥 Coletados: {len(dados_brutos)}...")
                        
                        if len(dados_brutos) >= data.get('totalRegistros', 0): break
                        pagina += 1
                        time.sleep(0.05)

                # --- FILTRAGEM ---
                cod_ambito = {"JUSTIÇA FEDERAL": ".4.", "JUSTIÇA DO TRABALHO": ".5.", "JUSTIÇA ESTADUAL": ".8."}.get(ambito, "")
                processos_filtrados = []

                for item in dados_brutos:
                    caps = item.get('processoCapturados', [])
                    val_num = caps[0].get('numeroProcesso') if caps else item.get('paramentroCaptura')
                    num_proc = str(val_num).strip() if val_num else "N/A"
                    
                    match = False
                    if ambito == 'TODOS': match = True
                    elif tribunal_sigla == 'TODOS':
                        if cod_ambito in num_proc: match = True
                    else:
                        codigo_especifico = MAPA_CNJ.get(tribunal_sigla)
                        if codigo_especifico and codigo_especifico in num_proc: match = True

                    if match:
                        processos_filtrados.append({
                            "Processo": num_proc, "Tribunal": item.get('tribunal'), 
                            "id_central": item.get('codigoCentralCapturaProcesso')
                        })

                if not processos_filtrados:
                    st.warning("Nenhum processo encontrado.")
                else:
                    st.write(f"🔍 {len(processos_filtrados)} filtrados. Buscando Demandas...")
                    finais = []
                    progress_bar = st.progress(0)
                    for idx, p in enumerate(processos_filtrados):
                        url_f = f"https://broly.sajadv.com.br/api/acompanhamento?token={TOKEN_FORNECEDOR}&cdArrendatario={cd_arrendatario}&cdCentralCapturaProcesso={p['id_central']}"
                        id_demanda = "N/A"
                        try:
                            rf = requests.get(url_f, timeout=10)
                            if rf.status_code == 200: id_demanda = rf.json().get('idDemanda') or "N/A"
                        except: pass
                        finais.append({"Processo": p['Processo'], "Tribunal": p['Tribunal'], "ID Demanda": id_demanda, "Link": url_f})
                        progress_bar.progress((idx + 1) / len(processos_filtrados))
                    
                    df_final = pd.DataFrame(finais)
                    
                    output = BytesIO()
                    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                        df_final.to_excel(writer, index=False)
                    
                    nome_arquivo = f"{status_usuario} - {ambito} - {tribunal_sigla} - {cd_arrendatario}.xlsx".replace("/", "_")
                    
                    status_box.update(label="✅ Extração concluída!", state="complete")
                    st.download_button(
                        label="📥 Baixar Planilha Excel",
                        data=output.getvalue(),
                        file_name=nome_arquivo,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )

            except Exception as e:
                st.error(f"Erro: {e}")
