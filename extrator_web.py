import streamlit as st
import requests
import pandas as pd
import time

from io import BytesIO
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURAÇÕES
# ============================================================

URL_API_PROJURIS = (
    "https://api.projurisadv.com.br/adv-service/consulta/central-captura-processo"
)

URL_API_ACOMPANHAMENTO = (
    "https://broly.sajadv.com.br/api/acompanhamento"
)

QUANTIDADE_POR_PAGINA = 100

TIMEOUT_CONEXAO = 15
TIMEOUT_LEITURA_PROJURIS = 120
TIMEOUT_LEITURA_ACOMPANHAMENTO = 60
MAX_TENTATIVAS_PAGINA = 5
MAX_TENTATIVAS_DEMANDA = 4
PAUSA_ENTRE_PAGINAS = 0.5
PAUSA_ENTRE_DEMANDAS = 0.1


# ============================================================
# TOKEN DO FORNECEDOR
# ============================================================

try:
    TOKEN_FORNECEDOR = st.secrets["TOKEN_FORNECEDOR"]
except KeyError:
    st.error(
        "Erro: TOKEN_FORNECEDOR não configurado nos Secrets."
    )
    st.stop()


# ============================================================
# MAPAS DE DADOS
# ============================================================

MAPA_FILTROS = {
    "ERRO": "ERRO",
    "EM_ANDAMENTO": "FILTRO_EM_ANDAMENTO",
    "PENDENTE": "FILTRO_PENDENTES",
    "VINCULADOS": "VINCULADOS",
    "OUTROS (Segredo/Credencial)": "ERRO",
}

MAPA_CNJ = {
    "TRF1": ".4.01.",
    "TRF2": ".4.02.",
    "TRF3": ".4.03.",
    "TRF4": ".4.04.",
    "TRF5": ".4.05.",
    "TRF6": ".4.06.",

    "TRT1": ".5.01.",
    "TRT2": ".5.02.",
    "TRT3": ".5.03.",
    "TRT4": ".5.04.",
    "TRT5": ".5.05.",
    "TRT6": ".5.06.",
    "TRT7": ".5.07.",
    "TRT8": ".5.08.",
    "TRT9": ".5.09.",
    "TRT10": ".5.10.",
    "TRT11": ".5.11.",
    "TRT12": ".5.12.",
    "TRT13": ".5.13.",
    "TRT14": ".5.14.",
    "TRT15": ".5.15.",
    "TRT16": ".5.16.",
    "TRT17": ".5.17.",
    "TRT18": ".5.18.",
    "TRT19": ".5.19.",
    "TRT20": ".5.20.",
    "TRT21": ".5.21.",
    "TRT22": ".5.22.",
    "TRT23": ".5.23.",
    "TRT24": ".5.24.",

    "TJAC": ".8.01.",
    "TJAL": ".8.02.",
    "TJAM": ".8.04.",
    "TJAP": ".8.03.",
    "TJBA": ".8.05.",
    "TJCE": ".8.06.",
    "TJDFT": ".8.07.",
    "TJES": ".8.08.",
    "TJGO": ".8.09.",
    "TJMA": ".8.10.",
    "TJMG": ".8.13.",
    "TJMS": ".8.12.",
    "TJMT": ".8.11.",
    "TJPA": ".8.14.",
    "TJPB": ".8.15.",
    "TJPE": ".8.17.",
    "TJPI": ".8.18.",
    "TJPR": ".8.16.",
    "TJRJ": ".8.19.",
    "TJRN": ".8.20.",
    "TJRO": ".8.22.",
    "TJRR": ".8.23.",
    "TJRS": ".8.21.",
    "TJSC": ".8.24.",
    "TJSE": ".8.25.",
    "TJSP": ".8.26.",
    "TJTO": ".8.27.",
}

DIC_TRIBUNAIS = {
    "TODOS": ["TODOS"],
    "JUSTIÇA FEDERAL": (
        ["TODOS"]
        + sorted(
            tribunal
            for tribunal in MAPA_CNJ
            if tribunal.startswith("TRF")
        )
    ),
    "JUSTIÇA DO TRABALHO": (
        ["TODOS"]
        + sorted(
            tribunal
            for tribunal in MAPA_CNJ
            if tribunal.startswith("TRT")
        )
    ),
    "JUSTIÇA ESTADUAL": (
        ["TODOS"]
        + sorted(
            tribunal
            for tribunal in MAPA_CNJ
            if tribunal.startswith("TJ")
        )
    ),
}


# ============================================================
# FUNÇÕES
# ============================================================

def criar_sessao_http():
    """
    Cria uma sessão HTTP reutilizável.

    A sessão reaproveita conexões e aplica retentativas para
    determinados erros HTTP temporários.
    """

    estrategia_retry = Retry(
        total=3,
        connect=3,
        read=0,
        status=3,
        backoff_factor=1,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=[
            "GET",
            "POST",
        ],
        raise_on_status=False,
    )

    adaptador = HTTPAdapter(
        max_retries=estrategia_retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    sessao = requests.Session()
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)

    return sessao


def consultar_pagina_projuris(
    sessao,
    headers,
    filtro,
    pagina,
):
    """
    Consulta uma página da Central de Captura.

    Realiza até MAX_TENTATIVAS_PAGINA tentativas em caso de
    timeout ou erro de conexão.
    """

    payload = {
        "habilitado": True,
        "tipoFiltroConsulta": filtro,
    }

    parametros = {
        "quan-registros": QUANTIDADE_POR_PAGINA,
        "pagina": pagina,
    }

    ultimo_erro = None

    for tentativa in range(
        1,
        MAX_TENTATIVAS_PAGINA + 1,
    ):
        try:
            resposta = sessao.post(
                URL_API_PROJURIS,
                headers=headers,
                params=parametros,
                json=payload,
                timeout=(
                    TIMEOUT_CONEXAO,
                    TIMEOUT_LEITURA_PROJURIS,
                ),
            )

            return resposta

        except requests.exceptions.ReadTimeout:
            ultimo_erro = (
                "A API demorou mais de "
                f"{TIMEOUT_LEITURA_PROJURIS} segundos "
                "para responder."
            )

        except requests.exceptions.ConnectTimeout:
            ultimo_erro = (
                "O tempo limite de conexão com a API "
                "foi atingido."
            )

        except requests.exceptions.ConnectionError as erro:
            ultimo_erro = (
                f"Erro de conexão: {erro}"
            )

        except requests.exceptions.RequestException as erro:
            ultimo_erro = (
                f"Erro na requisição: {erro}"
            )

        if tentativa < MAX_TENTATIVAS_PAGINA:
            tempo_espera = tentativa * 5

            st.warning(
                f"⏳ Página {pagina}: tentativa "
                f"{tentativa}/{MAX_TENTATIVAS_PAGINA} "
                f"falhou. Nova tentativa em "
                f"{tempo_espera} segundos."
            )

            time.sleep(tempo_espera)

    raise RuntimeError(
        f"Não foi possível consultar a página {pagina} "
        f"após {MAX_TENTATIVAS_PAGINA} tentativas. "
        f"Último erro: {ultimo_erro}"
    )


def extrair_numero_processo(item):
    """
    Obtém o número do processo a partir do item retornado.
    """

    valor_numero = item.get("paramentroCaptura")

    if not valor_numero:
        processos_capturados = item.get(
            "processoCapturados",
            [],
        )

        if processos_capturados:
            valor_numero = processos_capturados[0].get(
                "numeroProcesso"
            )

    if not valor_numero:
        return "N/A"

    return str(valor_numero).strip()


def processo_corresponde_ao_filtro(
    numero_processo,
    ambito,
    tribunal_sigla,
):
    """
    Verifica se o processo corresponde ao âmbito e tribunal
    selecionados.
    """

    if ambito == "TODOS":
        return True

    codigos_ambito = {
        "JUSTIÇA FEDERAL": ".4.",
        "JUSTIÇA DO TRABALHO": ".5.",
        "JUSTIÇA ESTADUAL": ".8.",
    }

    codigo_ambito = codigos_ambito.get(
        ambito,
        "",
    )

    if tribunal_sigla == "TODOS":
        return codigo_ambito in numero_processo

    codigo_especifico = MAPA_CNJ.get(
        tribunal_sigla
    )

    if not codigo_especifico:
        return False

    return codigo_especifico in numero_processo


def buscar_id_demanda(
    sessao,
    cd_arrendatario,
    id_central,
):
    """
    Busca o ID da demanda na API de acompanhamento.

    Retorna também uma situação para facilitar a identificação
    de registros que falharam.
    """

    parametros = {
        "token": TOKEN_FORNECEDOR,
        "cdArrendatario": cd_arrendatario,
        "cdCentralCapturaProcesso": id_central,
    }

    ultimo_erro = None

    for tentativa in range(
        1,
        MAX_TENTATIVAS_DEMANDA + 1,
    ):
        try:
            resposta = sessao.get(
                URL_API_ACOMPANHAMENTO,
                params=parametros,
                timeout=(
                    TIMEOUT_CONEXAO,
                    TIMEOUT_LEITURA_ACOMPANHAMENTO,
                ),
            )

            if resposta.status_code == 200:
                try:
                    dados = resposta.json()
                except ValueError:
                    return (
                        "N/A",
                        "RESPOSTA JSON INVÁLIDA",
                        resposta.url,
                    )

                id_demanda = dados.get("idDemanda")

                if id_demanda:
                    return (
                        id_demanda,
                        "SUCESSO",
                        resposta.url,
                    )

                return (
                    "N/A",
                    "ID NÃO ENCONTRADO",
                    resposta.url,
                )

            if resposta.status_code in [
                429,
                500,
                502,
                503,
                504,
            ]:
                ultimo_erro = (
                    f"Erro HTTP {resposta.status_code}"
                )

                if tentativa < MAX_TENTATIVAS_DEMANDA:
                    tempo_espera = tentativa * 2
                    time.sleep(tempo_espera)
                    continue

            return (
                "N/A",
                f"ERRO HTTP {resposta.status_code}",
                resposta.url,
            )

        except requests.exceptions.ReadTimeout:
            ultimo_erro = "TIMEOUT DE LEITURA"

        except requests.exceptions.ConnectTimeout:
            ultimo_erro = "TIMEOUT DE CONEXÃO"

        except requests.exceptions.ConnectionError:
            ultimo_erro = "ERRO DE CONEXÃO"

        except requests.exceptions.RequestException as erro:
            ultimo_erro = (
                f"ERRO NA REQUISIÇÃO: {erro}"
            )

        if tentativa < MAX_TENTATIVAS_DEMANDA:
            tempo_espera = tentativa * 2
            time.sleep(tempo_espera)

    requisicao_preparada = requests.Request(
        "GET",
        URL_API_ACOMPANHAMENTO,
        params=parametros,
    ).prepare()

    return (
        "N/A",
        ultimo_erro or "ERRO DESCONHECIDO",
        requisicao_preparada.url,
    )


# ============================================================
# INTERFACE
# ============================================================

st.set_page_config(
    page_title="Extrator Projuris Web",
    layout="wide",
)

st.title(
    "📂 Extração de Capturas - Projuris ADV"
)

with st.sidebar:
    st.header("Configurações")

    token_user_raw = st.text_input(
        "Token",
        type="password",
    )

    cd_arrendatario = st.text_input(
        "Arrendatário",
        value="",
    )

    status_usuario = st.selectbox(
        "Status",
        list(MAPA_FILTROS.keys()),
        index=2,
    )

    st.divider()
    st.header("Filtros")

    ambito = st.selectbox(
        "Âmbito",
        list(DIC_TRIBUNAIS.keys()),
    )

    tribunal_sigla = st.selectbox(
        "Tribunal",
        DIC_TRIBUNAIS[ambito],
    )


# ============================================================
# EXECUÇÃO
# ============================================================

if st.button("🚀 Iniciar Extração"):
    if not token_user_raw:
        st.error("Insira o Token.")

    elif not cd_arrendatario:
        st.error("Insira o Arrendatário.")

    else:
        with st.status(
            "Extraindo processos...",
            expanded=True,
        ) as status_box:
            try:
                sessao = criar_sessao_http()

                token_limpo = token_user_raw.strip()

                if token_limpo.lower().startswith(
                    "bearer "
                ):
                    token_final = token_limpo
                else:
                    token_final = (
                        f"Bearer {token_limpo}"
                    )

                headers = {
                    "Authorization": token_final,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                }

                filtro_api = MAPA_FILTROS.get(
                    status_usuario
                )

                if status_usuario == "VINCULADOS":
                    filtros_api_lista = [
                        "VINCULADOS",
                        "PROCESSO_VINCULADO",
                    ]
                else:
                    filtros_api_lista = [
                        filtro_api
                    ]

                dados_brutos = []

                # ------------------------------------------------
                # COLETA PAGINADA
                # ------------------------------------------------

                for filtro_atual in filtros_api_lista:
                    st.write(
                        f"🛰️ Consultando "
                        f"{filtro_atual}..."
                    )

                    pagina = 0
                    total_coletado_filtro = 0
                    total_registros_filtro = None

                    while True:
                        resposta = consultar_pagina_projuris(
                            sessao=sessao,
                            headers=headers,
                            filtro=filtro_atual,
                            pagina=pagina,
                        )

                        if resposta.status_code != 200:
                            if resposta.status_code == 412:
                                st.error(
                                    "Erro 412: verifique o "
                                    "Arrendatário ou o Token."
                                )
                            else:
                                st.error(
                                    f"Erro HTTP "
                                    f"{resposta.status_code} "
                                    f"na página {pagina}."
                                )

                                st.code(
                                    resposta.text[:1000]
                                )

                            break

                        try:
                            data = resposta.json()
                        except ValueError:
                            st.error(
                                f"A página {pagina} retornou "
                                "uma resposta inválida."
                            )
                            break

                        itens = data.get(
                            "centralCapturaProcessoConsultaResultadoWs",
                            [],
                        )

                        if not itens:
                            st.write(
                                f"Página {pagina} sem "
                                "novos registros."
                            )
                            break

                        dados_brutos.extend(itens)

                        total_coletado_filtro += len(
                            itens
                        )

                        total_registros_filtro = data.get(
                            "totalRegistros",
                            total_registros_filtro,
                        )

                        if total_registros_filtro:
                            st.write(
                                f"📥 Filtro {filtro_atual}: "
                                f"{total_coletado_filtro}/"
                                f"{total_registros_filtro} "
                                "registros coletados."
                            )
                        else:
                            st.write(
                                f"📥 Filtro {filtro_atual}: "
                                f"{total_coletado_filtro} "
                                "registros coletados."
                            )

                        if (
                            total_registros_filtro is not None
                            and total_coletado_filtro
                            >= total_registros_filtro
                        ):
                            break

                        if (
                            len(itens)
                            < QUANTIDADE_POR_PAGINA
                        ):
                            break

                        pagina += 1

                        time.sleep(
                            PAUSA_ENTRE_PAGINAS
                        )

                if not dados_brutos:
                    status_box.update(
                        label=(
                            "⚠️ Nenhum registro retornado."
                        ),
                        state="complete",
                    )

                    st.warning(
                        "Nenhum registro foi retornado "
                        "pela API."
                    )

                    st.stop()

                # ------------------------------------------------
                # FILTRAGEM DOS PROCESSOS
                # ------------------------------------------------

                processos_filtrados = []

                for item in dados_brutos:
                    numero_processo = (
                        extrair_numero_processo(item)
                    )

                    corresponde = (
                        processo_corresponde_ao_filtro(
                            numero_processo=numero_processo,
                            ambito=ambito,
                            tribunal_sigla=tribunal_sigla,
                        )
                    )

                    if corresponde:
                        processos_filtrados.append(
                            {
                                "Processo": numero_processo,
                                "Tribunal": item.get(
                                    "tribunal"
                                ),
                                "id_central": item.get(
                                    "codigoCentralCapturaProcesso"
                                ),
                            }
                        )

                # Remove duplicidade antes da busca das demandas,
                # evitando chamadas desnecessárias.
                processos_unicos = {}

                for processo in processos_filtrados:
                    chave = processo["Processo"]

                    if chave not in processos_unicos:
                        processos_unicos[chave] = processo

                processos_filtrados = list(
                    processos_unicos.values()
                )

                if not processos_filtrados:
                    status_box.update(
                        label=(
                            "⚠️ Nenhum processo encontrado."
                        ),
                        state="complete",
                    )

                    st.warning(
                        "Nenhum processo encontrado "
                        "com os filtros selecionados."
                    )

                    st.stop()

                total_processos = len(
                    processos_filtrados
                )

                st.write(
                    f"🔍 {total_processos} processos "
                    "filtrados. Buscando IDs de demanda..."
                )

                # ------------------------------------------------
                # BUSCA DOS IDS DE DEMANDA
                # ------------------------------------------------

                resultados_finais = []

                progress_bar = st.progress(0)
                texto_progresso = st.empty()

                for indice, processo in enumerate(
                    processos_filtrados,
                    start=1,
                ):
                    texto_progresso.write(
                        f"Consultando processo "
                        f"{indice} de {total_processos}: "
                        f"{processo['Processo']}"
                    )

                    id_central = processo.get(
                        "id_central"
                    )

                    if not id_central:
                        id_demanda = "N/A"
                        situacao = (
                            "ID CENTRAL NÃO ENCONTRADO"
                        )
                        link_consulta = "N/A"

                    else:
                        (
                            id_demanda,
                            situacao,
                            link_consulta,
                        ) = buscar_id_demanda(
                            sessao=sessao,
                            cd_arrendatario=(
                                cd_arrendatario
                            ),
                            id_central=id_central,
                        )

                    resultados_finais.append(
                        {
                            "Processo": (
                                processo["Processo"]
                            ),
                            "Tribunal": (
                                processo["Tribunal"]
                            ),
                            "ID Central": id_central,
                            "ID Demanda": id_demanda,
                            "Situação": situacao,
                            "Link": link_consulta,
                        }
                    )

                    progress_bar.progress(
                        indice / total_processos
                    )

                    time.sleep(
                        PAUSA_ENTRE_DEMANDAS
                    )

                texto_progresso.empty()

                # ------------------------------------------------
                # GERAÇÃO DA PLANILHA
                # ------------------------------------------------

                df_final = pd.DataFrame(
                    resultados_finais
                )

                total_sucesso = (
                    df_final["Situação"]
                    .eq("SUCESSO")
                    .sum()
                )

                total_falhas = (
                    len(df_final) - total_sucesso
                )

                output = BytesIO()

                with pd.ExcelWriter(
                    output,
                    engine="xlsxwriter",
                ) as writer:
                    df_final.to_excel(
                        writer,
                        index=False,
                        sheet_name="Resultados",
                    )

                    workbook = writer.book
                    worksheet = writer.sheets[
                        "Resultados"
                    ]

                    formato_cabecalho = (
                        workbook.add_format(
                            {
                                "bold": True,
                                "border": 1,
                            }
                        )
                    )

                    for coluna_numero, nome_coluna in enumerate(
                        df_final.columns
                    ):
                        worksheet.write(
                            0,
                            coluna_numero,
                            nome_coluna,
                            formato_cabecalho,
                        )

                    worksheet.set_column(
                        "A:A",
                        28,
                    )
                    worksheet.set_column(
                        "B:B",
                        18,
                    )
                    worksheet.set_column(
                        "C:D",
                        18,
                    )
                    worksheet.set_column(
                        "E:E",
                        28,
                    )
                    worksheet.set_column(
                        "F:F",
                        100,
                    )

                    worksheet.autofilter(
                        0,
                        0,
                        len(df_final),
                        len(df_final.columns) - 1,
                    )

                    worksheet.freeze_panes(
                        1,
                        0,
                    )

                output.seek(0)

                nome_arquivo = (
                    f"{status_usuario} - "
                    f"{ambito} - "
                    f"{tribunal_sigla} - "
                    f"{cd_arrendatario}.xlsx"
                ).replace("/", "_")

                status_box.update(
                    label="✅ Extração concluída!",
                    state="complete",
                )

                st.success(
                    f"Extração concluída. "
                    f"{len(df_final)} processos consultados, "
                    f"{total_sucesso} IDs encontrados e "
                    f"{total_falhas} registros sem sucesso."
                )

                if total_falhas > 0:
                    st.warning(
                        "Alguns registros não retornaram um "
                        "ID de demanda. Consulte a coluna "
                        "'Situação' da planilha para identificar "
                        "timeouts, erros HTTP ou IDs não "
                        "encontrados."
                    )

                st.download_button(
                    label="📥 Baixar Planilha Excel",
                    data=output.getvalue(),
                    file_name=nome_arquivo,
                    mime=(
                        "application/vnd.openxmlformats-"
                        "officedocument.spreadsheetml.sheet"
                    ),
                )

            except Exception as erro:
                status_box.update(
                    label="❌ Erro durante a extração",
                    state="error",
                )

                st.error(
                    f"Erro: {erro}"
                )
