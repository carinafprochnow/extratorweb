import gc
import hashlib
import html
import os
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL_API_PROJURIS = (
    "https://api.projurisadv.com.br/"
    "adv-service/consulta/central-captura-processo"
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
MAX_THREADS = 3
INTERVALO_CHECKPOINT = 25
TAMANHO_LOTE_BROLY = 50
LIMITE_PREVIA = 30
PAUSA_ENTRE_PAGINAS = 0.5
VERSAO_CHECKPOINT = "v11_sem_dashboard"

try:
    TOKEN_FORNECEDOR = st.secrets["TOKEN_FORNECEDOR"]
except KeyError:
    st.error(
        "Erro: TOKEN_FORNECEDOR não configurado nos Secrets."
    )
    st.stop()

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
    "JUSTIÇA FEDERAL": [
        "TODOS"
    ] + sorted(
        k for k in MAPA_CNJ if k.startswith("TRF")
    ),
    "JUSTIÇA DO TRABALHO": [
        "TODOS"
    ] + sorted(
        k for k in MAPA_CNJ if k.startswith("TRT")
    ),
    "JUSTIÇA ESTADUAL": [
        "TODOS"
    ] + sorted(
        k for k in MAPA_CNJ if k.startswith("TJ")
    ),
}

COLUNAS_RESULTADO = [
    "Processo",
    "codigoCentralCapturaProcesso",
    "Tribunal",
    "ID Demanda",
    "Status",
    "Fornecedor",
    "Link",
]

_thread_local = threading.local()


def criar_sessao_http():
    retry = Retry(
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

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_THREADS + 5,
        pool_maxsize=MAX_THREADS + 5,
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


def gerar_caminho_checkpoint(
    token_usuario,
    cd_arrendatario,
    status_usuario,
    ambito,
    tribunal_sigla,
):
    identificador = "|".join(
        [
            VERSAO_CHECKPOINT,
            token_usuario,
            cd_arrendatario,
            status_usuario,
            ambito,
            tribunal_sigla,
        ]
    )

    hash_execucao = hashlib.sha256(
        identificador.encode("utf-8")
    ).hexdigest()[:16]

    arrendatario_seguro = limpar_identificador(
        cd_arrendatario
    )

    return os.path.join(
        "/tmp",
        (
            "checkpoint_projuris_"
            f"{arrendatario_seguro}_"
            f"{hash_execucao}.csv"
        ),
    )


def normalizar_dataframe_resultados(df):
    for coluna in COLUNAS_RESULTADO:
        if coluna not in df.columns:
            df[coluna] = "N/A"

    df = df[COLUNAS_RESULTADO]
    df = df.fillna("N/A")

    return df


def salvar_checkpoint(resultados, caminho):
    if not resultados:
        return

    df = pd.DataFrame(resultados)
    df = normalizar_dataframe_resultados(df)

    df = df.drop_duplicates(
        subset=["Processo", "codigoCentralCapturaProcesso"],
        keep="last",
    )

    caminho_temporario = f"{caminho}.tmp"

    df.to_csv(
        caminho_temporario,
        index=False,
        encoding="utf-8-sig",
    )

    os.replace(
        caminho_temporario,
        caminho,
    )


def carregar_checkpoint(caminho):
    if not os.path.exists(caminho):
        return []

    try:
        df = pd.read_csv(
            caminho,
            dtype=str,
            encoding="utf-8-sig",
        )

        if not set(COLUNAS_RESULTADO).issubset(
            set(df.columns)
        ):
            return []

        df = normalizar_dataframe_resultados(df)

        df = df.drop_duplicates(
            subset=["Processo", "codigoCentralCapturaProcesso"],
            keep="last",
        )

        return df.to_dict(
            orient="records"
        )

    except Exception:
        return []


def consultar_pagina_projuris(
    sessao,
    headers,
    filtro,
    pagina,
):
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
            return sessao.post(
                URL_API_PROJURIS,
                headers=headers,
                params=parametros,
                json=payload,
                timeout=(
                    TIMEOUT_CONEXAO,
                    TIMEOUT_LEITURA_PROJURIS,
                ),
            )

        except requests.exceptions.ReadTimeout:
            ultimo_erro = (
                "A API demorou mais de "
                f"{TIMEOUT_LEITURA_PROJURIS} "
                "segundos para responder."
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
            espera = tentativa * 5

            st.warning(
                f"⏳ A página {pagina} demorou para "
                "responder. "
                f"Nova tentativa {tentativa + 1} de "
                f"{MAX_TENTATIVAS_PAGINA} em "
                f"{espera} segundos."
            )

            time.sleep(espera)

    raise RuntimeError(
        f"Não foi possível consultar a página {pagina} "
        f"após {MAX_TENTATIVAS_PAGINA} tentativas. "
        f"Último erro: {ultimo_erro}"
    )


def extrair_numero_processo(item):
    valor = item.get("paramentroCaptura")

    if not valor:
        processos_capturados = item.get(
            "processoCapturados",
            [],
        )

        if processos_capturados:
            valor = processos_capturados[0].get(
                "numeroProcesso"
            )

    return (
        str(valor).strip()
        if valor
        else "N/A"
    )


def processo_corresponde_ao_filtro(
    numero_processo,
    ambito,
    tribunal_sigla,
):
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

    return bool(
        codigo_especifico
        and codigo_especifico in numero_processo
    )


def identificar_tribunal(
    numero_processo,
    tribunal_api,
):
    for sigla, codigo in MAPA_CNJ.items():
        if codigo in numero_processo:
            return sigla

    tribunal_api = str(
        tribunal_api or ""
    ).strip()

    return (
        tribunal_api
        if tribunal_api
        else "TRIBUNAL_NAO_IDENTIFICADO"
    )


def montar_link_completo(
    cd_arrendatario,
    id_central,
):
    return (
        f"{URL_API_ACOMPANHAMENTO}"
        f"?token={TOKEN_FORNECEDOR}"
        f"&cdArrendatario={cd_arrendatario}"
        f"&cdCentralCapturaProcesso={id_central}"
    )


def nome_local_tag(tag):
    """
    Remove namespaces XML.

    Exemplo:
    {http://www.w3.org/1999/xhtml}provedor
    vira:
    provedor
    """
    return str(tag).split("}")[-1]


def extrair_valor_xml(raiz, nomes_possiveis):
    """
    Procura uma tag ignorando:
    - namespace;
    - diferença entre maiúsculas e minúsculas;
    - pequenas variações de nome.
    """
    nomes_normalizados = {
        nome.lower()
        for nome in nomes_possiveis
    }

    for elemento in raiz.iter():
        nome = nome_local_tag(
            elemento.tag
        ).lower()

        if nome in nomes_normalizados:
            texto = elemento.text

            if texto is not None:
                texto = texto.strip()

                if texto:
                    return texto

    return "N/A"


def extrair_tag_do_texto(texto, tag):
    """
    Extrai uma tag diretamente do texto bruto retornado pelo Broly.

    Aceita tags simples e tags com prefixo de namespace, por exemplo:
    <idDemanda>...</idDemanda>
    <ns:idDemanda>...</ns:idDemanda>
    """
    if not texto:
        return "N/A"

    texto = html.unescape(str(texto)).replace("\x00", "")

    padrao = (
        rf"<(?:[A-Za-z0-9_.-]+:)?{re.escape(tag)}\b[^>]*>"
        rf"\s*(.*?)\s*"
        rf"</(?:[A-Za-z0-9_.-]+:)?{re.escape(tag)}\s*>"
    )

    correspondencia = re.search(
        padrao,
        texto,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not correspondencia:
        return "N/A"

    valor = correspondencia.group(1)
    valor = re.sub(
        r"<!\[CDATA\[(.*?)\]\]>",
        r"\1",
        valor,
        flags=re.DOTALL,
    )
    valor = re.sub(r"<[^>]+>", "", valor)
    valor = html.unescape(valor).strip()

    return valor or "N/A"


def extrair_primeira_url(texto):
    """Extrai a primeira URL da resposta, mesmo quando o XML está quebrado."""
    if not texto:
        return "N/A"

    texto = html.unescape(str(texto)).replace("\x00", "")

    url_tag = extrair_tag_do_texto(texto, "url")
    if url_tag != "N/A":
        return url_tag

    correspondencia = re.search(
        r"https?://[^\s<>\"\']+",
        texto,
        flags=re.IGNORECASE,
    )

    if not correspondencia:
        return "N/A"

    return correspondencia.group(0).rstrip(".,;)")


def identificar_fornecedor_pela_url(url):
    """Usa a URL como fallback quando <provedor> não existe ou está vazio."""
    url_normalizada = str(url or "").strip().lower()

    if not url_normalizada or url_normalizada == "n/a":
        return "N/A"

    if "codilo" in url_normalizada:
        return "CODILO"

    if "hub" in url_normalizada:
        return "HUB"

    try:
        dominio = urlparse(url_normalizada).netloc
    except ValueError:
        dominio = ""

    return dominio.upper() if dominio else "OUTRO"


def extrair_uuid_demanda_fallback(texto, url_encontrada="N/A"):
    """
    Localiza o UUID da demanda mesmo quando a tag <idDemanda> está ausente
    ou o XML vem sem estrutura. Prioriza o UUID imediatamente anterior à URL.
    """
    if not texto:
        return "N/A"

    texto = html.unescape(str(texto)).replace("\x00", "")
    padrao_uuid = (
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        r"[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-"
        r"[0-9a-fA-F]{12}\b"
    )

    # Caso mais seguro: UUID entre o status principal e a URL da solicitação.
    if url_encontrada != "N/A":
        posicao_url = texto.find(url_encontrada)
        if posicao_url >= 0:
            trecho_anterior = texto[max(0, posicao_url - 500):posicao_url]
            uuids = re.findall(padrao_uuid, trecho_anterior)
            if uuids:
                return uuids[-1]

    # Fallback: primeiro UUID no início da resposta.
    correspondencia = re.search(padrao_uuid, texto[:5000])
    return correspondencia.group(0) if correspondencia else "N/A"


def interpretar_resposta_broly(resposta):
    """
    Extrai idDemanda, excecao e provedor do Broly com baixo uso de memória.

    Evita criar várias cópias decodificadas do mesmo XML/XHTML, algo que pode
    consumir muita memória quando a resposta do Broly é muito grande.
    """
    conteudo = resposta.content or b""

    try:
        texto = resposta.text or ""
    except Exception:
        texto = ""

    if not texto and conteudo:
        texto = conteudo.decode("utf-8", errors="replace")

    id_demanda = extrair_tag_do_texto(texto, "idDemanda")
    status = extrair_tag_do_texto(texto, "excecao")
    fornecedor = extrair_tag_do_texto(texto, "provedor")
    url_origem = extrair_primeira_url(texto)

    # Só monta uma árvore XML se alguma informação importante não veio
    # pela leitura direta do texto.
    if (
        id_demanda == "N/A"
        or status == "N/A"
        or fornecedor == "N/A"
        or url_origem == "N/A"
    ):
        try:
            raiz = ET.fromstring(conteudo)
        except (ET.ParseError, ValueError, TypeError):
            raiz = None

        if raiz is not None:
            if id_demanda == "N/A":
                id_demanda = extrair_valor_xml(
                    raiz,
                    ["idDemanda", "iddemanda"],
                )

            if status == "N/A":
                status = extrair_valor_xml(
                    raiz,
                    ["excecao"],
                )

            if fornecedor == "N/A":
                fornecedor = extrair_valor_xml(
                    raiz,
                    ["provedor"],
                )

            if url_origem == "N/A":
                url_origem = extrair_valor_xml(
                    raiz,
                    ["url"],
                )

            del raiz

    # Respostas antigas/fora do padrão podem vir concatenadas, sem tags úteis.
    if id_demanda == "N/A":
        id_demanda = extrair_uuid_demanda_fallback(
            texto,
            url_origem,
        )

    if fornecedor == "N/A":
        fornecedor = identificar_fornecedor_pela_url(
            url_origem
        )

    return id_demanda, status, fornecedor

def buscar_dados_demanda(
    cd_arrendatario,
    id_central,
):
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

    for tentativa in range(
        1,
        MAX_TENTATIVAS_DEMANDA + 1,
    ):
        try:
            resposta = sessao.get(
                URL_API_ACOMPANHAMENTO,
                params=parametros,
                headers={
                    "Accept": "application/xml, text/xml, application/xhtml+xml, */*",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=(
                    TIMEOUT_CONEXAO,
                    TIMEOUT_LEITURA_ACOMPANHAMENTO,
                ),
            )

            if resposta.status_code == 200:
                (
                    id_demanda,
                    status,
                    fornecedor,
                ) = interpretar_resposta_broly(
                    resposta
                )

                if (
                    id_demanda == "N/A"
                    and status == "N/A"
                    and fornecedor == "N/A"
                ):
                    status = (
                        "DADOS NÃO ENCONTRADOS "
                        "NA RESPOSTA DO BROLY"
                    )

                return (
                    id_demanda,
                    status,
                    fornecedor,
                    link_completo,
                )

            if resposta.status_code in [
                429,
                500,
                502,
                503,
                504,
            ]:
                ultimo_erro = (
                    "ERRO HTTP "
                    f"{resposta.status_code}"
                )

                if (
                    tentativa
                    < MAX_TENTATIVAS_DEMANDA
                ):
                    time.sleep(
                        tentativa * 2
                    )
                    continue

            return (
                "N/A",
                (
                    "ERRO HTTP "
                    f"{resposta.status_code}"
                ),
                "N/A",
                link_completo,
            )

        except requests.exceptions.ReadTimeout:
            ultimo_erro = (
                "TIMEOUT DE LEITURA"
            )

        except requests.exceptions.ConnectTimeout:
            ultimo_erro = (
                "TIMEOUT DE CONEXÃO"
            )

        except requests.exceptions.ConnectionError as erro:
            ultimo_erro = (
                "ERRO DE CONEXÃO: "
                f"{erro}"
            )

        except requests.exceptions.RequestException as erro:
            ultimo_erro = (
                "ERRO NA REQUISIÇÃO: "
                f"{erro}"
            )

        if tentativa < MAX_TENTATIVAS_DEMANDA:
            time.sleep(
                tentativa * 2
            )

    return (
        "N/A",
        ultimo_erro or "ERRO DESCONHECIDO",
        "N/A",
        link_completo,
    )


def consultar_processo(
    processo,
    cd_arrendatario,
):
    id_central = processo.get(
        "id_central"
    )

    if not id_central:
        return {
            "Processo": processo["Processo"],
            "codigoCentralCapturaProcesso": str(id_central or "N/A"),
            "Tribunal": processo["Tribunal"],
            "ID Demanda": "N/A",
            "Status": (
                "ID CENTRAL NÃO ENCONTRADO"
            ),
            "Fornecedor": "N/A",
            "Link": "N/A",
        }

    try:
        (
            id_demanda,
            status,
            fornecedor,
            link,
        ) = buscar_dados_demanda(
            cd_arrendatario,
            id_central,
        )

        return {
            "Processo": processo["Processo"],
            "codigoCentralCapturaProcesso": str(id_central or "N/A"),
            "Tribunal": processo["Tribunal"],
            "ID Demanda": id_demanda,
            "Status": status,
            "Fornecedor": fornecedor,
            "Link": link,
        }

    except Exception as erro:
        return {
            "Processo": processo["Processo"],
            "codigoCentralCapturaProcesso": str(id_central or "N/A"),
            "Tribunal": processo["Tribunal"],
            "ID Demanda": "N/A",
            "Status": (
                "ERRO INESPERADO: "
                f"{erro}"
            ),
            "Fornecedor": "N/A",
            "Link": montar_link_completo(
                cd_arrendatario,
                id_central,
            ),
        }


def gerar_excel_tribunal(df_tribunal):
    output = BytesIO()

    df_tribunal = normalizar_dataframe_resultados(
        df_tribunal.copy()
    )

    with pd.ExcelWriter(
        output,
        engine="xlsxwriter",
    ) as writer:
        df_tribunal.to_excel(
            writer,
            index=False,
            sheet_name="Resultados",
        )

        workbook = writer.book
        worksheet = writer.sheets[
            "Resultados"
        ]

        formato_cabecalho = workbook.add_format(
            {
                "bold": True,
                "border": 1,
            }
        )

        formato_link = workbook.add_format(
            {
                "font_color": "blue",
                "underline": 1,
            }
        )

        for (
            numero_coluna,
            nome_coluna,
        ) in enumerate(
            df_tribunal.columns
        ):
            worksheet.write(
                0,
                numero_coluna,
                nome_coluna,
                formato_cabecalho,
            )

        worksheet.set_column(
            "A:A",
            28,
        )
        worksheet.set_column(
            "B:B",
            30,
        )
        worksheet.set_column(
            "C:C",
            18,
        )
        worksheet.set_column(
            "D:D",
            38,
        )
        worksheet.set_column(
            "E:E",
            28,
        )
        worksheet.set_column(
            "F:F",
            30,
        )
        worksheet.set_column(
            "G:G",
            110,
        )

        if "Link" in df_tribunal.columns:
            coluna_link = (
                df_tribunal.columns.get_loc(
                    "Link"
                )
            )

            for indice, link in enumerate(
                df_tribunal["Link"],
                start=1,
            ):
                if (
                    isinstance(link, str)
                    and link.startswith("http")
                ):
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

        worksheet.freeze_panes(
            1,
            0,
        )

    output.seek(0)

    return output


def gerar_zip_por_tribunal(
    df_final,
    status_usuario,
    cd_arrendatario,
):
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
                tribunal
                or "TRIBUNAL_NAO_IDENTIFICADO"
            )

            nome_excel = limpar_nome_arquivo(
                f"{status_usuario} - "
                f"{tribunal_nome} - "
                f"{cd_arrendatario}.xlsx"
            )

            excel = gerar_excel_tribunal(
                df_tribunal.reset_index(
                    drop=True
                )
            )

            arquivo_zip.writestr(
                nome_excel,
                excel.getvalue(),
            )

    zip_output.seek(0)

    return zip_output




def preparar_dataframe_consulta(resultados, processos_filtrados):
    df = pd.DataFrame(resultados)
    df = normalizar_dataframe_resultados(df)
    df = df.drop_duplicates(subset=["Processo", "codigoCentralCapturaProcesso"], keep="last")

    ordem_capturas = {
        (
            str(processo.get("Processo", "N/A")),
            str(processo.get("id_central", "N/A")),
        ): indice
        for indice, processo in enumerate(processos_filtrados)
    }

    df["_ordem"] = [
        ordem_capturas.get(
            (str(processo), str(codigo_central)),
            len(ordem_capturas),
        )
        for processo, codigo_central in zip(
            df["Processo"],
            df["codigoCentralCapturaProcesso"],
        )
    ]

    df = (
        df.sort_values("_ordem", na_position="last")
        .drop(columns=["_ordem"])
        .reset_index(drop=True)
    )

    return df




def normalizar_token_usuario(token_raw):
    """Normaliza e valida o token informado antes de enviá-lo no header HTTP."""
    token = str(token_raw or "").strip()

    # Remove caracteres invisíveis comuns em cópias de navegador/Teams.
    token = token.replace("\u200b", "").replace("\ufeff", "")

    # Aceita, por conveniência, token colado como URL ou token=valor.
    if "token=" in token.lower():
        match = re.search(r"(?:^|[?&])token=([^&]+)", token, flags=re.IGNORECASE)
        if match:
            token = match.group(1).strip()

    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    # Reticências indicam que o token foi copiado truncado/abreviado.
    if "…" in token or "..." in token:
        raise ValueError(
            "O token parece estar abreviado: ele contém reticências (…). "
            "Copie o token completo da fonte original, sem 'token=', URL ou parâmetros adicionais."
        )

    # Tokens de autenticação devem ser transmitidos como caracteres ASCII.
    try:
        token.encode("ascii")
    except UnicodeEncodeError as erro:
        caractere = token[erro.start:erro.end]
        raise ValueError(
            "O token contém um caractere inválido ou invisível "
            f"({caractere!r}). Apague o campo e cole novamente o token completo."
        ) from erro

    # Remove espaços/quebras acidentais no meio da cópia.
    token = re.sub(r"\s+", "", token)

    if not token:
        raise ValueError("Informe um token válido.")

    return token


def anexar_resultados_csv(resultados, caminho):
    """
    Acrescenta apenas o lote recém-processado ao CSV.

    Isso evita reescrever e manter todos os resultados na memória a cada
    checkpoint.
    """
    if not resultados:
        return

    df_lote = pd.DataFrame(resultados)
    df_lote = normalizar_dataframe_resultados(df_lote)

    existe = os.path.exists(caminho) and os.path.getsize(caminho) > 0

    df_lote.to_csv(
        caminho,
        mode="a",
        header=not existe,
        index=False,
        encoding="utf-8-sig" if not existe else "utf-8",
    )

    del df_lote


def carregar_chaves_concluidas(caminho):
    if not os.path.exists(caminho):
        return set()

    try:
        df_chaves = pd.read_csv(
            caminho,
            dtype=str,
            usecols=[
                "Processo",
                "codigoCentralCapturaProcesso",
            ],
            encoding="utf-8-sig",
        ).fillna("N/A")

        chaves = set(
            zip(
                df_chaves["Processo"].astype(str),
                df_chaves[
                    "codigoCentralCapturaProcesso"
                ].astype(str),
            )
        )
        del df_chaves
        return chaves
    except Exception:
        return set()


def gerar_caminho_resultado(caminho_checkpoint):
    nome = os.path.basename(caminho_checkpoint).replace(
        "checkpoint_projuris_",
        "consulta_projuris_",
    )
    return os.path.join("/tmp", nome)


def finalizar_resultado_csv(
    caminho_checkpoint,
    caminho_resultado,
    processos_filtrados,
):
    df_final = pd.read_csv(
        caminho_checkpoint,
        dtype=str,
        encoding="utf-8-sig",
    )
    df_final = normalizar_dataframe_resultados(df_final)

    df_final = df_final.drop_duplicates(
        subset=[
            "Processo",
            "codigoCentralCapturaProcesso",
        ],
        keep="last",
    )

    ordem = {
        (
            str(processo.get("Processo", "N/A")),
            str(processo.get("id_central", "N/A")),
        ): indice
        for indice, processo in enumerate(processos_filtrados)
    }

    df_final["_ordem"] = [
        ordem.get(
            (str(processo), str(codigo)),
            len(ordem),
        )
        for processo, codigo in zip(
            df_final["Processo"],
            df_final["codigoCentralCapturaProcesso"],
        )
    ]

    df_final = (
        df_final.sort_values("_ordem", na_position="last")
        .drop(columns=["_ordem"])
        .reset_index(drop=True)
    )

    df_final.to_csv(
        caminho_resultado,
        index=False,
        encoding="utf-8-sig",
    )

    total = len(df_final)
    del df_final
    gc.collect()

    return total


def consultar_capturas_completas(
    token_user_raw,
    cd_arrendatario,
    status_usuario,
    ambito,
    tribunal_sigla,
    retomar_checkpoint,
    status_box,
):
    """
    Consulta Projuris e Broly mantendo em RAM somente:
    - os dados mínimos de cada captura;
    - um lote pequeno de respostas do Broly.

    Os resultados do Broly são gravados incrementalmente em /tmp.
    """
    token_limpo = normalizar_token_usuario(token_user_raw)
    token_final = f"Bearer {token_limpo}"

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
    caminho_resultado = gerar_caminho_resultado(
        caminho_checkpoint
    )

    if not retomar_checkpoint:
        for caminho in (
            caminho_checkpoint,
            caminho_resultado,
        ):
            if os.path.exists(caminho):
                try:
                    os.remove(caminho)
                except OSError:
                    pass

    filtro_api = MAPA_FILTROS.get(status_usuario)
    filtros_api_lista = (
        ["VINCULADOS", "PROCESSO_VINCULADO"]
        if status_usuario == "VINCULADOS"
        else [filtro_api]
    )

    # Em vez de guardar o JSON bruto de todas as páginas, guardamos somente
    # três campos por captura.
    processos_unicos = {}
    sessao_principal = criar_sessao_http()

    try:
        for filtro_atual in filtros_api_lista:
            st.write(
                "🛰️ Consultando registros no Projuris ADV..."
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
                            "Erro 412: verifique o Arrendatário "
                            "ou o Token."
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
                        f"A página {pagina} retornou uma "
                        "resposta JSON inválida."
                    ) from erro

                itens = data.get(
                    "centralCapturaProcessoConsultaResultadoWs",
                    [],
                )

                if not itens:
                    del data, resposta
                    break

                for item in itens:
                    numero_processo = extrair_numero_processo(
                        item
                    )

                    if not processo_corresponde_ao_filtro(
                        numero_processo,
                        ambito,
                        tribunal_sigla,
                    ):
                        continue

                    id_central = item.get(
                        "codigoCentralCapturaProcesso"
                    )

                    processo = {
                        "Processo": numero_processo,
                        "Tribunal": identificar_tribunal(
                            numero_processo,
                            item.get("tribunal"),
                        ),
                        "id_central": id_central,
                    }

                    chave = (
                        str(numero_processo or "N/A"),
                        str(id_central or "N/A"),
                    )
                    processos_unicos[chave] = processo

                total_coletado_filtro += len(itens)
                total_registros_filtro = data.get(
                    "totalRegistros",
                    total_registros_filtro,
                )

                if total_registros_filtro is not None:
                    st.write(
                        f"📥 {total_coletado_filtro} de "
                        f"{total_registros_filtro} "
                        "registros analisados."
                    )
                else:
                    st.write(
                        f"📥 {total_coletado_filtro} "
                        "registros analisados."
                    )

                terminou = (
                    (
                        total_registros_filtro is not None
                        and total_coletado_filtro
                        >= total_registros_filtro
                    )
                    or len(itens) < QUANTIDADE_POR_PAGINA
                )

                # Libera imediatamente a página bruta da API.
                del itens, data, resposta
                gc.collect()

                if terminou:
                    break

                pagina += 1
                time.sleep(PAUSA_ENTRE_PAGINAS)
    finally:
        try:
            sessao_principal.close()
        except Exception:
            pass

    processos_filtrados = list(processos_unicos.values())
    del processos_unicos
    gc.collect()

    if not processos_filtrados:
        raise RuntimeError(
            "Nenhum processo encontrado com os filtros "
            "selecionados."
        )

    total_processos = len(processos_filtrados)
    st.write(
        f"🔍 {total_processos} capturas encontradas."
    )

    capturas_ja_concluidas = (
        carregar_chaves_concluidas(caminho_checkpoint)
        if retomar_checkpoint
        else set()
    )

    processos_pendentes = [
        processo
        for processo in processos_filtrados
        if (
            str(processo.get("Processo", "N/A")),
            str(processo.get("id_central", "N/A")),
        ) not in capturas_ja_concluidas
    ]

    quantidade_recuperada = len(
        capturas_ja_concluidas
    )

    if quantidade_recuperada:
        st.info(
            f"♻️ {quantidade_recuperada} capturas "
            "recuperadas da consulta anterior."
        )

    del capturas_ja_concluidas
    gc.collect()

    total_pendentes = len(processos_pendentes)

    # A consulta ao Broly é processada em lotes e com concorrência
    # controlada para manter o app estável no Streamlit Cloud.
    aviso_consultas = None

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

    try:
        for inicio_lote in range(
            0,
            total_pendentes,
            TAMANHO_LOTE_BROLY,
        ):
            lote = processos_pendentes[
                inicio_lote:
                inicio_lote + TAMANHO_LOTE_BROLY
            ]
            resultados_lote = []

            with ThreadPoolExecutor(
                max_workers=MAX_THREADS
            ) as executor:
                futures = {
                    executor.submit(
                        consultar_processo,
                        processo,
                        cd_arrendatario,
                    ): processo
                    for processo in lote
                }

                for future in as_completed(futures):
                    processo_original = futures[future]

                    try:
                        resultado = future.result()
                    except Exception as erro:
                        id_central = processo_original.get(
                            "id_central"
                        )
                        resultado = {
                            "Processo": processo_original[
                                "Processo"
                            ],
                            "codigoCentralCapturaProcesso": str(
                                id_central or "N/A"
                            ),
                            "Tribunal": processo_original[
                                "Tribunal"
                            ],
                            "ID Demanda": "N/A",
                            "Status": (
                                "ERRO INESPERADO NA THREAD: "
                                f"{erro}"
                            ),
                            "Fornecedor": "N/A",
                            "Link": (
                                montar_link_completo(
                                    cd_arrendatario,
                                    id_central,
                                )
                                if id_central
                                else "N/A"
                            ),
                        }

                    resultados_lote.append(resultado)
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

                    if concluidos_nesta_execucao:
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

                        texto_progresso.write(
                            f"✅ {total_concluido} de "
                            f"{total_processos} "
                            "capturas consultadas."
                        )
                        texto_estimativa.caption(
                            f"Restantes: {restantes} | "
                            "Estimativa aproximada: "
                            f"{int(estimativa // 60)} min "
                            f"{int(estimativa % 60)} s"
                        )

            # O lote inteiro vai ao disco de uma vez.
            anexar_resultados_csv(
                resultados_lote,
                caminho_checkpoint,
            )

            del resultados_lote, futures, lote
            gc.collect()

        if not os.path.exists(caminho_checkpoint):
            raise RuntimeError(
                "A consulta terminou sem gerar resultados."
            )

        total_final = finalizar_resultado_csv(
            caminho_checkpoint,
            caminho_resultado,
            processos_filtrados,
        )

        status_box.update(
            label="✅ Consulta concluída!",
            state="complete",
        )

        return caminho_resultado, total_final

    finally:
        if aviso_consultas is not None:
            aviso_consultas.empty()
        texto_progresso.empty()
        texto_estimativa.empty()
        del processos_pendentes
        gc.collect()

def gerar_excel_unico(df_final):
    return gerar_excel_tribunal(df_final)


def gerar_zip_por_fornecedor(
    df_final,
    status_usuario,
    cd_arrendatario,
):
    zip_output = BytesIO()

    with zipfile.ZipFile(
        zip_output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as arquivo_zip:
        grupos = df_final.groupby(
            "Fornecedor",
            dropna=False,
            sort=True,
        )

        for fornecedor, df_fornecedor in grupos:
            fornecedor_nome = limpar_nome_arquivo(
                fornecedor or "FORNECEDOR_NAO_IDENTIFICADO"
            )
            nome_excel = limpar_nome_arquivo(
                f"{status_usuario} - {fornecedor_nome} - "
                f"{cd_arrendatario}.xlsx"
            )
            excel = gerar_excel_unico(
                df_fornecedor.reset_index(drop=True)
            )
            arquivo_zip.writestr(nome_excel, excel.getvalue())

    zip_output.seek(0)
    return zip_output


def gerar_zip_por_tribunal_e_fornecedor(
    df_final,
    status_usuario,
    cd_arrendatario,
):
    zip_output = BytesIO()

    with zipfile.ZipFile(
        zip_output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as arquivo_zip:
        grupos = df_final.groupby(
            ["Fornecedor", "Tribunal"],
            dropna=False,
            sort=True,
        )

        for (fornecedor, tribunal), df_grupo in grupos:
            fornecedor_nome = limpar_nome_arquivo(
                fornecedor or "FORNECEDOR_NAO_IDENTIFICADO"
            )
            tribunal_nome = limpar_nome_arquivo(
                tribunal or "TRIBUNAL_NAO_IDENTIFICADO"
            )
            nome_excel = limpar_nome_arquivo(
                f"{status_usuario} - {tribunal_nome} - "
                f"{cd_arrendatario}.xlsx"
            )
            caminho_zip = f"{fornecedor_nome}/{nome_excel}"
            excel = gerar_excel_unico(
                df_grupo.reset_index(drop=True)
            )
            arquivo_zip.writestr(caminho_zip, excel.getvalue())

    zip_output.seek(0)
    return zip_output


def aplicar_filtros_pos_consulta(
    df,
    fornecedores,
    status_broly,
    tribunais,
    somente_com_id,
):
    df_filtrado = df.copy()

    if fornecedores:
        df_filtrado = df_filtrado[
            df_filtrado["Fornecedor"].isin(fornecedores)
        ]

    if status_broly:
        df_filtrado = df_filtrado[
            df_filtrado["Status"].isin(status_broly)
        ]

    if tribunais:
        df_filtrado = df_filtrado[
            df_filtrado["Tribunal"].isin(tribunais)
        ]

    if somente_com_id:
        df_filtrado = df_filtrado[
            df_filtrado["ID Demanda"].astype(str).str.strip().ne("N/A")
        ]

    return df_filtrado.reset_index(drop=True)


def criar_opcoes_com_quantidade(df, coluna):
    contagens = (
        df[coluna]
        .fillna("N/A")
        .astype(str)
        .value_counts(dropna=False)
    )
    mapa = {
        f"{valor} ({quantidade:,})".replace(",", "."): valor
        for valor, quantidade in contagens.items()
    }
    return list(mapa.keys()), mapa


st.set_page_config(
    page_title="Extrator Projuris Web",
    layout="wide",
)

st.title("📂 Consulta e Extração de Capturas - Projuris ADV")
st.caption(
    "Primeiro consulte as capturas e analise a distribuição. "
    "Depois aplique os filtros e gere os arquivos desejados."
)

for chave, valor_padrao in {
    "caminho_consulta": None,
    "parametros_consulta": None,
}.items():
    if chave not in st.session_state:
        st.session_state[chave] = valor_padrao

with st.sidebar:
    st.header("Configurações da consulta")

    token_user_raw = st.text_input(
        "Token",
        type="password",
    )

    cd_arrendatario = st.text_input(
        "Arrendatário",
        value="",
    )

    status_usuario = st.selectbox(
        "Status no Projuris",
        list(MAPA_FILTROS.keys()),
        index=2,
    )

    st.divider()
    st.header("Filtros iniciais")

    ambito = st.selectbox(
        "Âmbito",
        list(DIC_TRIBUNAIS.keys()),
    )

    tribunal_sigla = st.selectbox(
        "Tribunal",
        DIC_TRIBUNAIS[ambito],
    )

    st.divider()

    retomar_checkpoint = st.checkbox(
        "Retomar consulta interrompida",
        value=True,
        help=(
            "Reaproveita lotes já gravados em disco "
            "se uma execução anterior for interrompida."
        ),
    )

    consultar = st.button(
        "🔎 Consultar capturas",
        type="primary",
        width="stretch",
    )

    limpar_consulta = st.button(
        "🧹 Limpar consulta atual",
        width="stretch",
        disabled=(
            st.session_state["caminho_consulta"]
            is None
        ),
    )

if limpar_consulta:
    caminho_antigo = st.session_state.get(
        "caminho_consulta"
    )
    if caminho_antigo and os.path.exists(caminho_antigo):
        try:
            os.remove(caminho_antigo)
        except OSError:
            pass

    st.session_state["caminho_consulta"] = None
    st.session_state["parametros_consulta"] = None
    gc.collect()
    st.rerun()

if consultar:
    if not token_user_raw:
        st.error("Insira o Token.")
    elif not cd_arrendatario:
        st.error("Insira o Arrendatário.")
    else:
        with st.status(
            "Consultando capturas...",
            expanded=True,
        ) as status_box:
            try:
                (
                    caminho_consulta,
                    total_consulta,
                ) = consultar_capturas_completas(
                    token_user_raw=token_user_raw,
                    cd_arrendatario=cd_arrendatario,
                    status_usuario=status_usuario,
                    ambito=ambito,
                    tribunal_sigla=tribunal_sigla,
                    retomar_checkpoint=retomar_checkpoint,
                    status_box=status_box,
                )

                st.session_state[
                    "caminho_consulta"
                ] = caminho_consulta
                st.session_state[
                    "parametros_consulta"
                ] = {
                    "cd_arrendatario": cd_arrendatario,
                    "status_usuario": status_usuario,
                    "ambito": ambito,
                    "tribunal_sigla": tribunal_sigla,
                }

                st.success(
                    (
                        f"Consulta concluída com "
                        f"{total_consulta:,} capturas."
                    ).replace(",", ".")
                )

            except Exception as erro:
                status_box.update(
                    label="❌ Erro durante a consulta",
                    state="error",
                )
                st.error(f"Erro: {erro}")
                st.info(
                    "Os lotes já concluídos permanecem no "
                    "checkpoint para uma nova tentativa."
                )

caminho_consulta = st.session_state.get(
    "caminho_consulta"
)

if (
    not caminho_consulta
    or not os.path.exists(caminho_consulta)
):
    if caminho_consulta:
        # A instância do Streamlit pode ter reiniciado e apagado /tmp.
        st.session_state["caminho_consulta"] = None
        st.session_state["parametros_consulta"] = None

    st.info(
        "Preencha os filtros na barra lateral e clique em "
        "'Consultar capturas'."
    )
    st.stop()

# O DataFrame existe apenas durante este rerun. Ele não fica preso ao
# session_state entre interações.
df_consulta = pd.read_csv(
    caminho_consulta,
    dtype=str,
    encoding="utf-8-sig",
).fillna("N/A")

df_consulta = normalizar_dataframe_resultados(
    df_consulta
)
parametros = st.session_state[
    "parametros_consulta"
]

st.divider()
st.subheader("📋 Resumo da consulta")

if parametros:
    st.caption(
        f"Arrendatário: {parametros['cd_arrendatario']} | "
        f"Status: {parametros['status_usuario']} | "
        f"Âmbito: {parametros['ambito']} | "
        f"Tribunal inicial: {parametros['tribunal_sigla']}"
    )

total_processos = len(df_consulta)

total_com_id = int(
    df_consulta["ID Demanda"]
    .astype(str)
    .str.strip()
    .ne("N/A")
    .sum()
)
total_sem_id = total_processos - total_com_id

st.success(
    f"Foram encontradas {total_processos:,} capturas "
    "com os filtros selecionados."
    .replace(",", ".")
)

# Resumo simples por fornecedor. Não cria gráficos, tabs,
# tabelas de dashboard ou cruzamentos pesados.
contagem_fornecedor = (
    df_consulta["Fornecedor"]
    .fillna("N/A")
    .astype(str)
    .str.strip()
    .replace("", "N/A")
    .value_counts(dropna=False)
)

if not contagem_fornecedor.empty:
    st.markdown("**Distribuição por fornecedor:**")

    for fornecedor, quantidade in contagem_fornecedor.items():
        fornecedor_exibicao = (
            "Fornecedor não identificado"
            if str(fornecedor).strip() == "N/A"
            else str(fornecedor).strip()
        )

        percentual = (
            (int(quantidade) / total_processos * 100)
            if total_processos
            else 0
        )

        st.write(
            f"• {fornecedor_exibicao}: "
            f"{int(quantidade):,} "
            f"({percentual:.1f}%)"
            .replace(",", ".")
        )

if total_sem_id:
    st.caption(
        f"{total_sem_id:,} capturas não retornaram ID Demanda."
        .replace(",", ".")
    )
else:
    st.caption(
        "Todas as capturas retornaram ID Demanda."
    )

# Libera o Series de resumo assim que ele deixa de ser necessário.
del contagem_fornecedor
gc.collect()

st.divider()
st.subheader("🎯 Filtros para extração")
st.caption(
    "Estes filtros são aplicados aos dados já consultados. "
    "Alterá-los não faz novas requisições ao Projuris ou ao Broly."
)

opcoes_fornecedor_labels, mapa_fornecedor = criar_opcoes_com_quantidade(
    df_consulta, "Fornecedor"
)
opcoes_status_labels, mapa_status = criar_opcoes_com_quantidade(
    df_consulta, "Status"
)
opcoes_tribunal_labels, mapa_tribunal = criar_opcoes_com_quantidade(
    df_consulta, "Tribunal"
)

f1, f2, f3 = st.columns(3)

with f1:
    fornecedores_labels = st.multiselect(
        "Fornecedor",
        options=opcoes_fornecedor_labels,
        default=opcoes_fornecedor_labels,
        help="Valores exatos retornados pela tag <provedor> do Broly.",
    )

with f2:
    status_labels = st.multiselect(
        "Status retornado pelo Broly",
        options=opcoes_status_labels,
        default=opcoes_status_labels,
    )

with f3:
    tribunais_labels = st.multiselect(
        "Tribunal",
        options=opcoes_tribunal_labels,
        default=opcoes_tribunal_labels,
    )

fornecedores_selecionados = [
    mapa_fornecedor[label] for label in fornecedores_labels
]
status_selecionados = [
    mapa_status[label] for label in status_labels
]
tribunais_selecionados = [
    mapa_tribunal[label] for label in tribunais_labels
]

somente_com_id = st.checkbox(
    "Incluir somente registros com ID Demanda",
    value=False,
)

df_filtrado = aplicar_filtros_pos_consulta(
    df=df_consulta,
    fornecedores=fornecedores_selecionados,
    status_broly=status_selecionados,
    tribunais=tribunais_selecionados,
    somente_com_id=somente_com_id,
)

st.info(
    f"O filtro atual selecionou {len(df_filtrado):,} de "
    f"{len(df_consulta):,} capturas."
    .replace(",", ".")
)

st.subheader("👀 Prévia dos resultados")
st.dataframe(
    df_filtrado[
        [
            "Processo",
            "codigoCentralCapturaProcesso",
            "Tribunal",
            "ID Demanda",
            "Status",
            "Fornecedor",
            "Link",
        ]
    ].head(LIMITE_PREVIA),
    width="stretch",
    hide_index=True,
)

if len(df_filtrado) > LIMITE_PREVIA:
    st.caption(
        f"A prévia mostra as primeiras {LIMITE_PREVIA} linhas. "
        "O arquivo incluirá todos os registros selecionados."
    )

st.divider()
st.subheader("📦 Gerar arquivos")

organizacao = st.radio(
    "Organização dos arquivos",
    options=[
        "Excel único",
        "Separar por tribunal",
        "Separar por fornecedor",
        "Separar por fornecedor e tribunal",
    ],
    horizontal=True,
)

if df_filtrado.empty:
    st.warning(
        "Nenhuma captura corresponde aos filtros atuais. "
        "Ajuste os filtros para liberar a geração."
    )
else:
    status_nome = parametros["status_usuario"]
    arrendatario_nome = parametros["cd_arrendatario"]

    st.caption(
        "O arquivo só será montado ao clicar no botão abaixo. "
        "Assim, trocar filtros não recria Excel ou ZIP automaticamente."
    )

    preparar_arquivo = st.button(
        "⚙️ Preparar arquivo para download",
        type="primary",
        width="stretch",
    )

    if preparar_arquivo:
        with st.spinner("Gerando arquivo..."):
            if organizacao == "Excel único":
                arquivo_saida = gerar_excel_unico(df_filtrado)

                tribunais_no_arquivo = sorted(
                    {
                        str(tribunal).strip()
                        for tribunal in df_filtrado["Tribunal"].dropna().tolist()
                        if str(tribunal).strip()
                    }
                )

                if len(tribunais_no_arquivo) == 1:
                    identificador_tribunal = tribunais_no_arquivo[0]
                elif len(tribunais_no_arquivo) == 2:
                    identificador_tribunal = " + ".join(tribunais_no_arquivo)
                elif len(tribunais_no_arquivo) > 2:
                    identificador_tribunal = "MULTIPLOS TRIBUNAIS"
                else:
                    identificador_tribunal = "TRIBUNAL NAO IDENTIFICADO"

                nome_arquivo = limpar_nome_arquivo(
                    f"{status_nome} - {identificador_tribunal} - "
                    f"{arrendatario_nome}.xlsx"
                )
                mime = (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                )
            elif organizacao == "Separar por tribunal":
                arquivo_saida = gerar_zip_por_tribunal(
                    df_filtrado,
                    status_nome,
                    arrendatario_nome,
                )
                nome_arquivo = limpar_nome_arquivo(
                    f"{status_nome} - POR TRIBUNAL - "
                    f"{arrendatario_nome}.zip"
                )
                mime = "application/zip"
            elif organizacao == "Separar por fornecedor":
                arquivo_saida = gerar_zip_por_fornecedor(
                    df_filtrado,
                    status_nome,
                    arrendatario_nome,
                )
                nome_arquivo = limpar_nome_arquivo(
                    f"{status_nome} - POR FORNECEDOR - "
                    f"{arrendatario_nome}.zip"
                )
                mime = "application/zip"
            else:
                arquivo_saida = gerar_zip_por_tribunal_e_fornecedor(
                    df_filtrado,
                    status_nome,
                    arrendatario_nome,
                )
                nome_arquivo = limpar_nome_arquivo(
                    f"{status_nome} - FORNECEDOR E TRIBUNAL - "
                    f"{arrendatario_nome}.zip"
                )
                mime = "application/zip"

            dados_arquivo = arquivo_saida.getvalue()
            arquivo_saida.close()

        st.success(
            f"Arquivo pronto: {nome_arquivo} "
            f"({len(dados_arquivo) / 1024 / 1024:.2f} MB)"
        )
        st.download_button(
            label=f"📥 Baixar {organizacao.lower()}",
            data=dados_arquivo,
            file_name=nome_arquivo,
            mime=mime,
            type="primary",
            width="stretch",
            on_click="ignore",
        )

        del dados_arquivo
        gc.collect()
