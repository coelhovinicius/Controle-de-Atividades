import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import unicodedata
import time
import os
import hmac
import secrets as pysecrets
from io import StringIO
from datetime import datetime, timedelta
from database_core import DatabaseConnection, LogRepository
from importer_core import HistoryParser

st.set_page_config(page_title="Task Tracker ", layout="wide", initial_sidebar_state="collapsed")

# ==========================================
# LOGIN
# ==========================================
# O PORQUE: o plano gratuito do Streamlit Community Cloud só permite 1 app
# privado por conta, e essa cota já está em uso por outro app. Em vez de
# depender do controle de acesso nativo do Streamlit (email convidado), o
# app fica "Public" no Streamlit, e o controle de acesso de verdade é feito
# aqui: nada do app roda (nem a conexão com o banco) até o login ser
# validado contra usuário/senha configurados nos Secrets.
#
# Configuração esperada em .streamlit/secrets.toml (local) e em
# Settings > Secrets (Community Cloud) -- veja secrets.toml.example:
#   [credentials]
#   "seu_usuario" = "sua_senha"
#   "outro_usuario" = "outra_senha"

# O PORQUE: st.session_state é por sessão de navegador e se perde ao
# recarregar a página (F5) -- por isso, sozinho, não sustenta "ficar logado
# até clicar em Sair". Este dicionário vive no nível do processo Python (não
# dentro de session_state), sobrevive a reruns/reloads de qualquer aba
# enquanto o servidor Streamlit continuar de pé, e guarda só um token
# aleatório -> {usuário, validade} (nunca a senha). O token vai para a URL
# (?s=...) para sobreviver a um F5; ao clicar em "Sair", o token é removido
# daqui -- então mesmo quem tiver guardado aquele link antigo não consegue
# mais entrar com ele.
_ACTIVE_SESSIONS = {}
SESSION_TTL_HOURS = 12


def _criar_sessao(username: str) -> str:
    token = pysecrets.token_urlsafe(32)
    _ACTIVE_SESSIONS[token] = {
        "username": username,
        "expires_at": datetime.now() + timedelta(hours=SESSION_TTL_HOURS),
    }
    return token


def _validar_sessao(token: str):
    info = _ACTIVE_SESSIONS.get(token)
    if not info:
        return None
    if datetime.now() > info["expires_at"]:
        _ACTIVE_SESSIONS.pop(token, None)
        return None
    return info["username"]


def _revogar_sessao(token: str):
    _ACTIVE_SESSIONS.pop(token, None)


def _validar_login(username: str, password: str) -> bool:
    try:
        credentials = st.secrets.get("credentials", {})
    except Exception:
        # O PORQUE: se .streamlit/secrets.toml ainda não existir (ex.: primeira
        # vez rodando localmente antes de configurar), st.secrets pode levantar
        # exceção em vez de simplesmente devolver vazio. Tratamos aqui para
        # mostrar uma mensagem clara em vez de um traceback confuso.
        credentials = {}

    if not credentials:
        st.error(
            "Nenhuma credencial configurada em `[credentials]` no secrets.toml. "
            "Veja `.streamlit/secrets.toml.example`."
        )
        return False

    if not username or username not in credentials:
        # O PORQUE: mesmo com usuário inexistente, ainda comparamos contra uma
        # senha "vazia" via compare_digest (em vez de retornar False na hora)
        # -- assim o tempo de resposta não denuncia se o usuário existe ou
        # não (mitigação simples de timing attack).
        hmac.compare_digest("", password or "")
        return False
    return hmac.compare_digest(str(credentials[username]), password or "")


def _limpar_sessao_local(revogar: bool = True):
    # O PORQUE: ao sair, apagamos TODO o session_state (não só a flag de
    # login) -- pesquisas, filtros de data do Dashboard, dados de
    # sincronização pendentes, edições em andamento, tudo. Assim, se outra
    # pessoa logar em seguida no mesmo navegador, não encontra nenhum
    # resquício da sessão anterior.
    if revogar:
        token = st.session_state.get("auth_token")
        if token:
            _revogar_sessao(token)
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.query_params.clear()
    st.session_state.authenticated = False


@st.dialog("Sair")
def _dialog_confirmar_logout():
    st.write("Tem certeza que deseja sair?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, sair", type="primary", use_container_width=True):
            with st.spinner("Encerrando sessão..."):
                _limpar_sessao_local()
            st.rerun()
    with col2:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()


def _tela_login():
    st.write("")
    st.write("")
    col_a, col_b, col_c = st.columns([1, 1.1, 1])
    with col_b:
        st.title("📊 Task Tracker")
        st.subheader("Acesso restrito")
        with st.form("login_form"):
            username = st.text_input("Usuário")
            password = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)

        if entrar:
            with st.spinner("Verificando credenciais..."):
                login_ok = _validar_login(username, password)
            if login_ok:
                token = _criar_sessao(username)
                st.session_state.authenticated = True
                st.session_state.auth_username = username
                st.session_state.auth_token = token
                st.query_params["s"] = token
                st.rerun()
            else:
                st.error("Usuário ou senha incorretos.")


if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

    # O PORQUE: primeira execução desta aba/sessão (ex.: acabou de dar F5).
    # Antes de exigir login de novo, verifica se a URL já traz um token de
    # uma sessão anterior ainda válida (?s=...) -- se validar, restaura o
    # login automaticamente, sem pedir usuário/senha de novo.
    qp_token = st.query_params.get("s")
    if qp_token:
        qp_username = _validar_sessao(qp_token)
        if qp_username:
            st.session_state.authenticated = True
            st.session_state.auth_username = qp_username
            st.session_state.auth_token = qp_token

if not st.session_state.authenticated:
    _tela_login()
    st.stop()

# O PORQUE: por padrão, o texto das abas do Streamlit sai pequeno e sem
# destaque visual, dificultando a navegação. Este CSS aumenta o tamanho da
# fonte e o peso do texto das abas. Os seletores cobrem tanto a estrutura
# mais recente do Streamlit (com <p> dentro do botão) quanto uma variação
# mais antiga, para o destaque funcionar independente da versão instalada.
st.markdown(
    """
    <style>
    .stTabs [data-baseweb="tab-list"] button {
        height: auto;
        padding: 14px 24px;
    }
    .stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p {
        font-size: 1.5rem;
        font-weight: 700;
    }
    .stTabs [data-baseweb="tab-list"] button div {
        font-size: 1.5rem;
        font-weight: 700;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# O PORQUE: o Streamlit já mostra um indicador nativo (o "stStatusWidget",
# um ícone pequeno no canto superior direito) enquanto o script está
# rodando, mas ele é discreto e não impede visualmente o usuário de tentar
# clicar em outra coisa enquanto isso. Este CSS: (1) deixa esse indicador
# nativo bem mais visível, com texto "Carregando..."; (2) usa o seletor
# :has() para desenhar um overlay escurecido cobrindo a tela inteira e
# bloquear cliques (pointer-events: none) em TUDO enquanto esse indicador
# estiver presente no DOM -- ou seja, enquanto o app estiver processando.
# ATENÇÃO: :has() exige um navegador razoavelmente recente (Chrome/Edge 105+,
# Firefox 121+); em navegadores muito antigos o overlay simplesmente não
# aparece (mas o app continua funcionando normalmente, só sem o bloqueio
# visual extra). Se o testid "stStatusWidget" mudar em versões futuras do
# Streamlit, este seletor pode parar de funcionar -- nesse caso, confira no
# DevTools (F12) qual testid o indicador de "running" usa na sua versão.
st.markdown(
    """
    <style>
    [data-testid="stStatusWidget"] {
        transform: scale(1.6);
        transform-origin: top right;
    }
    body:has([data-testid="stStatusWidget"]) {
        cursor: progress;
    }
    body:has([data-testid="stStatusWidget"])::after {
        content: "Carregando, aguarde...";
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.55);
        color: white;
        font-size: 1.8rem;
        font-weight: 700;
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 999999;
        pointer-events: all;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# O PORQUE: sem um mapa de cores fixo, cada grafico (px.bar, px.pie, px.line)
# atribui cores automaticamente na ordem em que os valores aparecem nos dados
# filtrados - por isso "Outros" podia sair azul num grafico e rosa em outro.
# Fixando aqui, o mesmo projeto/categoria tem sempre a mesma cor em qualquer
# grafico do app, inclusive nas barras do Pareto.
PROJECT_COLORS = {
    "Sustentacao": "#1f77b4",
    "Passaporte": "#ff7f0e",
    "360": "#2ca02c",
    "Job Boards": "#d62728",
    "Vagas": "#9467bd",
    "Motor RCE": "#8c564b",
    "Price Up": "#e377c2",
    "Backoffice": "#17becf",
    "Cockpit": "#bcbd22",
    "Outros": "#7f7f7f",
}

CATEGORY_COLORS = {
    "Desenvolvimento de Testes": "#1f77b4",
    "Execucao de Testes": "#ff7f0e",
    "Documentacao": "#2ca02c",
    "Reuniao": "#d62728",
    "Resolucao/Testes de BUG/Problema": "#9467bd",
    "Estudos/Certificacao": "#8c564b",
    "Outros": "#7f7f7f",
}

# O PORQUE: as listas de Projeto/Categoria antes eram fixas nos selectbox dos
# formulários. Agora são a base + o que o usuário cadastrar em "custom_options"
# (tabela no banco). "BASE_" porque continuam sendo o ponto de partida; a
# lista final é montada em runtime por get_project_options()/get_category_options().
# Backoffice e Cockpit entraram na base (e não como customizados) porque
# viraram projetos "oficiais" do parser de importação (import_history.py /
# importer_core.py), deixando de ser absorvidos por "360".
BASE_PROJECT_OPTIONS = ["Sustentacao", "Passaporte", "360", "Job Boards", "Vagas", "Motor RCE", "Price Up", "Backoffice", "Cockpit", "Outros"]
BASE_CATEGORY_OPTIONS = ["Desenvolvimento de Testes", "Execucao de Testes", "Documentacao", "Reuniao", "Resolucao/Testes de BUG/Problema", "Estudos/Certificacao"]

# O PORQUE: paleta de reserva para Projetos/Categorias criados pelo usuário,
# que não têm cor fixa definida em PROJECT_COLORS/CATEGORY_COLORS. Cicla pela
# lista caso existam mais itens customizados do que cores disponíveis.
EXTRA_COLOR_PALETTE = px.colors.qualitative.Set3


def build_color_map(base_map: dict, values) -> dict:
    color_map = dict(base_map)
    idx = 0
    for v in values:
        if v not in color_map:
            color_map[v] = EXTRA_COLOR_PALETTE[idx % len(EXTRA_COLOR_PALETTE)]
            idx += 1
    return color_map

# O PORQUE: Limite de upload em MB. O valor "oficial" (que barra o arquivo
# antes mesmo de chegar ao servidor) fica em .streamlit/config.toml
# (server.maxUploadSize). Essa constante e a checagem abaixo sao uma segunda
# camada de defesa: garante a mesma regra mesmo que o app rode em outro
# ambiente sem esse config.toml, e da uma mensagem de erro amigavel em
# portugues em vez do erro generico do Streamlit.
MAX_UPLOAD_SIZE_MB = 20
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


@st.cache_resource
def get_repository():
    db_conn = DatabaseConnection()
    return LogRepository(db_conn)


repo = get_repository()

if not repo.db_connection.using_turso and (os.environ.get("TURSO_DATABASE_URL") or os.environ.get("TURSO_AUTH_TOKEN")):
    # O PORQUE: só mostra este aviso se havia credencial configurada mas a
    # conexão caiu para o SQLite local mesmo assim -- isso é sério no
    # Streamlit Cloud (disco efêmero: os dados gravados "somem" no próximo
    # redeploy/sleep). Se não há credencial nenhuma (ex.: rodando local de
    # propósito), o fallback é esperado e não precisa alarmar ninguém.
    st.warning(
        "⚠️ Não foi possível conectar ao banco Turso -- o app está usando um banco local "
        "temporário. Se isto estiver rodando no Streamlit Cloud, os dados gravados agora "
        "**serão perdidos** no próximo deploy. Verifique TURSO_DATABASE_URL/TURSO_AUTH_TOKEN "
        "em Settings → Secrets (veja os logs em 'Manage app' para o motivo exato).",
        icon="⚠️",
    )


def _current_user() -> str:
    # O PORQUE: ponto único usado em toda consulta/gravação no banco para
    # saber "de quem" são os dados. Se por algum motivo ainda não houver
    # usuário autenticado neste ponto do código (não deveria acontecer, já
    # que o app para em st.stop() antes do login), cai em string vazia em
    # vez de None -- assim uma consulta "WHERE username = ''" não devolve
    # os dados de ninguém, em vez de quebrar com um erro de tipo.
    return st.session_state.get("auth_username") or ""


def get_project_options() -> list:
    # O PORQUE: mantém "Outros" sempre por último (comportamento original),
    # inserindo os projetos customizados logo antes dele. Cada usuário só vê
    # (e só pode gerenciar) os projetos customizados que ele mesmo criou.
    custom = [p for p in repo.get_custom_options("project", _current_user()) if p not in BASE_PROJECT_OPTIONS]
    return BASE_PROJECT_OPTIONS[:-1] + custom + [BASE_PROJECT_OPTIONS[-1]]


def get_category_options() -> list:
    custom = [c for c in repo.get_custom_options("category", _current_user()) if c not in BASE_CATEGORY_OPTIONS]
    return BASE_CATEGORY_OPTIONS + custom


# O PORQUE: opção especial no fim dos dropdowns de Projeto/Categoria dos
# formulários de Registro/Edição. Ao escolhê-la, um campo de texto aparece
# na hora para o usuário digitar um nome novo -- que é criado e persistido
# em custom_options assim que confirmado (Enter/Tab), ficando disponível
# nesse e em qualquer registro futuro, inclusive após sincronização via
# upload de txt/csv (que só mexe em work_logs, nunca em custom_options).
NEW_OPTION_SENTINEL = "➕ Criar novo..."


def creatable_option_picker(label: str, option_type: str, options_fn, key_prefix: str, current_value: str = None):
    # O PORQUE: precisa ficar FORA de qualquer st.form. Dentro de um form,
    # widgets só reagem no submit -- aqui, ao escolher "Criar novo...", o
    # campo de texto tem que aparecer imediatamente, e confirmar o texto já
    # precisa gravar a nova opção e voltar a mostrar o dropdown com o valor
    # recém-criado selecionado, tudo sem esperar o usuário clicar em Salvar.
    options = list(options_fn())
    if current_value and current_value not in options:
        options.append(current_value)
    display_options = options + [NEW_OPTION_SENTINEL]

    select_key = f"{key_prefix}_select"
    text_key = f"{key_prefix}_new_text"

    if select_key not in st.session_state:
        st.session_state[select_key] = current_value if current_value in display_options else display_options[0]

    def _stage_new_option_confirmation():
        # O PORQUE: callback do on_change do text_input. Em vez de gravar
        # direto, abre a confirmação -- a criação de fato só acontece se o
        # usuário confirmar no modal (ver dispatcher de 'processing_action').
        typed = st.session_state.get(text_key, "").strip()
        if not typed:
            return
        request_confirmation(
            action_type="add_custom_option_inline",
            payload={"option_type": option_type, "value": typed, "select_key": select_key, "text_key": text_key},
            title=f"Criar novo(a) {label.lower()}",
            message=f'Deseja criar o(a) {label.lower()} "{typed}" e usá-lo(a) neste registro?',
            success_message=f'{label} "{typed}" criado(a) com sucesso!',
            processing_message=f"Criando {label.lower()}...",
            confirm_label="Sim, criar",
            on_cancel_cleanup={text_key: ""},
        )

    choice = st.selectbox(label, display_options, key=select_key)

    if choice == NEW_OPTION_SENTINEL:
        st.text_input(
            f"Digite o novo nome e pressione Enter",
            key=text_key,
            on_change=_stage_new_option_confirmation,
        )
        return None

    return choice

# O PORQUE: Sessões adicionadas para gerenciar os DataFrames temporários de sincronização (Diffing) sem perder o estado a cada interação com os checkboxes.
if 'action_state' not in st.session_state:
    st.session_state.action_state = 'idle'
if 'target_id' not in st.session_state:
    st.session_state.target_id = None
if 'view_state' not in st.session_state:
    st.session_state.view_state = 'grid'
if 'confirm_state' not in st.session_state:
    st.session_state.confirm_state = None
if 'pending_data' not in st.session_state:
    st.session_state.pending_data = {}
if 'current_page' not in st.session_state:
    st.session_state.current_page = 1
if 'search_term' not in st.session_state:
    st.session_state.search_term = ""
if 'sync_analyzed' not in st.session_state:
    st.session_state.sync_analyzed = False
if 'df_to_insert' not in st.session_state:
    st.session_state.df_to_insert = pd.DataFrame()
if 'df_to_delete' not in st.session_state:
    st.session_state.df_to_delete = pd.DataFrame()
if 'dashboard_filters_applied' not in st.session_state:
    st.session_state.dashboard_filters_applied = False
if 'sync_file_name' not in st.session_state:
    st.session_state.sync_file_name = "raw_history.txt"
# O PORQUE: guarda qual coluna da tabela (ID, Data, Projeto ou Categoria) e em
# qual direção (asc/desc) o usuário escolheu ordenar, clicando no cabeçalho.
if 'sort_column' not in st.session_state:
    st.session_state.sort_column = None
if 'sort_ascending' not in st.session_state:
    st.session_state.sort_ascending = True
if 'daily_report' not in st.session_state:
    st.session_state.daily_report = None
# O PORQUE: sistema genérico de confirmação + "processando" usado por todas
# as ações que escrevem no banco (Projetos/Categorias customizados e, agora
# também, os registros de atividade). 'confirm_action' guarda os dados de
# uma confirmação pendente (título/mensagem/o que fazer se confirmado);
# 'processing' + 'processing_action' guardam a ação já confirmada, aguardando
# ser executada no próximo rerun -- é esse rerun intermediário que garante
# que a tela de "Processando..." (sem nenhum outro widget interativo) seja
# de fato desenhada ANTES da escrita no banco acontecer.
if 'confirm_action' not in st.session_state:
    st.session_state.confirm_action = None
if 'processing' not in st.session_state:
    st.session_state.processing = False
if 'processing_action' not in st.session_state:
    st.session_state.processing_action = None

# O PORQUE: mapeia o rótulo exibido no cabeçalho para o nome real da coluna
# no DataFrame, usado tanto para renderizar o botão quanto para ordenar.
SORTABLE_COLUMNS = {
    "ID": "id",
    "Data": "log_date",
}


def reset_states(full_reset=False):
    st.session_state.confirm_state = None
    st.session_state.pending_data = {}
    if full_reset:
        st.session_state.view_state = 'grid'
        st.session_state.target_id = None
        # O PORQUE: sem isso, o dropdown de Projeto/Categoria do próximo
        # "Novo Registro" reabriria com a última seleção (ou o texto digitado
        # para criar uma opção nova) ainda preenchidos, por causa da key
        # fixa do widget persistindo em st.session_state entre execuções.
        for k in ("add_proj_select", "add_proj_new_text", "add_cat_select", "add_cat_new_text"):
            st.session_state.pop(k, None)


# ==========================================
# CONFIRMAÇÃO + "PROCESSANDO..." (genérico)
# ==========================================
def render_processing_overlay(message: str = "Processando..."):
    # O PORQUE: um <div> fixed cobrindo 100% da tela, com z-index alto,
    # transmite visualmente o "sombreamento" pedido. A blindagem de verdade
    # contra interação vem de como essa função é usada: enquanto
    # st.session_state.processing for True, o script SÓ desenha isso (ver
    # bloco mais abaixo) -- nenhum outro botão/campo é renderizado nessa
    # execução, então não existe o que clicar.
    st.markdown(
        f"""
        <div style="
            position: fixed; inset: 0; width: 100vw; height: 100vh;
            background: rgba(15, 17, 22, 0.55); backdrop-filter: blur(2px);
            z-index: 999999; display: flex; flex-direction: column;
            align-items: center; justify-content: center;
        ">
            <div style="
                border: 6px solid rgba(255,255,255,0.25); border-top: 6px solid #ffffff;
                border-radius: 50%; width: 54px; height: 54px;
                animation: app-spin 0.8s linear infinite;
            "></div>
            <div style="margin-top: 18px; font-size: 1.05rem; font-weight: 600; color: #ffffff;">
                {message}
            </div>
        </div>
        <style>
        @keyframes app-spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def request_confirmation(action_type: str, payload: dict, title: str, message: str,
                          success_message: str, processing_message: str = "Processando...",
                          confirm_label: str = "Sim, confirmar", on_cancel_cleanup: dict = None):
    # O PORQUE: guarda a intenção de escrita (o quê + com quais dados) em vez
    # de já executá-la. Só quando o usuário confirmar no modal é que ela vira
    # 'processing_action' e é de fato executada (ver dispatcher abaixo).
    st.session_state.confirm_action = {
        "type": action_type,
        "payload": payload,
        "title": title,
        "message": message,
        "success_message": success_message,
        "processing_message": processing_message,
        "confirm_label": confirm_label,
        "on_cancel_cleanup": on_cancel_cleanup or {},
    }
    st.rerun()


def render_pending_confirmation():
    action = st.session_state.confirm_action

    @st.dialog(action["title"])
    def _dialog():
        st.write(action["message"])
        col1, col2 = st.columns(2)
        with col1:
            if st.button(action["confirm_label"], type="primary", use_container_width=True):
                st.session_state.processing = True
                st.session_state.processing_action = action
                st.session_state.confirm_action = None
                st.rerun()
        with col2:
            if st.button("Cancelar", use_container_width=True):
                # O PORQUE: limpa campos de texto/seleção deixados pendentes
                # (ex.: o nome digitado no dropdown "Criar novo...") para a
                # tela voltar ao estado anterior à tentativa de confirmação.
                for k, v in action.get("on_cancel_cleanup", {}).items():
                    st.session_state[k] = v
                st.session_state.confirm_action = None
                st.rerun()

    _dialog()


def execute_processing_action(action: dict) -> bool:
    t = action["type"]
    p = action["payload"]
    username = _current_user()

    if t == "add_custom_option":
        ok = repo.add_custom_option(p["option_type"], username, p["value"])
        st.session_state[f"manage_{p['option_type']}_new"] = ""
        return ok

    elif t == "rename_custom_option":
        ok = repo.rename_custom_option(p["option_type"], username, p["old_value"], p["new_value"])
        if ok:
            st.session_state[f"manage_{p['option_type']}_select"] = p["new_value"]
        return ok

    elif t == "delete_custom_option":
        repo.delete_custom_option(p["option_type"], username, p["value"])
        # O PORQUE: o valor excluído não existe mais na lista de opções --
        # sem isso, o selectbox da sidebar (que guarda a seleção pela key)
        # tentaria reabrir com um valor inválido e quebraria.
        st.session_state.pop(f"manage_{p['option_type']}_select", None)
        return True

    elif t == "add_custom_option_inline":
        ok = repo.add_custom_option(p["option_type"], username, p["value"])
        if ok:
            st.session_state[p["select_key"]] = p["value"]
        st.session_state[p["text_key"]] = ""
        return ok

    elif t == "insert_log":
        d = p
        repo.insert_log(username, d['date'], d['proj'], d['cat'], d['desc'], d['eff'], d.get('imp', False), d.get('duv', False))
        reset_states(full_reset=True)
        return True

    elif t == "update_log":
        d = p
        repo.update_log(d['target_id'], username, d['date'], d['proj'], d['cat'], d['desc'], d['eff'], d.get('imp', False), d.get('duv', False))
        reset_states(full_reset=True)
        return True

    elif t == "delete_log":
        repo.delete_log(p["log_id"], username)
        reset_states(full_reset=True)
        return True

    return True


if st.session_state.processing:
    # O PORQUE: nada além do overlay é desenhado nesta execução -- é isso
    # que "impossibilita qualquer tipo de interação" enquanto processa, e não
    # apenas um efeito visual por cima de botões que ainda poderiam ser
    # clicados.
    action = st.session_state.processing_action
    render_processing_overlay(action.get("processing_message", "Processando..."))
    ok = execute_processing_action(action)
    st.session_state.processing = False
    st.session_state.processing_action = None
    if ok:
        st.toast(action["success_message"], icon="✅")
    else:
        st.toast(
            action.get("failure_message", "Não foi possível concluir: nome inválido ou já existente."),
            icon="⚠️",
        )
    time.sleep(0.6)
    st.rerun()

if st.session_state.confirm_action:
    render_pending_confirmation()


def remove_accents(input_str: str) -> str:
    if pd.isna(input_str):
        return ""
    nfkd_form = unicodedata.normalize('NFKD', str(input_str))
    return u"".join([c for c in nfkd_form if not unicodedata.combining(c)])


def format_date_ptbr(iso_date: str) -> str:
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return iso_date


def build_daily_suggestion(df_all: pd.DataFrame, date_from, date_to, flag_col: str) -> str:
    # O PORQUE: gera o texto padrão de Impedimentos/Dúvidas a partir dos
    # registros já marcados com a flag correspondente (is_impedimento /
    # is_duvida), dentro do intervalo [date_from, date_to] selecionado na
    # Daily. A flag vem de duas origens possíveis: marcada manualmente no
    # checkbox do formulário, ou inferida automaticamente na importação de
    # arquivo (.txt/.csv) via keywords.
    empty_default = "Nenhum." if flag_col == "is_impedimento" else "Nenhuma."
    if df_all.empty or flag_col not in df_all.columns:
        return empty_default

    df_tmp = df_all.copy()
    df_tmp["log_date_dt"] = pd.to_datetime(df_tmp["log_date"]).dt.date
    mask = (
        (df_tmp["log_date_dt"] >= date_from)
        & (df_tmp["log_date_dt"] <= date_to)
        & (df_tmp[flag_col].astype(int) == 1)
    )
    flagged = df_tmp.loc[mask]

    if flagged.empty:
        return empty_default

    lines = [f"- [{row['project']}] {row['description']}" for _, row in flagged.iterrows()]
    return "\n".join(lines)


def validar_formulario_atividade(descricao: str, esforco_horas) -> list:
    # O PORQUE: todos os campos do formulário de Registro de Atividades são
    # obrigatórios. Data, Projeto e Categoria já vêm sempre preenchidos
    # (date_input e selectbox nunca ficam vazios), então a validação real
    # que falta é: descrição não pode ficar em branco, e o esforço em horas
    # precisa ser maior que zero (0h não representa uma atividade de fato).
    erros = []
    if not descricao or not descricao.strip():
        erros.append("O campo **Descrição da Atividade** é obrigatório.")
    if esforco_horas is None or esforco_horas <= 0:
        erros.append("Informe um **Esforço (Horas)** maior que zero.")
    return erros


# ==========================================
# MODAIS DE CONFIRMAÇÃO
# ==========================================
# O PORQUE: confirmações passaram a ser feitas em modal (st.dialog) em vez de
# substituir a tela inteira por uma "página" de confirmação. Assim, o
# usuário continua vendo o formulário ou a listagem ao fundo, e só decide
# sim/não numa janela sobreposta, sem perder o contexto do que estava fazendo.
@st.dialog("Excluir registro")
def dialog_confirmar_exclusao():
    st.write(f"Tem certeza que deseja excluir o registro **ID {st.session_state.target_id}**?")
    st.caption("Essa ação não pode ser desfeita.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, excluir", type="primary", use_container_width=True):
            st.session_state.processing = True
            st.session_state.processing_action = {
                "type": "delete_log",
                "payload": {"log_id": st.session_state.target_id},
                "success_message": "Registro excluído com sucesso!",
                "processing_message": "Excluindo registro...",
            }
            st.session_state.confirm_state = None
            st.rerun()
    with col2:
        if st.button("Cancelar", use_container_width=True):
            reset_states(full_reset=True)
            st.rerun()


@st.dialog("Salvar novo registro")
def dialog_confirmar_novo_registro():
    st.write("Tem certeza que deseja salvar este novo registro?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, salvar", type="primary", use_container_width=True):
            st.session_state.processing = True
            st.session_state.processing_action = {
                "type": "insert_log",
                "payload": dict(st.session_state.pending_data),
                "success_message": "Registro salvo com sucesso!",
                "processing_message": "Salvando registro...",
            }
            st.session_state.confirm_state = None
            st.rerun()
    with col2:
        if st.button("Voltar", use_container_width=True):
            st.session_state.confirm_state = None
            st.rerun()


@st.dialog("Descartar novo registro")
def dialog_confirmar_descarte_novo():
    st.write("Tem certeza que deseja descartar este novo registro? As informações preenchidas serão perdidas.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, descartar", type="primary", use_container_width=True):
            reset_states(full_reset=True)
            st.rerun()
    with col2:
        if st.button("Voltar", use_container_width=True):
            st.session_state.confirm_state = None
            st.rerun()


@st.dialog("Salvar alterações")
def dialog_confirmar_edicao():
    st.write("Tem certeza que deseja salvar as alterações neste registro?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, salvar", type="primary", use_container_width=True):
            payload = dict(st.session_state.pending_data)
            payload['target_id'] = st.session_state.target_id
            st.session_state.processing = True
            st.session_state.processing_action = {
                "type": "update_log",
                "payload": payload,
                "success_message": "Registro atualizado com sucesso!",
                "processing_message": "Salvando alterações...",
            }
            st.session_state.confirm_state = None
            st.rerun()
    with col2:
        if st.button("Voltar", use_container_width=True):
            st.session_state.confirm_state = None
            st.rerun()


@st.dialog("Descartar alterações")
def dialog_confirmar_descarte_edicao():
    st.write("Tem certeza que deseja descartar as alterações feitas?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, descartar", type="primary", use_container_width=True):
            reset_states(full_reset=True)
            st.rerun()
    with col2:
        if st.button("Voltar", use_container_width=True):
            st.session_state.confirm_state = None
            st.rerun()


# ==========================================
# SIDEBAR: GERENCIAR PROJETOS E CATEGORIAS
# ==========================================
# O PORQUE: fica na sidebar (e não dentro de uma aba específica) para estar
# disponível em qualquer tela, já que Projeto/Categoria são usados tanto no
# Registro de Atividades quanto nos filtros do Dashboard.
def _manage_options_panel(label_singular: str, option_type: str, base_options: list, get_options_fn):
    # O PORQUE: painel único reaproveitado para Projeto e Categoria. Mostra
    # um dropdown com TODOS os nomes cadastrados (padrão do sistema +
    # customizados) para consulta/seleção, com ações de editar (renomear) e
    # excluir para os customizados, além de um campo para incluir novos.
    # Os nomes "padrão" (BASE_PROJECT_OPTIONS/BASE_CATEGORY_OPTIONS) também
    # alimentam PROJECT_KEYWORDS/CATEGORY_KEYWORDS (parser de importação) e o
    # mapa de cores fixo dos gráficos -- por isso ficam visíveis aqui para
    # consulta, mas só os customizados podem ser renomeados/excluídos.
    all_options = get_options_fn()
    custom_options = repo.get_custom_options(option_type, _current_user())

    picked = st.selectbox(
        f"{label_singular}s cadastrados",
        all_options,
        key=f"manage_{option_type}_select",
    )
    is_custom = picked in custom_options

    if is_custom:
        col_edit, col_del = st.columns([3, 1])
        new_name = col_edit.text_input(
            "Renomear para",
            value=picked,
            key=f"manage_{option_type}_rename_{picked}",
            label_visibility="collapsed",
        )
        if col_edit.button("💾 Salvar novo nome", key=f"manage_{option_type}_rename_btn_{picked}", use_container_width=True):
            new_name_clean = (new_name or "").strip()
            if not new_name_clean or new_name_clean == picked:
                st.warning("Digite um nome novo e diferente do atual.")
            else:
                request_confirmation(
                    action_type="rename_custom_option",
                    payload={"option_type": option_type, "old_value": picked, "new_value": new_name_clean},
                    title=f"Renomear {label_singular.lower()}",
                    message=(
                        f'Deseja renomear "{picked}" para "{new_name_clean}"? '
                        f"Os registros que já usam \"{picked}\" serão atualizados para o novo nome."
                    ),
                    success_message=f'"{picked}" renomeado(a) para "{new_name_clean}" com sucesso!',
                    processing_message=f"Renomeando {label_singular.lower()}...",
                    confirm_label="Sim, renomear",
                )
        if col_del.button("🗑️", key=f"manage_{option_type}_del_{picked}", help=f"Excluir '{picked}' (não apaga registros já usando este {label_singular.lower()})", use_container_width=True):
            request_confirmation(
                action_type="delete_custom_option",
                payload={"option_type": option_type, "value": picked},
                title=f"Excluir {label_singular.lower()}",
                message=(
                    f'Tem certeza que deseja excluir "{picked}"? Essa ação não pode ser desfeita '
                    f"(registros que já usam este nome não serão apagados)."
                ),
                success_message=f'"{picked}" excluído(a) com sucesso!',
                processing_message=f"Excluindo {label_singular.lower()}...",
                confirm_label="Sim, excluir",
            )
    else:
        st.caption(f"'{picked}' é um {label_singular.lower()} padrão do sistema e não pode ser editado ou excluído.")

    st.markdown("---")
    st.caption(f"Incluir novo {label_singular.lower()}:")
    new_value = st.text_input(f"Nome do(a) novo(a) {label_singular.lower()}", key=f"manage_{option_type}_new", label_visibility="collapsed")
    if st.button(f"➕ Adicionar {label_singular.lower()}", key=f"manage_{option_type}_add_btn", use_container_width=True):
        new_value_clean = (new_value or "").strip()
        if not new_value_clean:
            st.error("Digite um nome válido.")
        elif new_value_clean in all_options:
            st.warning(f"Esse {label_singular.lower()} já existe.")
        else:
            request_confirmation(
                action_type="add_custom_option",
                payload={"option_type": option_type, "value": new_value_clean},
                title=f"Criar novo(a) {label_singular.lower()}",
                message=f'Deseja criar o(a) {label_singular.lower()} "{new_value_clean}"?',
                success_message=f'"{new_value_clean}" criado(a) com sucesso!',
                processing_message=f"Criando {label_singular.lower()}...",
                confirm_label="Sim, criar",
            )


with st.sidebar:
    st.caption(f"Logado como **{st.session_state.get('auth_username', '')}**")
    if st.button("🚪 Sair", use_container_width=True):
        _dialog_confirmar_logout()
    st.markdown("---")

with st.sidebar:
    st.subheader("⚙️ Projetos e Categorias")

    with st.expander("📁 Novo Nome de Projeto"):
        _manage_options_panel("Projeto", "project", BASE_PROJECT_OPTIONS, get_project_options)

    with st.expander("🏷️ Novo Nome de Categoria"):
        _manage_options_panel("Categoria", "category", BASE_CATEGORY_OPTIONS, get_category_options)

st.title("📊 Task Tracker")

tab_manage, tab_daily, tab_dashboard, tab_sync = st.tabs(["Registro de Atividades", "Daily Scrum", "Dashboard & Relatórios", "Sincronização de Arquivo"])

# ==========================================
# TAB 1: REGISTRO DE ATIVIDADES (GRID & CRUD)
# ==========================================
with tab_manage:
    # O PORQUE: os modais são disparados aqui, no topo da aba, mas a
    # tela de fundo (listagem ou formulário) continua sendo renderizada
    # normalmente logo abaixo — é assim que o modal "flutua" sobre o
    # conteúdo em vez de substituí-lo por completo.
    if st.session_state.confirm_state == 'delete':
        dialog_confirmar_exclusao()
    elif st.session_state.confirm_state == 'save_add':
        dialog_confirmar_novo_registro()
    elif st.session_state.confirm_state == 'cancel_add':
        dialog_confirmar_descarte_novo()
    elif st.session_state.confirm_state == 'save_edit':
        dialog_confirmar_edicao()
    elif st.session_state.confirm_state == 'cancel_edit':
        dialog_confirmar_descarte_edicao()

    if st.session_state.view_state == 'grid':
        col_header, col_add = st.columns([5, 1])
        with col_header:
            st.header("Suas Atividades")
        with col_add:
            st.write("")
            if st.button("➕ Novo Registro", use_container_width=True, type="primary"):
                st.session_state.view_state = 'add'
                reset_states(full_reset=False)
                st.rerun()

        df_all = repo.get_all_logs_as_dataframe(_current_user())

        if df_all.empty:
            st.info("Você ainda não tem nenhum registro cadastrado. Clique em **Novo Registro** para começar.")
        else:
            col_search, col_clear = st.columns([4, 1])
            with col_search:
                search_input = st.text_input("🔍 Buscar (ignora acentos e maiúsculas)", value=st.session_state.search_term)
            with col_clear:
                st.write("")
                st.write("")
                if st.button("Limpar Busca", use_container_width=True):
                    st.session_state.search_term = ""
                    st.session_state.current_page = 1
                    st.rerun()

            if search_input != st.session_state.search_term:
                st.session_state.search_term = search_input
                st.session_state.current_page = 1
                st.rerun()

            if st.session_state.search_term:
                search_term_clean = remove_accents(st.session_state.search_term).lower()
                text_series = df_all.astype(str).apply(lambda x: ' '.join(x), axis=1)
                text_series_clean = text_series.apply(remove_accents).str.lower()
                df_display = df_all[text_series_clean.str.contains(search_term_clean, na=False)].copy()
            else:
                df_display = df_all.copy()

            # O PORQUE: aplica a ordenação clicada pelo usuário no cabeçalho
            # (ID, Data, Projeto ou Categoria) antes de paginar, para que a
            # ordem escolhida valha sobre o conjunto filtrado inteiro, e não
            # só sobre a página atual.
            if st.session_state.sort_column:
                df_display = df_display.sort_values(
                    by=st.session_state.sort_column,
                    ascending=st.session_state.sort_ascending,
                    kind="stable",
                )

            total_records = len(df_display)

            if total_records == 0:
                st.warning("Nenhum registro encontrado para essa busca.")
            else:
                col_per_page, col_page_jump, _ = st.columns([1, 1, 4])
                with col_per_page:
                    items_per_page = st.selectbox("Registros por página", [10, 25, 50, 100], index=1)

                total_pages = max(1, (total_records + items_per_page - 1) // items_per_page)
                if st.session_state.current_page > total_pages:
                    st.session_state.current_page = total_pages

                with col_page_jump:
                    jump_page = st.number_input("Ir para a página", min_value=1, max_value=total_pages, value=st.session_state.current_page)
                    if jump_page != st.session_state.current_page:
                        st.session_state.current_page = jump_page
                        st.rerun()

                col_prev, col_info, col_next, _ = st.columns([1, 2, 1, 4])
                with col_prev:
                    if st.button("⬅️ Anterior", disabled=(st.session_state.current_page == 1), use_container_width=True):
                        st.session_state.current_page -= 1
                        st.rerun()
                with col_info:
                    st.markdown(f"<div style='text-align: center; padding-top: 5px; font-weight: bold;'>Página {st.session_state.current_page} de {total_pages} (Total: {total_records})</div>", unsafe_allow_html=True)
                with col_next:
                    if st.button("Próximo ➡️", disabled=(st.session_state.current_page == total_pages), use_container_width=True):
                        st.session_state.current_page += 1
                        st.rerun()

                st.markdown("---")
                grid_cols = st.columns([0.5, 1, 1.5, 2, 4, 1, 1.5])
                headers = ["ID", "Data", "Projeto", "Categoria", "Descrição", "Horas", "Ações"]
                for col, header in zip(grid_cols, headers):
                    if header in SORTABLE_COLUMNS:
                        col_key = SORTABLE_COLUMNS[header]
                        # O PORQUE: a seta indica visualmente qual coluna está
                        # ordenando a tabela no momento e em qual direção.
                        if st.session_state.sort_column == col_key:
                            arrow = " 🔼" if st.session_state.sort_ascending else " 🔽"
                        else:
                            arrow = ""
                        if col.button(f"{header}{arrow}", key=f"sort_btn_{col_key}", use_container_width=True):
                            if st.session_state.sort_column == col_key:
                                # O PORQUE: clicar de novo na mesma coluna
                                # inverte a direção (asc -> dsc -> asc ...).
                                st.session_state.sort_ascending = not st.session_state.sort_ascending
                            else:
                                st.session_state.sort_column = col_key
                                st.session_state.sort_ascending = True
                            st.session_state.current_page = 1
                            st.rerun()
                    else:
                        col.markdown(f"**{header}**")

                start_idx = (st.session_state.current_page - 1) * items_per_page
                end_idx = start_idx + items_per_page
                df_page = df_display.iloc[start_idx:end_idx]

                for _, row in df_page.iterrows():
                    cols = st.columns([0.5, 1, 1.5, 2, 4, 1, 1.5])
                    cols[0].write(str(row["id"]))
                    cols[1].write(format_date_ptbr(row["log_date"]))
                    cols[2].write(row["project"])
                    cols[3].write(row["category"])
                    # O PORQUE: prefixo visual (não altera o texto salvo) para
                    # identificar de relance quais registros estão marcados
                    # como Impedimento e/ou Dúvida sem precisar abrir cada um.
                    flag_prefix = ""
                    if bool(int(row.get("is_impedimento", 0) or 0)):
                        flag_prefix += "🚧 "
                    if bool(int(row.get("is_duvida", 0) or 0)):
                        flag_prefix += "❓ "
                    cols[4].write(f"{flag_prefix}{row['description']}")
                    cols[5].write(str(row["effort_hours"]))

                    with cols[6]:
                        btn_col1, btn_col2 = st.columns(2)
                        with btn_col1:
                            if st.button("✏️", key=f"edit_{row['id']}", help="Editar"):
                                st.session_state.target_id = row['id']
                                st.session_state.view_state = 'edit'
                                st.rerun()
                        with btn_col2:
                            if st.button("🗑️", key=f"del_{row['id']}", help="Excluir"):
                                st.session_state.target_id = row['id']
                                st.session_state.confirm_state = 'delete'
                                st.rerun()
                st.markdown("---")

    if st.session_state.view_state == 'add':
        st.header("Novo Registro")
        st.caption(
            "Todos os campos abaixo são obrigatórios. Em Projeto/Categoria, escolha "
            "\"➕ Criar novo...\" para digitar (e já criar) um nome novo na hora."
        )
        # O PORQUE: este bloco NÃO usa st.form -- Projeto/Categoria precisam
        # reagir imediatamente (mostrar o campo de texto ao escolher "Criar
        # novo...", e voltar ao dropdown assim que o nome for confirmado),
        # o que só funciona fora de um form. As confirmações continuam
        # existindo do mesmo jeito, via modal (st.dialog), como no resto do app.
        col_d, col_p, col_c, col_e = st.columns(4)
        with col_d:
            log_date = st.date_input("Data (DD/MM/AAAA)", format="DD/MM/YYYY", key="add_log_date")
        with col_p:
            project = creatable_option_picker("Projeto", "project", get_project_options, key_prefix="add_proj")
        with col_c:
            category = creatable_option_picker("Categoria", "category", get_category_options, key_prefix="add_cat")
        with col_e:
            effort_hours = st.number_input("Esforço (Horas)", min_value=0.0, step=0.5, value=1.0, key="add_effort")

        description = st.text_area("Descrição da Atividade *", key="add_description")

        col_imp, col_duv = st.columns(2)
        with col_imp:
            is_impedimento = st.checkbox("🚧 É um impedimento?", key="add_is_impedimento")
        with col_duv:
            is_duvida = st.checkbox("❓ É uma dúvida?", key="add_is_duvida")

        col_save, col_canc = st.columns(2)
        with col_save:
            btn_save = st.button("Salvar Registro", type="primary", use_container_width=True, key="add_btn_save")
        with col_canc:
            btn_canc = st.button("Cancelar", use_container_width=True, key="add_btn_canc")

        if btn_save:
            erros = validar_formulario_atividade(description, effort_hours)
            if not project:
                erros.append("Selecione um **Projeto** ou digite e confirme um nome novo.")
            if not category:
                erros.append("Selecione uma **Categoria** ou digite e confirme um nome novo.")
            if erros:
                for erro in erros:
                    st.error(erro)
            else:
                st.session_state.pending_data = {
                    'date': str(log_date), 'proj': project, 'cat': category, 'desc': description, 'eff': effort_hours,
                    'imp': is_impedimento, 'duv': is_duvida,
                }
                st.session_state.confirm_state = 'save_add'
                st.rerun()
        if btn_canc:
            st.session_state.confirm_state = 'cancel_add'
            st.rerun()

    if st.session_state.view_state == 'edit' and st.session_state.target_id:
        st.header(f"Editar Registro (ID {st.session_state.target_id})")
        st.caption(
            "Todos os campos abaixo são obrigatórios. Em Projeto/Categoria, escolha "
            "\"➕ Criar novo...\" para digitar (e já criar) um nome novo na hora."
        )
        df_target = repo.get_all_logs_as_dataframe(_current_user())
        target_row = df_target[df_target['id'] == st.session_state.target_id].iloc[0]
        # O PORQUE: assim como no "Novo Registro", este bloco não usa st.form
        # para permitir que os dropdowns de Projeto/Categoria reajam na hora
        # a "Criar novo...". As chaves dos widgets incluem o target_id para
        # não herdar a seleção deixada pela edição de um registro anterior.
        edit_key_suffix = st.session_state.target_id

        col_d, col_p, col_c, col_e = st.columns(4)
        with col_d:
            parsed_date = datetime.strptime(target_row["log_date"], "%Y-%m-%d").date()
            log_date = st.date_input("Data (DD/MM/AAAA)", value=parsed_date, format="DD/MM/YYYY", key=f"edit_log_date_{edit_key_suffix}")
        with col_p:
            project = creatable_option_picker(
                "Projeto", "project", get_project_options,
                key_prefix=f"edit_proj_{edit_key_suffix}", current_value=target_row["project"],
            )
        with col_c:
            category = creatable_option_picker(
                "Categoria", "category", get_category_options,
                key_prefix=f"edit_cat_{edit_key_suffix}", current_value=target_row["category"],
            )
        with col_e:
            effort_hours = st.number_input(
                "Esforço (Horas)", min_value=0.0, step=0.5, value=float(target_row["effort_hours"]),
                key=f"edit_effort_{edit_key_suffix}",
            )

        description = st.text_area("Descrição da Atividade *", value=target_row["description"], key=f"edit_description_{edit_key_suffix}")

        col_imp, col_duv = st.columns(2)
        with col_imp:
            is_impedimento = st.checkbox(
                "🚧 É um impedimento?", value=bool(int(target_row.get("is_impedimento", 0) or 0)),
                key=f"edit_is_impedimento_{edit_key_suffix}",
            )
        with col_duv:
            is_duvida = st.checkbox(
                "❓ É uma dúvida?", value=bool(int(target_row.get("is_duvida", 0) or 0)),
                key=f"edit_is_duvida_{edit_key_suffix}",
            )

        col_save, col_canc = st.columns(2)
        with col_save:
            btn_save = st.button("Salvar Alterações", type="primary", use_container_width=True, key=f"edit_btn_save_{edit_key_suffix}")
        with col_canc:
            btn_canc = st.button("Cancelar", use_container_width=True, key=f"edit_btn_canc_{edit_key_suffix}")

        if btn_save:
            erros = validar_formulario_atividade(description, effort_hours)
            if not project:
                erros.append("Selecione um **Projeto** ou digite e confirme um nome novo.")
            if not category:
                erros.append("Selecione uma **Categoria** ou digite e confirme um nome novo.")
            if erros:
                for erro in erros:
                    st.error(erro)
            else:
                st.session_state.pending_data = {
                    'date': str(log_date), 'proj': project, 'cat': category, 'desc': description, 'eff': effort_hours,
                    'imp': is_impedimento, 'duv': is_duvida,
                }
                st.session_state.confirm_state = 'save_edit'
                st.rerun()
        if btn_canc:
            st.session_state.confirm_state = 'cancel_edit'
            st.rerun()

# ==========================================
# TAB 2: DAILY SCRUM
# ==========================================
with tab_daily:
    st.header("Resumo para a Daily")
    st.caption(
        "Gera um resumo do que você fez ontem e do que vai fazer hoje, "
        "pronto para consultar durante a Daily."
    )

    default_ontem = datetime.today().date() - timedelta(days=1)
    default_hoje = datetime.today().date()

    # O PORQUE: mesmo padrão do filtro de período do Dashboard -- as duas
    # datas ficam dentro de um st.form, então trocar "Ontem" ou "Hoje" não
    # dispara nada sozinho. Só depois de clicar em "Aplicar Período" é que o
    # valor escolhido passa a valer para as sugestões e para o relatório.
    with st.form("daily_period_form"):
        st.markdown("### Escolha o Período")
        col_d1, col_d2 = st.columns(2)
        with col_d1:
            d_ontem_input = st.date_input("Data Anterior (Ontem)", value=default_ontem, format="DD/MM/YYYY", key="daily_d_ontem")
        with col_d2:
            d_hoje_input = st.date_input("Data Atual (Hoje)", value=default_hoje, format="DD/MM/YYYY", key="daily_d_hoje")
        apply_daily_period = st.form_submit_button("Aplicar Período", type="primary")

    if apply_daily_period:
        st.session_state.daily_period_ontem = d_ontem_input
        st.session_state.daily_period_hoje = d_hoje_input

    if "daily_period_ontem" not in st.session_state:
        st.session_state.daily_period_ontem = default_ontem
    if "daily_period_hoje" not in st.session_state:
        st.session_state.daily_period_hoje = default_hoje

    d_ontem = st.session_state.daily_period_ontem
    d_hoje = st.session_state.daily_period_hoje

    st.caption(f"Período aplicado: Ontem = {d_ontem.strftime('%d/%m/%Y')} • Hoje = {d_hoje.strftime('%d/%m/%Y')}")

    # O PORQUE: Impedimentos/Dúvidas passam a ter uma sugestão automática,
    # montada a partir dos registros do período acima que estiverem marcados
    # com is_impedimento/is_duvida (via checkbox manual no formulário ou
    # inferência automática na importação de arquivo). O botão é separado do
    # "Processar Relatório" de propósito: assim você pode ajustar as datas,
    # puxar a sugestão, editar à mão o que quiser, e só então gerar o resumo
    # final -- sem perder o que já tinha digitado a cada rerun da tela.
    _daily_txt_editing_lock = st.session_state.get("daily_txt_editing", False)
    if _daily_txt_editing_lock:
        st.warning("✏️ Finalize (salve) a edição do texto corrido abaixo para liberar os outros botões desta aba.")

    def _merge_daily_suggestion(current_text: str, suggestion_text: str, empty_placeholder: str) -> str:
        # O PORQUE: antes, "Atualizar sugestões da base de dados" SOBRESCREVIA
        # por completo o que o usuário já tinha digitado à mão em
        # Impedimentos/Dúvidas. Agora, cada linha já digitada manualmente é
        # preservada, e só as linhas da sugestão automática que ainda não
        # estão lá são adicionadas (sem duplicar). O placeholder padrão
        # ("Nenhum."/"Nenhuma.") não conta como conteúdo real do usuário.
        current_lines = [ln.strip() for ln in (current_text or "").strip().splitlines() if ln.strip()]
        current_lines = [ln for ln in current_lines if ln.lower() != empty_placeholder.lower()]

        suggestion_lines = [ln.strip() for ln in (suggestion_text or "").strip().splitlines() if ln.strip()]
        suggestion_lines = [ln for ln in suggestion_lines if ln.lower() != empty_placeholder.lower()]

        combined = list(current_lines)
        for ln in suggestion_lines:
            if ln not in combined:
                combined.append(ln)

        return "\n".join(combined) if combined else empty_placeholder

    if st.button("🔄 Atualizar sugestões da base de dados", disabled=_daily_txt_editing_lock):
        df_all_suggestion = repo.get_all_logs_as_dataframe(_current_user())
        new_imp_suggestion = build_daily_suggestion(df_all_suggestion, d_ontem, d_hoje, "is_impedimento")
        new_duv_suggestion = build_daily_suggestion(df_all_suggestion, d_ontem, d_hoje, "is_duvida")
        st.session_state["impedimentos_input"] = _merge_daily_suggestion(
            st.session_state.get("impedimentos_input", ""), new_imp_suggestion, "Nenhum."
        )
        st.session_state["duvidas_input"] = _merge_daily_suggestion(
            st.session_state.get("duvidas_input", ""), new_duv_suggestion, "Nenhuma."
        )
        st.rerun()

    if "impedimentos_input" not in st.session_state:
        st.session_state["impedimentos_input"] = "Nenhum."
    if "duvidas_input" not in st.session_state:
        st.session_state["duvidas_input"] = "Nenhuma."

    impedimentos = st.text_area(
        "Impedimentos", key="impedimentos_input",
        help="Puxado automaticamente dos registros marcados como 🚧 Impedimento no período acima. Edite livremente.",
    )
    duvidas = st.text_area(
        "Dúvidas", key="duvidas_input",
        help="Puxado automaticamente dos registros marcados como ❓ Dúvida no período acima. Edite livremente.",
    )

    def _format_bullets(text: str) -> str:
        # O PORQUE: antes, só a 1ª linha de Impedimentos/Dúvidas ganhava o
        # marcador "- " (era um único f"- {texto}" com o texto inteiro,
        # inclusive multi-linha, embutido depois do marcador). Se o usuário
        # digitasse mais de um item (uma por linha), as linhas seguintes
        # ficavam sem marcador e "coladas" ao final -- dando a impressão de
        # que o conteúdo tinha sumido no texto corrido, mesmo estando lá.
        # Esta função garante que TODA linha não vazia vire seu próprio item.
        lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
        if not lines:
            return "- Nenhum registro informado."
        return "\n".join(f"- {ln[1:].strip() if ln.startswith('-') else ln}" for ln in lines)

    gen_daily = st.button("Processar Relatório", type="primary", disabled=_daily_txt_editing_lock)

    if gen_daily:
        df_all_daily = repo.get_all_logs_as_dataframe(_current_user())
        if not df_all_daily.empty:
            df_all_daily["log_date_dt"] = pd.to_datetime(df_all_daily["log_date"]).dt.date
            df_ontem = df_all_daily[df_all_daily["log_date_dt"] == d_ontem]
            df_hoje = df_all_daily[df_all_daily["log_date_dt"] == d_hoje]
        else:
            df_ontem = pd.DataFrame(columns=["project", "description", "effort_hours"])
            df_hoje = pd.DataFrame(columns=["project", "description", "effort_hours"])

        # Versão em texto puro, usada tanto no download quanto na cópia rápida.
        report_txt = f"=== DAILY SCRUM ===\nData: {datetime.today().strftime('%d/%m/%Y')}\n\n"
        report_txt += f"O QUE FIZ ONTEM ({d_ontem.strftime('%d/%m/%Y')}):\n"
        if df_ontem.empty:
            report_txt += "- Sem registros mapeados.\n"
        else:
            for _, row in df_ontem.iterrows():
                report_txt += f"- [{row['project']}] {row['description']} ({row['effort_hours']}h)\n"
        report_txt += f"\nO QUE FAREI HOJE ({d_hoje.strftime('%d/%m/%Y')}):\n"
        if df_hoje.empty:
            report_txt += "- Sem registros mapeados.\n"
        else:
            for _, row in df_hoje.iterrows():
                report_txt += f"- [{row['project']}] {row['description']} ({row['effort_hours']}h)\n"
        report_txt += f"\nIMPEDIMENTOS:\n{_format_bullets(impedimentos)}\n"
        report_txt += f"\nDÚVIDAS:\n{_format_bullets(duvidas)}\n"

        # O PORQUE: guardamos em session_state para o resumo não sumir da tela
        # assim que o usuário interage com o botão de download (o Streamlit
        # reexecuta o script nesse clique, e "gen_daily" voltaria a False já
        # que o formulário não foi reenviado).
        st.session_state.daily_report = {
            "d_ontem": d_ontem,
            "d_hoje": d_hoje,
            "df_ontem": df_ontem,
            "df_hoje": df_hoje,
            "impedimentos": impedimentos,
            "duvidas": duvidas,
            "report_txt": report_txt,
        }

        # O PORQUE: um novo relatório processado sai do modo de edição (se
        # estivesse ativo) e limpa qualquer rascunho/erro pendente de uma
        # edição anterior, para não misturar edições de um resumo antigo com
        # o resumo recém-gerado.
        st.session_state["daily_txt_editing"] = False
        st.session_state["daily_txt_save_error"] = False
        st.session_state.pop("daily_txt_draft", None)

    if st.session_state.daily_report:
        rep = st.session_state.daily_report
        st.markdown("---")
        st.subheader("📋 Resumo para a Daily")
        st.caption(f"Gerado em {datetime.today().strftime('%d/%m/%Y %H:%M')}")

        with st.container(border=True):
            st.markdown(f"**✅ O que fiz ontem** — {rep['d_ontem'].strftime('%d/%m/%Y')}")
            if rep["df_ontem"].empty:
                st.info("Sem registros mapeados.")
            else:
                for _, row in rep["df_ontem"].iterrows():
                    st.markdown(f"- **[{row['project']}]** {row['description']}  `{row['effort_hours']}h`")

        with st.container(border=True):
            st.markdown(f"**🎯 O que farei hoje** — {rep['d_hoje'].strftime('%d/%m/%Y')}")
            if rep["df_hoje"].empty:
                st.info("Sem registros mapeados.")
            else:
                for _, row in rep["df_hoje"].iterrows():
                    st.markdown(f"- **[{row['project']}]** {row['description']}  `{row['effort_hours']}h`")

        col_imp, col_duv = st.columns(2)
        with col_imp:
            st.markdown("**🚧 Impedimentos**")
            imp = rep["impedimentos"].strip()
            if imp and imp.lower() not in ("nenhum.", "nenhum"):
                st.warning(imp)
            else:
                st.success("Nenhum impedimento.")
        with col_duv:
            st.markdown("**❓ Dúvidas**")
            duv = rep["duvidas"].strip()
            if duv and duv.lower() not in ("nenhuma.", "nenhuma"):
                st.warning(duv)
            else:
                st.success("Nenhuma dúvida.")

        with st.expander("📄 Ver texto corrido (para copiar e colar)", expanded=True):
            # O PORQUE: o texto corrido é obrigatório (não pode ficar em
            # branco) e por padrão fica só para leitura -- edição só é
            # possível clicando em "Editar", e só sai do modo de edição
            # salvando (não existe "descartar", já que o campo é
            # obrigatório e não teria para onde "voltar" em branco).
            #
            # As funções de callback (on_click) são a forma correta, segundo
            # a própria documentação do Streamlit, de alterar o
            # st.session_state de uma key ligada a um widget: o callback roda
            # ANTES do script reexecutar do zero, ou seja, antes do widget
            # ser instanciado novamente -- diferente de atribuir direto
            # st.session_state[key] = valor no meio do script DEPOIS que o
            # widget daquela key já foi desenhado no mesmo rerun (isso é o
            # que causava o StreamlitAPIException reportado).
            if "daily_txt_editing" not in st.session_state:
                st.session_state["daily_txt_editing"] = False

            def _start_editing_daily_txt():
                st.session_state["daily_txt_draft"] = rep["report_txt"]
                st.session_state["daily_txt_editing"] = True
                st.session_state["daily_txt_save_error"] = False

            def _save_daily_txt():
                new_text = st.session_state.get("daily_txt_draft", "").strip()
                if not new_text:
                    # O PORQUE: campo obrigatório -- não salva e mantém o
                    # modo de edição aberto, mostrando o erro no próximo rerun.
                    st.session_state["daily_txt_save_error"] = True
                else:
                    # O PORQUE: rep é o mesmo dict guardado em
                    # st.session_state.daily_report, então esta atribuição já
                    # atualiza o relatório oficial usado pelo download abaixo.
                    rep["report_txt"] = st.session_state["daily_txt_draft"]
                    st.session_state["daily_txt_editing"] = False
                    st.session_state["daily_txt_save_error"] = False

            def _cancel_editing_daily_txt():
                # O PORQUE: só sai do modo de edição e descarta o rascunho --
                # como o campo é obrigatório, não há risco de "cancelar para
                # um estado em branco": rep["report_txt"] (última versão
                # salva) continua intacto e volta a ser exibido.
                st.session_state["daily_txt_editing"] = False
                st.session_state["daily_txt_save_error"] = False

            editing = st.session_state["daily_txt_editing"]

            if not editing:
                st.text_area(
                    # O PORQUE: aqui NÃO usamos "key" junto de "value" -- se
                    # usássemos, o Streamlit só aplicaria "value" na primeira
                    # vez que essa key aparecesse; nas próximas vezes ele
                    # ignoraria o novo "value" e manteria travado o que já
                    # estava salvo em session_state daquela key (era
                    # exatamente o bug: o texto corrido ficava congelado no
                    # primeiro "Nenhum."/"Nenhuma." gerado, mesmo depois de
                    # reprocessar o relatório com dados novos).
                    "Copia rápida", value=rep["report_txt"], height=300,
                    label_visibility="collapsed", disabled=True,
                )
                st.button(
                    "✏️ Editar texto", key="btn_edit_daily_txt",
                    on_click=_start_editing_daily_txt, use_container_width=True,
                )
            else:
                st.text_area(
                    "Copia rápida (editando)", key="daily_txt_draft", height=300,
                    label_visibility="collapsed",
                )
                if st.session_state.get("daily_txt_save_error"):
                    st.error("O texto não pode ficar em branco. Escreva algo antes de salvar.")
                st.caption("✍️ Editando — salve para aplicar a mudança e liberar os outros botões da aba.")
                col_save, col_cancel = st.columns(2)
                with col_save:
                    st.button(
                        "💾 Salvar alterações", key="btn_save_daily_txt", type="primary",
                        on_click=_save_daily_txt, use_container_width=True,
                    )
                with col_cancel:
                    st.button(
                        "🚫 Cancelar edição", key="btn_cancel_daily_txt",
                        on_click=_cancel_editing_daily_txt, use_container_width=True,
                    )

        # O PORQUE: enquanto o texto corrido estiver em modo de edição, o
        # download (e os outros botões da aba, travados lá em cima) ficam
        # bloqueados -- o arquivo baixado nunca diverge do que está na tela
        # sem uma decisão explícita do usuário (salvar).
        pending_changes = st.session_state.get("daily_txt_editing", False)

        st.markdown("---")
        if pending_changes:
            st.info("⚠️ Salve as alterações no texto corrido acima para liberar o download.")
        st.download_button(
            label="⬇️ Baixar Resumo da Daily (.txt)",
            data=rep["report_txt"].encode("utf-8"),
            file_name=f"daily_{datetime.today().strftime('%Y%m%d')}.txt",
            mime="text/plain",
            use_container_width=True,
            type="primary",
            disabled=pending_changes,
        )

# ==========================================
# TAB 3: DASHBOARD, RELATÓRIOS E EXPORTAÇÃO
# ==========================================
with tab_dashboard:
    st.header("Seus Números")
    df_logs = repo.get_all_logs_as_dataframe(_current_user())

    if df_logs.empty:
        st.info("Você ainda não tem nenhum registro cadastrado.")
    else:
        df_logs["log_date_dt"] = pd.to_datetime(df_logs["log_date"])
        min_db_date = df_logs["log_date_dt"].min().date()
        max_db_date = df_logs["log_date_dt"].max().date()
        today = datetime.today().date()
        min_allowed_date = datetime.strptime("2000-01-01", "%Y-%m-%d").date()

        # O PORQUE: por padrão, ao abrir o Dashboard, mostramos os últimos 30
        # dias a partir de hoje — evita a tela vazia/genérica do primeiro
        # acesso. Limitamos (clamp) entre a data mais antiga e a mais recente
        # que existem no banco, para o filtro já nascer válido mesmo que o
        # histórico seja mais curto que 30 dias ou não tenha dado recente.
        default_start_date = today - timedelta(days=30)
        default_start_date = max(default_start_date, min_db_date)
        default_start_date = min(default_start_date, max_db_date)

        with st.form("dashboard_filter_form"):
            st.markdown("### Escolha o Período")
            c_start, c_end = st.columns(2)
            with c_start:
                start_date = st.date_input("Data Inicial", value=default_start_date, format="DD/MM/YYYY")
            with c_end:
                end_date = st.date_input("Data Final", value=max_db_date, format="DD/MM/YYYY")

            apply_filters = st.form_submit_button("Aplicar Filtro", type="primary")

        has_error = False
        if start_date > end_date:
            st.error("A Data Inicial não pode ser depois da Data Final.")
            has_error = True
        if start_date < min_allowed_date:
            st.error("Não aceitamos datas anteriores ao ano 2000.")
            has_error = True
        if end_date > today:
            st.warning("Você selecionou uma Data Final no futuro. Registros com essa data (se existirem) também serão incluídos.")

        # O PORQUE: apply_filters (retorno do form_submit_button) só é True no
        # exato rerun em que o botão foi clicado. Sem persistir esse estado em
        # session_state, qualquer widget fora do form (ex.: o seletor do
        # Pareto) dispararia um rerun que faria o dashboard inteiro sumir,
        # pois apply_filters voltaria a ser False.
        if apply_filters and not has_error:
            st.session_state.dashboard_filters_applied = True
            st.session_state.dashboard_start_date = start_date
            st.session_state.dashboard_end_date = end_date
        elif apply_filters and has_error:
            st.session_state.dashboard_filters_applied = False

        if st.session_state.get("dashboard_filters_applied") and not has_error:
            start_date = st.session_state.dashboard_start_date
            end_date = st.session_state.dashboard_end_date
            with st.spinner('Calculando seus números...'):
                time.sleep(0.5)

                mask = (df_logs["log_date_dt"].dt.date >= start_date) & (df_logs["log_date_dt"].dt.date <= end_date)
                df_filtered = df_logs.loc[mask].copy()

                if df_filtered.empty:
                    st.warning("Nenhum registro encontrado nesse período.")
                else:
                    st.success(f"Período selecionado: {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}")

                    df_filtered["Data_PTBR"] = df_filtered["log_date_dt"].dt.strftime("%d/%m/%Y")
                    df_display_dash = df_filtered[["id", "Data_PTBR", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"]]

                    st.dataframe(df_display_dash, use_container_width=True, hide_index=True)

                    # O PORQUE: Com ranges de data longos, o gráfico diário fica
                    # poluído (dezenas/centenas de pontos ilegíveis no eixo X).
                    # Por isso a granularidade progride em 3 níveis conforme o
                    # tamanho do intervalo selecionado:
                    #   < 14 dias        -> diário (granularidade original)
                    #   14 a ~180 dias   -> semanal (segunda a domingo, ISO)
                    #   >= ~180 dias     -> mensal (agrupado por mês/ano)
                    # Isso evita que ranges de vários meses/anos gerem dezenas de
                    # rótulos semanais amontoados e ilegíveis no eixo X.
                    range_days = (end_date - start_date).days
                    is_monthly_view = range_days >= 180
                    is_weekly_view = (not is_monthly_view) and range_days >= 14

                    MESES_ABREV_PT = {
                        1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
                        7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez",
                    }

                    # O PORQUE: PROJECT_COLORS/CATEGORY_COLORS só têm cor fixa
                    # para os itens originais. Projetos/Categorias criados pela
                    # sidebar entram aqui e ganham uma cor da paleta de reserva,
                    # em vez de ficarem sem cor definida nos gráficos abaixo.
                    dynamic_project_colors = build_color_map(PROJECT_COLORS, df_filtered["project"].unique())
                    dynamic_category_colors = build_color_map(CATEGORY_COLORS, df_filtered["category"].unique())

                    if is_monthly_view:
                        df_filtered["period_start"] = df_filtered["log_date_dt"].values.astype("datetime64[M]")
                        df_filtered["period_label"] = df_filtered["period_start"].apply(
                            lambda d: f"{MESES_ABREV_PT[d.month]}/{d.year}"
                        )
                        period_axis_title = "Mês"
                        chart_title_suffix = "Mensal"
                        line_title_suffix = "Mês"
                    elif is_weekly_view:
                        df_filtered["period_start"] = df_filtered["log_date_dt"] - pd.to_timedelta(
                            df_filtered["log_date_dt"].dt.weekday, unit="D"
                        )
                        period_end = df_filtered["period_start"] + pd.Timedelta(days=6)
                        df_filtered["period_label"] = (
                            df_filtered["period_start"].dt.strftime("%d/%m")
                            + " a "
                            + period_end.dt.strftime("%d/%m/%Y")
                        )
                        period_axis_title = "Semana"
                        chart_title_suffix = "Semanal"
                        line_title_suffix = "Semana"
                    else:
                        df_filtered["period_start"] = df_filtered["log_date_dt"]
                        df_filtered["period_label"] = df_filtered["Data_PTBR"]
                        period_axis_title = "Data"
                        chart_title_suffix = "Diário"
                        line_title_suffix = "Dia"

                    st.markdown("---")
                    st.subheader("Como o Tempo foi Distribuído")
                    c_chart1, c_chart2 = st.columns(2)
                    with c_chart1:
                        df_bar_grouped = (
                            df_filtered.groupby(["period_start", "period_label", "project"])["effort_hours"]
                            .sum()
                            .reset_index()
                            .sort_values("period_start")
                        )
                        fig_time = px.bar(
                            df_bar_grouped, x="period_label", y="effort_hours", color="project",
                            title=f"Alocação de Esforço {chart_title_suffix}", color_discrete_map=dynamic_project_colors,
                            labels={"period_label": period_axis_title, "effort_hours": "Horas", "project": "Projeto"},
                        )
                        fig_time.update_xaxes(
                            type="category", categoryorder="array",
                            categoryarray=df_bar_grouped["period_label"].drop_duplicates().tolist(),
                        )
                        st.plotly_chart(fig_time, use_container_width=True)
                    with c_chart2:
                        df_grouped_cat = df_filtered.groupby("category")["effort_hours"].sum().reset_index()
                        fig_cat = px.pie(
                            df_grouped_cat, values="effort_hours", names="category", color="category",
                            title="Horas por Área de Atuação", hole=0.4, color_discrete_map=dynamic_category_colors,
                        )
                        st.plotly_chart(fig_cat, use_container_width=True)

                    st.markdown("---")
                    st.subheader("Tendência ao Longo do Tempo e Análise de Pareto")
                    c_chart3, c_chart4 = st.columns(2)

                    with c_chart3:
                        # O PORQUE: Gráfico de linhas mostra a tendência de horas
                        # trabalhadas ao longo do período filtrado, por projeto,
                        # complementando a visão de volume total do gráfico de barras.
                        # Usa a mesma granularidade (diária ou semanal) definida
                        # acima a partir do tamanho do range selecionado.
                        df_daily = (
                            df_filtered.groupby(["period_start", "period_label", "project"])["effort_hours"]
                            .sum()
                            .reset_index()
                            .sort_values("period_start")
                        )
                        fig_line = px.line(
                            df_daily, x="period_label", y="effort_hours", color="project",
                            markers=True, title=f"Evolução Temporal do Esforço (Horas/{line_title_suffix})",
                            labels={"period_label": period_axis_title, "effort_hours": "Horas", "project": "Projeto"},
                            color_discrete_map=dynamic_project_colors,
                        )
                        fig_line.update_xaxes(
                            type="category", categoryorder="array",
                            categoryarray=df_daily["period_label"].drop_duplicates().tolist(),
                        )
                        fig_line.update_layout(hovermode="x unified")

                        # O PORQUE: além da evolução por projeto, calculamos uma
                        # tendência linear simples (regressão) sobre o TOTAL de
                        # horas por período (soma de todos os projetos) e a
                        # estendemos alguns períodos à frente como previsão. Isso
                        # dá uma leitura rápida de "esforço subindo, caindo ou
                        # estável" e uma projeção do que esperar a seguir.
                        df_total_period = (
                            df_filtered.groupby(["period_start", "period_label"])["effort_hours"]
                            .sum()
                            .reset_index()
                            .sort_values("period_start")
                        )

                        FORECAST_PERIODS = 3
                        if len(df_total_period) >= 2:
                            x_numeric = np.arange(len(df_total_period))
                            slope, intercept = np.polyfit(x_numeric, df_total_period["effort_hours"], 1)
                            fitted_values = np.clip(slope * x_numeric + intercept, 0, None)

                            # O PORQUE: gera os rótulos dos períodos futuros no
                            # mesmo formato usado para os períodos reais (mês
                            # "Mmm/aaaa", semana "dd/mm a dd/mm/aaaa" ou dia
                            # "dd/mm/aaaa"), para que a previsão apareça
                            # continuando o mesmo eixo X. Meses usam
                            # DateOffset (e não Timedelta de dias fixos), já
                            # que a duração de um mês varia.
                            last_period_start = df_total_period["period_start"].iloc[-1]
                            if is_monthly_view:
                                future_starts = [
                                    last_period_start + pd.DateOffset(months=i)
                                    for i in range(1, FORECAST_PERIODS + 1)
                                ]
                            else:
                                step_days = 7 if is_weekly_view else 1
                                future_starts = [
                                    last_period_start + pd.Timedelta(days=step_days * i)
                                    for i in range(1, FORECAST_PERIODS + 1)
                                ]
                            future_labels = []
                            for f_start in future_starts:
                                if is_monthly_view:
                                    future_labels.append(f"{MESES_ABREV_PT[f_start.month]}/{f_start.year}")
                                elif is_weekly_view:
                                    f_end = f_start + pd.Timedelta(days=6)
                                    future_labels.append(f"{f_start.strftime('%d/%m')} a {f_end.strftime('%d/%m/%Y')}")
                                else:
                                    future_labels.append(f_start.strftime("%d/%m/%Y"))

                            future_x_numeric = np.arange(len(df_total_period), len(df_total_period) + FORECAST_PERIODS)
                            forecast_values = np.clip(slope * future_x_numeric + intercept, 0, None)

                            trend_labels_all = list(df_total_period["period_label"]) + future_labels
                            trend_values_all = list(fitted_values) + list(forecast_values)

                            fig_line.add_trace(
                                go.Scatter(
                                    x=trend_labels_all, y=trend_values_all,
                                    mode="lines", name="Tendência / Previsão (Total)",
                                    line=dict(color="#ffffff", width=2, dash="dot"),
                                )
                            )

                            # O PORQUE: o eixo X é categórico (para respeitar a
                            # ordem cronológica exata dos rótulos já formatados),
                            # então precisamos incluir os rótulos futuros na lista
                            # de categorias, ou eles não apareceriam no gráfico.
                            fig_line.update_xaxes(
                                categoryarray=df_daily["period_label"].drop_duplicates().tolist() + future_labels,
                            )

                            # O PORQUE: linha vertical demarcando onde os dados
                            # reais terminam e a previsão começa. Envolvido em
                            # try/except pois add_vline com eixo categórico pode
                            # não ser suportado em todas as versões do Plotly.
                            try:
                                fig_line.add_vline(
                                    x=df_total_period["period_label"].iloc[-1],
                                    line_dash="dash", line_color="gray",
                                )
                            except Exception:
                                pass

                        st.plotly_chart(fig_line, use_container_width=True)

                    with c_chart4:
                        # O PORQUE: Pareto clássico = barras ordenadas decrescentemente
                        # + linha de % acumulado em eixo secundário, com referência nos
                        # 80% (regra 80/20) para apontar o que concentra a maior parte
                        # do esforço. O seletor permite trocar a dimensão de análise
                        # entre Categoria e Projeto sem duplicar o gráfico.
                        pareto_dim_label = st.radio(
                            "Analisar por:", ["Categoria", "Projeto"],
                            horizontal=True, key="pareto_dimension",
                        )
                        pareto_dim_col = "category" if pareto_dim_label == "Categoria" else "project"
                        pareto_color_map = dynamic_category_colors if pareto_dim_label == "Categoria" else dynamic_project_colors

                        df_pareto = (
                            df_filtered.groupby(pareto_dim_col)["effort_hours"]
                            .sum()
                            .sort_values(ascending=False)
                            .reset_index()
                        )
                        df_pareto["cum_pct"] = df_pareto["effort_hours"].cumsum() / df_pareto["effort_hours"].sum() * 100
                        # O PORQUE: cada barra usa a cor fixa da sua própria
                        # categoria/projeto (mesma paleta dos outros 3 gráficos),
                        # em vez de uma única cor sólida para todas as barras.
                        bar_colors = [pareto_color_map.get(v, "#4C78A8") for v in df_pareto[pareto_dim_col]]

                        fig_pareto = make_subplots(specs=[[{"secondary_y": True}]])
                        fig_pareto.add_trace(
                            # O PORQUE: as barras usam uma cor por categoria/projeto
                            # (bar_colors), então um único item "Horas" na legenda
                            # não representa fielmente as cores exibidas — por isso
                            # essa trace fica de fora da legenda (showlegend=False).
                            go.Bar(x=df_pareto[pareto_dim_col], y=df_pareto["effort_hours"], name="Horas", marker_color=bar_colors, showlegend=False),
                            secondary_y=False,
                        )
                        fig_pareto.add_trace(
                            go.Scatter(
                                x=df_pareto[pareto_dim_col], y=df_pareto["cum_pct"], name="% Acumulado",
                                mode="lines+markers", line=dict(color="firebrick"),
                            ),
                            secondary_y=True,
                        )
                        fig_pareto.add_hline(y=80, line_dash="dash", line_color="gray", annotation_text="80%", secondary_y=True)
                        fig_pareto.update_xaxes(categoryorder="array", categoryarray=df_pareto[pareto_dim_col].tolist())
                        fig_pareto.update_yaxes(title_text="Horas", secondary_y=False)
                        fig_pareto.update_yaxes(title_text="% Acumulado", range=[0, 105], secondary_y=True)
                        fig_pareto.update_layout(title=f"Pareto de Esforço por {pareto_dim_label}", legend=dict(orientation="h", y=-0.2))
                        st.plotly_chart(fig_pareto, use_container_width=True)

                    # O PORQUE: a exportação fica ao final da página, depois de
                    # todos os gráficos, para o usuário primeiro enxergar a
                    # análise visual e só então, se quiser, baixar os dados brutos.
                    st.markdown("---")
                    st.markdown("### Baixar seus Dados")

                    # O PORQUE do bug do "x10" (1 virava 10, 2 virava 20): o to_csv
                    # original usava sep=";" mas NAO especificava decimal=",",
                    # entao effort_hours saia com ponto decimal (ex: "1.0"). Com
                    # sep=";" (convencao pt-BR) porem decimal="." (ponto), o Excel
                    # configurado em pt-BR pode interpretar esse ponto como
                    # separador de MILHAR (nao decimal) e descartar seu efeito,
                    # virando "1.0" -> 10. A correcao e explicitar decimal=",", que
                    # e o que o Excel pt-BR realmente espera para casar com o
                    # separador de coluna ";". A linha "sep=;" no topo do arquivo
                    # e uma diretiva que o proprio Excel respeita independente da
                    # configuracao regional, evitando tambem problema de coluna.
                    csv_buffer = StringIO()
                    df_display_dash.to_csv(csv_buffer, index=False, sep=";", decimal=",")
                    csv_data = ("sep=;\n" + csv_buffer.getvalue()).encode("utf-8-sig")

                    report_text = f"RELATÓRIO DE ATIVIDADES\nPeríodo: {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}\n\n"
                    report_text += f"Total de Registros: {len(df_filtered)}\n"
                    report_text += f"Total de Horas: {df_filtered['effort_hours'].sum()}h\n\n"
                    report_text += "==== HORAS POR PROJETO ====\n"
                    proj_summary = df_filtered.groupby("project")["effort_hours"].sum().reset_index()
                    for _, row in proj_summary.iterrows():
                        report_text += f" > {row['project']}: {row['effort_hours']}h\n"
                    report_text += "\n==== DETALHAMENTO ====\n"
                    for _, row in df_display_dash.iterrows():
                        flag_prefix = ""
                        if bool(int(row.get("is_impedimento", 0) or 0)):
                            flag_prefix += "[IMPEDIMENTO] "
                        if bool(int(row.get("is_duvida", 0) or 0)):
                            flag_prefix += "[DÚVIDA] "
                        report_text += f"[{row['Data_PTBR']}] {row['project']} | {row['category']} \n  -> {flag_prefix}{row['description']} ({row['effort_hours']}h)\n\n"

                    c_down_csv, c_down_txt = st.columns(2)
                    with c_down_csv:
                        st.download_button(label="Baixar Planilha (.csv)", data=csv_data, file_name=f"extrato_atividades_{start_date}_{end_date}.csv", mime="text/csv", use_container_width=True)
                    with c_down_txt:
                        st.download_button(label="Baixar Relatório (.txt)", data=report_text, file_name=f"relatorio_atividades_{start_date}_{end_date}.txt", mime="text/plain", use_container_width=True)
                    st.caption(
                        "Se algum número aparecer errado ao abrir a planilha no Excel, use "
                        "Dados > Obter Dados > De Texto/CSV (em vez de dar duplo-clique no arquivo) "
                        "e confirme ';' como separador de coluna e ',' como separador decimal."
                    )

# ==========================================
# TAB 4: SINCRONIZAÇÃO E OVERRIDE DE ARQUIVO
# ==========================================
@st.dialog("Cancelar sincronização")
def dialog_confirmar_cancelamento_sync():
    st.write("Tem certeza que deseja cancelar esta sincronização?")
    st.caption("Os registros a inserir/remover que você analisou serão descartados. Nenhuma alteração já foi salva no banco.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, cancelar", type="primary", use_container_width=True):
            st.session_state.sync_analyzed = False
            st.session_state.df_to_insert = pd.DataFrame()
            st.session_state.df_to_delete = pd.DataFrame()
            st.session_state.confirm_state = None
            st.rerun()
    with col2:
        if st.button("Voltar", use_container_width=True):
            st.session_state.confirm_state = None
            st.rerun()


with tab_sync:
    if st.session_state.confirm_state == 'cancel_sync':
        dialog_confirmar_cancelamento_sync()

    st.header("Sincronizar Arquivo de Histórico")
    st.info("Envie o arquivo de histórico (.txt ou .csv) para comparar com os registros já salvos. Você poderá escolher, um a um, o que aplicar. No final, vamos pedir que você digite o nome do arquivo enviado para confirmar a operação.")

    # O PORQUE: Upload em memória (UploadedFile) substitui a leitura fixa de
    # "raw_history.txt" na raiz do projeto. Isso permite sincronizar a partir
    # de qualquer máquina/pasta, sem depender do arquivo estar no diretório
    # de execução do Streamlit. Aceita tanto o formato de log em .txt quanto
    # um .csv já estruturado nas colunas log_date;project;category;description;effort_hours.
    st.caption(f"Tamanho máximo permitido: {MAX_UPLOAD_SIZE_MB}MB.")
    uploaded_file = st.file_uploader(
        "Arquivo de histórico (.txt ou .csv)",
        type=["txt", "csv"],
        key="sync_uploader",
        help=(
            "Arquivos .txt seguem o formato de log manual (datas + tarefas, uma por linha). "
            "Arquivos .csv devem ter as colunas log_date;project;category;description;effort_hours "
            "(separador ';' e decimal ',' -- padrão pt-BR -- ou separador ',' e decimal '.' -- padrão US; "
            "datas em dd/mm/aaaa ou aaaa-mm-dd)."
        ),
    )

    upload_too_large = False
    if uploaded_file is not None and uploaded_file.size > MAX_UPLOAD_SIZE_BYTES:
        upload_too_large = True
        uploaded_size_mb = uploaded_file.size / (1024 * 1024)
        st.error(
            f"Arquivo '{uploaded_file.name}' tem {uploaded_size_mb:.1f}MB, "
            f"acima do limite de {MAX_UPLOAD_SIZE_MB}MB. Envie um arquivo menor."
        )

    if st.button("Analisar Arquivo Enviado", type="primary", disabled=(uploaded_file is None or upload_too_large)):
        with st.spinner("Comparando com os registros salvos..."):
            raw_bytes = uploaded_file.read()
            file_ext = os.path.splitext(uploaded_file.name)[1].lower()
            parser = HistoryParser()

            if file_ext == ".csv":
                df_txt = parser.parse_csv(raw_bytes)
                if df_txt.empty:
                    st.error(
                        "Não foi possível reconhecer as colunas do CSV. Esperado: "
                        "log_date;project;category;description;effort_hours "
                        "(ou separado por vírgula, no padrão US)."
                    )
            else:
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                df_txt = parser.parse_text(raw_text)

            df_db = repo.get_all_logs_as_dataframe(_current_user())

            # O PORQUE: normaliza is_impedimento/is_duvida para o mesmo tipo
            # (int 0/1) dos dois lados antes do merge -- df_db vem do SQLite
            # como int64, df_txt vem do parser como bool. Se os dtypes não
            # baterem, o merge por essas colunas nunca dá match e todo
            # registro pareceria "novo", mesmo já existindo.
            COMPARE_COLUMNS = ["log_date", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"]
            if not df_txt.empty:
                df_txt["is_impedimento"] = df_txt["is_impedimento"].astype(int)
                df_txt["is_duvida"] = df_txt["is_duvida"].astype(int)

            # O PORQUE: Manipulação via Pandas Merge para identificar Deltas (Insertions vs Deletions) mantendo a performance de O(N).
            if not df_db.empty:
                df_db_comp = df_db.drop(columns=["id", "created_at"])
                df_db_comp["is_impedimento"] = df_db_comp["is_impedimento"].astype(int)
                df_db_comp["is_duvida"] = df_db_comp["is_duvida"].astype(int)
            else:
                df_db_comp = pd.DataFrame(columns=COMPARE_COLUMNS)

            if not df_txt.empty:
                df_merged = df_txt.merge(df_db_comp, on=COMPARE_COLUMNS, how='outer', indicator=True)

                df_to_insert = df_merged[df_merged['_merge'] == 'left_only'].drop(columns=['_merge']).copy()
                df_to_delete_comp = df_merged[df_merged['_merge'] == 'right_only'].drop(columns=['_merge']).copy()

                # Recupera os IDs para remoção exata
                if not df_to_delete_comp.empty:
                    df_to_delete = df_db.merge(df_to_delete_comp, on=COMPARE_COLUMNS, how='inner')
                else:
                    df_to_delete = pd.DataFrame()
            else:
                df_to_insert = pd.DataFrame()
                df_to_delete = df_db.copy()

            df_to_insert.insert(0, "_Aplicar", True)

            if not df_to_delete.empty:
                df_to_delete.insert(0, "_Aplicar", True)

            st.session_state.df_to_insert = df_to_insert
            st.session_state.df_to_delete = df_to_delete
            st.session_state.sync_analyzed = True
            st.session_state.sync_file_name = uploaded_file.name

    if st.session_state.sync_analyzed:
        st.markdown("---")

        edited_insert = pd.DataFrame()
        edited_delete = pd.DataFrame()

        col_ins, col_del = st.columns(2)

        with col_ins:
            st.subheader("🟢 Novos Registros")
            if st.session_state.df_to_insert.empty:
                st.success("Nenhum registro novo encontrado no arquivo.")
            else:
                st.write("Desmarque a caixa `_Aplicar` para ignorar o registro.")
                # O PORQUE: st.data_editor permite manipulação booleana direto no DataFrame sem loops complexos.
                # DateColumn com format="DD/MM/YYYY" exibe a data no padrão brasileiro
                # mesmo com o valor por baixo continuando em ISO (YYYY-MM-DD).
                edited_insert = st.data_editor(
                    st.session_state.df_to_insert,
                    column_config={
                        "_Aplicar": st.column_config.CheckboxColumn("Aplicar", default=True),
                        "log_date": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                        "is_impedimento": st.column_config.CheckboxColumn("🚧 Impedimento"),
                        "is_duvida": st.column_config.CheckboxColumn("❓ Dúvida"),
                    },
                    disabled=["log_date", "project", "category", "description", "effort_hours", "is_impedimento", "is_duvida"],
                    hide_index=True,
                    use_container_width=True,
                    key="editor_insert"
                )

        with col_del:
            st.subheader("🔴 Registros para Remover")
            if st.session_state.df_to_delete.empty:
                st.success("Nenhum registro para remover.")
            else:
                st.write("Desmarque a caixa `_Aplicar` para impedir a exclusão.")
                edited_delete = st.data_editor(
                    st.session_state.df_to_delete,
                    column_config={
                        "_Aplicar": st.column_config.CheckboxColumn("Excluir", default=True),
                        "log_date": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                        "is_impedimento": st.column_config.CheckboxColumn("🚧 Impedimento"),
                        "is_duvida": st.column_config.CheckboxColumn("❓ Dúvida"),
                    },
                    disabled=["id", "log_date", "project", "category", "description", "effort_hours", "created_at", "is_impedimento", "is_duvida"],
                    hide_index=True,
                    use_container_width=True,
                    key="editor_delete"
                )

        st.markdown("### Confirmação de Segurança")
        expected_name = st.session_state.get("sync_file_name", "raw_history.txt")
        st.warning(f"Para confirmar, digite exatamente `{expected_name}` (o nome do arquivo que você enviou).")

        confirm_text = st.text_input("Confirmação:")

        col_sync, col_cancel_sync = st.columns(2)
        with col_sync:
            btn_sync = st.button("Sincronizar", type="primary", use_container_width=True)
        with col_cancel_sync:
            if st.button("Cancelar", use_container_width=True):
                st.session_state.confirm_state = 'cancel_sync'
                st.rerun()

        if btn_sync:
            if confirm_text != expected_name:
                st.error("Nome do arquivo incorreto. Tente novamente.")
            else:
                records_inserted = 0
                records_deleted = 0

                if not edited_insert.empty:
                    to_insert = edited_insert[edited_insert["_Aplicar"] == True]
                    sync_username = _current_user()
                    for _, row in to_insert.iterrows():
                        # O PORQUE: DateColumn pode devolver datetime.date (ou
                        # Timestamp) em vez de string ao ler o data_editor de volta;
                        # normalizamos para ISO (YYYY-MM-DD) antes de gravar, que é
                        # o formato esperado pela coluna log_date no SQLite.
                        log_date_iso = row["log_date"].strftime("%Y-%m-%d") if hasattr(row["log_date"], "strftime") else str(row["log_date"])
                        repo.insert_log(
                            sync_username, log_date_iso, row["project"], row["category"], row["description"], row["effort_hours"],
                            bool(row.get("is_impedimento", False)), bool(row.get("is_duvida", False)),
                        )
                        records_inserted += 1

                if not edited_delete.empty:
                    to_delete = edited_delete[edited_delete["_Aplicar"] == True]
                    sync_username = _current_user()
                    for _, row in to_delete.iterrows():
                        repo.delete_log(row["id"], sync_username)
                        records_deleted += 1

                st.session_state.sync_analyzed = False
                st.success(f"Prontinho! {records_inserted} registro(s) adicionado(s) e {records_deleted} removido(s).")
                time.sleep(2)
                st.rerun()