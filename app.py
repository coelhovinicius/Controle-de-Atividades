import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import unicodedata
import time
import os
from io import StringIO
from datetime import datetime, timedelta
from database_core import DatabaseConnection, LogRepository
from importer_core import HistoryParser

st.set_page_config(page_title="QA Task Tracker", layout="wide")

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
    "Outros": "#7f7f7f",
}

CATEGORY_COLORS = {
    "Desenvolvimento de Testes": "#1f77b4",
    "Execucao de Testes": "#ff7f0e",
    "Documentacao": "#2ca02c",
    "Reuniao": "#d62728",
    "Resolucao de BUG/Problema": "#9467bd",
    "Estudos/Certificacao": "#8c564b",
    "Outros": "#7f7f7f",
}

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
            repo.delete_log(st.session_state.target_id)
            reset_states(full_reset=True)
            st.success("Registro excluído com sucesso!")
            time.sleep(1)
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
            d = st.session_state.pending_data
            repo.insert_log(d['date'], d['proj'], d['cat'], d['desc'], d['eff'])
            reset_states(full_reset=True)
            st.success("Registro salvo com sucesso!")
            time.sleep(1)
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
            d = st.session_state.pending_data
            repo.update_log(st.session_state.target_id, d['date'], d['proj'], d['cat'], d['desc'], d['eff'])
            reset_states(full_reset=True)
            st.success("Registro atualizado com sucesso!")
            time.sleep(1)
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


st.title("QA Tracker & Insights")

tab_manage, tab_dashboard, tab_sync = st.tabs(["Registro de Atividades", "Dashboard & Relatórios", "Sincronização de Arquivo"])

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

        df_all = repo.get_all_logs_as_dataframe()

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
                    cols[4].write(row["description"])
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
        st.caption("Todos os campos abaixo são obrigatórios.")
        with st.form("add_form"):
            col_d, col_p, col_c, col_e = st.columns(4)
            with col_d:
                log_date = st.date_input("Data (DD/MM/AAAA)", format="DD/MM/YYYY")
            with col_p:
                project = st.selectbox("Projeto", ["Sustentacao", "Passaporte", "360", "Job Boards", "Vagas", "Motor RCE", "Price Up", "Outros"])
            with col_c:
                category = st.selectbox("Categoria", ["Desenvolvimento de Testes", "Execucao de Testes", "Documentacao", "Reuniao", "Resolucao de BUG/Problema", "Estudos/Certificacao"])
            with col_e:
                effort_hours = st.number_input("Esforço (Horas)", min_value=0.0, step=0.5, value=1.0)

            description = st.text_area("Descrição da Atividade *")

            col_save, col_canc = st.columns(2)
            with col_save:
                btn_save = st.form_submit_button("Salvar Registro", type="primary", use_container_width=True)
            with col_canc:
                btn_canc = st.form_submit_button("Cancelar", use_container_width=True)

            if btn_save:
                erros = validar_formulario_atividade(description, effort_hours)
                if erros:
                    for erro in erros:
                        st.error(erro)
                else:
                    st.session_state.pending_data = {
                        'date': str(log_date), 'proj': project, 'cat': category, 'desc': description, 'eff': effort_hours
                    }
                    st.session_state.confirm_state = 'save_add'
                    st.rerun()
            if btn_canc:
                st.session_state.confirm_state = 'cancel_add'
                st.rerun()

    if st.session_state.view_state == 'edit' and st.session_state.target_id:
        st.header(f"Editar Registro (ID {st.session_state.target_id})")
        st.caption("Todos os campos abaixo são obrigatórios.")
        df_target = repo.get_all_logs_as_dataframe()
        target_row = df_target[df_target['id'] == st.session_state.target_id].iloc[0]

        with st.form("edit_form"):
            col_d, col_p, col_c, col_e = st.columns(4)
            with col_d:
                parsed_date = datetime.strptime(target_row["log_date"], "%Y-%m-%d").date()
                log_date = st.date_input("Data (DD/MM/AAAA)", value=parsed_date, format="DD/MM/YYYY")
            with col_p:
                p_opts = ["Sustentacao", "Passaporte", "360", "Job Boards", "Vagas", "Motor RCE", "Price Up", "Outros"]
                p_idx = p_opts.index(target_row["project"]) if target_row["project"] in p_opts else 0
                project = st.selectbox("Projeto", p_opts, index=p_idx)
            with col_c:
                c_opts = ["Desenvolvimento de Testes", "Execucao de Testes", "Documentacao", "Reuniao", "Resolucao de BUG/Problema", "Estudos/Certificacao"]
                c_idx = c_opts.index(target_row["category"]) if target_row["category"] in c_opts else 0
                category = st.selectbox("Categoria", c_opts, index=c_idx)
            with col_e:
                effort_hours = st.number_input("Esforço (Horas)", min_value=0.0, step=0.5, value=float(target_row["effort_hours"]))

            description = st.text_area("Descrição da Atividade *", value=target_row["description"])

            col_save, col_canc = st.columns(2)
            with col_save:
                btn_save = st.form_submit_button("Salvar Alterações", type="primary", use_container_width=True)
            with col_canc:
                btn_canc = st.form_submit_button("Cancelar", use_container_width=True)

            if btn_save:
                erros = validar_formulario_atividade(description, effort_hours)
                if erros:
                    for erro in erros:
                        st.error(erro)
                else:
                    st.session_state.pending_data = {
                        'date': str(log_date), 'proj': project, 'cat': category, 'desc': description, 'eff': effort_hours
                    }
                    st.session_state.confirm_state = 'save_edit'
                    st.rerun()
            if btn_canc:
                st.session_state.confirm_state = 'cancel_edit'
                st.rerun()

# ==========================================
# TAB 2: DASHBOARD, RELATÓRIOS E EXPORTAÇÃO
# ==========================================
with tab_dashboard:
    st.header("Seus Números")
    df_logs = repo.get_all_logs_as_dataframe()

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
                    df_display_dash = df_filtered[["id", "Data_PTBR", "project", "category", "description", "effort_hours"]]

                    st.dataframe(df_display_dash, use_container_width=True, hide_index=True)

                    csv_buffer = StringIO()
                    df_display_dash.to_csv(csv_buffer, index=False, sep=";")
                    csv_data = csv_buffer.getvalue()

                    report_text = f"RELATÓRIO DE ATIVIDADES - QA\nPeríodo: {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}\n\n"
                    report_text += f"Total de Registros: {len(df_filtered)}\n"
                    report_text += f"Total de Horas: {df_filtered['effort_hours'].sum()}h\n\n"
                    report_text += "==== HORAS POR PROJETO ====\n"
                    proj_summary = df_filtered.groupby("project")["effort_hours"].sum().reset_index()
                    for _, row in proj_summary.iterrows():
                        report_text += f" > {row['project']}: {row['effort_hours']}h\n"
                    report_text += "\n==== DETALHAMENTO ====\n"
                    for _, row in df_display_dash.iterrows():
                        report_text += f"[{row['Data_PTBR']}] {row['project']} | {row['category']} \n  -> {row['description']} ({row['effort_hours']}h)\n\n"

                    st.markdown("### Baixar seus Dados")
                    c_down_csv, c_down_txt = st.columns(2)
                    with c_down_csv:
                        st.download_button(label="Baixar Planilha (.csv)", data=csv_data, file_name=f"extrato_qa_{start_date}_{end_date}.csv", mime="text/csv", use_container_width=True)
                    with c_down_txt:
                        st.download_button(label="Baixar Relatório (.txt)", data=report_text, file_name=f"relatorio_qa_{start_date}_{end_date}.txt", mime="text/plain", use_container_width=True)

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
                            title=f"Alocação de Esforço {chart_title_suffix}", color_discrete_map=PROJECT_COLORS,
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
                            title="Horas por Área de Atuação", hole=0.4, color_discrete_map=CATEGORY_COLORS,
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
                            color_discrete_map=PROJECT_COLORS,
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
                        pareto_color_map = CATEGORY_COLORS if pareto_dim_label == "Categoria" else PROJECT_COLORS

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

# ==========================================
# TAB 3: SINCRONIZAÇÃO E OVERRIDE DE ARQUIVO
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
    st.info("Envie o arquivo de histórico (.txt) para comparar com os registros já salvos. Você poderá escolher, um a um, o que aplicar. No final, vamos pedir que você digite o nome do arquivo enviado para confirmar a operação.")

    # O PORQUE: Upload em memória (UploadedFile) substitui a leitura fixa de
    # "raw_history.txt" na raiz do projeto. Isso permite sincronizar a partir
    # de qualquer máquina/pasta, sem depender do arquivo estar no diretório
    # de execução do Streamlit.
    st.caption(f"Tamanho máximo permitido: {MAX_UPLOAD_SIZE_MB}MB.")
    uploaded_txt = st.file_uploader("Arquivo de histórico (.txt)", type=["txt"], key="sync_uploader")

    upload_too_large = False
    if uploaded_txt is not None and uploaded_txt.size > MAX_UPLOAD_SIZE_BYTES:
        upload_too_large = True
        uploaded_size_mb = uploaded_txt.size / (1024 * 1024)
        st.error(
            f"Arquivo '{uploaded_txt.name}' tem {uploaded_size_mb:.1f}MB, "
            f"acima do limite de {MAX_UPLOAD_SIZE_MB}MB. Envie um arquivo menor."
        )

    if st.button("Analisar Arquivo Enviado", type="primary", disabled=(uploaded_txt is None or upload_too_large)):
        with st.spinner("Comparando com os registros salvos..."):
            raw_text = uploaded_txt.read().decode("utf-8", errors="replace")
            parser = HistoryParser()
            df_txt = parser.parse_text(raw_text)
            df_db = repo.get_all_logs_as_dataframe()

            # O PORQUE: Manipulação via Pandas Merge para identificar Deltas (Insertions vs Deletions) mantendo a performance de O(N).
            if not df_db.empty:
                df_db_comp = df_db.drop(columns=["id", "created_at"])
            else:
                df_db_comp = pd.DataFrame(columns=["log_date", "project", "category", "description", "effort_hours"])

            if not df_txt.empty:
                df_merged = df_txt.merge(df_db_comp, on=["log_date", "project", "category", "description", "effort_hours"], how='outer', indicator=True)

                df_to_insert = df_merged[df_merged['_merge'] == 'left_only'].drop(columns=['_merge']).copy()
                df_to_delete_comp = df_merged[df_merged['_merge'] == 'right_only'].drop(columns=['_merge']).copy()

                # Recupera os IDs para remoção exata
                if not df_to_delete_comp.empty:
                    df_to_delete = df_db.merge(df_to_delete_comp, on=["log_date", "project", "category", "description", "effort_hours"], how='inner')
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
            st.session_state.sync_file_name = uploaded_txt.name

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
                    },
                    disabled=["log_date", "project", "category", "description", "effort_hours"],
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
                    },
                    disabled=["id", "log_date", "project", "category", "description", "effort_hours", "created_at"],
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
                    for _, row in to_insert.iterrows():
                        # O PORQUE: DateColumn pode devolver datetime.date (ou
                        # Timestamp) em vez de string ao ler o data_editor de volta;
                        # normalizamos para ISO (YYYY-MM-DD) antes de gravar, que é
                        # o formato esperado pela coluna log_date no SQLite.
                        log_date_iso = row["log_date"].strftime("%Y-%m-%d") if hasattr(row["log_date"], "strftime") else str(row["log_date"])
                        repo.insert_log(log_date_iso, row["project"], row["category"], row["description"], row["effort_hours"])
                        records_inserted += 1

                if not edited_delete.empty:
                    to_delete = edited_delete[edited_delete["_Aplicar"] == True]
                    for _, row in to_delete.iterrows():
                        repo.delete_log(row["id"])
                        records_deleted += 1

                st.session_state.sync_analyzed = False
                st.success(f"Prontinho! {records_inserted} registro(s) adicionado(s) e {records_deleted} removido(s).")
                time.sleep(2)
                st.rerun()
