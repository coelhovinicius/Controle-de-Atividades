import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import unicodedata
import time
import os
import sys
import io
import hmac
import base64
import hashlib
import bcrypt
import requests
import secrets as pysecrets
from io import StringIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database_core import DatabaseConnection, LogRepository, TursoRequiredError
from importer_core import HistoryParser

st.set_page_config(page_title="Task Tracker ", layout="wide", initial_sidebar_state="collapsed")

# ==========================================
# CONEXÃO COM O BANCO (Turso obrigatório, opcionalmente)
# ==========================================
# O PORQUE: este bloco roda ANTES da tela de login (e antes de qualquer
# outra coisa) de propósito. Com REQUIRE_TURSO=true nos Secrets (recomendado
# em produção -- veja TURSO_DEPLOY.md), database_core.py levanta
# TursoRequiredError em vez de cair silenciosamente para o SQLite local
# quando o Turso não está acessível. Capturamos essa exceção aqui e paramos
# o app por completo: ninguém consegue nem tentar logar enquanto o banco
# persistente não estiver disponível -- isso evita que alguém grave
# atividades "reais" num banco local que se perde no próximo redeploy/sleep
# do Streamlit Cloud. Localmente, sem REQUIRE_TURSO configurado (o padrão),
# nada disto muda o comportamento de sempre.
@st.cache_resource
def get_repository():
    db_conn = DatabaseConnection()
    return LogRepository(db_conn)


try:
    repo = get_repository()
except TursoRequiredError as e:
    st.title("📊 Task Tracker")
    st.error(
        "🚫 **Não foi possível conectar ao banco de dados Turso, e o app está "
        "configurado para exigi-lo (`REQUIRE_TURSO=true`).**\n\n"
        f"Motivo: {e}\n\n"
        "O app foi interrompido de propósito para não gravar dados num banco local "
        "temporário (que seria perdido no próximo redeploy/sleep). Verifique "
        "`TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN` em Settings → Secrets."
    )
    st.stop()

if not repo.db_connection.using_turso and (os.environ.get("TURSO_DATABASE_URL") or os.environ.get("TURSO_AUTH_TOKEN")):
    # O PORQUE: só chega aqui quando REQUIRE_TURSO NÃO está ativo (senão o
    # bloco acima já teria parado o app) -- então isto é só um aviso, para
    # quem preferir permitir o fallback mesmo assim. Mostra se havia
    # credencial configurada mas a conexão caiu para o SQLite local mesmo
    # assim -- sério no Streamlit Cloud (disco efêmero: os dados gravados
    # "somem" no próximo redeploy/sleep). Se não há credencial nenhuma (ex.:
    # rodando local de propósito), o fallback é esperado e não precisa
    # alarmar ninguém.
    st.warning(
        "⚠️ Não foi possível conectar ao banco Turso -- o app está usando um banco local "
        "temporário. Se isto estiver rodando no Streamlit Cloud, os dados gravados agora "
        "**serão perdidos** no próximo deploy. Verifique TURSO_DATABASE_URL/TURSO_AUTH_TOKEN "
        "em Settings → Secrets (veja os logs em 'Manage app' para o motivo exato). "
        "Para impedir que o app sequer rode nessa situação, defina `REQUIRE_TURSO = \"true\"` "
        "nos Secrets.",
        icon="⚠️",
    )

# ==========================================
# LOGIN
# ==========================================
# O PORQUE: o plano gratuito do Streamlit Community Cloud só permite 1 app
# privado por conta, e essa cota já está em uso por outro app. Em vez de
# depender do controle de acesso nativo do Streamlit (email convidado), o
# app fica "Public" no Streamlit, e o controle de acesso de verdade é feito
# aqui: nenhuma tela de dados/formulário é exibida até o login ser validado
# contra usuário/senha (hash bcrypt) configurados nos Secrets. (A conexão
# com o banco, por sua vez, é verificada logo acima, ANTES até do login --
# ver bloco "CONEXÃO COM O BANCO".)
#
# Configuração esperada em .streamlit/secrets.toml (local) e em
# Settings > Secrets (Community Cloud) -- veja secrets.toml.example:
#   [credentials]
#   "seu_usuario" = "$2b$12$...hash bcrypt, gerado com gerar_hash_senha.py..."

# O PORQUE: datetime.now()/datetime.today() sozinhos pegam o horário do
# SISTEMA que roda o servidor -- no Streamlit Community Cloud, isso é UTC
# (Greenwich), não o horário de Brasília, mesmo o público sendo daqui (é
# por isso que rodando local -- Windows já configurado pro fuso certo --
# os horários saíam certos, e publicado saíam 3h adiantados). Esta função
# sempre devolve o horário de Brasília de verdade (America/Sao_Paulo,
# UTC-3, sem horário de verão desde 2019), rodando local ou publicado, sem
# depender do fuso configurado na máquina que executa o servidor. Use
# SEMPRE agora_br() no lugar de datetime.now()/datetime.today() daqui pra
# frente, em qualquer parte nova do app.
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def agora_br() -> datetime:
    return datetime.now(FUSO_BRASILIA)


def _comparar_com_agora_br(timestamp_iso: str) -> bool:
    """
    Devolve True se o timestamp (armazenado em formato ISO) já passou do
    agora. Lida tanto com timestamps ANTIGOS (salvos antes desta correção,
    sem informação de fuso -- nesse caso, assume que já era horário de
    Brasília, que é o que o servidor gerava localmente até aqui) quanto com
    os NOVOS (já salvos com fuso embutido) -- sem isso, comparar um
    timestamp antigo (sem fuso) com agora_br() (com fuso) quebraria com
    erro de tipo.
    """
    dt = datetime.fromisoformat(timestamp_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_BRASILIA)
    return agora_br() > dt

# O PORQUE: st.session_state é por sessão de navegador e se perde ao
# recarregar a página (F5) -- por isso, sozinho, não sustenta "ficar logado
# até clicar em Sair". O token vai para um COOKIE (gravado no navegador via
# JavaScript no momento do login) para sobreviver a um F5, e ele mesmo
# carrega usuário + validade, ASSINADOS com HMAC -- ou seja, validar o
# token não depende de nenhum dicionário na memória do processo. Isso
# significa que a sessão sobrevive não só a um F5, mas também a um restart
# do servidor (Ctrl+C + rodar de novo, ou um redeploy no Streamlit Cloud),
# desde que SESSION_SECRET_KEY esteja configurada nos Secrets (veja
# abaixo) -- sem isso, cada restart gera uma chave nova sozinho, e a sessão
# se comporta como antes (só sobrevive a F5, não a restart).
#
# O PORQUE de 1h (em vez de algo maior): pesado o suficiente pra não pedir
# login toda hora, mas curto o bastante para limitar até onde um cookie
# vazado (cenário raro -- acesso físico ao navegador de alguém, por
# exemplo) continuaria valendo, mesmo sobrevivendo a um restart do
# servidor. Se 1h se mostrar curto demais no uso real (pedindo login com
# frequência incômoda), é só aumentar este número.
SESSION_TTL_HOURS = 1

# O PORQUE: chave que assina (HMAC) o token de sessão. NÃO é uma senha de
# ninguém -- é só um segredo do servidor usado pra garantir que o token não
# foi forjado. Se SESSION_SECRET_KEY não estiver configurada nos Secrets,
# cai num valor aleatório gerado uma vez por processo -- funciona
# perfeitamente para F5 (que não reinicia o processo), mas qualquer restart
# do servidor invalida as sessões existentes (comportamento seguro por
# padrão: nunca cai num valor fixo/previsível "por engano"). Para a sessão
# sobreviver também a restarts, gere uma vez com:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# e cole o resultado em SESSION_SECRET_KEY nos Secrets.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", "").strip() or pysecrets.token_urlsafe(32)

# O PORQUE: quando um convidado entra pelo link de acesso aprovado, ele
# precisa ver os dados de alguém -- não os dele mesmo (convidado não tem
# work_logs próprios). Esta chave diz qual username (de [credentials]) é
# "o dono dos dados" que os convidados enxergam. Sem ela configurada, o
# fluxo de convidado fica desativado (não tem como saber de quem mostrar
# os dados).
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()

# O PORQUE: tokens revogados explicitamente (botão "Sair") antes do prazo
# natural de expiração. O token em si já é auto-verificável (HMAC) e não
# depende desta lista pra ser considerado válido -- ela só serve pra
# lembrar "isto aqui foi desconectado antes da hora". Ainda vive em
# memória (reseta num restart do servidor) -- na prática, isso só significa
# que um "Sair" feito poucas horas antes de um restart poderia, em teoria,
# voltar a valer até a expiração natural depois do restart. Risco baixo
# para um app pessoal, e a troca vale a pena pelo ganho de sobreviver a
# restarts no caso comum (sem logout no meio).
_REVOKED_TOKENS = set()


def _obter_user_agent_hash() -> str:
    # O PORQUE: usado para "amarrar" o token de sessão ao navegador que fez
    # login. Se alguém copiar a URL (com o token de sessão) e colar em
    # outro navegador/dispositivo, o User-Agent enviado é diferente, e o
    # token passa a ser recusado mesmo estando corretamente assinado --
    # trava exatamente o cenário relatado (copiar/colar o link em outro
    # navegador). Não é uma garantia absoluta (um atacante deliberado pode
    # forjar esse cabeçalho), mas é uma camada de defesa real contra o caso
    # comum, sem custo nenhum de UX pra quem só está dando F5 no mesmo
    # navegador (o User-Agent não muda entre um F5 e outro).
    try:
        ua = st.context.headers.get("User-Agent", "") or ""
    except Exception:
        ua = ""
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]


def _criar_sessao(username: str) -> str:
    expires_at = (agora_br() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
    ua_hash = _obter_user_agent_hash()
    payload = f"{username}|{expires_at}|{ua_hash}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = hmac.new(SESSION_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), digestmod="sha256").hexdigest()
    return f"{payload_b64}.{signature}"


def _validar_sessao(token: str):
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.rpartition(".")

    # O PORQUE: hmac.compare_digest em vez de "==" -- evita que o tempo de
    # resposta vaze informação sobre até onde a assinatura bateu certo
    # (mesma mitigação de timing attack usada no login).
    expected_signature = hmac.new(SESSION_SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), digestmod="sha256").hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return None
    if token in _REVOKED_TOKENS:
        return None

    try:
        padding = "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        username, expires_at_iso, ua_hash_esperado = payload.rsplit("|", 2)
    except Exception:
        return None

    # O PORQUE: _comparar_com_agora_br() (não uma comparação direta) --
    # tokens criados ANTES desta correção de fuso não têm informação de
    # fuso embutida; comparar direto com agora_br() (que tem fuso) quebraria
    # com erro de tipo. A função trata os dois casos sem crashar -- pior
    # cenário, alguém logado bem na hora do deploy tem a sessão encerrada
    # um pouco mais cedo ou mais tarde que o normal, nada grave.
    try:
        if _comparar_com_agora_br(expires_at_iso):
            return None
    except Exception:
        return None

    # O PORQUE: aqui é onde a amarração ao navegador é de fato aplicada --
    # compara o hash do User-Agent gravado na criação do token com o do
    # navegador que está tentando usá-lo agora.
    if _obter_user_agent_hash() != ua_hash_esperado:
        return None

    return username


def _revogar_sessao(token: str):
    _REVOKED_TOKENS.add(token)


# O PORQUE: nome do cookie que carrega o token de sessão. Curto e específico
# o bastante pra não colidir com outros cookies do navegador.
_COOKIE_SESSAO = "ttk_session"


def _definir_cookie_sessao(token: str):
    # O PORQUE: isto é "fire-and-forget" de propósito -- só grava o cookie
    # no navegador, sem esperar nem recarregar nada. A sessão ATUAL não
    # depende do cookie pra funcionar (st.session_state.authenticated já
    # foi setado direto em Python, logo acima de onde esta função é
    # chamada) -- o cookie só importa pra uma FUTURA abertura da página
    # (F5, reabrir a aba), quando um st.session_state novo (vazio) precisa
    # descobrir que já existe um login válido. Como document.cookie é
    # síncrono no navegador, o cookie já está gravado assim que este
    # componente é renderizado -- não precisa de reload nem de clique
    # nenhum para a sessão atual continuar.
    max_age = int(SESSION_TTL_HOURS * 3600)
    st.components.v1.html(
        f'<script>document.cookie = "{_COOKIE_SESSAO}={token}; max-age={max_age}; path=/; SameSite=Lax";</script>',
        height=0,
    )


def _limpar_cookie_sessao():
    # O PORQUE: mesmo raciocínio -- só apaga o cookie (max-age=0) pra uma
    # FUTURA abertura da página não encontrar mais uma sessão válida. A
    # sessão atual já é encerrada separadamente, limpando st.session_state
    # (ver _limpar_sessao_local).
    st.components.v1.html(
        f'<script>document.cookie = "{_COOKIE_SESSAO}=; max-age=0; path=/; SameSite=Lax";</script>',
        height=0,
    )



# O PORQUE: hash bcrypt "morto" (não corresponde a nenhuma senha real), usado
# só para gastar o mesmo tempo de CPU de uma verificação de verdade quando o
# usuário nem existe -- evita que o tempo de resposta denuncie por timing se
# o usuário existe ou não. Gerado uma única vez com bcrypt.hashpw sobre uma
# string aleatória qualquer; não precisa (nem deve) ser trocado.
_DUMMY_BCRYPT_HASH = b"$2b$12$C6UzMDM.H6dfI/f/IKcEeO0nUxx9wLpXt2z0LU5j7dJp6YbMkMQZm"


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

    # O PORQUE: os valores em [credentials] agora são HASHES bcrypt (gerados
    # com gerar_hash_senha.py), não mais a senha em texto puro. Assim, mesmo
    # que alguém tenha acesso de leitura aos Secrets (um segundo admin do
    # workspace, um backup, um print de tela por engano, um vazamento do
    # painel do Streamlit Cloud), não dá pra recuperar a senha original --
    # só o hash, que não é reversível.
    password_bytes = (password or "").encode("utf-8")
    stored_value = credentials.get(username) if username else None

    if stored_value is None:
        # O PORQUE: mesmo com usuário inexistente, ainda rodamos uma
        # verificação bcrypt contra um hash "morto" -- bcrypt.checkpw sozinho
        # já tem custo (e portanto tempo de resposta) parecido entre
        # sucesso/falha, e isso reforça que a ausência do usuário não seja
        # visível por diferença de tempo.
        try:
            bcrypt.checkpw(password_bytes, _DUMMY_BCRYPT_HASH)
        except Exception:
            pass
        return False

    try:
        return bcrypt.checkpw(password_bytes, str(stored_value).encode("utf-8"))
    except (ValueError, TypeError):
        # O PORQUE: um valor mal formado em [credentials] (ex.: alguém colou
        # a senha em texto puro por engano, em vez do hash gerado pelo
        # gerar_hash_senha.py) não deve derrubar o app com uma exceção --
        # trata como login inválido e avisa no log do servidor, para o dono
        # do app perceber e corrigir o secrets.toml.
        print(f"AVISO: valor em [credentials] para '{username}' não é um hash bcrypt válido.", file=sys.stderr)
        return False


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
            # O PORQUE: só marcamos a intenção aqui e saímos do diálogo com
            # um rerun normal -- rodar a limpeza de cookie/sessão (que
            # termina com st.stop()) DE DENTRO do callback do diálogo se
            # mostrou instável (loop). A limpeza de verdade acontece um
            # pouco mais abaixo no script, fora de qualquer modal.
            st.session_state["_solicitar_logout"] = True
            st.rerun()
    with col2:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()


def _dialog_confirmar_solicitacao(nome: str, email: str, justificativa: str):
    @st.dialog("Confirmar solicitação de acesso")
    def _inner():
        st.write(f"Confirma o envio da solicitação de acesso para **{nome}** ({email})?")
        st.caption("Um administrador vai revisar e aprovar (ou não) o seu pedido.")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Sim, enviar", type="primary", use_container_width=True):
                repo.create_access_request(nome, email, justificativa)
                st.session_state["_solicitacao_enviada"] = True
                st.rerun()
        with col2:
            if st.button("Cancelar", use_container_width=True):
                st.rerun()
    _inner()


def _tela_login():
    st.write("")
    st.write("")
    col_a, col_b, col_c = st.columns([1, 1.1, 1])
    with col_b:
        st.title("📊 Task Tracker")

        guest_link_invalido = st.session_state.pop("_guest_link_invalido", False)
        if guest_link_invalido:
            st.error(
                "Este link de convidado não é válido ou o acesso foi revogado. "
                "Solicite um novo acesso abaixo, se precisar."
            )

        st.subheader("Acesso restrito")
        with st.form("login_form"):
            username = st.text_input("Usuário", key="login_username_field")
            password = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("Entrar", type="primary", use_container_width=True)

        # O PORQUE: st.components.v1.html() roda dentro de um iframe --
        # localmente costuma dar pra "escapar" dele via window.top/parent
        # pra alcançar a página de verdade, mas publicado (Streamlit Cloud)
        # esse acesso é bloqueado por segurança do navegador (o iframe é
        # tratado como origem diferente, mesmo sendo o mesmo site) -- é
        # exatamente por isso que funcionava local e não online. st.html()
        # com unsafe_allow_javascript=True resolve isso: o conteúdo entra
        # DIRETO na página principal, sem iframe nenhum, então o script já
        # nasce no lugar certo -- sem precisar de nenhum truque pra escapar
        # de onde quer que seja.
        st.html(
            """
            <script>
            const campo = document.querySelector('input[aria-label="Usuário"]');
            if (campo) {
                setTimeout(function() { campo.focus(); }, 150);
            }
            </script>
            """,
            unsafe_allow_javascript=True,
        )

        if entrar:
            with st.spinner("Verificando credenciais..."):
                login_ok = _validar_login(username, password)
            if login_ok:
                token = _criar_sessao(username)
                st.session_state.authenticated = True
                st.session_state.auth_username = username
                st.session_state.auth_token = token
                st.session_state.user_role = "admin"
                _definir_cookie_sessao(token)
                st.rerun()
            else:
                st.error("Usuário ou senha incorretos.")

        # O PORQUE: em vez de compartilhar uma senha fixa de "convidado" com
        # qualquer pessoa (o que era o modelo antigo), quem não tem
        # usuário/senha pede acesso aqui -- fica pendente até um admin
        # aprovar manualmente na área administrativa (barra lateral).
        st.markdown("---")
        if st.session_state.pop("_solicitacao_enviada", False):
            st.success("✅ Solicitação enviada! Aguarde a aprovação de um administrador.")
        with st.expander("Não tem acesso? Solicitar acesso de convidado"):
            if not ADMIN_USERNAME:
                st.caption("Solicitação de acesso desativada no momento (não configurada pelo administrador).")
            else:
                with st.form("access_request_form", clear_on_submit=True):
                    req_nome = st.text_input("Nome")
                    req_email = st.text_input("E-mail")
                    req_justificativa = st.text_area("Por que você precisa de acesso?")
                    req_enviar = st.form_submit_button("Enviar Solicitação", use_container_width=True)

                if req_enviar:
                    email_normalizado = (req_email or "").strip().lower()
                    if not req_nome.strip() or not email_normalizado or not req_justificativa.strip():
                        st.error("Preencha nome, e-mail e justificativa.")
                    elif "@" not in email_normalizado or "." not in email_normalizado.split("@")[-1]:
                        st.error("Digite um e-mail válido.")
                    elif repo.get_active_access_request_by_email(email_normalizado):
                        st.error("Já existe uma solicitação ativa com esse e-mail. Aguarde a análise.")
                    elif repo.count_active_access_requests() >= 5:
                        st.error("Não há mais solicitações disponíveis no momento. Tente novamente mais tarde.")
                    else:
                        _dialog_confirmar_solicitacao(req_nome.strip(), email_normalizado, req_justificativa.strip())


if st.session_state.get("_solicitar_logout"):
    # O PORQUE: rodar isso aqui, fora de qualquer diálogo/modal, é o que
    # resolve o loop relatado ao confirmar "Sair" dentro do modal.
    _limpar_cookie_sessao()
    _limpar_sessao_local()
    st.rerun()

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

    # O PORQUE: primeira execução desta aba/sessão (ex.: acabou de dar F5).
    # Antes de exigir login de novo, verifica se o NAVEGADOR já tem um
    # cookie de uma sessão anterior ainda válida -- se validar, restaura o
    # login automaticamente, sem pedir usuário/senha de novo. Usar cookie
    # (em vez do token na URL, como era antes) é essencial: um cookie não
    # viaja quando você copia/cola o endereço da página, então colar o link
    # em outro navegador/aba anônima NÃO loga mais ninguém -- só o
    # navegador que genuinamente recebeu o cookie no login continua
    # autenticado.
    try:
        qp_token = st.context.cookies.get(_COOKIE_SESSAO)
    except Exception:
        qp_token = None
    if qp_token:
        qp_username = _validar_sessao(qp_token)
        if qp_username:
            st.session_state.authenticated = True
            st.session_state.auth_username = qp_username
            st.session_state.auth_token = qp_token
            st.session_state.user_role = "admin"

    # O PORQUE: mesma ideia do "?s=", mas para o link de convidado
    # (?g=<token>) -- em vez de validar por assinatura (HMAC), checa AO VIVO
    # no banco se aquele token ainda corresponde a uma solicitação com
    # status 'approved'. É isso que permite revogar o acesso de um
    # convidado instantaneamente (rejeitar/excluir o pedido na área admin),
    # sem esperar nenhum prazo expirar.
    if not st.session_state.authenticated:
        qp_guest_token = st.query_params.get("g")
        if qp_guest_token:
            if not ADMIN_USERNAME:
                st.session_state["_guest_link_invalido"] = True
            else:
                guest_info = repo.get_access_request_by_token(qp_guest_token)
                if guest_info:
                    st.session_state.authenticated = True
                    # O PORQUE: usamos o username do ADMIN aqui de propósito --
                    # é de quem são os dados que o convidado deve enxergar
                    # (work_logs são gravados por username, e o convidado não
                    # tem os seus próprios). A identidade real do convidado
                    # fica em guest_name/guest_email, só para exibição.
                    st.session_state.auth_username = ADMIN_USERNAME
                    st.session_state.user_role = "guest"
                    st.session_state.guest_name = guest_info["name"]
                    st.session_state.guest_email = guest_info["email"]
                    st.session_state.guest_token = qp_guest_token
                else:
                    st.session_state["_guest_link_invalido"] = True

if not st.session_state.authenticated:
    _tela_login()
    st.stop()

# O PORQUE: por padrão, o texto das abas do Streamlit sai pequeno e sem
# destaque visual, dificultando a navegação. Este CSS aumenta o tamanho da
# fonte e o peso do texto das abas. Os seletores cobrem tanto a estrutura
# mais recente do Streamlit (com <p> dentro do botão) quanto uma variação
# mais antiga, para o destaque funcionar independente da versão instalada.
#
# O PORQUE (responsividade): o 1.5rem fixo de antes ficava ótimo em
# desktop/tablet, mas em celular "comia" a tela toda com texto de aba
# gigante, e em monitores muito grandes (ultrawide, 4K) o conteúdo esticava
# de ponta a ponta, ficando fino e difícil de ler. A solução usa 3 partes:
#   1) Um teto de largura no container principal (.main .block-container),
#      centralizado, para telas muito grandes -- em vez de esticar o
#      conteúdo, sobra respiro nas laterais, como em qualquer site bem
#      comportado.
#   2) Media query para tablets/telas médias -- ajusta só o padding lateral.
#   3) Media query para celular (max-width: 640px) -- reduz fonte de abas,
#      títulos (h1/h2/h3) e padding, e empilha melhor os elementos, sem
#      depender de detectar o dispositivo por JavaScript.
st.markdown(
    """
    <style>
    /* Base: todas as telas */
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

    /* Teto de largura para telas grandes/ultrawide -- evita conteúdo
       esticado de ponta a ponta; abaixo desse ponto (a maioria dos
       desktops/tablets), o layout continua exatamente como antes. */
    .main .block-container {
        max-width: 1600px;
        margin-left: auto;
        margin-right: auto;
    }

    /* O PORQUE: a grid de Registro de Atividades usa st.columns() (7
       colunas: ID/Data/Projeto/Categoria/Descrição/Horas/Ações) uma vez
       por linha de cabeçalho e uma vez por registro. Por padrão, o
       Streamlit EMPILHA colunas em telas estreitas -- ótimo pra um par de
       botões, péssimo aqui: cada tarefa da lista viraria 7 blocos
       empilhados, tornando a lista enorme e impossível de escanear no
       celular.
       TENTATIVA ANTERIOR (removida): um min-width de 600px num seletor
       genérico demais acabou grudando em CADA coluna individualmente (não
       só na linha inteira), fazendo a coluna "ID" sozinha ficar mais larga
       que a tela do celular. Corrigido usando ">" (filho direto, não
       qualquer descendente) e um valor bem mais modesto -- mesmo que o
       seletor pegue algo a mais do que o previsto, o estrago é pequeno,
       não catastrófico como antes.
    */
    .st-key-atividades_grid {
        overflow-x: auto;
        padding-bottom: 6px;
    }
    .st-key-atividades_grid [data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap !important;
    }
    .st-key-atividades_grid [data-testid="stHorizontalBlock"] > div {
        min-width: 45px !important;
        flex-shrink: 1 !important;
    }
    @media (max-width: 640px) {
        .st-key-atividades_grid [data-testid="stMarkdownContainer"] p,
        .st-key-atividades_grid [data-testid="stMarkdownContainer"] div,
        .st-key-atividades_grid button {
            font-size: 0.72rem !important;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
    }

    /* Tablets e notebooks menores: só reduz um pouco o padding lateral */
    @media (max-width: 992px) {
        .main .block-container {
            padding-left: 1.5rem;
            padding-right: 1.5rem;
        }
    }

    /* Celular: abas, títulos e padding menores, para não dominar a tela */
    @media (max-width: 640px) {
        .stTabs [data-baseweb="tab-list"] button {
            padding: 8px 10px;
        }
        .stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p,
        .stTabs [data-baseweb="tab-list"] button div {
            font-size: 0.95rem !important;
            font-weight: 600 !important;
        }
        .main .block-container {
            padding-left: 0.7rem;
            padding-right: 0.7rem;
            padding-top: 1.5rem;
        }
        h1 { font-size: 1.5rem !important; }
        h2 { font-size: 1.2rem !important; }
        h3 { font-size: 1.05rem !important; }
        /* Botões grandes de "Aplicar Filtro"/"Novo Registro" etc. ficam
           ocupando linha inteira e um pouco mais baixos, mais fáceis de
           tocar com o dedo. */
        .stButton button, .stFormSubmitButton button {
            padding-top: 0.6rem;
            padding-bottom: 0.6rem;
        }
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
# O PORQUE: a versão anterior deste indicador escurecia a tela inteira e
# bloqueava clique a CADA rerun do Streamlit (mesmo os rapidinhos, <1s),
# sem nenhuma transição -- fazia até ações rápidas parecerem lentas/pesadas.
# Troquei pelo padrão "barra de progresso fina no topo" (o mesmo que
# YouTube/GitHub usam): leve, não bloqueia a tela, e some sozinha quando o
# rerun termina (a barra só existe enquanto "stStatusWidget" -- o indicador
# nativo de "processando" do Streamlit -- estiver no DOM).
#
# Dois truques pra parecer mais rápido de verdade, não só visualmente:
# 1) `animation-delay: 150ms` -- a barra fica INVISÍVEL nos primeiros 150ms.
#    Reruns mais rápidos que isso (a maioria dos cliques neste app) nunca
#    chegam a mostrar nada -- parecem instantâneos. Só reruns mais lentos
#    (chamada de IA, consulta grande no Turso) mostram o indicador.
# 2) `fade-in-leve` -- quando aparece, é com um fade suave (0.2s), não um
#    "pop" abrupto.
# ATENÇÃO: como antes, depende do testid "stStatusWidget" (pode mudar em
# versões futuras do Streamlit -- confira no DevTools se parar de aparecer).
st.markdown(
    """
    <style>
    @keyframes barra-progresso-sweep {
        0%   { background-position: 200% 0; }
        100% { background-position: -200% 0; }
    }
    @keyframes fade-in-leve {
        from { opacity: 0; }
        to   { opacity: 1; }
    }

    [data-testid="stStatusWidget"] {
        transform: scale(1.15);
        transform-origin: top right;
    }

    body:has([data-testid="stStatusWidget"])::before {
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        height: 3px;
        background: linear-gradient(90deg, transparent, #ff4b4b, transparent);
        background-size: 60% 100%;
        z-index: 999999;
        pointer-events: none;
        animation:
            barra-progresso-sweep 1s linear infinite,
            fade-in-leve 0.2s ease-in 150ms both;
    }

    body:has([data-testid="stStatusWidget"])::after {
        content: "Carregando...";
        position: fixed;
        top: 10px;
        right: 16px;
        background: rgba(38, 39, 48, 0.92);
        color: white;
        font-size: 0.8rem;
        font-weight: 600;
        padding: 4px 12px;
        border-radius: 999px;
        z-index: 999999;
        pointer-events: none;
        animation: fade-in-leve 0.2s ease-in 150ms both;
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


# O PORQUE (responsividade dos gráficos): o Plotly usa tamanho de fonte fixo
# em pixels, que não escala sozinho com a largura do container -- então o
# mesmo gráfico que fica ótimo num monitor grande pode sair com textos
# sobrepostos/apertados num celular, e com espaço em branco desperdiçado
# (margens grandes, legenda lateral) num monitor ultrawide. Em vez de tentar
# detectar o tamanho de tela real (exigiria JavaScript/componente extra),
# esta função aplica um layout "neutro", que funciona razoavelmente bem em
# qualquer largura: fonte um pouco menor que o padrão do Plotly, legenda
# horizontal embaixo do gráfico (em vez de do lado, que rouba largura útil
# em telas estreitas), margens enxutas, e rótulos do eixo X inclinados
# quando há muitas categorias (evita sobreposição de texto).
def apply_responsive_layout(fig, rotate_xaxis: bool = False):
    # O PORQUE: quando os rótulos do eixo X giram (rotate_xaxis=True, usado
    # quando há muitas categorias/nomes longos), eles descem bem mais que
    # rótulos na horizontal -- com a legenda numa posição fixa de sempre,
    # os dois brigavam de espaço (foi exatamente o que aconteceu: o texto
    # da legenda sobrepondo o rótulo/título do eixo). Com rótulos girados,
    # empurra a legenda mais pra baixo e reserva mais margem embaixo pra
    # caber os dois sem se tocar.
    y_legenda = -0.55 if rotate_xaxis else -0.25
    margem_inferior = 120 if rotate_xaxis else 10
    fig.update_layout(
        font=dict(size=12),
        title_font=dict(size=15),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=y_legenda,
            xanchor="center",
            x=0.5,
            font=dict(size=11),
        ),
        margin=dict(l=10, r=10, t=50, b=margem_inferior),
    )
    if rotate_xaxis:
        fig.update_xaxes(tickangle=-45, tickfont=dict(size=10))
    return fig


def renderizar_toggle_colunas_grafico(escopo: str):
    """
    Desenha o toggle "flutuante" (sticky) uma única vez por aba/tela, pra
    escolher entre 1 ou 2 gráficos lado a lado. Chame isso UMA vez, no topo
    da seção de gráficos -- os pares de coluna individuais de cada gráfico
    devem usar obter_par_colunas_grafico() (que só LÊ o valor, sem desenhar
    outro widget -- widgets repetidos com a mesma chave dão erro no
    Streamlit).
    """
    chave = f"chart_cols_{escopo}"
    if chave not in st.session_state:
        st.session_state[chave] = 2

    container_key = f"toggle_graficos_{escopo}"
    st.markdown(
        f"""
        <style>
        .st-key-{container_key} {{
            position: sticky;
            top: 0.4rem;
            z-index: 999;
            display: inline-block;
            background: rgba(120, 120, 120, 0.18);
            backdrop-filter: blur(4px);
            padding: 2px 10px;
            border-radius: 999px;
            margin-bottom: 8px;
        }}
        .st-key-{container_key} [data-testid="stRadio"] > label {{
            display: none;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key=container_key):
        escolha = st.radio(
            "Gráficos por linha",
            ["1 gráfico por linha", "2 gráficos por linha"],
            index=(0 if st.session_state[chave] == 1 else 1),
            horizontal=True,
            key=f"{chave}_radio",
            label_visibility="collapsed",
        )
        st.session_state[chave] = 1 if escolha.startswith("1") else 2


def obter_par_colunas_grafico(escopo: str):
    """
    Só LÊ a preferência já escolhida em renderizar_toggle_colunas_grafico()
    (chamada antes, uma vez só) e devolve duas referências de coluna prontas
    pra usar em "with col_a:"/"with col_b:" já existentes:
      - 2 por linha: col_a e col_b são colunas DIFERENTES (lado a lado).
      - 1 por linha: col_a e col_b são o MESMO container -- os dois "with"
        escrevem um embaixo do outro, sem precisar reescrever nenhum
        gráfico já pronto.
    """
    cols_por_linha = st.session_state.get(f"chart_cols_{escopo}", 2)
    if cols_por_linha == 2:
        return st.columns(2)
    container_unico = st.container()
    return container_unico, container_unico


# O PORQUE: paleta única, usada tanto nos gráficos (matplotlib) quanto nas
# tabelas/cabeçalho (reportlab) dos PDFs -- dá uma identidade visual
# consistente ao relatório, em vez de cada gráfico sair com as cores
# padrão (aleatórias) do matplotlib.
PDF_COR_PRIMARIA = "#FF4B4B"
PDF_COR_TEXTO = "#262730"
PDF_COR_MUTED = "#6E6E6E"
PDF_COR_FUNDO_CLARO = "#F7F7F9"
PDF_PALETA_GRAFICOS = ["#FF4B4B", "#4B7BFF", "#2CA58D", "#F2A65A", "#8E7DBE", "#5FA8D3", "#D45D79", "#8CC63F"]


def _aplicar_estilo_mpl():
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "axes.edgecolor": "#DDDDDD",
        "axes.linewidth": 0.8,
        "axes.grid": True,
        "grid.color": "#EEEEEE",
        "grid.linewidth": 0.8,
        "axes.axisbelow": True,
        "text.color": PDF_COR_TEXTO,
        "axes.labelcolor": PDF_COR_TEXTO,
        "xtick.color": "#444444",
        "ytick.color": "#444444",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
    })


def _grafico_barras_mpl(df: pd.DataFrame, x_col: str, y_col: str, titulo: str, xlabel: str, ylabel: str,
                         cor_col: str = None, figsize=(8, 4.3), mostrar_valores: bool = True):
    # O PORQUE: matplotlib (não Plotly/kaleido) para os gráficos do PDF --
    # ver justificativa completa em gerar_pdf_relatorio(). "Agg" é o
    # backend sem tela do matplotlib, o correto para rodar num servidor
    # (sem monitor conectado).
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _aplicar_estilo_mpl()

    fig, ax = plt.subplots(figsize=figsize)
    if cor_col:
        pivot = df.pivot_table(index=x_col, columns=cor_col, values=y_col, aggfunc="sum", fill_value=0)
        pivot.plot(kind="bar", ax=ax, color=PDF_PALETA_GRAFICOS[:max(len(pivot.columns), 1)], edgecolor="white", linewidth=0.6)
        ax.legend(frameon=False, fontsize=8)
    else:
        agrupado = df.groupby(x_col)[y_col].sum().sort_values(ascending=False)
        cores = [PDF_PALETA_GRAFICOS[i % len(PDF_PALETA_GRAFICOS)] for i in range(len(agrupado))]
        agrupado.plot(kind="bar", ax=ax, color=cores, edgecolor="white", linewidth=0.6)
        if mostrar_valores:
            # O PORQUE: rótulo do valor em cima de cada barra -- pedido
            # explícito de relatório "mais detalhado", em vez de precisar
            # olhar pro eixo pra estimar o número.
            for i, v in enumerate(agrupado.values):
                ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8, color=PDF_COR_TEXTO)
    ax.set_title(titulo, fontsize=13, fontweight="bold", color=PDF_COR_TEXTO, pad=12)
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    return fig


def _grafico_pizza_mpl(df: pd.DataFrame, nomes_col: str, valores_col: str, titulo: str, figsize=(6, 5)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _aplicar_estilo_mpl()

    fig, ax = plt.subplots(figsize=figsize)
    agrupado = df.groupby(nomes_col)[valores_col].sum().sort_values(ascending=False)
    cores = [PDF_PALETA_GRAFICOS[i % len(PDF_PALETA_GRAFICOS)] for i in range(len(agrupado))]
    _, _, autotexts = ax.pie(
        agrupado.values, labels=agrupado.index, autopct="%1.0f%%",
        colors=cores, textprops={"fontsize": 9},
        wedgeprops={"edgecolor": "white", "linewidth": 1.5},
        pctdistance=0.75,
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontweight("bold")
    ax.set_title(titulo, fontsize=13, fontweight="bold", color=PDF_COR_TEXTO, pad=12)
    fig.tight_layout()
    return fig


def _grafico_impedimentos_mpl(df: pd.DataFrame, x_col: str, figsize=(8, 4)):
    # O PORQUE: gráfico novo, não existia nos PDFs antes -- responde "os
    # impedimentos/dúvidas estão concentrados em algum período?", útil pra
    # retro, e usa colunas (is_impedimento/is_duvida) que já vêm no
    # DataFrame filtrado, sem precisar de consulta nova ao banco.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _aplicar_estilo_mpl()

    agrupado = df.groupby(x_col)[["is_impedimento", "is_duvida"]].sum()
    agrupado.columns = ["Impedimentos", "Dúvidas"]
    fig, ax = plt.subplots(figsize=figsize)
    agrupado.plot(kind="bar", stacked=True, ax=ax, color=["#FF4B4B", "#F2A65A"], edgecolor="white", linewidth=0.6)
    ax.set_title("Impedimentos e Dúvidas ao Longo do Tempo", fontsize=13, fontweight="bold", color=PDF_COR_TEXTO, pad=12)
    ax.set_xlabel("")
    ax.set_ylabel("Quantidade", fontsize=9)
    ax.legend(frameon=False, fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    return fig


def _mpl_fig_para_png_bytes(fig, dpi: int = 170) -> bytes:
    import matplotlib.pyplot as plt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def gerar_pdf_relatorio(titulo: str, subtitulo: str, paragrafos: list = None, figuras: list = None,
                         kpis: list = None, tabelas: list = None) -> bytes:
    """
    Monta um PDF (cabeçalho com marca, KPIs em destaque, texto, tabelas e
    gráficos) e devolve os bytes prontos para usar em st.download_button.

    paragrafos: lista de itens -- cada item é uma string (parágrafo normal)
    ou uma tupla (texto, nome_do_estilo) para usar um estilo diferente
    (ex.: ("O que fiz ontem:", "Heading4")). Uma string vazia ("") vira um
    espaçamento em branco entre blocos. O PORQUE de usar (texto, estilo) em
    vez de tags tipo "<b>...</b>" dentro do texto: todo texto passa por um
    escape de <, > e & (necessário pra uma descrição de tarefa com esses
    caracteres não quebrar o PDF) -- se o negrito fosse uma tag embutida no
    mesmo texto, o escape anularia a tag também. Separar "o quê" (texto) de
    "como" (estilo) evita esse conflito.
    kpis: lista opcional de tuplas (valor: str, rótulo: str) -- vira uma
    fileira de "cartões" logo no topo (ex.: [("42h", "Total de Horas")]).
    tabelas: lista opcional de tuplas (legenda: str, cabecalhos: list[str],
    linhas: list[list[str]]) -- números exatos, complementando os gráficos
    (que são melhores pra enxergar proporção/tendência, não pra ler um
    valor preciso).
    figuras: lista de tuplas (legenda: str, fig: matplotlib.figure.Figure)
    -- use _grafico_barras_mpl()/_grafico_pizza_mpl()/_grafico_impedimentos_mpl()
    para montar cada uma a partir de um DataFrame, não os objetos Plotly
    usados na tela (Plotly é interativo; dentro de um PDF só existe imagem
    estática).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle

    paragrafos = paragrafos or []
    figuras = figuras or []
    kpis = kpis or []
    tabelas = tabelas or []

    cor_primaria = rl_colors.HexColor(PDF_COR_PRIMARIA)
    cor_texto = rl_colors.HexColor(PDF_COR_TEXTO)
    cor_muted = rl_colors.HexColor(PDF_COR_MUTED)
    cor_fundo_claro = rl_colors.HexColor(PDF_COR_FUNDO_CLARO)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=30 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
    )
    largura_util = A4[0] - 36 * mm

    base = getSampleStyleSheet()
    estilos = {
        "Title": ParagraphStyle("TituloRelatorio", parent=base["Title"], textColor=cor_texto, fontSize=19, spaceAfter=2, alignment=0),
        "Normal": ParagraphStyle("CorpoRelatorio", parent=base["Normal"], textColor=cor_texto, fontSize=10, leading=14),
        "Subtitulo": ParagraphStyle("SubtituloRelatorio", parent=base["Normal"], textColor=cor_muted, fontSize=10),
        "Heading3": ParagraphStyle("SecaoRelatorio", parent=base["Heading3"], textColor=cor_primaria, fontSize=13, spaceBefore=2, spaceAfter=4),
        "Heading4": ParagraphStyle("SubsecaoRelatorio", parent=base["Heading4"], textColor=cor_texto, fontSize=11, spaceBefore=2, spaceAfter=2),
    }

    def _cabecalho_rodape(canvas, documento):
        # O PORQUE: SimpleDocTemplate não desenha cabeçalho/rodapé sozinho
        # -- isso roda a cada página, via callback de baixo nível do
        # reportlab (canvas), pra dar a faixa colorida no topo (identidade
        # visual) e o rodapé com data de geração + número de página.
        canvas.saveState()
        canvas.setFillColor(cor_primaria)
        canvas.rect(0, A4[1] - 14 * mm, A4[0], 14 * mm, fill=1, stroke=0)
        canvas.setFillColor(rl_colors.white)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(18 * mm, A4[1] - 9.5 * mm, "Task Tracker")
        canvas.setFillColor(cor_muted)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(18 * mm, 10 * mm, f"Gerado em {agora_br().strftime('%d/%m/%Y às %H:%M')}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Página {documento.page}")
        canvas.restoreState()

    story = [Paragraph(titulo, estilos["Title"])]
    if subtitulo:
        story.append(Paragraph(subtitulo, estilos["Subtitulo"]))
    story.append(Spacer(1, 8 * mm))

    if kpis:
        # O PORQUE: cartões de KPI em vez de texto corrido -- leitura
        # instantânea dos números principais, sem precisar ler frase por
        # frase, igual aos cartões que já existem na tela do Dashboard.
        linha_valores = [Paragraph(f"<font size=17 color='{PDF_COR_PRIMARIA}'><b>{v}</b></font>", estilos["Normal"]) for v, _ in kpis]
        linha_rotulos = [Paragraph(f"<font size=8 color='{PDF_COR_MUTED}'>{l}</font>", estilos["Normal"]) for _, l in kpis]
        largura_col = largura_util / len(kpis)
        tabela_kpi = Table([linha_valores, linha_rotulos], colWidths=[largura_col] * len(kpis))
        tabela_kpi.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), cor_fundo_claro),
            ("TOPPADDING", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ]))
        story.append(tabela_kpi)
        story.append(Spacer(1, 8 * mm))

    # O PORQUE: tabelas ANTES de parágrafos de propósito -- no PDF da Daily,
    # as tabelas de atividades (O que fiz ontem/O que farei hoje) precisam
    # aparecer antes do texto de Impedimentos/Dúvidas. O PDF do Dashboard
    # não usa "paragrafos" (só kpis/tabelas/figuras), então essa ordem não
    # muda nada por lá.
    for item_tabela in tabelas:
        # O PORQUE: aceita tanto (legenda, cabecalhos, linhas) quanto
        # (legenda, cabecalhos, linhas, larguras) -- o 4º elemento é
        # opcional, só usado quando uma coluna precisa de largura
        # proporcional específica (ex.: "Descrição" bem mais larga que
        # "Horas"). Sem ele, divide a largura da página igualmente entre as
        # colunas -- ok pra tabelas com conteúdo curto (ex.: "Resumo por
        # Projeto"), mas seria estreito demais pra uma coluna de texto
        # longo, daí o suporte às larguras explícitas.
        if len(item_tabela) == 4:
            legenda, cabecalhos, linhas, larguras = item_tabela
        else:
            legenda, cabecalhos, linhas = item_tabela
            larguras = [largura_util / len(cabecalhos)] * len(cabecalhos)

        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(legenda, estilos["Heading3"]))
        story.append(Spacer(1, 2 * mm))

        def _celula_segura(texto, estilo):
            # O PORQUE: célula vira Paragraph (não string crua) -- é o que
            # permite o texto QUEBRAR LINHA dentro da largura da coluna, em
            # vez de forçar a coluna a alargar até caber numa linha só
            # (isso é o que causava o PDF "cortado": uma coluna de
            # descrição longa empurrando a tabela pra fora da página). Mesmo
            # escape de sempre, já que Paragraph interpreta <, > e & como
            # marcação.
            texto_seguro = str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return Paragraph(texto_seguro, estilo)

        estilo_cabecalho = ParagraphStyle(
            "CabecalhoTabela", parent=estilos["Normal"], textColor=rl_colors.white,
            fontName="Helvetica-Bold", fontSize=9,
        )
        estilo_celula = ParagraphStyle("CelulaTabela", parent=estilos["Normal"], fontSize=9, leading=11)

        linha_cabecalho = [_celula_segura(c, estilo_cabecalho) for c in cabecalhos]
        linhas_dados = [[_celula_segura(c, estilo_celula) for c in linha] for linha in linhas]

        tabela = Table([linha_cabecalho] + linhas_dados, colWidths=larguras, hAlign="LEFT", repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), cor_primaria),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, cor_fundo_claro]),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#E2E2E2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(tabela)

    for item in paragrafos:
        if item == "":
            story.append(Spacer(1, 4 * mm))
            continue
        texto, nome_estilo = item if isinstance(item, tuple) else (item, "Normal")
        # O PORQUE: escapa < > & antes de passar pro Paragraph -- ele
        # interpreta o texto como XML/HTML simplificado (é assim que
        # <sub>/<super>/<b> funcionam internamente no reportlab), então um
        # "&" ou "<" cru dentro de uma descrição de tarefa, por exemplo,
        # quebraria a geração do PDF em vez de aparecer como texto.
        texto_seguro = str(texto).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(texto_seguro, estilos.get(nome_estilo, estilos["Normal"])))

    for legenda, fig in figuras:
        story.append(Spacer(1, 8 * mm))
        # O PORQUE: removido o título em Paragraph que ficava aqui em cima
        # (repetia o mesmo texto que o próprio matplotlib já desenha DENTRO
        # da imagem, via ax.set_title() -- redundante) -- além de duplicado,
        # causava um problema pior: texto e imagem eram dois elementos
        # separados, então o reportlab podia colocar o título sozinho no
        # fim de uma página e só o gráfico na página seguinte, deixando um
        # título "órfão" com um vão vazio embaixo. Sem o título solto, sobra
        # só a imagem (que já é auto-suficiente) -- se não couber no
        # espaço restante da página, ela inteira passa pra próxima, sem
        # deixar nada pra trás.
        png_bytes = _mpl_fig_para_png_bytes(fig)

        # O PORQUE: mede a proporção da IMAGEM JÁ SALVA (depois do corte
        # bbox_inches="tight" em _mpl_fig_para_png_bytes), não do figsize
        # nominal usado pra criar a figura. O corte "tight" remove espaço
        # em branco sobrando ao redor do conteúdo real -- e pode remover
        # quantidades BEM diferentes de cada lado (ex.: rótulos de uma
        # pizza que se espalham mais na horizontal que na vertical, ou
        # vice-versa, dependendo de quantas fatias/nomes tem). Usar o
        # figsize original (de antes do corte) pra decidir a proporção da
        # caixa no PDF ficava incompatível com o formato real da imagem
        # depois de cortada -- e é isso que causava a pizza saindo
        # esticada (numa direção ou na outra, dependendo do gráfico).
        from PIL import Image as _PILImage
        img_info = _PILImage.open(io.BytesIO(png_bytes))
        largura_px, altura_px = img_info.size
        razao_altura_largura = altura_px / largura_px

        altura_maxima = 100 * mm
        largura_final = largura_util
        altura_final = largura_final * razao_altura_largura
        if altura_final > altura_maxima:
            altura_final = altura_maxima
            largura_final = altura_final / razao_altura_largura

        imagem = RLImage(io.BytesIO(png_bytes), width=largura_final, height=altura_final)
        # O PORQUE: quando o gráfico fica mais estreito que a página (caso
        # da pizza, depois de limitada pela altura máxima), centraliza em
        # vez de deixar "grudado" na margem esquerda com espaço vazio sobrando
        # à direita.
        imagem.hAlign = "CENTER"
        story.append(imagem)

    doc.build(story, onFirstPage=_cabecalho_rodape, onLaterPages=_cabecalho_rodape)
    buffer.seek(0)
    return buffer.getvalue()


# O PORQUE: Limite de upload em MB. O valor "oficial" (que barra o arquivo
# antes mesmo de chegar ao servidor) fica em .streamlit/config.toml
# (server.maxUploadSize). Essa constante e a checagem abaixo sao uma segunda
# camada de defesa: garante a mesma regra mesmo que o app rode em outro
# ambiente sem esse config.toml, e da uma mensagem de erro amigavel em
# portugues em vez do erro generico do Streamlit.
MAX_UPLOAD_SIZE_MB = 20
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


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


# ==========================================
# ESTIMATIVA DE ESFORÇO POR IA (via n8n)
# ==========================================
# O PORQUE: chave nova, opcional -- se não estiver configurada, os recursos
# de IA (estimativa de duração + classificação de projeto/categoria) ficam
# desligados automaticamente, e tanto a Sincronização quanto o formulário
# manual caem no comportamento anterior (esforço digitado/flat + escolha
# manual de projeto/categoria). Nada quebra pra quem não configurar isso.
N8N_AI_ESTIMATE_WEBHOOK_URL = os.environ.get("N8N_AI_ESTIMATE_WEBHOOK_URL", "").strip()

# O PORQUE: palavras literais que você mesmo escreve nas suas anotações pra
# marcar um dia de plantão/hora extra -- é uma DETECÇÃO POR PALAVRA-CHAVE
# (não por IA) de propósito: é um marcador que você escolhe escrever, não
# algo que precise de julgamento/interpretação. "OVERTIME" cobre os
# registros mais recentes; "plantão"/"plantao" cobre os mais antigos (>1-2
# meses), como você descreveu.
OVERTIME_DAY_KEYWORDS = ["overtime", "plantão", "plantao"]


def estimar_esforco_com_ia(tasks: list, known_projects: list, known_categories: list, timeout: int = 90):
    """
    tasks: lista de dicts {"id": int, "description": str}.
    Retorna (results, error_message). Em caso de sucesso, error_message é
    None e results é uma lista de dicts {"id", "estimated_minutes",
    "project", "category", "is_new_project", "is_new_category"}. Em caso de
    falha (webhook não configurado, n8n fora do ar, resposta inválida da
    IA etc.), results é None e error_message explica o motivo -- quem
    chamar esta função deve, nesse caso, cair de volta pro comportamento
    sem IA (mesmo padrão de fallback usado para o Turso).
    """
    if not N8N_AI_ESTIMATE_WEBHOOK_URL:
        return None, "N8N_AI_ESTIMATE_WEBHOOK_URL não configurado nos Secrets."
    if not tasks:
        return [], None
    try:
        resp = requests.post(
            N8N_AI_ESTIMATE_WEBHOOK_URL,
            json={"known_projects": known_projects, "known_categories": known_categories, "tasks": tasks},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results")
        if not isinstance(results, list):
            return None, data.get("error", "Resposta do workflow de IA em formato inesperado.")
        return results, None
    except requests.exceptions.Timeout:
        return None, f"O workflow de IA não respondeu em {timeout}s (timeout)."
    except Exception as e:
        return None, f"Falha ao chamar o workflow de IA: {e}"


def compute_overtime_days(df: pd.DataFrame) -> set:
    # O PORQUE: um dia inteiro é "de plantão/overtime" se QUALQUER uma das
    # descrições registradas naquele dia contiver a palavra-chave -- não
    # precisa estar em toda tarefa do dia, só precisa aparecer uma vez pra
    # sinalizar que aquele dia foge do padrão de 8h.
    overtime_days = set()
    if df.empty or "log_date" not in df.columns:
        return overtime_days
    for log_date, group in df.groupby("log_date"):
        combined = " ".join(group["description"].astype(str)).lower()
        if any(kw in combined for kw in OVERTIME_DAY_KEYWORDS):
            overtime_days.add(log_date)
    return overtime_days


def normalizar_horas_por_dia(df: pd.DataFrame, overtime_days: set, target_hours: float = 8.0) -> pd.DataFrame:
    """
    Para cada dia que NÃO estiver em overtime_days, reescala effort_hours
    proporcionalmente para que a soma do dia bata em target_hours -- mantém
    a proporção relativa entre as tarefas que a IA estimou (uma tarefa
    estimada em o dobro da duração de outra continua com o dobro depois de
    normalizada), só ajusta a escala geral. Dias em overtime_days ficam
    exatamente como a IA estimou, sem nenhum teto -- na prática, esses dias
    variam demais (de 30min a 9h a mais) pra qualquer regra fixa fazer
    sentido; a soma crua da estimativa de cada tarefa é o que reflete a
    realidade.
    """
    df = df.copy()
    if df.empty or "log_date" not in df.columns:
        return df
    for log_date, group_idx in df.groupby("log_date").groups.items():
        if log_date in overtime_days:
            continue
        total = df.loc[group_idx, "effort_hours"].sum()
        if total <= 0:
            continue
        factor = target_hours / total
        df.loc[group_idx, "effort_hours"] = (df.loc[group_idx, "effort_hours"] * factor).round(2)
    return df


def aplicar_estimativa_ia_e_normalizacao(df_to_insert: pd.DataFrame) -> tuple:
    """
    Função de alto nível que orquestra: chamar a IA (via n8n) para estimar
    duração + classificar projeto/categoria de cada linha de df_to_insert,
    detectar dias de overtime/plantão, normalizar as horas por dia, e
    devolver os nomes de projeto/categoria que são realmente novos (para
    quem chamar decidir se cadastra automaticamente).

    Retorna (df_atualizado, novos_projetos, novas_categorias, aviso).
    'aviso' é None em caso de sucesso, ou uma string explicando por que a
    estimativa por IA não foi aplicada (nesse caso, df_atualizado volta
    IGUAL ao que entrou, sem nenhuma mudança -- comportamento anterior).
    """
    if df_to_insert.empty:
        return df_to_insert, [], [], None

    known_projects = get_project_options()
    known_categories = get_category_options()
    tasks = [{"id": i, "description": str(desc)} for i, desc in enumerate(df_to_insert["description"].tolist())]

    results, error = estimar_esforco_com_ia(tasks, known_projects, known_categories)
    if error:
        return df_to_insert, [], [], error

    df = df_to_insert.reset_index(drop=True).copy()
    novos_projetos, novas_categorias = set(), set()

    by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
    for i in range(len(df)):
        r = by_id.get(i)
        if not r:
            continue
        minutes = r.get("estimated_minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            df.at[i, "effort_hours"] = round(minutes / 60.0, 2)
        project = (r.get("project") or "").strip()
        category = (r.get("category") or "").strip()
        if project:
            df.at[i, "project"] = project
            if r.get("is_new_project"):
                novos_projetos.add(project)
        if category:
            df.at[i, "category"] = category
            if r.get("is_new_category"):
                novas_categorias.add(category)

    overtime_days = compute_overtime_days(df)
    df = normalizar_horas_por_dia(df, overtime_days)

    return df, sorted(novos_projetos), sorted(novas_categorias), None


def recalcular_esforco_periodo_com_ia(username: str, start_date, end_date):
    """
    Busca os registros JÁ SALVOS entre start_date e end_date, reestima
    esforço/projeto/categoria por IA (reaproveitando a mesma
    aplicar_estimativa_ia_e_normalizacao usada na Sincronização e no
    formulário manual), e GRAVA as mudanças de volta no banco -- diferente
    da Sincronização, que só afeta linhas novas ainda não gravadas.

    Retorna (quantidade_atualizada, novos_projetos, novas_categorias, aviso).
    'aviso' != None significa que nada foi alterado (motivo explicado nele).
    """
    df_all = repo.get_all_logs_as_dataframe(username)
    if df_all.empty:
        return 0, [], [], "Você não tem nenhum registro salvo."

    # O PORQUE: log_date pode vir com hora/timezone dependendo do driver
    # (sqlite3 local vs libsql/Turso) -- normalizamos pra só "YYYY-MM-DD"
    # antes de comparar como texto, evitando comparação inconsistente entre
    # os dois bancos.
    log_date_str = df_all["log_date"].astype(str).str[:10]
    mask = (log_date_str >= str(start_date)) & (log_date_str <= str(end_date))
    df_periodo = df_all.loc[mask].copy()
    if df_periodo.empty:
        return 0, [], [], "Nenhum registro encontrado nesse período."

    df_atualizado, novos_projetos, novas_categorias, aviso = aplicar_estimativa_ia_e_normalizacao(df_periodo)
    if aviso:
        return 0, [], [], aviso

    for proj in novos_projetos:
        repo.add_custom_option("project", username, proj)
    for cat in novas_categorias:
        repo.add_custom_option("category", username, cat)

    updates = [
        {
            "id": int(row["id"]),
            "effort_hours": float(row["effort_hours"]),
            "project": row["project"],
            "category": row["category"],
        }
        for _, row in df_atualizado.iterrows()
    ]
    qtd = repo.update_logs_bulk(username, updates)
    return qtd, novos_projetos, novas_categorias, None


@st.dialog("Recalcular Esforço com IA")
def _dialog_confirmar_recalculo_ia(start_date, end_date):
    st.write(
        f"Isso vai **reestimar o esforço (horas)** e **reclassificar projeto/categoria**, "
        f"via IA, de todos os registros salvos entre **{start_date.strftime('%d/%m/%Y')}** "
        f"e **{end_date.strftime('%d/%m/%Y')}**. Os valores atuais desses campos serão "
        f"substituídos pelos sugeridos pela IA."
    )
    st.caption("Data, descrição, impedimento e dúvida não são alterados -- só esforço, projeto e categoria.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, recalcular", type="primary", use_container_width=True):
            run_blocking_action(
                "ia_recalcular_periodo",
                {"start_date": start_date, "end_date": end_date},
                processing_message="Consultando IA e recalculando...",
                success_message="✅ Recálculo concluído.",
                failure_message="⚠️ Não foi possível recalcular.",
            )
    with col2:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()


@st.dialog("Revogar acesso")
def _dialog_confirmar_revogar_acesso(request_id: int, nome: str):
    st.write(
        f"Revogar o acesso de **{nome}**? O link que foi compartilhado com essa "
        "pessoa deixa de funcionar imediatamente."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, revogar", type="primary", use_container_width=True):
            repo.reject_access_request(request_id)
            st.rerun()
    with col2:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()


@st.dialog("Excluir solicitação")
def _dialog_confirmar_excluir_solicitacao(request_id: int, nome: str):
    st.write(f"Excluir permanentemente a solicitação de **{nome}**? Esta ação não pode ser desfeita.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sim, excluir", type="primary", use_container_width=True):
            repo.delete_access_request(request_id)
            st.rerun()
    with col2:
        if st.button("Cancelar", use_container_width=True):
            st.rerun()


def render_admin_solicitacoes():
    # O PORQUE: área administrativa própria (não é mais uma aba) -- some o
    # resto do app enquanto estiver aberta, evitando misturar a tela de
    # administração com as abas normais de trabalho.
    st.header("🔐 Solicitações de Acesso de Convidado")
    if st.button("← Voltar para o app"):
        st.session_state.mostrar_admin_solicitacoes = False
        st.rerun()

    df_reqs = repo.list_access_requests()
    ativas = 0 if df_reqs.empty else int((df_reqs["status"].isin(["pending", "approved"])).sum())
    st.caption(f"{ativas} de 5 vagas ativas em uso (pendentes + aprovadas).")

    if df_reqs.empty:
        st.info("Nenhuma solicitação recebida ainda.")
        return

    status_labels = {"pending": "🟡 Pendente", "approved": "🟢 Aprovado", "rejected": "🔴 Rejeitado/Revogado"}

    for _, row in df_reqs.iterrows():
        request_id = int(row["id"])
        with st.container(border=True):
            c_info, c_acoes = st.columns([3, 1])
            with c_info:
                st.markdown(f"**{row['name']}** — {row['email']}")
                st.caption(f"{status_labels.get(row['status'], row['status'])} • solicitado em {row['requested_at']}")
                if row["justification"]:
                    st.caption(f"Justificativa: {row['justification']}")
                if row["status"] == "approved" and row["access_token"]:
                    st.code(f"?g={row['access_token']}", language=None)
                    st.caption(
                        "Copie e adicione ao final da URL do seu app (ex.: "
                        "`https://seu-app.streamlit.app/?g=...`) e mande para o convidado."
                    )
                    # O PORQUE: mostra a validade atual em linguagem simples --
                    # "sem expiração" (expires_at vazio) ou a data/hora exata em
                    # que o link vai parar de funcionar sozinho.
                    expira_em = row.get("expires_at")
                    if expira_em:
                        try:
                            expira_fmt = datetime.fromisoformat(str(expira_em)).strftime("%d/%m/%Y às %H:%M")
                            if _comparar_com_agora_br(str(expira_em)):
                                st.caption(f"⏳ Expirou em {expira_fmt} (o link já não funciona mais, mesmo sem revogar).")
                            else:
                                st.caption(f"⏳ Expira em {expira_fmt}.")
                        except Exception:
                            st.caption("⏳ Validade configurada, mas não foi possível ler a data.")
                    else:
                        st.caption("⏳ Sem expiração definida (vale até você revogar).")
            with c_acoes:
                if row["status"] == "pending":
                    # O PORQUE: escolhe a validade ANTES de aprovar -- 0 dias
                    # significa "sem expiração", à sua escolha, ajustável depois
                    # a qualquer momento (ver bloco "approved" abaixo).
                    dias_novo = st.number_input(
                        "Validade (dias)", min_value=0, value=7, step=1, key=f"dias_aprovar_{request_id}",
                        help="0 = sem expiração (vale até você revogar manualmente).",
                    )
                    if st.button("✅ Aprovar", key=f"aprovar_{request_id}", use_container_width=True):
                        repo.approve_access_request(request_id, int(dias_novo))
                        st.rerun()
                    if st.button("❌ Rejeitar", key=f"rejeitar_{request_id}", use_container_width=True):
                        repo.reject_access_request(request_id)
                        st.rerun()
                elif row["status"] == "approved":
                    # O PORQUE: ajustar a validade de um acesso JÁ aprovado sem
                    # precisar revogar e aprovar de novo (o que trocaria o
                    # token, invalidando um link que talvez já tenha sido
                    # compartilhado com a pessoa).
                    dias_ajuste = st.number_input(
                        "Nova validade (dias)", min_value=0, value=0, step=1, key=f"dias_ajustar_{request_id}",
                        help="A partir de agora. 0 = remove a expiração (passa a valer até você revogar).",
                    )
                    if st.button("🔄 Atualizar validade", key=f"atualizar_validade_{request_id}", use_container_width=True):
                        repo.update_access_request_expiry(request_id, int(dias_ajuste))
                        st.rerun()
                    if st.button("🚫 Revogar", key=f"revogar_{request_id}", use_container_width=True):
                        _dialog_confirmar_revogar_acesso(request_id, row["name"])
                if st.button("🗑️ Excluir", key=f"excluir_{request_id}", use_container_width=True):
                    _dialog_confirmar_excluir_solicitacao(request_id, row["name"])


# O PORQUE: opção especial no fim dos dropdowns de Projeto/Categoria dos
# formulários de Registro/Edição. Ao escolhê-la, um campo de texto aparece
# na hora para o usuário digitar um nome novo -- que é criado e persistido
# em custom_options assim que confirmado (Enter/Tab), ficando disponível
# nesse e em qualquer registro futuro, inclusive após sincronização via
# upload de txt/csv (que só mexe em work_logs, nunca em custom_options).
NEW_OPTION_SENTINEL = "➕ Criar novo..."


def render_banco_de_horas():
    # O PORQUE: mesmo padrão do painel de Solicitações de Acesso -- área
    # própria (não é uma aba nova), acionada por um botão na lateral, some
    # o resto do app enquanto estiver aberta.
    st.header("⏱️ Banco de Horas")
    if st.button("← Voltar para o app"):
        st.session_state.mostrar_banco_horas = False
        st.rerun()

    # O PORQUE: 11/02/2026 como piso é uma regra EXCLUSIVA desta
    # calculadora (pedido explícito) -- nenhuma outra tela/filtro de data
    # do app usa essa constante ou essa regra; todo o resto continua
    # funcionando exatamente como antes.
    DATA_MINIMA_BANCO_HORAS = datetime(2026, 2, 11).date()
    HORAS_PADRAO_DIA = 8.0
    hoje = agora_br().date()
    username = _current_user()

    st.caption(
        f"Compara as horas lançadas em cada dia contra a jornada padrão de "
        f"{HORAS_PADRAO_DIA:.0f}h. Dias sem nenhum registro são ignorados "
        f"(não contam como déficit)."
    )

    # O PORQUE: saldo inicial -- um ponto de partida (ex.: banco de horas
    # que a pessoa já tinha antes de começar a rastrear isso no app). Fica
    # salvo no banco (não é só desta sessão) -- define uma vez e não
    # precisa digitar de novo toda vez que abrir esta tela.
    saldo_inicial_salvo = repo.get_banco_horas_saldo_inicial(username)
    with st.expander("⚙️ Saldo inicial (opcional)", expanded=False):
        st.caption(
            "Ponto de partida do cálculo -- útil se você já tinha um saldo de "
            "banco de horas antes de começar a usar esta calculadora. Fica "
            "salvo, não precisa preencher de novo depois."
        )
        novo_saldo_inicial = st.number_input(
            "Saldo inicial (horas)",
            value=float(saldo_inicial_salvo),
            step=0.5,
            format="%.2f",
            key="banco_horas_saldo_inicial_input",
        )
        if st.button("💾 Salvar saldo inicial"):
            repo.set_banco_horas_saldo_inicial(username, novo_saldo_inicial)
            st.success("Saldo inicial salvo.")
            st.rerun()

    col_ini, col_fim = st.columns(2)
    with col_ini:
        data_inicio = st.date_input(
            "De",
            value=st.session_state.get("banco_horas_inicio", DATA_MINIMA_BANCO_HORAS),
            min_value=DATA_MINIMA_BANCO_HORAS,
            format="DD/MM/YYYY",
            key="banco_horas_inicio",
        )
    with col_fim:
        data_fim = st.date_input(
            "Até",
            value=st.session_state.get("banco_horas_fim", hoje),
            min_value=DATA_MINIMA_BANCO_HORAS,
            format="DD/MM/YYYY",
            key="banco_horas_fim",
        )

    if data_fim < data_inicio:
        st.error("A data final não pode ser anterior à data inicial.")
        return

    df = repo.get_all_logs_as_dataframe(username)
    if df.empty:
        st.info("Nenhum registro encontrado.")
        return

    df["log_date_dt"] = pd.to_datetime(df["log_date"]).dt.date
    mask = (df["log_date_dt"] >= data_inicio) & (df["log_date_dt"] <= data_fim)
    df_periodo = df[mask]

    if df_periodo.empty:
        st.info("Nenhum registro no período selecionado.")
        return

    # O PORQUE: agrupa por dia e soma as horas -- só dias que aparecem
    # aqui (ou seja, que TÊM pelo menos um registro) entram na conta. Um
    # dia sem nenhuma linha simplesmente não aparece no groupby, então não
    # é contado nem como déficit nem como excedente -- é como se não
    # existisse pro cálculo (pedido explícito do usuário).
    horas_por_dia = df_periodo.groupby("log_date_dt")["effort_hours"].sum().reset_index()
    horas_por_dia.columns = ["Data", "Horas Trabalhadas"]
    horas_por_dia["Diferença"] = horas_por_dia["Horas Trabalhadas"] - HORAS_PADRAO_DIA
    horas_por_dia = horas_por_dia.sort_values("Data")

    saldo_calculado_periodo = horas_por_dia["Diferença"].sum()
    total_banco_horas = saldo_inicial_salvo + saldo_calculado_periodo

    st.markdown("---")
    sinal = "+" if total_banco_horas >= 0 else ""
    st.metric(
        "Saldo do Banco de Horas",
        f"{sinal}{total_banco_horas:.2f}h".replace(".", ","),
    )
    if saldo_inicial_salvo != 0:
        sinal_ini = "+" if saldo_inicial_salvo >= 0 else ""
        sinal_calc = "+" if saldo_calculado_periodo >= 0 else ""
        st.caption(
            f"Saldo inicial: {sinal_ini}{saldo_inicial_salvo:.2f}h".replace(".", ",")
            + f" • Calculado no período: {sinal_calc}{saldo_calculado_periodo:.2f}h".replace(".", ",")
        )
    st.caption(
        f"Período: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')} • "
        f"{len(horas_por_dia)} dia(s) com registro considerado(s)"
    )

    st.markdown("##### Detalhamento por dia")
    tabela_exibicao = horas_por_dia.copy()
    tabela_exibicao["Data"] = tabela_exibicao["Data"].apply(lambda d: d.strftime("%d/%m/%Y"))
    tabela_exibicao["Horas Trabalhadas"] = tabela_exibicao["Horas Trabalhadas"].apply(
        lambda h: f"{h:.2f}h".replace(".", ",")
    )
    tabela_exibicao["Diferença"] = tabela_exibicao["Diferença"].apply(
        lambda d: (f"+{d:.2f}h" if d >= 0 else f"{d:.2f}h").replace(".", ",")
    )
    st.dataframe(tabela_exibicao, use_container_width=True, hide_index=True)


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
# Padrão: Data descendente (atividade mais recente primeiro) -- o usuário
# pode clicar em qualquer cabeçalho pra mudar isso a qualquer momento.
if 'sort_column' not in st.session_state:
    st.session_state.sort_column = "log_date"
if 'sort_ascending' not in st.session_state:
    st.session_state.sort_ascending = False
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
    "Projeto": "project",
    "Categoria": "category",
    "Descrição": "description",
    "Horas": "effort_hours",
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


def reset_add_form_stay():
    # O PORQUE: usado pelo botão "Salvar e Novo" -- limpa os campos do
    # formulário (igual reset_states faria), MAS mantém view_state='add',
    # pra continuar na tela de registro em vez de voltar pra listagem. Só é
    # seguro chamar isso durante o bloqueio de tela cheia (dentro do
    # dispatcher, com processing=True) -- é o único momento em que os
    # widgets do formulário ainda não foram desenhados nesta execução, e
    # portanto ainda é permitido escrever nas keys deles.
    st.session_state.confirm_state = None
    st.session_state.pending_data = {}
    st.session_state.target_id = None
    for k in ("add_proj_select", "add_proj_new_text", "add_cat_select", "add_cat_new_text"):
        st.session_state.pop(k, None)
    st.session_state["add_description"] = ""
    st.session_state["add_effort"] = 1.0
    st.session_state["add_log_date"] = agora_br().date()
    st.session_state["add_is_impedimento"] = False
    st.session_state["add_is_duvida"] = False


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


def run_blocking_action(action_type: str, payload: dict, processing_message: str = "Processando...",
                         success_message: str = "Concluído.", failure_message: str = "Não foi possível concluir."):
    # O PORQUE: mesmo bloqueio "nada mais é desenhado nesta execução" do
    # request_confirmation(), mas SEM o modal de "tem certeza?" antes -- para
    # ações que não são destrutivas o bastante pra pedir confirmação (ex.:
    # pedir uma sugestão da IA, analisar um arquivo antes de decidir o que
    # aplicar), mas que envolvem uma chamada de rede/IA que pode demorar
    # alguns segundos. Durante esse tempo, a tela inteira (inclusive a
    # barra lateral e as outras abas) fica bloqueada -- não é só um efeito
    # visual, é a mesma garantia usada nas ações com confirmação: nenhum
    # outro widget é renderizado nesta execução, então não existe o que
    # clicar.
    st.session_state.processing = True
    st.session_state.processing_action = {
        "type": action_type,
        "payload": payload,
        "processing_message": processing_message,
        "success_message": success_message,
        "failure_message": failure_message,
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
    # O PORQUE: segunda camada de proteção -- todas as ações daqui pra baixo
    # escrevem no banco (ou chamam a IA em nome do usuário). A tela já
    # esconde os botões que disparam essas ações para quem está logado como
    # convidado, mas não confiamos só nisso: se por algum motivo futuro um
    # botão administrativo ficar visível/alcançável por engano, o backend
    # recusa aqui mesmo assim, antes de tocar no banco.
    if st.session_state.get("user_role") != "admin":
        action["failure_message"] = "Ação não permitida para o seu nível de acesso."
        return False

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
        if d.get('continuar_adicionando'):
            reset_add_form_stay()
            action["success_message"] = "Registro salvo! Formulário pronto para o próximo."
        else:
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

    elif t == "ia_sugerir_registro":
        # O PORQUE: mesma lógica que já existia inline no botão "Sugerir com
        # IA", só que agora rodando dentro do bloqueio de tela cheia -- a
        # chamada de rede pro n8n pode levar alguns segundos, e durante esse
        # tempo nenhum outro botão do app pode ser clicado.
        results_ia, erro_ia = estimar_esforco_com_ia(
            [{"id": 0, "description": p["descricao"]}], get_project_options(), get_category_options(),
        )
        if erro_ia:
            action["failure_message"] = f"⚠️ Não foi possível consultar a IA agora ({erro_ia})."
            return False

        r = results_ia[0] if results_ia else {}
        minutes = r.get("estimated_minutes")
        project_sugerido = (r.get("project") or "").strip()
        category_sugerida = (r.get("category") or "").strip()
        if project_sugerido and r.get("is_new_project"):
            repo.add_custom_option("project", username, project_sugerido)
        if category_sugerida and r.get("is_new_category"):
            repo.add_custom_option("category", username, category_sugerida)
        # O PORQUE: mesmo raciocínio de antes -- não dá pra escrever direto
        # nas keys dos widgets (add_effort/add_proj_select/add_cat_select)
        # aqui, porque esta função roda ANTES do próximo rerun desenhar o
        # formulário; guardamos como "pendente" e o topo do formulário
        # aplica no próximo desenho (ver bloco antes dos widgets do "Novo
        # Registro").
        st.session_state["_ia_sugestao_pendente"] = {
            "effort_hours": round(minutes / 60.0, 2) if isinstance(minutes, (int, float)) and minutes > 0 else None,
            "project": project_sugerido or None,
            "category": category_sugerida or None,
        }
        return True

    elif t == "ia_recalcular_periodo":
        # O PORQUE: mesma lógica que já existia inline no diálogo "Recalcular
        # Esforço com IA", agora rodando dentro do bloqueio de tela cheia --
        # essa é a ação mais demorada de todas (chama a IA para CADA registro
        # do período), então é a que mais se beneficia de travar a tela
        # inteira enquanto processa.
        qtd, novos_projetos, novas_categorias, aviso = recalcular_esforco_periodo_com_ia(
            username, p["start_date"], p["end_date"]
        )
        if aviso:
            st.session_state["_recalculo_ia_resultado"] = {"tipo": "aviso", "mensagem": aviso}
            action["failure_message"] = f"⚠️ {aviso}"
            return False
        st.session_state["_recalculo_ia_resultado"] = {
            "tipo": "sucesso", "qtd": qtd, "novos_projetos": novos_projetos, "novas_categorias": novas_categorias,
        }
        return True

    elif t == "ia_analisar_arquivo":
        # O PORQUE: mesma lógica que já existia inline no botão "Analisar
        # Arquivo Enviado" -- ver payload montado no botão (raw_bytes,
        # file_ext, file_name), já que o widget de upload em si não existe
        # mais nesta execução (bloqueio de tela cheia).
        raw_bytes = p["raw_bytes"]
        file_ext = p["file_ext"]
        file_name = p["file_name"]
        parser = HistoryParser()

        if file_ext == ".csv":
            df_txt = parser.parse_csv(raw_bytes)
            if df_txt.empty:
                action["failure_message"] = (
                    "Não foi possível reconhecer as colunas do CSV. Esperado: "
                    "log_date;project;category;description;effort_hours "
                    "(ou separado por vírgula, no padrão US)."
                )
                return False
        else:
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            df_txt = parser.parse_text(raw_text)

        df_db = repo.get_all_logs_as_dataframe(username)

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

        ia_aviso = None
        novos_projetos, novas_categorias = [], []
        if N8N_AI_ESTIMATE_WEBHOOK_URL and not df_to_insert.empty:
            df_to_insert, novos_projetos, novas_categorias, ia_aviso = aplicar_estimativa_ia_e_normalizacao(df_to_insert)
            if not ia_aviso:
                for proj in novos_projetos:
                    repo.add_custom_option("project", username, proj)
                for cat in novas_categorias:
                    repo.add_custom_option("category", username, cat)

        st.session_state.df_to_insert = df_to_insert
        st.session_state.df_to_delete = df_to_delete
        st.session_state.sync_analyzed = True
        st.session_state.sync_file_name = file_name
        # O PORQUE: st.info/st.warning chamados AQUI DENTRO não apareceriam
        # pro usuário -- ficam atrás do overlay de tela cheia e somem no
        # rerun automático logo em seguida. Guardamos como resultado
        # pendente, exibido depois (ver logo após "if
        # st.session_state.sync_analyzed:"), já com a tela de revisão
        # normal desenhada por cima.
        st.session_state["_analise_arquivo_ia_info"] = {
            "ia_aviso": ia_aviso,
            "novos_projetos": novos_projetos,
            "novas_categorias": novas_categorias,
        }
        return True

    elif t == "ia_sincronizar":
        # O PORQUE: mesma lógica que já existia inline no botão
        # "Sincronizar" -- edited_insert/edited_delete vêm prontos no
        # payload (capturados do data_editor antes deste bloqueio), já que
        # os widgets em si não existem mais nesta execução.
        edited_insert = p["edited_insert"]
        edited_delete = p["edited_delete"]
        records_inserted = 0
        records_deleted = 0

        if not edited_insert.empty:
            to_insert = edited_insert[edited_insert["_Aplicar"] == True]
            rows_to_insert = []
            for _, row in to_insert.iterrows():
                # O PORQUE: DateColumn pode devolver datetime.date (ou
                # Timestamp) em vez de string ao ler o data_editor de volta;
                # normalizamos para ISO (YYYY-MM-DD) antes de gravar, que é
                # o formato esperado pela coluna log_date no SQLite.
                log_date_iso = row["log_date"].strftime("%Y-%m-%d") if hasattr(row["log_date"], "strftime") else str(row["log_date"])
                rows_to_insert.append({
                    "log_date": log_date_iso,
                    "project": row["project"],
                    "category": row["category"],
                    "description": row["description"],
                    "effort_hours": row["effort_hours"],
                    "is_impedimento": bool(row.get("is_impedimento", False)),
                    "is_duvida": bool(row.get("is_duvida", False)),
                })
            records_inserted = repo.insert_logs_bulk(username, rows_to_insert)

        if not edited_delete.empty:
            to_delete = edited_delete[edited_delete["_Aplicar"] == True]
            records_deleted = repo.delete_logs_bulk(username, to_delete["id"].tolist())

        st.session_state.sync_analyzed = False
        action["success_message"] = f"Prontinho! {records_inserted} registro(s) adicionado(s) e {records_deleted} removido(s)."
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
    is_admin = st.session_state.get("user_role") == "admin"

if is_admin:
    with st.sidebar:
        st.subheader("⚙️ Projetos e Categorias")

        with st.expander("📁 Novo Nome de Projeto"):
            _manage_options_panel("Projeto", "project", BASE_PROJECT_OPTIONS, get_project_options)

        with st.expander("🏷️ Novo Nome de Categoria"):
            _manage_options_panel("Categoria", "category", BASE_CATEGORY_OPTIONS, get_category_options)

if is_admin:
    with st.sidebar:
        st.markdown("---")
        st.subheader("🤖 Recalcular Esforço com IA")
        st.caption("Reprocessa registros já salvos: reestima horas e reclassifica projeto/categoria.")

        if not N8N_AI_ESTIMATE_WEBHOOK_URL:
            st.caption("Configure `N8N_AI_ESTIMATE_WEBHOOK_URL` nos Secrets para habilitar.")
        else:
            # O PORQUE: a aba Dashboard & Relatórios já guarda o período aplicado
            # em st.session_state.dashboard_start_date/end_date -- por padrão
            # reaproveitamos esse período (opção mais comum: "recalcula o que eu
            # já estou vendo"), mas sem obrigar -- desmarcando a caixa, aparece um
            # date_input próprio aqui na barra lateral pra qualquer outra data,
            # independente do que estiver filtrado no Dashboard no momento.
            dash_start = st.session_state.get("dashboard_start_date")
            dash_end = st.session_state.get("dashboard_end_date")

            usar_periodo_dashboard = st.checkbox(
                "Usar o período já aplicado no Dashboard",
                value=True,
                key="ia_recalc_usar_dashboard",
            )

            if usar_periodo_dashboard:
                if dash_start and dash_end:
                    st.caption(f"Período: {dash_start.strftime('%d/%m/%Y')} a {dash_end.strftime('%d/%m/%Y')}")
                    recalc_start, recalc_end = dash_start, dash_end
                else:
                    st.caption("Nenhum período aplicado ainda na aba Dashboard & Relatórios.")
                    recalc_start, recalc_end = None, None
            else:
                c_recalc1, c_recalc2 = st.columns(2)
                with c_recalc1:
                    recalc_start = st.date_input(
                        "De", value=dash_start or agora_br().date(), format="DD/MM/YYYY", key="ia_recalc_start"
                    )
                with c_recalc2:
                    recalc_end = st.date_input(
                        "Até", value=dash_end or agora_br().date(), format="DD/MM/YYYY", key="ia_recalc_end"
                    )

            btn_recalcular_ia = st.button(
                "🤖 Recalcular com IA",
                use_container_width=True,
                key="btn_recalcular_ia",
                disabled=(recalc_start is None or recalc_end is None),
                help="Abre uma confirmação antes de aplicar -- nada muda sem você confirmar.",
            )
            if btn_recalcular_ia:
                if recalc_start > recalc_end:
                    st.error("A data inicial não pode ser depois da data final.")
                else:
                    _dialog_confirmar_recalculo_ia(recalc_start, recalc_end)

            # O PORQUE: o resultado só existe em session_state por UM rerun (o que
            # acontece logo depois do st.rerun() dentro do diálogo) -- .pop() já
            # remove na hora de ler, então a mensagem aparece uma vez e não fica
            # "grudada" reaparecendo em reruns futuros não relacionados.
            resultado_recalculo = st.session_state.pop("_recalculo_ia_resultado", None)
            if resultado_recalculo:
                if resultado_recalculo["tipo"] == "aviso":
                    st.warning(resultado_recalculo["mensagem"])
                else:
                    msg = f"✅ {resultado_recalculo['qtd']} registro(s) recalculado(s)."
                    novos_p = resultado_recalculo["novos_projetos"]
                    novas_c = resultado_recalculo["novas_categorias"]
                    if novos_p or novas_c:
                        partes = []
                        if novos_p:
                            partes.append(f"projeto(s) {', '.join(novos_p)}")
                        if novas_c:
                            partes.append(f"categoria(s) {', '.join(novas_c)}")
                        msg += f" Adicionado(s) automaticamente: {' e '.join(partes)}."
                    st.success(msg)

if is_admin:
    with st.sidebar:
        st.markdown("---")
        st.session_state.setdefault("mostrar_admin_solicitacoes", False)
        if st.button("🔐 Solicitações de Acesso", use_container_width=True):
            st.session_state.mostrar_admin_solicitacoes = not st.session_state.mostrar_admin_solicitacoes
        if st.session_state.mostrar_admin_solicitacoes:
            qtd_pendentes = len(repo.list_access_requests().query("status == 'pending'")) if not repo.list_access_requests().empty else 0
            if qtd_pendentes:
                st.caption(f"🔔 {qtd_pendentes} pendente(s)")

# O PORQUE: disponível pra admin E convidado (não só admin) -- é um cálculo
# a partir dos mesmos registros que já aparecem no Dashboard, que o
# convidado já vê em modo leitura; não é uma ação que modifica dado
# nenhum, só mostra um número calculado.
with st.sidebar:
    st.markdown("---")
    st.session_state.setdefault("mostrar_banco_horas", False)
    if st.button("⏱️ Banco de Horas", use_container_width=True):
        st.session_state.mostrar_banco_horas = not st.session_state.mostrar_banco_horas

# O PORQUE: "Logado como" + "Sair" ficam por último de propósito -- é o
# fim natural do menu, junto de qualquer outra coisa que ainda venha a ser
# adicionada na barra lateral no futuro (a intenção é que esse bloco sempre
# feche o menu, não fique preso entre as seções).
with st.sidebar:
    st.markdown("---")
    if is_admin:
        st.caption(f"Logado como **{st.session_state.get('auth_username', '')}**")
    else:
        st.caption(f"👁️ Convidado (somente leitura): **{st.session_state.get('guest_name', '')}**")
    if st.button("🚪 Sair", use_container_width=True):
        _dialog_confirmar_logout()

st.title("📊 Task Tracker")

if is_admin and st.session_state.get("mostrar_admin_solicitacoes"):
    render_admin_solicitacoes()
    st.stop()

if st.session_state.get("mostrar_banco_horas"):
    render_banco_de_horas()
    st.stop()

if is_admin:
    tab_manage, tab_daily, tab_dashboard, tab_sync = st.tabs(
        ["Registro de Atividades", "Daily Scrum", "Dashboard & Relatórios", "Sincronização de Arquivo"]
    )
else:
    # O PORQUE: convidado só vê visualização -- registro (somente leitura) e
    # dashboard. Daily Scrum e Sincronização são ferramentas de trabalho do
    # dono da conta, sem sentido (e sem permissão) para quem só está
    # consultando.
    tab_manage, tab_dashboard = st.tabs(["Registro de Atividades", "Dashboard & Relatórios"])
    tab_daily = None
    tab_sync = None

# ==========================================
# TAB 1: REGISTRO DE ATIVIDADES (GRID & CRUD)
# ==========================================
with tab_manage:
    # O PORQUE: convidado é só-leitura -- mesmo que por algum motivo o
    # session_state tenha ficado em 'add'/'edit' de uma sessão anterior
    # (não deveria, já que os botões que levam a esses estados ficam
    # escondidos abaixo), força de volta pra 'grid' como camada extra de
    # proteção.
    if not is_admin and st.session_state.view_state != 'grid':
        st.session_state.view_state = 'grid'

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
            if is_admin:
                st.write("")
                if st.button("➕ Novo Registro", use_container_width=True, type="primary"):
                    st.session_state.view_state = 'add'
                    reset_states(full_reset=False)
                    st.rerun()

        # O PORQUE: antes, esta tela trazia o histórico INTEIRO do usuário a
        # cada abertura (get_all_logs_as_dataframe sem filtro nenhum) -- caro
        # contra um banco remoto (Turso) à medida que o histórico cresce.
        # Agora, filtra o período já na consulta SQL. Padrão: sempre "1 mês
        # de calendário atrás até hoje", calculado na hora (nunca uma data
        # travada) -- e, uma vez que o usuário aplicar outro período, ele
        # se mantém (mesmo padrão da aba Dashboard) até ser trocado de novo.
        hoje_grid = agora_br().date()
        default_grid_start = hoje_grid - timedelta(days=10)

        with st.form("grid_filter_form"):
            st.markdown("##### Período")
            c_gstart, c_gend, c_gbtn = st.columns([2, 2, 1])
            with c_gstart:
                grid_start_input = st.date_input(
                    "Data Inicial",
                    value=st.session_state.get("grid_start_date", default_grid_start),
                    format="DD/MM/YYYY",
                )
            with c_gend:
                grid_end_input = st.date_input(
                    "Data Final",
                    value=st.session_state.get("grid_end_date", hoje_grid),
                    format="DD/MM/YYYY",
                )
            with c_gbtn:
                # O PORQUE: st.date_input desenha um rótulo em cima do campo;
                # sem esse espaço em branco do mesmo tamanho, o botão fica
                # alinhado mais alto que os campos de data, em vez de na
                # mesma linha visual.
                st.markdown("<div style='height: 1.8rem'></div>", unsafe_allow_html=True)
                grid_apply = st.form_submit_button("Aplicar Filtro", type="primary", use_container_width=True)

        if grid_apply:
            if grid_start_input > grid_end_input:
                st.error("A Data Inicial não pode ser depois da Data Final.")
            else:
                st.session_state.grid_start_date = grid_start_input
                st.session_state.grid_end_date = grid_end_input
                st.session_state.current_page = 1

        grid_start_date = st.session_state.get("grid_start_date", default_grid_start)
        grid_end_date = st.session_state.get("grid_end_date", hoje_grid)

        df_all = repo.get_logs_as_dataframe_by_range(
            _current_user(), grid_start_date.isoformat(), grid_end_date.isoformat()
        )

        if df_all.empty:
            st.info(
                f"Nenhum registro entre **{grid_start_date.strftime('%d/%m/%Y')}** e "
                f"**{grid_end_date.strftime('%d/%m/%Y')}**. Ajuste o período acima "
                "ou clique em **Novo Registro** para começar."
            )
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
                # O PORQUE: só calcula aqui (não desenha os controles ainda) --
                # os widgets de paginação em si (seletor de itens por página,
                # ir pra página, Anterior/Próximo) ficam DEPOIS da tabela, no
                # rodapé da lista. items_per_page vem do valor já aplicado
                # (session_state), lido ANTES de desenhar o widget -- que só
                # aparece mais abaixo, depois da tabela.
                items_per_page = st.session_state.get("grid_items_per_page", 25)
                total_pages = max(1, (total_records + items_per_page - 1) // items_per_page)
                if st.session_state.current_page > total_pages:
                    st.session_state.current_page = total_pages

                st.markdown("---")
                with st.container(key="atividades_grid"):
                    grid_cols = st.columns([0.5, 1, 1.5, 2, 4, 1, 1.5])
                    headers = ["ID", "Data", "Projeto", "Categoria", "Descrição", "Horas", "Ações"]
                    for col, header in zip(grid_cols, headers):
                        if header in SORTABLE_COLUMNS:
                            col_key = SORTABLE_COLUMNS[header]
                            # O PORQUE: seta simples (▲/▼, caractere de texto puro)
                            # em vez de emoji colorido (🔼/🔽) -- o emoji, além de
                            # renderizar grande/colorido dependendo da fonte do
                            # sistema, podia até quebrar linha dentro do botão em
                            # colunas estreitas, fazendo a seta cair pra baixo do
                            # texto do cabeçalho. ▲/▼ é pequeno, mono, e fica
                            # sempre na mesma linha, à direita do título -- o
                            # mesmo padrão usado em qualquer tabela de banco.
                            if st.session_state.sort_column == col_key:
                                arrow = " ▲" if st.session_state.sort_ascending else " ▼"
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
                            if is_admin:
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
                            else:
                                st.write("")
                st.markdown("---")

                # O PORQUE: a tentativa anterior com CSS puro (:nth-of-type)
                # não funcionou -- o Streamlit não organiza as linhas da grid
                # como irmãs diretas de um mesmo pai, do jeito que o CSS
                # "nth-of-type" precisa pra contar certo. JavaScript resolve
                # isso de forma mais confiável: percorre TODOS os blocos de
                # linha dentro do container (documento em ordem visual, de
                # cima pra baixo, não importa a profundidade de aninhamento)
                # e aplica a cor alternada manualmente -- índice 0 é o
                # cabeçalho (sem sombra), os de índice ímpar (1ª, 3ª, 5ª...
                # linha de dados) ganham o fundo. Roda de novo a cada rerun
                # (troca de página, ordenação, novo registro etc.), já que
                # este componente é redesenhado do zero em toda execução.
                #
                # O PORQUE de st.html() em vez de st.components.v1.html():
                # o segundo roda dentro de um iframe -- localmente costuma
                # dar pra "escapar" dele via window.top/parent, mas
                # publicado (Streamlit Cloud) esse acesso é bloqueado por
                # segurança do navegador (iframe tratado como origem
                # diferente, mesmo sendo o mesmo site) -- por isso
                # funcionava local e não online. st.html() não usa iframe
                # nenhum -- o script já nasce direto na página de verdade.
                st.html(
                    """
                    <script>
                    function aplicarZebraGrid() {
                        const grid = document.querySelector('.st-key-atividades_grid');
                        if (!grid) return;
                        const todosBlocos = Array.from(grid.querySelectorAll('[data-testid="stHorizontalBlock"]'));
                        // O PORQUE: a coluna "Ações" tem um st.columns() ANINHADO
                        // por dentro (pros botões ✏️/🗑️) -- sem este filtro, cada
                        // linha visível contaria como 2 blocos (o de fora + o de
                        // dentro), o que quebrava a alternância par/ímpar (toda
                        // linha real caía sempre no mesmo grupo). Aqui, descarta
                        // qualquer bloco que tenha OUTRO bloco de linha entre ele
                        // e a grid -- sobra só os blocos de "linha de verdade".
                        const linhas = todosBlocos.filter(function(bloco) {
                            let pai = bloco.parentElement;
                            while (pai && pai !== grid) {
                                if (pai.matches && pai.matches('[data-testid="stHorizontalBlock"]')) {
                                    return false;
                                }
                                pai = pai.parentElement;
                            }
                            return true;
                        });
                        linhas.forEach(function(linha, indice) {
                            if (indice > 0 && indice % 2 === 1) {
                                linha.style.backgroundColor = 'rgba(120, 120, 120, 0.12)';
                                linha.style.borderRadius = '4px';
                            } else {
                                linha.style.backgroundColor = '';
                            }
                        });
                    }
                    setTimeout(aplicarZebraGrid, 250);
                    </script>
                    """,
                    unsafe_allow_javascript=True,
                )

                # O PORQUE: os 4 elementos pedidos (Registros por página, Ir
                # para a página, Anterior, Próximo) numa linha só, depois da
                # lista -- não mais antes dela. A legenda "Página X de Y"
                # fica numa linha própria acima, já que não fazia parte do
                # pedido de "mesma linha" dos 4 controles.
                st.caption(f"Página {st.session_state.current_page} de {total_pages} (Total: {total_records} registros)")
                col_per_page, col_page_jump, col_prev, col_next = st.columns([1.4, 1.4, 1, 1])
                with col_per_page:
                    opcoes_por_pagina = [10, 25, 50, 100]
                    idx_atual = opcoes_por_pagina.index(items_per_page) if items_per_page in opcoes_por_pagina else 1
                    novo_items_per_page = st.selectbox(
                        "Registros por página", opcoes_por_pagina, index=idx_atual, key="grid_items_per_page",
                    )
                with col_page_jump:
                    jump_page = st.number_input(
                        "Ir para a página", min_value=1, max_value=total_pages, value=st.session_state.current_page,
                    )
                    if jump_page != st.session_state.current_page:
                        st.session_state.current_page = jump_page
                        st.rerun()
                with col_prev:
                    st.markdown("<div style='height: 1.8rem'></div>", unsafe_allow_html=True)
                    if st.button("⬅️ Anterior", disabled=(st.session_state.current_page == 1), use_container_width=True):
                        st.session_state.current_page -= 1
                        st.rerun()
                with col_next:
                    st.markdown("<div style='height: 1.8rem'></div>", unsafe_allow_html=True)
                    if st.button("Próximo ➡️", disabled=(st.session_state.current_page == total_pages), use_container_width=True):
                        st.session_state.current_page += 1
                        st.rerun()

    if st.session_state.view_state == 'add':
        st.header("Novo Registro")
        st.caption(
            "Todos os campos abaixo são obrigatórios. Em Projeto/Categoria, escolha "
            "\"➕ Criar novo...\" para digitar (e já criar) um nome novo na hora."
        )
        # O PORQUE: Streamlit NÃO deixa escrever em st.session_state[key] de um
        # widget que já foi instanciado NESTA MESMA execução -- levanta
        # StreamlitAPIException na hora, mesmo que um st.rerun() venha logo
        # depois (foi exatamente o erro que apareceu ao clicar em "Sugerir com
        # IA": o botão fica ABAIXO dos campos no código, então quando o clique
        # é processado, add_effort/add_proj_select/add_cat_select já tinham
        # acabado de ser desenhados neste run). A solução padrão do Streamlit
        # pra isso: o botão só guarda a sugestão num campo "pendente" e chama
        # st.rerun() -- e é bem AQUI, no topo do bloco, ANTES de qualquer
        # widget do formulário ser criado, que a gente aplica a sugestão
        # pendente aos campos de verdade (nesse ponto do run, ainda é permitido
        # escrever, porque os widgets desta vez ainda não foram instanciados).
        sugestao_pendente = st.session_state.pop("_ia_sugestao_pendente", None)
        if sugestao_pendente:
            if sugestao_pendente.get("effort_hours") is not None:
                st.session_state["add_effort"] = sugestao_pendente["effort_hours"]
            if sugestao_pendente.get("project"):
                st.session_state["add_proj_select"] = sugestao_pendente["project"]
            if sugestao_pendente.get("category"):
                st.session_state["add_cat_select"] = sugestao_pendente["category"]

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

        col_ia, _col_ia_spacer = st.columns([1, 3])
        with col_ia:
            btn_ia_sugerir = st.button(
                "🤖 Sugerir com IA",
                use_container_width=True,
                key="add_btn_ia_sugerir",
                disabled=not N8N_AI_ESTIMATE_WEBHOOK_URL,
                help=(
                    "Estima a duração e sugere Projeto/Categoria com base na descrição acima "
                    "(você continua podendo ajustar tudo antes de salvar)."
                    if N8N_AI_ESTIMATE_WEBHOOK_URL
                    else "Configure N8N_AI_ESTIMATE_WEBHOOK_URL nos Secrets para habilitar esta sugestão."
                ),
            )
        if btn_ia_sugerir:
            desc_atual = st.session_state.get("add_description", "").strip()
            if not desc_atual:
                st.warning("Escreva a descrição da atividade antes de pedir uma sugestão.")
            else:
                run_blocking_action(
                    "ia_sugerir_registro",
                    {"descricao": desc_atual},
                    processing_message="Consultando IA...",
                    success_message="🤖 Sugestão aplicada.",
                    failure_message="⚠️ Não foi possível consultar a IA agora.",
                )

        col_imp, col_duv = st.columns(2)
        with col_imp:
            is_impedimento = st.checkbox("🚧 É um impedimento?", key="add_is_impedimento")
        with col_duv:
            is_duvida = st.checkbox("❓ É uma dúvida?", key="add_is_duvida")

        col_save, col_save_new, col_canc = st.columns(3)
        with col_save:
            btn_save = st.button("Salvar Registro", type="primary", use_container_width=True, key="add_btn_save")
        with col_save_new:
            btn_save_new = st.button(
                "💾➕ Salvar e Novo", use_container_width=True, key="add_btn_save_new",
                help="Salva este registro e já abre o formulário limpo para o próximo.",
            )
        with col_canc:
            btn_canc = st.button("Cancelar", use_container_width=True, key="add_btn_canc")

        if btn_save or btn_save_new:
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
                    # O PORQUE: essa flag viaja até o dispatcher (ver "insert_log"
                    # em execute_processing_action) para decidir, depois de salvar,
                    # se volta pra listagem (Salvar Registro normal) ou fica no
                    # formulário já limpo pra próxima tarefa (Salvar e Novo).
                    'continuar_adicionando': bool(btn_save_new),
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
if is_admin:
    with tab_daily:
        st.header("Resumo para a Daily")
        st.caption(
            "Gera um resumo do que você fez ontem e do que vai fazer hoje, "
            "pronto para consultar durante a Daily."
        )

        default_ontem = agora_br().date() - timedelta(days=1)
        default_hoje = agora_br().date()

        # O PORQUE: mesmo padrão do filtro de período do Dashboard -- as duas
        # datas ficam dentro de um st.form, então trocar "Ontem" ou "Hoje" não
        # dispara nada sozinho. Só depois de clicar em "Aplicar Período" é que o
        # valor escolhido passa a valer para as sugestões e para o relatório.
        with st.form("daily_period_form"):
            st.markdown("### Escolha o Período")
            col_d1, col_d2, col_d3 = st.columns([2, 2, 1])
            with col_d1:
                d_ontem_input = st.date_input("Data Anterior (Ontem)", value=default_ontem, format="DD/MM/YYYY", key="daily_d_ontem")
            with col_d2:
                d_hoje_input = st.date_input("Data Atual (Hoje)", value=default_hoje, format="DD/MM/YYYY", key="daily_d_hoje")
            with col_d3:
                # O PORQUE: mesmo truque de alinhamento vertical usado nos
                # outros filtros de período -- compensa a altura do rótulo
                # que st.date_input desenha acima do campo, senão o botão
                # fica "mais alto" que os campos de data na mesma linha.
                st.markdown("<div style='height: 1.8rem'></div>", unsafe_allow_html=True)
                apply_daily_period = st.form_submit_button("Aplicar Período", type="primary", use_container_width=True)

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

        _daily_txt_editing_lock = st.session_state.get("daily_txt_editing", False)
        if _daily_txt_editing_lock:
            st.warning("✏️ Finalize (salve) a edição do texto corrido abaixo para liberar os outros botões desta aba.")

        def _merge_daily_suggestion(current_text: str, suggestion_text: str, empty_placeholder: str) -> str:
            # O PORQUE: preserva cada linha já digitada à mão em
            # Impedimentos/Dúvidas -- só adiciona as linhas da sugestão
            # automática que ainda não estão lá (sem duplicar). O
            # placeholder padrão ("Nenhum."/"Nenhuma.") não conta como
            # conteúdo real do usuário.
            current_lines = [ln.strip() for ln in (current_text or "").strip().splitlines() if ln.strip()]
            current_lines = [ln for ln in current_lines if ln.lower() != empty_placeholder.lower()]

            suggestion_lines = [ln.strip() for ln in (suggestion_text or "").strip().splitlines() if ln.strip()]
            suggestion_lines = [ln for ln in suggestion_lines if ln.lower() != empty_placeholder.lower()]

            combined = list(current_lines)
            for ln in suggestion_lines:
                if ln not in combined:
                    combined.append(ln)

            return "\n".join(combined) if combined else empty_placeholder

        # O PORQUE: antes, era preciso clicar num segundo botão ("Atualizar
        # sugestões da base de dados") depois de aplicar o período. Agora,
        # aplicar o período já atualiza a sugestão de Impedimentos/Dúvidas
        # sozinho -- só roda quando "Aplicar Período" foi de fato clicado
        # NESTA execução (apply_daily_period), nunca em qualquer outro
        # rerun, senão sobrescreveria uma edição manual sem motivo. Isso
        # também precisa rodar ANTES dos widgets de texto corrido serem
        # criados mais abaixo (mesma regra de sempre: não dá pra escrever
        # em st.session_state[key] de um widget já instanciado neste run).
        if apply_daily_period and not _daily_txt_editing_lock:
            df_all_suggestion = repo.get_all_logs_as_dataframe(_current_user())
            new_imp_suggestion = build_daily_suggestion(df_all_suggestion, d_ontem, d_hoje, "is_impedimento")
            new_duv_suggestion = build_daily_suggestion(df_all_suggestion, d_ontem, d_hoje, "is_duvida")
            st.session_state["impedimentos_input"] = _merge_daily_suggestion(
                st.session_state.get("impedimentos_input", ""), new_imp_suggestion, "Nenhum."
            )
            st.session_state["duvidas_input"] = _merge_daily_suggestion(
                st.session_state.get("duvidas_input", ""), new_duv_suggestion, "Nenhuma."
            )

        if "impedimentos_input" not in st.session_state:
            st.session_state["impedimentos_input"] = "Nenhum."
        if "duvidas_input" not in st.session_state:
            st.session_state["duvidas_input"] = "Nenhuma."

        # O PORQUE: lado a lado (2 colunas) em vez de um embaixo do outro --
        # os dois textos costumam ser curtos, e ficam mais fáceis de comparar
        # de relance quando estão na mesma altura da tela.
        col_imp, col_duv = st.columns(2)
        with col_imp:
            impedimentos = st.text_area(
                "Impedimentos", key="impedimentos_input",
                help="Puxado automaticamente dos registros marcados como 🚧 Impedimento no período acima. Edite livremente.",
            )
        with col_duv:
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
            report_txt = f"=== DAILY SCRUM ===\nData: {agora_br().strftime('%d/%m/%Y')}\n\n"
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
            st.caption(f"Gerado em {agora_br().strftime('%d/%m/%Y %H:%M')}")

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
            c_down_txt, c_down_pdf, c_down_graf = st.columns(3)
            with c_down_txt:
                st.download_button(
                    label="⬇️ Baixar Resumo da Daily (.txt)",
                    data=rep["report_txt"].encode("utf-8"),
                    file_name=f"daily_{agora_br().strftime('%Y%m%d')}.txt",
                    mime="text/plain",
                    use_container_width=True,
                    type="primary",
                    disabled=pending_changes,
                )
            with c_down_graf:
                # O PORQUE: botão em vez de mostrar direto -- pedido explícito
                # (não precisa aparecer sozinho, só quando o usuário quiser ver).
                # Só marca um "quero ver" em session_state -- os gráficos de
                # verdade são montados mais abaixo, fora deste "with", tanto
                # faz se foi clicado agora ou num rerun anterior.
                if st.button("📊 Ver Gráficos do Período", use_container_width=True, disabled=pending_changes):
                    st.session_state["daily_mostrar_graficos"] = True
            with c_down_pdf:
                if st.button("📄 Gerar PDF da Daily", use_container_width=True, disabled=pending_changes):
                    with st.spinner("Montando o PDF..."):
                        df_daily_combo = pd.concat([
                            rep["df_ontem"].assign(Período=f"Ontem ({rep['d_ontem'].strftime('%d/%m')})"),
                            rep["df_hoje"].assign(Período=f"Hoje ({rep['d_hoje'].strftime('%d/%m')})"),
                        ], ignore_index=True) if not (rep["df_ontem"].empty and rep["df_hoje"].empty) else pd.DataFrame()

                        kpis_pdf = [
                            (f"{rep['df_ontem']['effort_hours'].sum():.1f}h", f"Ontem ({rep['d_ontem'].strftime('%d/%m')})"),
                            (f"{rep['df_hoje']['effort_hours'].sum():.1f}h", f"Hoje ({rep['d_hoje'].strftime('%d/%m')})"),
                            (f"{len(rep['df_ontem']) + len(rep['df_hoje'])}", "Registros"),
                        ]

                        # O PORQUE: antes, "O que fiz ontem"/"O que farei hoje"
                        # eram texto solto (bullet points via Paragraph) -- sem
                        # ser uma Table de verdade, não tinha como aplicar o
                        # sombreamento alternado (esse efeito só existe pra
                        # linhas de uma tabela). Convertido pra tabela (Projeto |
                        # Descrição | Horas) -- ganha o zebra automaticamente,
                        # de graça, pela mesma função que já faz isso em
                        # "Resumo por Projeto".
                        tabelas_pdf = []

                        # O PORQUE: largura da página A4 (210mm) menos as margens
                        # (18mm de cada lado, mesmo valor usado dentro de
                        # gerar_pdf_relatorio) -- "Descrição" precisa de bem mais
                        # espaço que "Projeto"/"Horas", senão o texto longo das
                        # tarefas não cabe e a tabela estoura a página.
                        from reportlab.lib.units import mm as _mm_pdf
                        _largura_pdf_util = (210 - 36) * _mm_pdf
                        larguras_atividades = [_largura_pdf_util * 0.22, _largura_pdf_util * 0.63, _largura_pdf_util * 0.15]

                        def _linhas_tabela_atividades(df):
                            return [[row["project"], row["description"], f"{row['effort_hours']}h"] for _, row in df.iterrows()]

                        if not rep["df_ontem"].empty:
                            tabelas_pdf.append((
                                f"O que fiz ontem ({rep['d_ontem'].strftime('%d/%m/%Y')})",
                                ["Projeto", "Descrição", "Horas"],
                                _linhas_tabela_atividades(rep["df_ontem"]),
                                larguras_atividades,
                            ))
                        if not rep["df_hoje"].empty:
                            tabelas_pdf.append((
                                f"O que farei hoje ({rep['d_hoje'].strftime('%d/%m/%Y')})",
                                ["Projeto", "Descrição", "Horas"],
                                _linhas_tabela_atividades(rep["df_hoje"]),
                                larguras_atividades,
                            ))

                        paragrafos_pdf = []
                        if rep["df_ontem"].empty:
                            paragrafos_pdf.append((f"O que fiz ontem ({rep['d_ontem'].strftime('%d/%m/%Y')}): sem registros mapeados.", "Heading4"))
                        if rep["df_hoje"].empty:
                            paragrafos_pdf.append((f"O que farei hoje ({rep['d_hoje'].strftime('%d/%m/%Y')}): sem registros mapeados.", "Heading4"))
                        if paragrafos_pdf:
                            paragrafos_pdf.append("")
                        paragrafos_pdf.append(("Impedimentos:", "Heading4"))
                        paragrafos_pdf.append(rep['impedimentos'] or "Nenhum.")
                        paragrafos_pdf.append("")
                        paragrafos_pdf.append(("Dúvidas:", "Heading4"))
                        paragrafos_pdf.append(rep['duvidas'] or "Nenhuma.")

                        # O PORQUE: gráfico "respectivo" pedido -- mostra as horas
                        # do próprio período da Daily (ontem + hoje) por projeto e
                        # por categoria, reaproveitando os mesmos dados já
                        # calculados pro resumo em texto, sem precisar consultar o
                        # banco de novo.
                        figuras_pdf = []
                        if not df_daily_combo.empty:
                            figuras_pdf.append(("Horas por Projeto (Ontem x Hoje)", _grafico_barras_mpl(
                                df_daily_combo, "project", "effort_hours",
                                "Horas por Projeto (Ontem x Hoje)", "Projeto", "Horas",
                                cor_col="Período",
                            )))
                            if "category" in df_daily_combo.columns and df_daily_combo["category"].nunique() > 1:
                                figuras_pdf.append(("Horas por Categoria", _grafico_pizza_mpl(
                                    df_daily_combo, "category", "effort_hours", "Horas por Categoria (Ontem + Hoje)",
                                )))

                        pdf_bytes = gerar_pdf_relatorio(
                            titulo="Resumo para a Daily",
                            subtitulo=f"Gerado em {agora_br().strftime('%d/%m/%Y %H:%M')}",
                            kpis=kpis_pdf,
                            paragrafos=paragrafos_pdf,
                            tabelas=tabelas_pdf,
                            figuras=figuras_pdf,
                        )
                    st.download_button(
                        label="⬇️ Baixar PDF pronto", data=pdf_bytes,
                        file_name=f"daily_{agora_br().strftime('%Y%m%d')}.pdf",
                        mime="application/pdf", use_container_width=True, type="primary",
                    )

            # O PORQUE: só monta/mostra os gráficos quando pedido (o botão
            # "Ver Gráficos do Período" acima só liga esta chave) -- gráfico
            # interativo (Plotly) tem um custo de render que não vale a pena
            # pagar toda vez que a Daily é gerada, se a pessoa só quer o texto.
            if st.session_state.get("daily_mostrar_graficos") and not pending_changes:
                df_daily_combo_tela = pd.concat([
                    rep["df_ontem"].assign(Período=f"Ontem ({rep['d_ontem'].strftime('%d/%m')})"),
                    rep["df_hoje"].assign(Período=f"Hoje ({rep['d_hoje'].strftime('%d/%m')})"),
                ], ignore_index=True) if not (rep["df_ontem"].empty and rep["df_hoje"].empty) else pd.DataFrame()

                st.markdown("---")
                st.subheader("📊 Gráficos do Período")
                if df_daily_combo_tela.empty:
                    st.info("Sem registros no período pra montar gráfico.")
                else:
                    # O PORQUE: pedido explícito -- em vez de outro gráfico de
                    # barra só pras horas totais por dia, um painel de números
                    # (mesmo estilo "Seus Números" do Dashboard: Total de
                    # Horas, Registros, Média Horas/Dia, % Impedimentos, %
                    # Dúvidas), calculado só com os registros de Ontem+Hoje --
                    # leitura mais rápida sem precisar interpretar gráfico
                    # nenhum. Mesma fórmula usada no Dashboard, só que
                    # aplicada neste recorte de 2 dias em vez do período
                    # completo escolhido lá.
                    total_horas_daily = df_daily_combo_tela["effort_hours"].sum()
                    total_registros_daily = len(df_daily_combo_tela)
                    dias_com_registro_daily = df_daily_combo_tela["Período"].nunique()
                    media_horas_dia_daily = (total_horas_daily / dias_com_registro_daily) if dias_com_registro_daily else 0
                    pct_impedimento_daily = (df_daily_combo_tela["is_impedimento"].astype(int).sum() / total_registros_daily * 100) if total_registros_daily else 0
                    pct_duvida_daily = (df_daily_combo_tela["is_duvida"].astype(int).sum() / total_registros_daily * 100) if total_registros_daily else 0

                    kpi_d1, kpi_d2, kpi_d3, kpi_d4, kpi_d5 = st.columns(5)
                    kpi_d1.metric("Total de Horas", f"{total_horas_daily:.1f}h")
                    kpi_d2.metric("Registros", f"{total_registros_daily}")
                    kpi_d3.metric(
                        "Média Horas/Dia", f"{media_horas_dia_daily:.1f}h",
                        help="Considera só os dias (Ontem/Hoje) em que houve pelo menos 1 registro.",
                    )
                    kpi_d4.metric("% Impedimentos", f"{pct_impedimento_daily:.0f}%")
                    kpi_d5.metric("% Dúvidas", f"{pct_duvida_daily:.0f}%")

                    st.markdown("---")
                    renderizar_toggle_colunas_grafico("daily")
                    c_graf1, c_graf2 = obter_par_colunas_grafico("daily")
                    with c_graf1:
                        fig_daily_proj = px.bar(
                            df_daily_combo_tela.groupby(["Período", "project"], as_index=False)["effort_hours"].sum(),
                            x="project", y="effort_hours", color="Período", barmode="group",
                            title="Horas por Projeto (Ontem x Hoje)",
                            labels={"project": "Projeto", "effort_hours": "Horas"},
                        )
                        apply_responsive_layout(fig_daily_proj, rotate_xaxis=True)
                        st.plotly_chart(fig_daily_proj, use_container_width=True)
                    with c_graf2:
                        if df_daily_combo_tela["category"].nunique() > 1:
                            fig_daily_cat = px.pie(
                                df_daily_combo_tela.groupby("category", as_index=False)["effort_hours"].sum(),
                                values="effort_hours", names="category", hole=0.4,
                                title="Horas por Categoria (Ontem + Hoje)",
                            )
                            apply_responsive_layout(fig_daily_cat)
                            st.plotly_chart(fig_daily_cat, use_container_width=True)
                        else:
                            st.caption("Só uma categoria no período -- sem gráfico de proporção pra mostrar.")

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
        today = agora_br().date()
        min_allowed_date = datetime.strptime("2000-01-01", "%Y-%m-%d").date()

        # O PORQUE: por padrão, ao abrir o Dashboard, mostramos os últimos 30
        # dias a partir de hoje — evita a tela vazia/genérica do primeiro
        # acesso. Limitamos (clamp) entre a data mais antiga e a mais recente
        # que existem no banco, para o filtro já nascer válido mesmo que o
        # histórico seja mais curto que 30 dias ou não tenha dado recente.
        default_start_date = today - timedelta(days=10)
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

                    # O PORQUE: antes dos gráficos, um resumo numérico rápido
                    # ("os números do período") -- dá uma leitura instantânea sem
                    # precisar interpretar nenhum gráfico. st.metric já empilha
                    # sozinho em telas estreitas (cada card vira uma linha), então
                    # não precisa de CSS extra para funcionar bem no celular.
                    total_horas_periodo = df_filtered["effort_hours"].sum()
                    total_registros_periodo = len(df_filtered)
                    dias_uteis_com_registro = df_filtered["log_date_dt"].dt.date.nunique()
                    media_horas_dia = (total_horas_periodo / dias_uteis_com_registro) if dias_uteis_com_registro else 0
                    pct_impedimento = (df_filtered["is_impedimento"].astype(int).sum() / total_registros_periodo * 100) if total_registros_periodo else 0
                    pct_duvida = (df_filtered["is_duvida"].astype(int).sum() / total_registros_periodo * 100) if total_registros_periodo else 0

                    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
                    kpi1.metric("Total de Horas", f"{total_horas_periodo:.1f}h")
                    kpi2.metric("Registros", f"{total_registros_periodo}")
                    kpi3.metric("Média Horas/Dia", f"{media_horas_dia:.1f}h", help="Considera só os dias em que houve pelo menos 1 registro, não os dias corridos do período.")
                    kpi4.metric("% Impedimentos", f"{pct_impedimento:.0f}%")
                    kpi5.metric("% Dúvidas", f"{pct_duvida:.0f}%")

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
                    renderizar_toggle_colunas_grafico("dashboard")
                    c_chart1, c_chart2 = obter_par_colunas_grafico("dashboard")
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
                        apply_responsive_layout(fig_time, rotate_xaxis=(len(df_bar_grouped["period_label"].unique()) > 8))
                        st.plotly_chart(fig_time, use_container_width=True)
                    with c_chart2:
                        df_grouped_cat = df_filtered.groupby("category")["effort_hours"].sum().reset_index()
                        fig_cat = px.pie(
                            df_grouped_cat, values="effort_hours", names="category", color="category",
                            title="Horas por Área de Atuação", hole=0.4, color_discrete_map=dynamic_category_colors,
                        )
                        apply_responsive_layout(fig_cat)
                        st.plotly_chart(fig_cat, use_container_width=True)

                    st.markdown("---")
                    st.subheader("Tendência ao Longo do Tempo e Análise de Pareto")
                    c_chart3, c_chart4 = obter_par_colunas_grafico("dashboard")

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
                                    mode="lines", name="Tendência linear (projeção simples)",
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

                        apply_responsive_layout(fig_line, rotate_xaxis=(len(df_daily["period_label"].unique()) > 8))
                        st.plotly_chart(fig_line, use_container_width=True)

                        # O PORQUE: o gráfico de evolução é o que mais gera dúvida de
                        # interpretação (mistura evolução real por projeto + uma
                        # tendência/projeção linear do total) -- este expander explica
                        # em linguagem simples o que está sendo mostrado, direto na
                        # tela, sem precisar perguntar pra ninguém.
                        with st.expander("ℹ️ Como ler este gráfico"):
                            st.markdown(
                                "- Cada **linha colorida** é um projeto: mostra quantas horas "
                                "você registrou nele em cada período (dia, semana ou mês, "
                                "dependendo do intervalo escolhido acima).\n"
                                "- A **linha branca pontilhada** é uma **tendência linear "
                                "simples** sobre o **total** de horas (somando todos os "
                                "projetos) -- ela ajuda a ver rapidamente se o esforço total "
                                "está subindo, caindo ou estável no período filtrado.\n"
                                "- Depois da **linha vertical cinza tracejada**, a linha "
                                "branca continua como **projeção**: é apenas a mesma reta "
                                "de tendência estendida para frente, **não** é um modelo "
                                "estatístico robusto. Com poucos pontos, ou períodos muito "
                                "irregulares (ex.: férias, mudança de projeto), essa "
                                "projeção pode enganar -- use-a como indício, não como "
                                "número exato a perseguir.\n"
                                "- É mais confiável quanto **mais pontos** o período filtrado "
                                "tiver, e quanto mais **regular** for seu ritmo de registro."
                            )

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
                        apply_responsive_layout(fig_pareto, rotate_xaxis=(len(df_pareto[pareto_dim_col]) > 8))
                        st.plotly_chart(fig_pareto, use_container_width=True)

                    # O PORQUE: nenhum dos 4 gráficos acima mostra a evolução de
                    # Impedimentos/Dúvidas ao longo do tempo -- só o volume total
                    # (via % nos KPIs). Este gráfico extra responde uma pergunta
                    # diferente e bem prática pra quem faz Daily/Retro: "os
                    # bloqueios estão aumentando, diminuindo, ou concentrados em
                    # algum período específico?" -- útil pra identificar, por
                    # exemplo, uma sprint ou projeto com atrito recorrente.
                    st.markdown("---")
                    st.subheader("Impedimentos e Dúvidas ao Longo do Tempo")
                    df_flags = (
                        df_filtered.groupby(["period_start", "period_label"])
                        .agg(Impedimentos=("is_impedimento", lambda s: s.astype(int).sum()),
                             Duvidas=("is_duvida", lambda s: s.astype(int).sum()))
                        .reset_index()
                        .sort_values("period_start")
                    )
                    if df_flags[["Impedimentos", "Duvidas"]].sum().sum() == 0:
                        st.info("Nenhum registro marcado como Impedimento ou Dúvida neste período. 🎉")
                    else:
                        df_flags_melt = df_flags.melt(
                            id_vars=["period_start", "period_label"], value_vars=["Impedimentos", "Duvidas"],
                            var_name="Tipo", value_name="Quantidade",
                        )
                        fig_flags = px.bar(
                            df_flags_melt, x="period_label", y="Quantidade", color="Tipo",
                            title=f"Impedimentos e Dúvidas por {period_axis_title}", barmode="stack",
                            labels={"period_label": period_axis_title, "Quantidade": "Nº de Registros"},
                            color_discrete_map={"Impedimentos": "#d62728", "Duvidas": "#bcbd22"},
                        )
                        fig_flags.update_xaxes(
                            type="category", categoryorder="array",
                            categoryarray=df_flags["period_label"].drop_duplicates().tolist(),
                        )
                        apply_responsive_layout(fig_flags, rotate_xaxis=(len(df_flags["period_label"].unique()) > 8))
                        st.plotly_chart(fig_flags, use_container_width=True)
                        st.caption(
                            "Cada barra soma quantos registros do período foram marcados como "
                            "🚧 Impedimento e/ou ❓ Dúvida (um mesmo registro pode contar nos dois)."
                        )

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

                    c_down_csv, c_down_txt, c_down_pdf = st.columns(3)
                    with c_down_csv:
                        st.download_button(label="Baixar Planilha (.csv)", data=csv_data, file_name=f"extrato_atividades_{start_date}_{end_date}.csv", mime="text/csv", use_container_width=True)
                    with c_down_txt:
                        st.download_button(label="Baixar Relatório (.txt)", data=report_text, file_name=f"relatorio_atividades_{start_date}_{end_date}.txt", mime="text/plain", use_container_width=True)
                    with c_down_pdf:
                        # O PORQUE: gerado só quando o botão é clicado (não a cada
                        # rerun da aba) -- montar o PDF envolve renderizar cada
                        # gráfico como imagem, mais pesado que só montar um
                        # texto/CSV; não vale a pena pagar esse custo antes de o
                        # usuário realmente pedir o arquivo. Os gráficos do PDF são
                        # reconstruídos com matplotlib a partir de df_filtered (não
                        # são os mesmos objetos Plotly da tela) -- ver justificativa
                        # em gerar_pdf_relatorio().
                        if st.button("📄 Gerar PDF do Dashboard", use_container_width=True):
                            with st.spinner("Montando o PDF (isso pode levar alguns segundos)..."):
                                kpis_pdf = [
                                    (f"{len(df_filtered)}", "Registros"),
                                    (f"{df_filtered['effort_hours'].sum():.1f}h", "Total de Horas"),
                                    (f"{media_horas_dia:.1f}h", "Média/Dia"),
                                    (f"{pct_impedimento:.0f}%", "Impedimentos"),
                                    (f"{pct_duvida:.0f}%", "Dúvidas"),
                                ]

                                figuras_pdf = [
                                    ("Horas por Projeto", _grafico_barras_mpl(
                                        df_filtered, "project", "effort_hours", "Horas por Projeto", "Projeto", "Horas",
                                    )),
                                    ("Horas por Categoria", _grafico_pizza_mpl(
                                        df_filtered, "category", "effort_hours", "Horas por Categoria",
                                    )),
                                ]
                                df_por_data = df_filtered.groupby("Data_PTBR", sort=False)["effort_hours"].sum().reset_index()
                                if len(df_por_data) > 1:
                                    figuras_pdf.append(("Horas por Data", _grafico_barras_mpl(
                                        df_por_data, "Data_PTBR", "effort_hours", "Horas por Data", "Data", "Horas",
                                    )))
                                # O PORQUE: gráfico novo -- nenhuma versão anterior do
                                # PDF mostrava a evolução de impedimentos/dúvidas, só o
                                # total (via % nos KPIs). Só entra se houver pelo menos
                                # um registro marcado, pra não desperdiçar uma página com
                                # um gráfico vazio.
                                if df_filtered[["is_impedimento", "is_duvida"]].astype(int).sum().sum() > 0:
                                    figuras_pdf.append(("Impedimentos e Dúvidas", _grafico_impedimentos_mpl(
                                        df_filtered, "Data_PTBR",
                                    )))

                                # O PORQUE: tabela com números exatos por projeto,
                                # complementando o gráfico de barras (bom pra ver
                                # proporção, ruim pra ler um valor preciso).
                                total_horas_pdf = df_filtered["effort_hours"].sum()
                                df_resumo_projeto = (
                                    df_filtered.groupby("project")["effort_hours"].sum()
                                    .sort_values(ascending=False).reset_index()
                                )
                                linhas_tabela_projeto = [
                                    [r["project"], f"{r['effort_hours']:.2f}h", f"{(r['effort_hours'] / total_horas_pdf * 100 if total_horas_pdf else 0):.0f}%"]
                                    for _, r in df_resumo_projeto.iterrows()
                                ]
                                tabelas_pdf = [("Resumo por Projeto", ["Projeto", "Horas", "% do Total"], linhas_tabela_projeto)]

                                pdf_bytes = gerar_pdf_relatorio(
                                    titulo="Relatório do Dashboard",
                                    subtitulo=f"Período: {start_date.strftime('%d/%m/%Y')} a {end_date.strftime('%d/%m/%Y')}",
                                    kpis=kpis_pdf,
                                    tabelas=tabelas_pdf,
                                    figuras=figuras_pdf,
                                )
                            st.download_button(
                                label="⬇️ Baixar PDF pronto", data=pdf_bytes,
                                file_name=f"dashboard_{start_date}_{end_date}.pdf",
                                mime="application/pdf", use_container_width=True, type="primary",
                            )
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


if is_admin:
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
            # O PORQUE: só capturamos os bytes/nome do arquivo aqui (enquanto o
            # widget uploaded_file ainda existe nesta execução) e delegamos o
            # processamento pesado (parse + comparação + IA) pro dispatcher --
            # durante o bloqueio de tela cheia, NENHUM widget é redesenhado
            # (nem o próprio uploader), então tudo que for necessário precisa
            # estar dentro do payload, não em variáveis/widgets locais.
            raw_bytes = uploaded_file.read()
            file_ext = os.path.splitext(uploaded_file.name)[1].lower()
            run_blocking_action(
                "ia_analisar_arquivo",
                {"raw_bytes": raw_bytes, "file_ext": file_ext, "file_name": uploaded_file.name},
                processing_message="Comparando com os registros salvos...",
                success_message="Análise concluída.",
                failure_message="Não foi possível analisar o arquivo.",
            )

        if st.session_state.sync_analyzed:
            st.markdown("---")

            # O PORQUE: resultado da estimativa por IA (calculado durante o
            # bloqueio de tela cheia, onde st.warning/st.info não apareceriam
            # pro usuário) -- exibido aqui, na primeira renderização normal
            # depois da análise. .pop() garante que só aparece uma vez.
            analise_ia_info = st.session_state.pop("_analise_arquivo_ia_info", None)
            if analise_ia_info:
                if analise_ia_info["ia_aviso"]:
                    st.warning(
                        f"⚠️ Não foi possível usar a estimativa por IA agora ({analise_ia_info['ia_aviso']}). "
                        "Os registros abaixo seguem com esforço fixo (1h) e classificação "
                        "por palavra-chave, como antes -- revise/ajuste manualmente se precisar."
                    )
                elif analise_ia_info["novos_projetos"] or analise_ia_info["novas_categorias"]:
                    partes = []
                    if analise_ia_info["novos_projetos"]:
                        partes.append(f"projeto(s) **{', '.join(analise_ia_info['novos_projetos'])}**")
                    if analise_ia_info["novas_categorias"]:
                        partes.append(f"categoria(s) **{', '.join(analise_ia_info['novas_categorias'])}**")
                    st.info(f"🤖 IA aplicada. Adicionado(s) automaticamente: {' e '.join(partes)}.")
                elif N8N_AI_ESTIMATE_WEBHOOK_URL:
                    st.info("🤖 Estimativa de esforço e classificação por IA aplicada.")

            edited_insert = pd.DataFrame()
            edited_delete = pd.DataFrame()

            col_ins, col_del = st.columns(2)

            with col_ins:
                st.subheader("🟢 Novos Registros")
                if st.session_state.df_to_insert.empty:
                    st.success("Nenhum registro novo encontrado no arquivo.")
                else:
                    st.write("Desmarque a caixa `_Aplicar` para ignorar o registro. Projeto, Categoria e Horas já vêm editáveis (úteis para corrigir uma sugestão da IA, se houver).")
                    # O PORQUE: st.data_editor permite manipulação booleana direto no DataFrame sem loops complexos.
                    # DateColumn com format="DD/MM/YYYY" exibe a data no padrão brasileiro
                    # mesmo com o valor por baixo continuando em ISO (YYYY-MM-DD).
                    # project/category viraram SelectboxColumn (com opção de digitar um
                    # novo valor não listado) e effort_hours virou editável, porque agora
                    # esses três campos podem vir de uma estimativa por IA que às vezes
                    # precisa de um ajuste manual antes de confirmar.
                    edited_insert = st.data_editor(
                        st.session_state.df_to_insert,
                        column_config={
                            "_Aplicar": st.column_config.CheckboxColumn("Aplicar", default=True),
                            "log_date": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                            "project": st.column_config.SelectboxColumn("Projeto", options=get_project_options()),
                            "category": st.column_config.SelectboxColumn("Categoria", options=get_category_options()),
                            "effort_hours": st.column_config.NumberColumn("Horas", min_value=0.0, max_value=24.0, step=0.25),
                            "is_impedimento": st.column_config.CheckboxColumn("🚧 Impedimento"),
                            "is_duvida": st.column_config.CheckboxColumn("❓ Dúvida"),
                        },
                        disabled=["log_date", "description", "is_impedimento", "is_duvida"],
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
                    run_blocking_action(
                        "ia_sincronizar",
                        {"edited_insert": edited_insert, "edited_delete": edited_delete},
                        processing_message="Sincronizando registros...",
                        success_message="Sincronização concluída.",
                        failure_message="Não foi possível sincronizar.",
                    )
