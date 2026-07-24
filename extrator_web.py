import hashlib
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL_API_PROJURIS = "https://api.projurisadv.com.br/adv-service/consulta/central-captura-processo"
URL_API_ACOMPANHAMENTO = "https://broly.sajadv.com.br/api/acompanhamento"

QUANTIDADE_POR_PAGINA = 100
TIMEOUT_CONEXAO = 15
TIMEOUT_LEITURA_PROJURIS = 120
TIMEOUT_LEITURA_ACOMPANHAMENTO = 60
MAX_TENTATIVAS_PAGINA = 5
MAX_TENTATIVAS_DEMANDA = 4
MAX_THREADS_PADRAO = 10
INTERVALO_CHECKPOINT = 50
PAUSA_ENTRE_PAGINAS = 0.5

try:
    TOKEN_FORNECEDOR = st.secrets["TOKEN_FORNECEDOR"]
except KeyError:
    st.error("Erro: TOKEN_FORNECEDOR não configurado nos Secrets.")
    st.stop()

MAPA_FILTROS = {
    "ERRO": "ERRO",
    "EM_ANDAMENTO": "FILTRO_EM_ANDAMENTO",
    "PENDENTE": "FILTRO_PENDENTES",
    "VINCULADOS": "VINCULADOS",
    "OUTROS (Segredo/Credencial)": "ERRO",
}

MAPA_CNJ = {
    "TRF1": ".4.01.", "TRF2": ".4.02.", "TRF3": ".4.03.", "TRF4": ".4.04.",
    "TRF5": ".4.05.", "TRF6": ".4.06.",
    "TRT1": ".5.01.", "TRT2": ".5.02.", "TRT3": ".5.03.", "TRT4": ".5.04.",
    "TRT5": ".5.05.", "TRT6": ".5.06.", "TRT7": ".5.07.", "TRT8": ".5.08.",
    "TRT9": ".5.09.", "TRT10": ".5.10.", "TRT11": ".5.11.", "TRT12": ".5.12.",
    "TRT13": ".5.13.", "TRT14": ".5.14.", "TRT15": ".5.15.", "TRT16": ".5.16.",
    "TRT17": ".5.17.", "TRT18": ".5.18.", "TRT19": ".5.19.", "TRT20": ".5.20.",
    "TRT21": ".5.21.", "TRT22": ".5.22.", "TRT23": ".5.23.", "TRT24": ".5.24.",
    "TJAC": ".8.01.", "TJAL": ".8.02.", "TJAM": ".8.04.", "TJAP": ".8.03.",
    "TJBA": ".8.05.", "TJCE": ".8.06.", "TJDFT": ".8.07.", "TJES": ".8.08.",
    "TJGO": ".8.09.", "TJMA": ".8.10.", "TJMG": ".8.13.", "TJMS": ".8.12.",
    "TJMT": ".8.11.", "TJPA": ".8.14.", "TJPB": ".8.15.", "TJPE": ".8.17.",
    "TJPI": ".8.18.", "TJPR": ".8.16.", "TJRJ": ".8.19.", "TJRN": ".8.20.",
    "TJRO": ".8.22.", "TJRR": ".8.23.", "TJRS": ".8.21.", "TJSC": ".8.24.",
    "TJSE": ".8.25.", "TJSP": ".8.26.", "TJTO": ".8.27.",
}

DIC_TRIBUNAIS = {
    "TODOS": ["TODOS"],
    "JUSTIÇA FEDERAL": ["TODOS"] + sorted(k for k in MAPA_CNJ if k.startswith("TRF")),
    "JUSTIÇA DO TRABALHO": ["TODOS"] + sorted(k for k in MAPA_CNJ if k.startswith("TRT")),
    "JUSTIÇA ESTADUAL": ["TODOS"] + sorted(k for k in MAPA_CNJ if k.startswith("TJ")),
}

_thread_local = threading.local()


def criar_sessao_http(max_threads=MAX_THREADS_PADRAO):
    retry = Retry(
        total=3,
        connect=3,
        read=0,
        status=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=max_threads + 5,
        pool_maxsize=max_threads + 5,
    )
    sessao = requests.Session()
    sessao.mount("https://", adapter)
    sessao.mount("http://", adapter)
    return sessao


def obter_sessao_thread():
    if not hasattr(_thread_local, "sessao"):
        _thread_local.sessao = criar_sessao_http()
    return _thread_local.sessao


def limpar_nome_arquivo(valor):
    valor = str(valor or "").strip()
    valor = re.sub(r'[\\/:*?"<>|]', "_", valor)
    valor = re.sub(r"\s+", " ", valor)
    return valor or "arquivo"


def limpar_identificador(valor):
    valor = str(valor or "").strip()
    valor = re.sub(r"[^a-zA-Z0-9_-]+", "_", valor)
    return valor[:80] or "sem_valor"


def gerar_caminho_checkpoint(token_usuario, cd_arrendatario, status_usuario, ambito, tribunal_sigla):
    identificador = "|".join([
        token_usuario,
        cd_arrendatario,
        status_usuario,
        ambito,
        tribunal_sigla,
    ])
    hash_execucao = hashlib.sha256(
        identificador.encode("utf-8")
    ).hexdigest()[:16]
    arrendatario_seguro = limpar_identificador(cd_arrendatario)
    return os.path.join(
        "/tmp",
        f"checkpoint_projuris_{arrendatario_seguro}_{hash_execucao}.csv",
    )


def salvar_checkpoint(resultados, caminho):
    if not resultados:
        return
    df = pd.DataFrame(resultados)
    df = df.drop_duplicates(subset=["Processo"], keep="last")
    caminho_temporario = f"{caminho}.tmp"
    df.to_csv(caminho_temporario, index=False, encoding="utf-8-sig")
    os.replace(caminho_temporario, caminho)


def carregar_checkpoint(caminho):
    if not os.path.exists(caminho):
        return []
    try:
        df = pd.read_csv(caminho, dtype=str, encoding="utf-8-sig")
        colunas = {
            "Processo",
            "Tribunal",
            "ID Central",
            "ID Demanda",
            "Situação",
            "Link",
        }
        if not colunas.issubset(set(df.columns)):
            return []
        df = df.fillna("N/A")
        df = df.drop_duplicates(subset=["Processo"], keep="last")
        return df.to_dict(orient="records")
    except Exception:
        return []


def consultar_pagina_projuris(sessao, headers, filtro, pagina):
    payload = {
        "habilitado": True,
        "tipoFiltroConsulta": filtro,
    }
    parametros = {
        "quan-registros": QUANTIDADE_POR_PAGINA,
        "pagina": pagina,
    }
    ultimo_erro = None

    for tentativa in range(1, MAX_TENTATIVAS_PAGINA + 1):
        try:
            return sessao.post(
                URL_API_PROJURIS,
                headers=headers,
                params=parametros,
                json=payload,
                timeout=(TIMEOUT_CONEXAO, TIMEOUT_LEITURA_PROJURIS),
            )
        except requests.exceptions.ReadTimeout:
            ultimo_erro = (
                f"A API demorou mais de {TIMEOUT_LEITURA_PROJURIS} "
                "segundos para responder."
            )
        except requests.exceptions.ConnectTimeout:
            ultimo_erro = "O tempo limite de conexão com a API foi atingido."
        except requests.exceptions.ConnectionError as erro:
            ultimo_erro = f"Erro de conexão: {erro}"
        except requests.exceptions.RequestException as erro:
            ultimo_erro = f"Erro na requisição: {erro}"

        if tentativa < MAX_TENTATIVAS_PAGINA:
            espera = tentativa * 5
            st.warning(
                f"⏳ Página {pagina}: tentativa {tentativa}/"
                f"{MAX_TENTATIVAS_PAGINA} falhou. "
                f"Nova tentativa em {espera} segundos."
            )
            time.sleep(espera)

    raise RuntimeError(
        f"Não foi possível consultar a página {pagina} após "
        f"{MAX_TENTATIVAS_PAGINA} tentativas. Último erro: {ultimo_erro}"
    )


def extrair_numero_processo(item):
    valor = item.get("paramentroCaptura")

    if not valor:
        processos_capturados = item.get("processoCapturados", [])
        if processos_capturados:
            valor = processos_capturados[0].get("numeroProcesso")

    return str(valor).strip() if valor else "N/A"


def processo_corresponde_ao_filtro(numero_processo, ambito, tribunal_sigla):
    if ambito == "TODOS":
        return True

    codigos_ambito = {
        "JUSTIÇA FEDERAL": ".4.",
        "JUSTIÇA DO TRABALHO": ".5.",
        "JUSTIÇA ESTADUAL": ".8.",
    }

    codigo_ambito = codigos_ambito.get(ambito, "")

    if tribunal_sigla == "TODOS":
        return codigo_ambito in numero_processo

    codigo_especifico = MAPA_CNJ.get(tribunal_sigla)
    return bool(codigo_especifico and codigo_especifico in numero_processo)


def identificar_tribunal(numero_processo, tribunal_api):
    """
    Identifica a sigla do tribunal pelo número CNJ.

    Isso garante que os arquivos sejam separados por TJSC, TJSP,
    TRT12, TRF4 etc., mesmo que o campo 'tribunal' da API venha
    com outro formato.
    """
    for sigla, codigo in MAPA_CNJ.items():
        if codigo in numero_processo:
            return sigla

    tribunal_api = str(tribunal_api or "").strip()
    return tribunal_api if tribunal_api else "TRIBUNAL_NAO_IDENTIFICADO"


def montar_link_completo(cd_arrendatario, id_central):
    return (
        f"{URL_API_ACOMPANHAMENTO}"
        f"?token={TOKEN_FORNECEDOR}"
        f"&cdArrendatario={cd_arrendatario}"
        f"&cdCentralCapturaProcesso={id_central}"
    )


def buscar_id_demanda(cd_arrendatario, id_central):
    sessao = obter_sessao_thread()

    parametros = {
        "token": TOKEN_FORNECEDOR,
        "cdArrendatario": cd_arrendatario,
        "cdCentralCapturaProcesso": id_central,
    }

    link_completo = montar_link_completo(
        cd_arrendatario,
        id_central,
    )

    ultimo_erro = None

    for tentativa in range(1, MAX_TENTATIVAS_DEMANDA + 1):
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
                    return "N/A", "RESPOSTA JSON INVÁLIDA", link_completo

                id_demanda = dados.get("idDemanda")

                if id_demanda:
                    return str(id_demanda), "SUCESSO", link_completo

                return "N/A", "ID NÃO ENCONTRADO", link_completo

            if resposta.status_code in [429, 500, 502, 503, 504]:
                ultimo_erro = f"ERRO HTTP {resposta.status_code}"

                if tentativa < MAX_TENTATIVAS_DEMANDA:
                    time.sleep(tentativa * 2)
                    continue

            return (
                "N/A",
                f"ERRO HTTP {resposta.status_code}",
                link_completo,
            )

        except requests.exceptions.ReadTimeout:
            ultimo_erro = "TIMEOUT DE LEITURA"
        except requests.exceptions.ConnectTimeout:
            ultimo_erro = "TIMEOUT DE CONEXÃO"
        except requests.exceptions.ConnectionError as erro:
            ultimo_erro = f"ERRO DE CONEXÃO: {erro}"
        except requests.exceptions.RequestException as erro:
            ultimo_erro = f"ERRO NA REQUISIÇÃO: {erro}"

        if tentativa < MAX_TENTATIVAS_DEMANDA:
            time.sleep(tentativa * 2)

    return (
        "N/A",
        ultimo_erro or "ERRO DESCONHECIDO",
        link_completo,
    )


def consultar_processo(processo, cd_arrendatario):
    id_central = processo.get("id_central")

    if not id_central:
        return {
            "Processo": processo["Processo"],
            "Tribunal": processo["Tribunal"],
            "ID Central": "N/A",
            "ID Demanda": "N/A",
            "Situação": "ID CENTRAL NÃO ENCONTRADO",
            "Link": "N/A",
        }

    try:
        id_demanda, situacao, link = buscar_id_demanda(
            cd_arrendatario,
            id_central,
        )

        return {
            "Processo": processo["Processo"],
            "Tribunal": processo["Tribunal"],
            "ID Central": str(id_central),
            "ID Demanda": id_demanda,
            "Situação": situacao,
            "Link": link,
        }

    except Exception as erro:
        return {
            "Processo": processo["Processo"],
            "Tribunal": processo["Tribunal"],
            "ID Central": str(id_central),
            "ID Demanda": "N/A",
            "Situação": f"ERRO INESPERADO: {erro}",
            "Link": montar_link_completo(
                cd_arrendatario,
                id_central,
            ),
        }


def gerar_excel_tribunal(df_tribunal):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df_tribunal.to_excel(
            writer,
            index=False,
            sheet_name="Resultados",
        )

        workbook = writer.book
        worksheet = writer.sheets["Resultados"]

        formato_cabecalho = workbook.add_format({
            "bold": True,
            "border": 1,
        })

        formato_link = workbook.add_format({
            "font_color": "blue",
            "underline": 1,
        })

        for numero_coluna, nome_coluna in enumerate(df_tribunal.columns):
            worksheet.write(
                0,
                numero_coluna,
                nome_coluna,
                formato_cabecalho,
            )

        worksheet.set_column("A:A", 28)
        worksheet.set_column("B:B", 18)
        worksheet.set_column("C:D", 18)
        worksheet.set_column("E:E", 35)
        worksheet.set_column("F:F", 110)

        if "Link" in df_tribunal.columns:
            coluna_link = df_tribunal.columns.get_loc("Link")

            for indice, link in enumerate(
                df_tribunal["Link"],
                start=1,
            ):
                if isinstance(link, str) and link.startswith("http"):
                    worksheet.write_url(
                        indice,
                        coluna_link,
                        link,
                        formato_link,
                        string=link,
                    )

        worksheet.autofilter(
            0,
            0,
            len(df_tribunal),
            len(df_tribunal.columns) - 1,
        )

        worksheet.freeze_panes(1, 0)

    output.seek(0)
    return output


def gerar_zip_por_tribunal(df_final, status_usuario, cd_arrendatario):
    zip_output = BytesIO()

    with zipfile.ZipFile(
        zip_output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as arquivo_zip:

        grupos = df_final.groupby(
            "Tribunal",
            dropna=False,
            sort=True,
        )

        for tribunal, df_tribunal in grupos:
            tribunal_nome = limpar_nome_arquivo(
                tribunal or "TRIBUNAL_NAO_IDENTIFICADO"
            )

            nome_excel = limpar_nome_arquivo(
                f"{status_usuario} - {tribunal_nome} - "
                f"{cd_arrendatario}.xlsx"
            )

            excel = gerar_excel_tribunal(
                df_tribunal.reset_index(drop=True)
            )

            arquivo_zip.writestr(
                nome_excel,
                excel.getvalue(),
            )

    zip_output.seek(0)
    return zip_output


st.set_page_config(
    page_title="Extrator Projuris Web",
    layout="wide",
)

st.title("📂 Extração de Capturas - Projuris ADV")

with st.sidebar:
    st.header("Configurações")

    with st.form("formulario_extracao"):
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

        st.divider()
        st.header("Desempenho")

        quantidade_threads = st.slider(
            "Consultas simultâneas no Broly",
            min_value=1,
            max_value=20,
            value=MAX_THREADS_PADRAO,
            help=(
                "Recomenda-se usar 10. Valores muito altos podem "
                "sobrecarregar a API ou provocar erro 429."
            ),
        )

        retomar_checkpoint = st.checkbox(
            "Retomar extração interrompida",
            value=True,
        )

        iniciar_extracao = st.form_submit_button(
            "🚀 Iniciar Extração",
            type="primary",
        )


if iniciar_extracao:
    if not token_user_raw:
        st.error("Insira o Token.")

    elif not cd_arrendatario:
        st.error("Insira o Arrendatário.")

    else:
        with st.status(
            "Extraindo processos...",
            expanded=True,
        ) as status_box:

            caminho_checkpoint = None
            resultados_finais = []

            try:
                sessao_principal = criar_sessao_http(
                    quantidade_threads
                )

                token_limpo = token_user_raw.strip()

                token_final = (
                    token_limpo
                    if token_limpo.lower().startswith("bearer ")
                    else f"Bearer {token_limpo}"
                )

                headers = {
                    "Authorization": token_final,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                }

                caminho_checkpoint = gerar_caminho_checkpoint(
                    token_limpo,
                    cd_arrendatario,
                    status_usuario,
                    ambito,
                    tribunal_sigla,
                )

                filtro_api = MAPA_FILTROS.get(
                    status_usuario
                )

                filtros_api_lista = (
                    ["VINCULADOS", "PROCESSO_VINCULADO"]
                    if status_usuario == "VINCULADOS"
                    else [filtro_api]
                )

                dados_brutos = []

                for filtro_atual in filtros_api_lista:
                    st.write(
                        f"🛰️ Consultando filtro {filtro_atual}..."
                    )

                    pagina = 0
                    total_coletado_filtro = 0
                    total_registros_filtro = None

                    while True:
                        resposta = consultar_pagina_projuris(
                            sessao_principal,
                            headers,
                            filtro_atual,
                            pagina,
                        )

                        if resposta.status_code != 200:
                            if resposta.status_code == 412:
                                raise RuntimeError(
                                    "Erro 412: verifique o "
                                    "Arrendatário ou o Token."
                                )

                            raise RuntimeError(
                                f"Erro HTTP {resposta.status_code} "
                                f"na página {pagina}. "
                                f"Resposta: {resposta.text[:500]}"
                            )

                        try:
                            data = resposta.json()
                        except ValueError as erro:
                            raise RuntimeError(
                                f"A página {pagina} retornou "
                                "uma resposta JSON inválida."
                            ) from erro

                        itens = data.get(
                            "centralCapturaProcessoConsultaResultadoWs",
                            [],
                        )

                        if not itens:
                            break

                        dados_brutos.extend(itens)
                        total_coletado_filtro += len(itens)

                        total_registros_filtro = data.get(
                            "totalRegistros",
                            total_registros_filtro,
                        )

                        if total_registros_filtro is not None:
                            st.write(
                                f"📥 {filtro_atual}: "
                                f"{total_coletado_filtro}/"
                                f"{total_registros_filtro} "
                                "registros coletados."
                            )
                        else:
                            st.write(
                                f"📥 {filtro_atual}: "
                                f"{total_coletado_filtro} "
                                "registros coletados."
                            )

                        if (
                            total_registros_filtro is not None
                            and total_coletado_filtro
                            >= total_registros_filtro
                        ):
                            break

                        if len(itens) < QUANTIDADE_POR_PAGINA:
                            break

                        pagina += 1
                        time.sleep(PAUSA_ENTRE_PAGINAS)

                if not dados_brutos:
                    status_box.update(
                        label="⚠️ Nenhum registro retornado.",
                        state="complete",
                    )
                    st.warning(
                        "Nenhum registro foi retornado pela API."
                    )
                    st.stop()

                processos_filtrados = []

                for item in dados_brutos:
                    numero_processo = extrair_numero_processo(
                        item
                    )

                    if processo_corresponde_ao_filtro(
                        numero_processo,
                        ambito,
                        tribunal_sigla,
                    ):
                        processos_filtrados.append({
                            "Processo": numero_processo,
                            "Tribunal": identificar_tribunal(
                                numero_processo,
                                item.get("tribunal"),
                            ),
                            "id_central": item.get(
                                "codigoCentralCapturaProcesso"
                            ),
                        })

                processos_unicos = {}

                for processo in processos_filtrados:
                    numero = processo["Processo"]

                    if numero not in processos_unicos:
                        processos_unicos[numero] = processo

                processos_filtrados = list(
                    processos_unicos.values()
                )

                if not processos_filtrados:
                    status_box.update(
                        label="⚠️ Nenhum processo encontrado.",
                        state="complete",
                    )
                    st.warning(
                        "Nenhum processo encontrado com os "
                        "filtros selecionados."
                    )
                    st.stop()

                total_processos = len(
                    processos_filtrados
                )

                st.write(
                    f"🔍 {total_processos} processos únicos "
                    "encontrados."
                )

                if retomar_checkpoint:
                    resultados_finais = carregar_checkpoint(
                        caminho_checkpoint
                    )

                processos_ja_concluidos = {
                    str(resultado["Processo"])
                    for resultado in resultados_finais
                }

                processos_pendentes = [
                    processo
                    for processo in processos_filtrados
                    if str(processo["Processo"])
                    not in processos_ja_concluidos
                ]

                quantidade_recuperada = len(
                    processos_ja_concluidos
                )

                if quantidade_recuperada > 0:
                    st.info(
                        f"♻️ {quantidade_recuperada} resultados "
                        "recuperados do checkpoint."
                    )

                total_pendentes = len(
                    processos_pendentes
                )

                st.write(
                    f"⚡ Consultando {total_pendentes} processos "
                    f"com até {quantidade_threads} consultas "
                    "simultâneas no Broly."
                )

                progress_bar = st.progress(
                    min(
                        quantidade_recuperada / total_processos,
                        1.0,
                    )
                )

                texto_progresso = st.empty()
                texto_estimativa = st.empty()

                inicio = time.monotonic()
                concluidos_nesta_execucao = 0

                if processos_pendentes:
                    with ThreadPoolExecutor(
                        max_workers=quantidade_threads
                    ) as executor:

                        futures = {
                            executor.submit(
                                consultar_processo,
                                processo,
                                cd_arrendatario,
                            ): processo
                            for processo in processos_pendentes
                        }

                        for future in as_completed(futures):
                            processo_original = futures[future]

                            try:
                                resultado = future.result()
                            except Exception as erro:
                                resultado = {
                                    "Processo": processo_original["Processo"],
                                    "Tribunal": processo_original["Tribunal"],
                                    "ID Central": (
                                        processo_original["id_central"]
                                        or "N/A"
                                    ),
                                    "ID Demanda": "N/A",
                                    "Situação": (
                                        "ERRO INESPERADO NA THREAD: "
                                        f"{erro}"
                                    ),
                                    "Link": "N/A",
                                }

                            resultados_finais.append(
                                resultado
                            )

                            concluidos_nesta_execucao += 1

                            total_concluido = (
                                quantidade_recuperada
                                + concluidos_nesta_execucao
                            )

                            progress_bar.progress(
                                min(
                                    total_concluido
                                    / total_processos,
                                    1.0,
                                )
                            )

                            tempo_decorrido = (
                                time.monotonic() - inicio
                            )

                            media = (
                                tempo_decorrido
                                / concluidos_nesta_execucao
                            )

                            restantes = (
                                total_pendentes
                                - concluidos_nesta_execucao
                            )

                            estimativa = media * restantes
                            minutos = int(estimativa // 60)
                            segundos = int(estimativa % 60)

                            texto_progresso.write(
                                f"Consultados {total_concluido}/"
                                f"{total_processos} processos."
                            )

                            texto_estimativa.caption(
                                f"Restantes: {restantes} | "
                                f"Estimativa aproximada: "
                                f"{minutos} min {segundos} s"
                            )

                            if (
                                concluidos_nesta_execucao
                                % INTERVALO_CHECKPOINT
                                == 0
                            ):
                                salvar_checkpoint(
                                    resultados_finais,
                                    caminho_checkpoint,
                                )

                salvar_checkpoint(
                    resultados_finais,
                    caminho_checkpoint,
                )

                texto_progresso.empty()
                texto_estimativa.empty()

                df_final = pd.DataFrame(
                    resultados_finais
                )

                df_final = df_final.drop_duplicates(
                    subset=["Processo"],
                    keep="last",
                )

                ordem_processos = {
                    str(processo["Processo"]): indice
                    for indice, processo in enumerate(
                        processos_filtrados
                    )
                }

                df_final["_ordem"] = (
                    df_final["Processo"]
                    .astype(str)
                    .map(ordem_processos)
                )

                df_final = (
                    df_final.sort_values(
                        "_ordem",
                        na_position="last",
                    )
                    .drop(columns=["_ordem"])
                    .reset_index(drop=True)
                )

                total_sucesso = (
                    df_final["Situação"]
                    .eq("SUCESSO")
                    .sum()
                )

                total_sem_sucesso = (
                    len(df_final) - total_sucesso
                )

                quantidade_tribunais = (
                    df_final["Tribunal"]
                    .fillna("TRIBUNAL_NAO_IDENTIFICADO")
                    .nunique()
                )

                output_zip = gerar_zip_por_tribunal(
                    df_final,
                    status_usuario,
                    cd_arrendatario,
                )

                nome_zip = limpar_nome_arquivo(
                    f"{status_usuario} - "
                    f"{ambito} - "
                    f"{tribunal_sigla} - "
                    f"{cd_arrendatario}.zip"
                )

                status_box.update(
                    label="✅ Extração concluída!",
                    state="complete",
                )

                st.success(
                    f"Extração concluída: "
                    f"{len(df_final)} processos, "
                    f"{total_sucesso} IDs encontrados, "
                    f"{total_sem_sucesso} registros sem sucesso "
                    f"e {quantidade_tribunais} planilhas geradas."
                )

                st.download_button(
                    label="📥 Baixar planilhas por tribunal",
                    data=output_zip.getvalue(),
                    file_name=nome_zip,
                    mime="application/zip",
                )

                if os.path.exists(caminho_checkpoint):
                    try:
                        os.remove(caminho_checkpoint)
                    except OSError:
                        pass

            except Exception as erro:
                if caminho_checkpoint and resultados_finais:
                    try:
                        salvar_checkpoint(
                            resultados_finais,
                            caminho_checkpoint,
                        )
                    except Exception:
                        pass

                status_box.update(
                    label="❌ Erro durante a extração",
                    state="error",
                )

                st.error(f"Erro: {erro}")

                st.info(
                    "Caso existam resultados já processados, eles foram preservados no checkpoint."
                )
