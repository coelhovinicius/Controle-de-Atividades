# Como o Task Tracker foi feito — do início ao fim

Este documento resume, de forma completa, tudo que compõe o app **Task Tracker**
(também chamado "QA Tracker & Insights"): o que ele faz, com que tecnologia foi
construído, onde os dados moram, onde está hospedado, e todas as ferramentas
envolvidas no processo. É pra você conseguir explicar o projeto inteiro sem
precisar checar nada.

---

## 1. O que é o app, em uma frase

Um sistema pessoal de controle de atividades de trabalho (registro diário de
tarefas, horas, projeto e categoria), com dashboards analíticos, gerador de
resumo para reuniões de Daily Scrum, e importação em massa de histórico — tudo
rodando como uma aplicação web feita em Python, publicada gratuitamente na
internet.

---

## 2. Linguagem e framework

- **Linguagem:** Python.
- **Framework de interface (front-end + back-end em um só lugar):**
  [Streamlit](https://streamlit.io) — biblioteca que transforma um script
  Python em uma aplicação web interativa, sem precisar escrever HTML/CSS/JS
  manualmente. Cada tela, botão, tabela e gráfico do app é gerado por comandos
  Streamlit (`st.button`, `st.form`, `st.dataframe`, `st.plotly_chart` etc.)
  dentro do arquivo principal `app.py`.
- **Bibliotecas Python usadas:**
  - `pandas` — manipulação e filtragem dos dados (tabelas, agrupamentos por
    período, cálculo de totais).
  - `numpy` — cálculos numéricos, incluindo a regressão linear simples usada
    na linha de tendência/previsão do dashboard (`np.polyfit`).
  - `plotly` (`plotly.express` e `plotly.graph_objects`) — todos os gráficos
    do Dashboard: barras por projeto/categoria, pizza, evolução temporal com
    tendência, e o gráfico de Pareto (80/20).
  - `sqlite3` — acesso ao banco de dados local.
  - `libsql` — driver que permite ao mesmo código conversar com o **Turso**
    (banco remoto), usando a mesma linguagem SQL do SQLite.
  - Módulos padrão do Python: `re` (expressões regulares para interpretar o
    histórico de texto), `datetime`, `io`, `os`, `sys`, `hmac` (comparação
    segura de senha no login).

---

## 3. Banco de dados

- **Motor:** SQLite (relacional, leve, baseado em arquivo).
- **Tabela principal:** `work_logs` — cada linha é um registro de atividade
  (data, projeto, categoria, descrição, horas, se é impedimento, se é dúvida,
  data de criação). Existe também `custom_options`, para os valores
  personalizados de projeto/categoria cadastrados na tela.
- **Duas fases de hospedagem do banco:**
  1. **Fase local:** um arquivo `personal_tracker.db` (SQLite puro) gravado no
     disco da própria máquina/servidor onde o app roda.
  2. **Fase produção (atual):** o banco foi migrado para o
     **[Turso](https://turso.tech)** — um serviço de banco de dados na nuvem,
     compatível com SQLite, com camada gratuita. Isso resolve um problema
     importante: hospedagens gratuitas como o Streamlit Community Cloud usam
     sistema de arquivos **efêmero** (o disco do container pode ser resetado a
     qualquer momento) — então um `.db` local ali dentro correria risco de
     perder dados. Com o Turso, o banco vive fora do container, de forma
     persistente.
  3. **Script de migração:** `migrate_to_turso.py` — leu todos os registros do
     `personal_tracker.db` local e copiou (em lote, com `INSERT OR IGNORE`
     para ser seguro rodar mais de uma vez) para o banco novo no Turso.
- **Como o app decide onde conectar:** em `database_core.py`, a classe
  `DatabaseConnection` primeiro tenta ler duas variáveis de ambiente
  (`TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN`). Se existirem, conecta no Turso;
  se não existirem (ex.: rodando local sem essas variáveis configuradas), cai
  automaticamente para o SQLite local — o mesmo código funciona nos dois
  cenários.

---

## 4. Estrutura do projeto (arquivos)

| Arquivo | Função |
|---|---|
| `app.py` | Aplicação principal — todas as telas/abas do Streamlit |
| `database_core.py` | Conexão com o banco (SQLite local ou Turso) e todas as operações de CRUD |
| `importer_core.py` | Motor de importação/classificação usado pela tela de Sincronização (upload de `.txt`/`.csv`) |
| `import_history.py` | Versão do importador para rodar via linha de comando, lendo `raw_history.txt` direto do disco |
| `migrate_to_turso.py` | Script (rodado uma única vez) para copiar os dados do SQLite local para o Turso |
| `fix_legacy_descriptions.py` | Script pontual para limpar um bug antigo de formatação nas descrições já importadas |
| `fix_project_reclassification.py` | Script pontual para corrigir a classificação de projeto de registros antigos |
| `requirements.txt` | Lista de dependências Python que o Streamlit Cloud instala automaticamente no deploy |
| `.streamlit/config.toml` | Configurações do servidor Streamlit (ex.: limite de tamanho de upload) |
| `.streamlit/secrets.toml` | Credenciais sensíveis (Turso + login) — **nunca vai para o GitHub** |
| `TURSO_DEPLOY.md` | Guia passo a passo do processo de deploy com Turso + login |

---

## 5. Principais funcionalidades (por aba)

1. **Registro de Atividades** — cadastro, edição e exclusão de tarefas, com
   busca (ignorando acentos/maiúsculas), paginação, e ordenação clicável pelas
   colunas **ID** e **Data**.
2. **Daily Scrum** — escolhe um período ("ontem"/"hoje", com botão explícito
   "Aplicar Período"), sugere automaticamente impedimentos/dúvidas com base em
   registros marcados no banco, permite editar manualmente (mesclando com as
   sugestões, sem apagar o que já foi digitado), gera um resumo formatado e
   permite baixar em `.txt`.
3. **Dashboard & Relatórios** — filtro de período com botão "Aplicar Filtro";
   gráficos de horas por projeto/categoria; gráfico de evolução temporal com
   granularidade adaptativa (diária, semanal ou mensal, dependendo do tamanho
   do intervalo escolhido) e uma linha de tendência/previsão calculada por
   regressão linear; gráfico de Pareto (80/20) para identificar onde se
   concentra a maior parte do esforço; exportação para `.csv`/`.txt`.
4. **Sincronização de Arquivo** — upload de um histórico (`.txt` ou `.csv`)
   para importar/reclassificar tarefas em massa, com confirmação por nome de
   arquivo digitado e botão de cancelar com modal de confirmação.
5. **Login** — tela de usuário/senha própria (não é o recurso nativo do
   Streamlit Cloud), comparando contra credenciais guardadas nos "Secrets" do
   app, usando `hmac.compare_digest` para evitar vazamento de informação por
   tempo de resposta.

---

## 6. Importação e classificação automática (o "ETL")

O arquivo `raw_history.txt` (texto solto, copiado de anotações antigas) é
interpretado por expressões regulares que:
- Detectam linhas de data (`dd/mm/aaaa`);
- Agrupam as linhas seguintes como tarefas daquele dia;
- Classificam automaticamente **projeto** e **categoria** por
  palavras-chave (dicionários `PROJECT_KEYWORDS` / `CATEGORY_KEYWORDS`);
- Marcam automaticamente se o item é um **impedimento** ou uma **dúvida**,
  também por palavras-chave (`IMPEDIMENT_KEYWORDS` / `QUESTION_KEYWORDS`).

Esses dicionários foram ajustados várias vezes conforme o histórico real
revelava padrões novos (ex.: separar "Vagas" de "Job Boards", criar
"Backoffice" e "Cockpit" como projetos próprios).

---

## 7. Hospedagem e deploy

- **Controle de versão:** GitHub — o código-fonte fica em um repositório, e
  o Streamlit Community Cloud lê direto dele.
- **Hospedagem em produção:** **Streamlit Community Cloud** — serviço
  oficial e gratuito da Streamlit para publicar apps ligados a um
  repositório GitHub. Basta selecionar o repositório/branch/arquivo principal
  e clicar em "Deploy"; a cada `git push`, o app atualiza sozinho.
- **URL do app:** `https://controle-de-atividades.streamlit.app` — um
  subdomínio dentro do domínio `streamlit.app`, escolhido por você nas
  configurações de deploy (não é um domínio próprio registrado).
- **Segredos (Secrets):** configurados manualmente na aba "Secrets" das
  configurações do app no Streamlit Cloud (nunca versionados no GitHub),
  contendo `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` e a seção `[credentials]`
  do login.
- **Caminho alternativo que também foi preparado (não é o que está no ar
  hoje):** uma instância gratuita "Always Free" na **Oracle Cloud
  Infrastructure** (VM Ampere A1, ARM, Ubuntu 24.04), com o app rodando via
  `systemd` (para reiniciar sozinho) e porta liberada manualmente no firewall
  interno e na Security List da VCN. Essa rota dá controle total do servidor
  (útil se um dia quiser sair do Streamlit Cloud), mas o que efetivamente
  ficou publicado foi o caminho Streamlit Cloud + Turso.

---

## 8. Domínio / DNS

Não há um domínio próprio registrado (tipo `.com` ou `.com.br`) nem
configuração de DNS personalizada. O endereço do app é um **subdomínio
gratuito fornecido pela própria Streamlit** dentro de `streamlit.app`
(`controle-de-atividades.streamlit.app`), com HTTPS já incluído por padrão.
Se um dia quiser um domínio próprio (ex. `tasktracker.com.br`), isso exigiria
registrar o domínio em um registrador (Registro.br, GoDaddy etc.) e apontar
um registro DNS (CNAME) para o Streamlit Cloud — mas isso não foi feito
neste projeto.

---

## 9. Segurança e boas práticas aplicadas

- Senhas e tokens nunca ficam no código nem no GitHub — vivem só nos
  "Secrets" do Streamlit Cloud (equivalente a variáveis de ambiente).
- Comparação de senha usa `hmac.compare_digest`, que evita um tipo de ataque
  por tempo de resposta (timing attack).
- Ações destrutivas (excluir registro, cancelar sincronização, descartar
  edição) sempre passam por um modal de confirmação antes de executar.
- Scripts de correção pontual (`fix_*.py`) foram desenhados para serem
  **idempotentes** — rodar mais de uma vez não duplica nem corrompe dados.

---

## 10. Resumo de ferramentas e serviços usados

| Categoria | Ferramenta/Serviço |
|---|---|
| Linguagem | Python |
| Framework web | Streamlit |
| Gráficos | Plotly |
| Dados/análise | Pandas, NumPy |
| Banco de dados | SQLite (local) → Turso (produção, via driver `libsql`) |
| Controle de versão | GitHub |
| Hospedagem | Streamlit Community Cloud |
| Domínio | Subdomínio gratuito `*.streamlit.app` (sem DNS próprio) |
| Autenticação | Login próprio (usuário/senha via Secrets + `hmac`) |
| Hospedagem alternativa (preparada, não usada) | Oracle Cloud Infrastructure (VM Always Free, Ubuntu, systemd) |

---

## 11. Linha do tempo resumida do processo

1. Construção do app em Streamlit: cadastro de atividades, dashboards e
   relatório de Daily Scrum.
2. Criação dos scripts de importação/ETL para transformar um histórico de
   texto solto em registros estruturados, com classificação automática por
   palavras-chave.
3. Ajustes finos de UX: fontes maiores, ordenação de colunas, botões
   "Aplicar" para todo filtro de período (evitando recomputar tudo a cada
   clique no calendário), correção de tendência/previsão nos gráficos,
   granularidade adaptativa do eixo de tempo, correção da legenda do Pareto.
4. Correções de bugs: fórmula de previsão, formatação de listas
   multi-linha no resumo da Daily, widget do Streamlit que travava valor
   antigo por causa de `key` + `value` conflitantes.
5. Planejamento de hospedagem gratuita: avaliação de duas rotas (Streamlit
   Community Cloud vs. Oracle Cloud).
6. Migração do banco de dados local (SQLite) para um banco persistente na
   nuvem (Turso), para não perder dados no ambiente efêmero do Streamlit
   Cloud.
7. Implementação de tela de login própria, já que a conta gratuita do
   Streamlit Cloud tinha a cota de apps privados esgotada.
8. Deploy final no Streamlit Community Cloud, conectado ao repositório
   GitHub, com Secrets configurados manualmente (Turso + credenciais de
   login).
9. Validação do login em produção e ajuste dos Secrets até o acesso
   funcionar corretamente.
