# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Sobre o projeto

Task Tracker: app Streamlit pessoal para controle de atividades de trabalho (registro diário de tarefas, horas, projeto e categoria), com Daily Scrum, dashboards/PDF, Banco de Horas, Diário (texto livre → registros via IA) e área de convidado somente leitura. Publicado no Streamlit Community Cloud a partir do GitHub (`coelhovinicius/Controle-de-Atividades`); cada push na branch configurada no Cloud faz redeploy. O dono do projeto fala português (pt-BR): responda em português.

O `README.md` é a referência atualizada de funcionalidades e secrets. `docs/Documentacao_v2.md` está parcialmente desatualizado (a seção de login ainda descreve senha em texto puro e token na URL; hoje é bcrypt + cookie assinado).

## Comandos

```powershell
py -3.12 -m venv venv          # libsql não tem wheel para versões muito novas do Python
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
python scripts\gerar_hash_de_senha.py   # hash bcrypt para [credentials]
```

Scripts em `scripts/` rodam a partir da raiz do projeto e leem `.streamlit/secrets.toml`.

Não há suíte de testes nem linter. Para verificar uma mudança sem navegador, use `streamlit.testing.v1.AppTest` pulando o login pelo `session_state`:

```python
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=60)
at.session_state["authenticated"] = True
at.session_state["auth_username"] = "teste"
at.session_state["user_role"] = "admin"
at.run()
assert not at.exception
```

Modais (`st.dialog`) não são clicáveis pelo AppTest: para simular o "Sim, confirmar", copie `at.session_state["confirm_action"]` para `processing_action`, ponha `processing = True` e rode de novo.

**Cuidado:** o `.streamlit/secrets.toml` local contém as credenciais do Turso de **produção**. Com ele presente, `streamlit run`/AppTest gravam no banco real. Para testar, rode numa cópia de `app.py`, `database_core.py` e `importer_core.py` sem `secrets.toml` (sem `TURSO_*`, o app cai sozinho para um SQLite local `personal_tracker.db`).

## Arquitetura

Três módulos na raiz:

- **`database_core.py`**: `DatabaseConnection` conecta no Turso (libsql, envolvido em `_ConexaoComRetentativa`, que reconecta quando a conexão HTTP expira) ou cai para SQLite local; com `REQUIRE_TURSO=true` levanta `TursoRequiredError` em vez de cair. `LogRepository` concentra todo o SQL. **Não há ferramenta de migração**: o schema é criado/evoluído em `_initialize_database()` a cada inicialização (`CREATE TABLE IF NOT EXISTS` + `_ensure_column`). Tabelas: `work_logs`, `custom_options`, `access_requests`, `user_settings`, `daily_notes`.
- **`importer_core.py`**: `HistoryParser` lê o texto no formato do OneNote (`dd/mm/aaaa:` + tarefas) e classifica por palavra-chave (`PROJECT_KEYWORDS`, `CATEGORY_KEYWORDS`, `IMPEDIMENT_KEYWORDS`, `QUESTION_KEYWORDS`). `parse_text()` é usado pela Sincronização e **precisa continuar produzindo exatamente as mesmas descrições** (a Sincronização compara com o banco por igualdade). O Diário usa `parse_text_por_linha()` (uma linha `---` encerra as tarefas do dia) e `split_text_by_date()` (guarda o texto inteiro para o Caderno). `scripts/import_history.py` é legado e tem cópias próprias das listas de palavras-chave.
- **`app.py`**: um único script Streamlit (~5 mil linhas) executado de cima para baixo a cada rerun, nesta ordem: conexão (`get_repository()`, `@st.cache_resource`, com o hash de `database_core.py` como argumento: um deploy não reinicia o processo, e sem isso o `LogRepository` antigo continuaria em uso), login (`st.stop()` se não autenticado), helpers e IA, inicialização do `st.session_state`, **dispatcher de ações**, barra lateral, abas.

### Padrão de escrita no banco (o mais importante para mexer no app)

Toda escrita passa pelo mesmo fluxo:

1. O botão chama `request_confirmation(tipo, payload, ...)` (abre modal "tem certeza?") ou `run_blocking_action(tipo, payload, ...)` (sem modal, para IA/análise). Ambos guardam a ação no `session_state` e dão `st.rerun()`.
2. No rerun seguinte, o bloco `if st.session_state.processing:` desenha só o overlay de "Processando...", chama `execute_processing_action(action)` e dá toast + rerun. Nada mais é desenhado nessa execução, o que bloqueia a interação.
3. `execute_processing_action` despacha por `action["type"]` numa cadeia de `elif t == "...":`. **Para uma nova ação de escrita, adicione um `elif` aí.** Ela recusa tudo se `user_role != "admin"`.

Consequências desse fluxo:
- O dispatcher roda **no meio do arquivo**: funções chamadas por ele precisam estar definidas **acima** desse bloco (ex.: `remove_accents` é definida depois dele e não pode ser usada lá; o Diário tem `_chave_descricao` por isso).
- Dentro do dispatcher é seguro escrever em keys de widgets (eles ainda não foram desenhados). Fora dele, não.
- `st.info`/`st.warning` dentro do dispatcher não aparecem: guarde o resultado no `session_state` e faça `.pop()` na tela, no rerun seguinte (ex.: `_analise_arquivo_ia_info`, `_recalculo_ia_resultado`).
- Payloads podem carregar DataFrames do `st.data_editor` (capturados antes do rerun).

### Outras regras do domínio

- **Multiusuário por `username`**: toda query filtra por `username`. O convidado (`user_role == "guest"`, link `?g=<token>` aprovado em `access_requests`) usa `auth_username = ADMIN_USERNAME` para ver os dados do dono, mas só vê Registro (leitura) e Dashboard.
- **Sessão do admin**: cookie `ttk_session` assinado com HMAC (`SESSION_SECRET_KEY`), validade de 12h; senhas em `[credentials]` como hash bcrypt.
- **Datas**: `log_date` é texto `YYYY-MM-DD`. O libsql pode devolver com hora, então normalize com `.astype(str).str[:10]` antes de comparar. Use `agora_br()` (America/Sao_Paulo), nunca `datetime.now()` (no Cloud o relógio é UTC).
- **Turso é remoto**: cada `commit` é uma ida e volta de rede. Escritas em lote usam `executemany` + um único `commit` (`insert_logs_bulk`, `update_logs_bulk`, `save_daily_notes_bulk`).
- **Projetos/categorias** = listas fixas `BASE_PROJECT_OPTIONS`/`BASE_CATEGORY_OPTIONS` em `app.py` + tabela `custom_options`; leia sempre via `get_project_options()`/`get_category_options()`. "Outros" é o "não classificado" do leitor por palavra-chave.
- **IA**: opcional, via webhook do n8n (`N8N_AI_ESTIMATE_WEBHOOK_URL`), com cascata Gemini → Groq → Mistral dentro do workflow. Contrato: POST `{known_projects, known_categories, tasks: [{id, description}]}` → `{results: [{id, estimated_minutes, project, category, is_new_project, is_new_category}]}`. `aplicar_estimativa_ia_e_normalizacao()` aplica o resultado e fecha cada dia em 8h, exceto dias com `OVERTIME`/`plantão` em alguma descrição. Se a IA falhar, tudo cai para 1h fixa + palavra-chave, sem quebrar. O JSON do workflow e o guia ficam em `.ignore/` (fora do Git), apesar de o README citá-los na raiz.
- **Sincronização × Diário**: a Sincronização **espelha** um arquivo inteiro (propõe remover o que está no banco e não está no arquivo, com confirmação digitando o nome do arquivo). O Diário **só insere**: descarta linhas já salvas (mesmo dia + descrição sem acento/maiúscula), desconta as horas já salvas no fechamento de 8h e guarda o texto original em `daily_notes` (uma linha por dia; modos `append`/`replace`/`skip` em `save_daily_notes_bulk`).
- **Caderno × registros**: o Caderno junta `daily_notes` e `work_logs` na hora de desenhar (`_montar_dias_caderno`); os registros nunca são copiados para `daily_notes`. Consistência nos dois sentidos: (1) **toda escrita em `work_logs` que mude data ou descrição precisa chamar `_refletir_registros_nas_anotacoes`** (ela atualiza a linha na anotação do dia); o recálculo por IA não chama porque só muda horas/projeto/categoria. (2) Editar um dia no Caderno com "Aplicar também nos registros" passa por `caderno_revisar` + `caderno_aplicar`, gravado em um único commit (`apply_day_changes`). A proposta vem de `comparar_tarefas`, que compara o texto antigo com o novo, e não com o banco, para nunca propor mexer em registro que a pessoa não editou. A leitura linha a linha fica só em `_eventos_por_linha` (`importer_core.py`), usada pelo Diário e pelo Caderno.

## Convenções de código

- Interface, comentários e mensagens em português. Decisões não óbvias levam um comentário `# O PORQUE:` explicando o motivo; siga esse padrão.
- Ações destrutivas sempre com confirmação explícita; SQL sempre parametrizado.
- `streamlit` está fixado em versão exata no `requirements.txt` (o Cloud reinstala tudo a cada deploy); as outras dependências usam `>=`.
- Fora do Git (`.gitignore`): `.streamlit/secrets.toml`, `*.db`, `raw_history.txt` (histórico bruto do OneNote, já importado) e `.ignore/` (prompts, workflows do n8n e chaves pessoais).
