import streamlit as st
import requests
import pandas as pd
from io import BytesIO

# --- CONFIGURAÇÕES FIXAS (Buscando dos Secrets do Streamlit) ---
URL_API_PROJURIS = "https://api.projurisadv.com.br/adv-service/consulta/central-captura-processo"

# Aqui ele tenta pegar o token escondido. Se não achar, avisa.
try:
    TOKEN_FORNECEDOR = st.secrets["TOKEN_FORNECEDOR"]
except:
    st.error("Erro: Token de Fornecedor não configurado nos Secrets.")
    st.stop()

MAPA_FILTROS = {
    'EM_ANDAMENTO': 'FILTRO_EM_ANDAMENTO',
    'PENDENTE': 'FILTRO_PENDENTES',
    'VINCULADOS': 'VINCULADOS',
    'ERRO': 'ERRO'
}

# Interface
st.set_page_config(page_title="Extrator Projuris", layout="centered")
st.title("📂 Extrator Projuris ADV")

with st.sidebar:
    st.header("Login")
    token_user = st.text_input("Seu Token (Bearer)", type="password")
    cd_arrendatario = st.text_input("Arrendatário", value="60470")
    status_usuario = st.selectbox("Status", list(MAPA_FILTROS.keys()))

if st.button("🚀 Iniciar Extração"):
    if not token_user:
        st.warning("Insira o seu Token para continuar.")
    else:
        with st.status("Processando...", expanded=True) as status:
            headers = {"Authorization": f"Bearer {token_user.strip()}", "Content-Type": "application/json"}
            filtro_api = MAPA_FILTROS.get(status_usuario)
            
            # Chamada simplificada da API (ajuste conforme sua lógica de paginação se precisar)
            res = requests.post(URL_API_PROJURIS, headers=headers, json={"habilitado": True, "tipoFiltroConsulta": filtro_api})
            
            if res.status_code == 200:
                itens = res.json().get('centralCapturaProcessoConsultaResultadoWs', [])
                df = pd.DataFrame([{"Processo": i.get('paramentroCaptura'), "Tribunal": i.get('tribunal')} for i in itens])
                
                # Gerar Excel na memória (Resolve o erro de permissão!)
                output = BytesIO()
                with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                    df.to_excel(writer, index=False)
                
                status.update(label="Concluído!", state="complete")
                st.download_button("📥 Baixar Planilha", output.getvalue(), f"extracao_{cd_arrendatario}.xlsx")
            else:
                st.error(f"Erro na API: {res.status_code}")
