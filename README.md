# Task Tracker

Sistema pessoal de controle de atividades de trabalho — registro diário de
tarefas, horas, projeto e categoria — com dashboards analíticos, geração
automática de resumo para Daily Scrum, importação em massa de histórico,
estimativa de esforço por IA e área de convidado com aprovação.
Feito em [Streamlit](https://streamlit.io), publicável gratuitamente na
internet (Streamlit Community Cloud).

---

## Funcionalidades

### Registro de Atividades
- Cadastro, edição e exclusão de tarefas (data, projeto, categoria,
  descrição, horas, impedimento, dúvida).
- Botão **Salvar e Novo** — salva e já abre o formulário limpo para a
  próxima tarefa.
- Ordenação clicável em qualquer coluna (seta ▲/▼ ao lado do título),
  padrão ID descendente.
- Filtro de período (padrão: últimos 10 dias), consultado direto no banco
  — não traz o histórico inteiro a cada abertura.
- Busca por texto (ignora acentos/maiúsculas), paginação.
- Sugestão de duração/projeto/categoria por IA no formulário manual (botão
  "🤖 Sugerir com IA").

### Daily Scrum
- Escolha de período (ontem/hoje), sugestão automática de
  impedimentos/dúvidas a partir dos registros marcados.
- Geração de resumo formatado, editável antes de salvar.
- Download em `.txt` ou **PDF** (resumo + gráfico comparativo Ontem x Hoje).

### Dashboard & Relatórios
- Filtro de período livre, gráficos de horas por projeto/categoria,
  evolução temporal com tendência, Pareto (80/20), impedimentos/dúvidas ao
  longo do tempo.
- KPIs no topo (Total de Horas, Registros, Média Horas/Dia, %
  Impedimentos, % Dúvidas).
- Exportação em `.csv`, `.txt` ou **PDF** (KPIs + gráficos do período).
- Botão de **recálculo de esforço por IA** (barra lateral) — reestima
  horas e reclassifica projeto/categoria de registros já salvos, com
  confirmação antes de aplicar.

### Sincronização de Arquivo
- Upload de histórico (`.txt` livre ou `.csv` estruturado) para
  importar/reclassificar tarefas em massa.
- Compara com o que já está salvo (mesma data/projeto/categoria/
  descrição/horas/flags = mesmo registro); o que não bate é marcado para
  inserir ou excluir, com revisão e confirmação antes de aplicar.
- Estimativa de esforço e classificação de projeto/categoria por IA nos
  registros novos, se configurada (ver seção IA abaixo).

### IA (opcional)
Integração com um workflow do [n8n](https://n8n.io) próprio (não usa API
paga por padrão) para:
- Estimar a duração realista de cada tarefa a partir da descrição.
- Classificar Projeto/Categoria contra o que já existe no banco,
  cadastrando automaticamente o que for genuinamente novo.
- Detectar dias de `OVERTIME`/`plantão` (palavra-chave na descrição) e não
  normalizar o total desses dias para 8h.
- Cascata de provedores gratuitos/baratos (Gemini → Groq → Mistral, nessa
  ordem) — só cai pro próximo se o anterior falhar.

Veja `n8n_workflow_estimativa_ia.json` e `GUIA_IA_ESFORCO_E_SESSAO.md`.

### Autenticação e controle de acesso
- Login por usuário/senha (hash bcrypt em `[credentials]`, nunca texto
  puro) — qualquer conta aqui é **administrador** completo.
- Sessão persistente via **cookie assinado (HMAC)**, gravado no navegador
  no login — sobrevive a F5 e (opcionalmente) a restart do servidor, mas
  **não é copiável via URL** (diferente de um token na barra de endereço).
- **Área de convidado**: quem não tem login preenche nome/e-mail/
  justificativa; um administrador aprova pela barra lateral e recebe um
  link único e revogável para compartilhar. Validade do link
  **configurável em dias** na hora de aprovar (ou "sem expiração"),
  **ajustável a qualquer momento depois** sem precisar gerar um link novo.
  Convidado só enxerga Registro de Atividades (leitura) e Dashboard — sem
  upload, sem CRUD, sem acesso administrativo. Limite de 5 solicitações
  ativas por vez.
- Checagem de autorização no backend (não só esconder botão na tela) antes
  de qualquer ação de escrita.

Veja `GUIA_CONVIDADO_ADMIN.md`.

---

## Stack

| Camada | Tecnologia |
|---|---|
| Interface | [Streamlit](https://streamlit.io) |
| Gráficos (tela) | Plotly |
| Gráficos (PDF) | matplotlib |
| Geração de PDF | reportlab |
| Dados/análise | pandas, NumPy |
| Banco de dados | SQLite (local) → [Turso](https://turso.tech) (produção, via `libsql`) |
| Autenticação | bcrypt + cookie assinado (HMAC) |
| IA | n8n (webhook) + Gemini/Groq/Mistral |
| Hospedagem | Streamlit Community Cloud |

---

## Estrutura do projeto

```
PersonalTrackerApp/
├── app.py                     # Aplicação principal (todas as abas)
├── database_core.py           # Conexão com o banco e operações de CRUD
├── importer_core.py           # Motor de importação/classificação (Sincronização)
├── requirements.txt
├── n8n_workflow_estimativa_ia.json   # Workflow de IA, pronto para importar no n8n
├── .streamlit/
│   ├── config.toml
│   ├── secrets.toml           # Credenciais reais -- NUNCA vai para o Git
│   └── secrets.toml.example   # Modelo de referência (pode versionar)
├── docs/
│   ├── Documentacao.md
│   ├── TURSO_DEPLOY.md
│   ├── CHANGELOG_SEGURANCA_E_UX.md
│   ├── GUIA_IA_ESFORCO_E_SESSAO.md
│   └── GUIA_CONVIDADO_ADMIN.md
└── scripts/
    ├── backfill_username.py
    ├── fix_legacy_descriptions.py
    ├── fix_project_reclassification.py
    ├── gerar_hash_de_senha.py
    ├── import_history.py
    └── migrate_to_turso.py
```

---

## Rodando localmente

```powershell
# 1) Ambiente virtual (recomendado Python 3.12 ou 3.13 -- libsql ainda não
#    tem wheel pronta para versões mais novas em alguns sistemas)
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1

# 2) Dependências
pip install -r requirements.txt

# 3) Secrets
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# edite .streamlit\secrets.toml com seus valores reais (veja seção abaixo)

# 4) Gere o hash da sua senha
python scripts\gerar_hash_de_senha.py

# 5) Rode
streamlit run app.py
```

Sem `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` configurados, o app cai
automaticamente para um arquivo SQLite local (`personal_tracker.db`) —
funciona 100% offline para desenvolvimento.

---

## Configuração (Secrets)

Todas as chaves ficam em `.streamlit/secrets.toml` (local) ou em
**Settings → Secrets** no painel do Streamlit Community Cloud (produção).
Veja `.streamlit/secrets.toml.example` para o formato completo comentado.

| Chave | Obrigatória? | Para quê |
|---|---|---|
| `[credentials]` | Sim | Usuário(s)/hash(es) bcrypt de login (gere com `scripts/gerar_hash_de_senha.py`) |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Recomendado em produção | Banco persistente (sem isso, cai para SQLite local — problemático no Streamlit Cloud, que tem disco efêmero) |
| `REQUIRE_TURSO` | Opcional | `"true"` faz o app recusar rodar se o Turso não conectar, em vez de cair silenciosamente para SQLite local |
| `SESSION_SECRET_KEY` | Opcional | Assina o cookie de sessão; sem ela, uma chave nova é gerada a cada restart do servidor (todo mundo é deslogado) |
| `ADMIN_USERNAME` | Opcional | Necessário para a área de convidado funcionar — diz de qual usuário os convidados veem os dados |
| `N8N_AI_ESTIMATE_WEBHOOK_URL` | Opcional | Ativa a estimativa de esforço/classificação por IA |

---

## Deploy (Streamlit Community Cloud + Turso)

Passo a passo completo em `docs/TURSO_DEPLOY.md`.

---

## Segurança

- Senhas nunca em texto puro (bcrypt) nem no código/Git (Secrets).
- Sessão via cookie assinado (HMAC-SHA256), não exposta na URL — copiar o
  link do navegador não loga ninguém em outro lugar.
- Toda ação de escrita passa por checagem de autorização no backend, não
  só por esconder o botão na tela.
- Ações destrutivas (excluir, revogar acesso, cancelar sincronização)
  sempre pedem confirmação explícita.
- SQL sempre parametrizado (sem concatenação de string vinda de input do
  usuário).

Detalhes da evolução de segurança em `docs/CHANGELOG_SEGURANCA_E_UX.md`.

---

## Scripts utilitários (`scripts/`)

| Script | Uso |
|---|---|
| `gerar_hash_de_senha.py` | Gera o hash bcrypt de uma senha para colar em `[credentials]` |
| `migrate_to_turso.py` | Copia o banco local para o Turso (rodar uma vez, na migração inicial) |
| `import_history.py` | Importa `raw_history.txt` via linha de comando |
| `backfill_username.py` | Atribui registros antigos (sem usuário) a um usuário dono |
| `fix_legacy_descriptions.py` / `fix_project_reclassification.py` | Correções pontuais de dados já importados |

Rode todos a partir da raiz do projeto (ex.: `python scripts/gerar_hash_de_senha.py`).