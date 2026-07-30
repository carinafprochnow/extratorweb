import hashlib
import os
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL_API_PROJURIS = (
    "https://api.projurisadv.com.br/adv-service/consulta/central-captura-processo"
)
URL_BROLY = (
    "https://broly.sajadv.com.br/api/acompanhamento"
)

QUANTIDADE_POR_PAGINA = 100
TIMEOUT_CONEXAO = 15
TIMEOUT_LEITURA_PROJURIS = 120
TIMEOUT_LEITURA_ACOMPANHAMENTO = 60
MAX_TENTATIVAS_PAGINA = 5
MAX_TENTATIVAS_DEMANDA = 4
MAX_THREADS = 20
INTERVALO_CHECKPOINT = 50
PAUSA_ENTRE_PAGINAS = 0.5
VERSAO_CHECKPOINT = "v7_codigo_central"

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
            subset=["Processo"],
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
        f"{URL_BROLY}"
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
    Extrai idDemanda, excecao e provedor do XML/XHTML do Broly.

    Estratégia em camadas:
    1. Leitura das tags no texto bruto.
    2. Leitura como XML estruturado.
    3. Fallback por padrões: UUID e URL.
    4. Identificação do fornecedor pela URL.
    """
    conteudo = resposta.content or b""
    candidatos_texto = []

    if resposta.text:
        candidatos_texto.append(resposta.text)

    for codificacao in (
        resposta.encoding,
        "utf-8-sig",
        "utf-8",
        "iso-8859-1",
        "windows-1252",
    ):
        if not codificacao:
            continue

        try:
            texto_decodificado = conteudo.decode(
                codificacao,
                errors="replace",
            )
        except (LookupError, UnicodeDecodeError):
            continue

        if texto_decodificado not in candidatos_texto:
            candidatos_texto.append(texto_decodificado)

    id_demanda = "N/A"
    status = "N/A"
    fornecedor = "N/A"
    url_origem = "N/A"

    for texto in candidatos_texto:
        if id_demanda == "N/A":
            id_demanda = extrair_tag_do_texto(texto, "idDemanda")

        if status == "N/A":
            status = extrair_tag_do_texto(texto, "excecao")

        if fornecedor == "N/A":
            fornecedor = extrair_tag_do_texto(texto, "provedor")

        if url_origem == "N/A":
            url_origem = extrair_primeira_url(texto)

    if id_demanda == "N/A" or status == "N/A" or fornecedor == "N/A":
        try:
            raiz = ET.fromstring(conteudo)
        except (ET.ParseError, ValueError):
            raiz = None

        if raiz is not None:
            if id_demanda == "N/A":
                id_demanda = extrair_valor_xml(
                    raiz,
                    ["idDemanda", "iddemanda"],
                )

            if status == "N/A":
                status = extrair_valor_xml(raiz, ["excecao"])

            if fornecedor == "N/A":
                fornecedor = extrair_valor_xml(raiz, ["provedor"])

            if url_origem == "N/A":
                url_origem = extrair_valor_xml(raiz, ["url"])

    # Último recurso para respostas concatenadas ou fora do padrão.
    for texto in candidatos_texto:
        if id_demanda == "N/A":
            id_demanda = extrair_uuid_demanda_fallback(
                texto,
                url_origem,
            )

        if url_origem == "N/A":
            url_origem = extrair_primeira_url(texto)

    if fornecedor == "N/A":
        fornecedor = identificar_fornecedor_pela_url(url_origem)

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
                URL_BROLY,
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


def consultar_capturas_completas(
    token_user_raw,
    cd_arrendatario,
    status_usuario,
    ambito,
    tribunal_sigla,
    retomar_checkpoint,
    status_box,
):
    sessao_principal = criar_sessao_http()
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

    filtro_api = MAPA_FILTROS.get(status_usuario)
    filtros_api_lista = (
        ["VINCULADOS", "PROCESSO_VINCULADO"]
        if status_usuario == "VINCULADOS"
        else [filtro_api]
    )

    dados_brutos = []

    for filtro_atual in filtros_api_lista:
        st.write("🛰️ Consultando registros no Projuris ADV...")

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
                        "Erro 412: verifique o Arrendatário ou o Token."
                    )

                raise RuntimeError(
                    f"Erro HTTP {resposta.status_code} na página {pagina}. "
                    f"Resposta: {resposta.text[:500]}"
                )

            try:
                data = resposta.json()
            except ValueError as erro:
                raise RuntimeError(
                    f"A página {pagina} retornou uma resposta JSON inválida."
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
                    f"📥 {total_coletado_filtro} de "
                    f"{total_registros_filtro} registros coletados."
                )
            else:
                st.write(
                    f"📥 {total_coletado_filtro} registros coletados."
                )

            if (
                total_registros_filtro is not None
                and total_coletado_filtro >= total_registros_filtro
            ):
                break

            if len(itens) < QUANTIDADE_POR_PAGINA:
                break

            pagina += 1
            time.sleep(PAUSA_ENTRE_PAGINAS)

    if not dados_brutos:
        raise RuntimeError("Nenhum registro foi retornado pela API.")

    processos_filtrados = []

    for item in dados_brutos:
        numero_processo = extrair_numero_processo(item)

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
        # Um mesmo número de processo pode possuir mais de uma captura.
        # A identidade correta de cada captura é o codigoCentralCapturaProcesso.
        chave = (
            str(processo.get("id_central") or "N/A"),
            str(processo.get("Processo") or "N/A"),
        )
        if chave not in processos_unicos:
            processos_unicos[chave] = processo

    processos_filtrados = list(processos_unicos.values())

    if not processos_filtrados:
        raise RuntimeError(
            "Nenhum processo encontrado com os filtros selecionados."
        )

    total_processos = len(processos_filtrados)
    st.write(f"🔍 {total_processos} capturas encontradas.")

    resultados_finais = []

    if retomar_checkpoint:
        resultados_finais = carregar_checkpoint(caminho_checkpoint)

    capturas_ja_concluidas = {
        (
            str(resultado.get("Processo", "N/A")),
            str(resultado.get("codigoCentralCapturaProcesso", "N/A")),
        )
        for resultado in resultados_finais
    }

    processos_pendentes = [
        processo
        for processo in processos_filtrados
        if (
            str(processo.get("Processo", "N/A")),
            str(processo.get("id_central", "N/A")),
        ) not in capturas_ja_concluidas
    ]

    quantidade_recuperada = len(capturas_ja_concluidas)

    if quantidade_recuperada > 0:
        st.info(
            f"♻️ {quantidade_recuperada} resultados recuperados "
            "da execução anterior."
        )

    total_pendentes = len(processos_pendentes)

    st.write(
        f"⚡ Consultando o Broly para {total_pendentes} processos."
    )

    aviso_consultas = st.info(
        "A contagem pode ficar alguns segundos parada enquanto "
        "as respostas são processadas."
    )

    progress_bar = st.progress(
        min(quantidade_recuperada / total_processos, 1.0)
    )
    texto_progresso = st.empty()
    texto_estimativa = st.empty()

    inicio = time.monotonic()
    concluidos_nesta_execucao = 0

    try:
        if processos_pendentes:
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
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
                        id_central = processo_original.get("id_central")
                        resultado = {
                            "Processo": processo_original["Processo"],
                            "codigoCentralCapturaProcesso": str(id_central or "N/A"),
                            "Tribunal": processo_original["Tribunal"],
                            "ID Demanda": "N/A",
                            "Status": f"ERRO INESPERADO NA THREAD: {erro}",
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

                    resultados_finais.append(resultado)
                    concluidos_nesta_execucao += 1
                    total_concluido = (
                        quantidade_recuperada
                        + concluidos_nesta_execucao
                    )

                    progress_bar.progress(
                        min(total_concluido / total_processos, 1.0)
                    )

                    tempo_decorrido = time.monotonic() - inicio
                    media = tempo_decorrido / concluidos_nesta_execucao
                    restantes = (
                        total_pendentes - concluidos_nesta_execucao
                    )
                    estimativa = media * restantes
                    minutos = int(estimativa // 60)
                    segundos = int(estimativa % 60)

                    texto_progresso.write(
                        f"✅ {total_concluido} de {total_processos} "
                        "processos consultados."
                    )
                    texto_estimativa.caption(
                        f"Restantes: {restantes} | "
                        f"Estimativa aproximada: {minutos} min {segundos} s"
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

        salvar_checkpoint(resultados_finais, caminho_checkpoint)

    except Exception:
        if resultados_finais:
            salvar_checkpoint(resultados_finais, caminho_checkpoint)
        raise

    finally:
        aviso_consultas.empty()
        texto_progresso.empty()
        texto_estimativa.empty()

    df_final = preparar_dataframe_consulta(
        resultados_finais,
        processos_filtrados,
    )

    if os.path.exists(caminho_checkpoint):
        try:
            os.remove(caminho_checkpoint)
        except OSError:
            pass

    status_box.update(
        label="✅ Consulta concluída!",
        state="complete",
    )

    return df_final


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
    "Primeiro consulte as capturas e analise os resultados. "
    "Depois aplique os filtros e gere os arquivos desejados. "
)

for chave, valor_padrao in {
    "dados_consulta": None,
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
            "Reaproveita resultados já consultados caso uma "
            "execução anterior tenha sido interrompida."
        ),
    )

    consultar = st.button(
        "🔎 Consultar capturas",
        type="primary",
        use_container_width=True,
    )

    limpar_consulta = st.button(
        "🧹 Limpar consulta atual",
        use_container_width=True,
        disabled=st.session_state["dados_consulta"] is None,
    )

if limpar_consulta:
    st.session_state["dados_consulta"] = None
    st.session_state["parametros_consulta"] = None
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
                df_consulta = consultar_capturas_completas(
                    token_user_raw=token_user_raw,
                    cd_arrendatario=cd_arrendatario,
                    status_usuario=status_usuario,
                    ambito=ambito,
                    tribunal_sigla=tribunal_sigla,
                    retomar_checkpoint=retomar_checkpoint,
                    status_box=status_box,
                )

                st.session_state["dados_consulta"] = df_consulta
                st.session_state["parametros_consulta"] = {
                    "cd_arrendatario": cd_arrendatario,
                    "status_usuario": status_usuario,
                    "ambito": ambito,
                    "tribunal_sigla": tribunal_sigla,
                }

                st.success(
                    "Consulta concluída. Os dados ficaram armazenados "
                    "nesta sessão para que você possa aplicar vários "
                    "filtros e gerar arquivos sem consultar novamente."
                )

            except Exception as erro:
                status_box.update(
                    label="❌ Erro durante a consulta",
                    state="error",
                )
                st.error(f"Erro: {erro}")
                st.info(
                    "Caso existam resultados já processados, eles foram "
                    "preservados para uma nova tentativa."
                )

if st.session_state["dados_consulta"] is None:
    st.info(
        "Preencha os filtros na barra lateral e clique em "
        "'Consultar capturas'."
    )
    st.stop()


df_consulta = st.session_state["dados_consulta"].copy()
parametros = st.session_state["parametros_consulta"]

st.divider()
st.subheader("📊 Resumo da consulta")

if parametros:
    st.caption(
        f"Arrendatário: {parametros['cd_arrendatario']} | "
        f"Status: {parametros['status_usuario']} | "
        f"Âmbito: {parametros['ambito']} | "
        f"Tribunal inicial: {parametros['tribunal_sigla']}"
    )

total_processos = len(df_consulta)
total_com_id = (
    df_consulta["ID Demanda"].astype(str).str.strip().ne("N/A").sum()
)
total_sem_id = total_processos - total_com_id
total_fornecedores = (
    df_consulta.loc[
        df_consulta["Fornecedor"].astype(str).str.strip().ne("N/A"),
        "Fornecedor",
    ].nunique()
)
total_tribunais = df_consulta["Tribunal"].nunique()

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Processos", f"{total_processos:,}".replace(",", "."))
col2.metric("Com ID Demanda", f"{total_com_id:,}".replace(",", "."))
col3.metric("Sem ID Demanda", f"{total_sem_id:,}".replace(",", "."))
col4.metric("Fornecedores", total_fornecedores)
col5.metric("Tribunais", total_tribunais)

aba_fornecedor, aba_status, aba_tribunal, aba_cruzamento = st.tabs([
    "Fornecedores",
    "Status do Broly",
    "Tribunais",
    "Fornecedor x Status",
])

with aba_fornecedor:
    resumo_fornecedor = (
        df_consulta.groupby(
            ["Fornecedor"],
            dropna=False,
        )
        .size()
        .reset_index(name="Quantidade")
        .sort_values("Quantidade", ascending=False)
    )
    resumo_fornecedor["Percentual"] = (
        resumo_fornecedor["Quantidade"] / total_processos * 100
    ).map(lambda valor: f"{valor:.1f}%".replace(".", ","))
    st.dataframe(
        resumo_fornecedor,
        use_container_width=True,
        hide_index=True,
    )
    st.bar_chart(
        resumo_fornecedor.set_index("Fornecedor")["Quantidade"]
    )

with aba_status:
    resumo_status = (
        df_consulta.groupby("Status", dropna=False)
        .size()
        .reset_index(name="Quantidade")
        .sort_values("Quantidade", ascending=False)
    )
    resumo_status["Percentual"] = (
        resumo_status["Quantidade"] / total_processos * 100
    ).map(lambda valor: f"{valor:.1f}%".replace(".", ","))
    st.dataframe(
        resumo_status,
        use_container_width=True,
        hide_index=True,
    )
    st.bar_chart(
        resumo_status.set_index("Status")["Quantidade"]
    )

with aba_tribunal:
    resumo_tribunal = (
        df_consulta.groupby("Tribunal", dropna=False)
        .size()
        .reset_index(name="Quantidade")
        .sort_values("Quantidade", ascending=False)
    )
    resumo_tribunal["Percentual"] = (
        resumo_tribunal["Quantidade"] / total_processos * 100
    ).map(lambda valor: f"{valor:.1f}%".replace(".", ","))
    st.dataframe(
        resumo_tribunal,
        use_container_width=True,
        hide_index=True,
    )
    st.bar_chart(
        resumo_tribunal.set_index("Tribunal")["Quantidade"]
    )

with aba_cruzamento:
    cruzamento = pd.crosstab(
        df_consulta["Fornecedor"],
        df_consulta["Status"],
        margins=True,
        margins_name="Total",
    ).reset_index()
    st.dataframe(
        cruzamento,
        use_container_width=True,
        hide_index=True,
    )

st.subheader("💡 Insights")

contagem_fornecedor = df_consulta["Fornecedor"].value_counts(dropna=False)
contagem_status = df_consulta["Status"].value_counts(dropna=False)
contagem_tribunal = df_consulta["Tribunal"].value_counts(dropna=False)

fornecedor_principal = str(contagem_fornecedor.index[0])
qtd_fornecedor_principal = int(contagem_fornecedor.iloc[0])
status_principal = str(contagem_status.index[0])
qtd_status_principal = int(contagem_status.iloc[0])
tribunal_principal = str(contagem_tribunal.index[0])
qtd_tribunal_principal = int(contagem_tribunal.iloc[0])

percentual_fornecedor = qtd_fornecedor_principal / total_processos * 100
percentual_status = qtd_status_principal / total_processos * 100

insights = [
    f"O fornecedor com mais registros é **{fornecedor_principal}**, com "
    f"**{qtd_fornecedor_principal:,} processos** "
    f"({percentual_fornecedor:.1f}% do total).".replace(",", "."),
    f"O status mais frequente é **{status_principal}**, com "
    f"**{qtd_status_principal:,} processos** "
    f"({percentual_status:.1f}% do total).".replace(",", "."),
    f"O tribunal com mais registros é **{tribunal_principal}**, com "
    f"**{qtd_tribunal_principal:,} processos**.".replace(",", "."),
]

if total_sem_id:
    insights.append(
        f"Existem **{total_sem_id:,} processos sem ID Demanda**."
        .replace(",", ".")
    )

for insight in insights:
    st.markdown(f"- {insight}")

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
    f"{len(df_consulta):,} processos."
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
    ].head(500),
    use_container_width=True,
    hide_index=True,
)

if len(df_filtrado) > 500:
    st.caption(
        "A prévia mostra as primeiras 500 linhas. "
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
        "Nenhum processo corresponde aos filtros atuais. "
        "Ajuste os filtros para liberar a geração."
    )
else:
    status_nome = parametros["status_usuario"]
    arrendatario_nome = parametros["cd_arrendatario"]

    if organizacao == "Excel único":
        arquivo_saida = gerar_excel_unico(df_filtrado)
        nome_arquivo = limpar_nome_arquivo(
            f"{status_nome} - FILTRADO - {arrendatario_nome}.xlsx"
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
            f"{status_nome} - POR TRIBUNAL - {arrendatario_nome}.zip"
        )
        mime = "application/zip"
    elif organizacao == "Separar por fornecedor":
        arquivo_saida = gerar_zip_por_fornecedor(
            df_filtrado,
            status_nome,
            arrendatario_nome,
        )
        nome_arquivo = limpar_nome_arquivo(
            f"{status_nome} - POR FORNECEDOR - {arrendatario_nome}.zip"
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

    st.download_button(
        label=f"📥 Baixar {organizacao.lower()}",
        data=arquivo_saida.getvalue(),
        file_name=nome_arquivo,
        mime=mime,
        type="primary",
        use_container_width=True,
    )
