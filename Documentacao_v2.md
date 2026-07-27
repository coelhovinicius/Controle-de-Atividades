# Task Tracker — Documentação Técnica

> Aplicativo pessoal de controle diário de atividades (QA/Testes), com login por usuário, dashboard de indicadores, geração de resumo para Daily Scrum e sincronização de histórico via upload de arquivo.

---

## 1. Visão Geral

O **Task Tracker** é uma aplicação web feita em **Python + Streamlit**, que permite registrar, editar, visualizar e analisar atividades de trabalho do dia a dia (projeto, categoria, descrição, horas de esforço, impedimentos e dúvidas). O banco de dados é multi-usuário: cada pessoa logada só vê e edita os próprios registros.

**Principais funcionalidades:**
- Login por usuário/senha, com sessão persistente (sobrevive a F5)
- CRUD completo de registros de atividade, com confirmação e indicador de "processando" em toda escrita no banco
- Gestão de Projetos/Categorias customizados (criar, renomear, excluir), inclusive "on the fly" direto no formulário de registro
- Geração de resumo para a Daily Scrum (texto corrido editável, com download em `.txt`)
- Dashboard com gráficos (distribuição por projeto/categoria, tendência no tempo, análise de Pareto) e exportação (`.csv`/`.txt`)
- Sincronização de histórico via upload de arquivo (`.txt` de log manual ou `.csv` estruturado), com comparação linha a linha antes de aplicar

---

## 2. Stack Tecnológica

| Camada | Tecnologia |
|---|---|
| Linguagem | Python 3 |
| Interface web | [Streamlit](https://streamlit.io) |
| Gráficos | Plotly (`plotly.express`, `plotly.graph_objects`) |
| Manipulação de dados | Pandas, NumPy |
| Banco de dados (produção) | **Turso** (libSQL — fork do SQLite com replicação e acesso via rede) |
| Banco de dados (fallback local) | SQLite puro (`sqlite3`, biblioteca padrão do Python) |
| Driver de acesso ao Turso | pacote `libsql` (Python) |
| Hospedagem do app | **Streamlit Community Cloud** |
| Hospedagem do banco | **Turso Cloud** (região `aws-us-east-1`) |
| Autenticação do app | Usuário/senha próprios, definidos em Secrets (não usa OAuth/login social) |
| Controle de versão | Git (repositório local sincronizado com o deploy do Streamlit Cloud) |

### 2.1 Dependências (`requirements.txt`)

```
streamlit==1.59.0
pandas>=2.2
numpy>=1.26
plotly>=5.24
libsql>=0.1.0
```

`streamlit` está fixado em versão exata (evita que uma atualização automática no Community Cloud mude o comportamento do app sem aviso); as demais usam piso mínimo (`>=`).

---

## 3. Arquitetura e Arquivos

```
├── app.py                          # Aplicação Streamlit (única tela, 4 abas)
├── database_core.py                # Camada de acesso a dados (Turso / SQLite)
├── importer_core.py                # Parser de histórico (.txt e .csv) — motor reutilizável
├── import_history.py               # Script de linha de comando (importação em lote única)
├── fix_legacy_descriptions.py      # Script de correção pontual (bug de bullet residual)
├── fix_project_reclassification.py # Script de correção pontual (reclassificação de projetos)
├── backfill_username.py            # Script de migração (atribuir dono a registros pré-login)
├── requirements.txt                # Dependências Python
├── .streamlit/
│   ├── config.toml                 # Configuração do servidor Streamlit (limite de upload)
│   └── secrets.toml                # Credenciais (local, NÃO versionado)
└── personal_tracker.db             # Banco SQLite local (apenas fallback/desenvolvimento)
```

### 3.1 `database_core.py` — Camada de dados

Duas classes:

- **`DatabaseConnection`**: decide para onde conectar.
  - Se as variáveis de ambiente/secrets `TURSO_DATABASE_URL` e `TURSO_AUTH_TOKEN` existirem, conecta no Turso via `libsql.connect(...)` — e **valida a conexão na hora** com um `SELECT 1` (o `libsql` não valida credenciais no `connect()`, só na primeira consulta; sem esse teste, uma falha de autenticação derrubava o app inteiro de forma não tratada).
  - Se a conexão com o Turso falhar por qualquer motivo (token inválido, URL errada, driver não instalado), cai automaticamente para **SQLite local** (`personal_tracker.db`), registrando o motivo no log do servidor.
  - Expõe `self.using_turso` (bool), que o `app.py` usa para mostrar um aviso visível na tela se estiver rodando no banco local por engano (importante em produção, onde o disco é efêmero).

- **`LogRepository`**: todas as operações de banco (CRUD de registros, CRUD de opções customizadas de Projeto/Categoria). Cria/migra o schema automaticamente na inicialização (`CREATE TABLE IF NOT EXISTS` + verificação de colunas via `PRAGMA table_info`, para bancos antigos ganharem as colunas novas sem perder dados).

**Tabelas:**

`work_logs`
| Coluna | Tipo | Observação |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `log_date` | DATE | formato ISO `YYYY-MM-DD` |
| `project` | TEXT | |
| `category` | TEXT | |
| `description` | TEXT | |
| `effort_hours` | REAL | |
| `created_at` | TIMESTAMP | default `CURRENT_TIMESTAMP` |
| `is_impedimento` | INTEGER (0/1) | adicionada via migração |
| `is_duvida` | INTEGER (0/1) | adicionada via migração |
| `username` | TEXT | adicionada via migração — dono do registro |

`custom_options`
| Coluna | Tipo | Observação |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `option_type` | TEXT | `'project'` ou `'category'` (CHECK constraint) |
| `value` | TEXT | |
| `username` | TEXT | dono da opção customizada |
| `created_at` | TIMESTAMP | |
| — | UNIQUE(`option_type`, `value`) | |

> Todas as consultas/gravações filtram por `username`, como camada de isolamento entre usuários (defesa em profundidade: mesmo um bug de tela não deixaria um usuário mexer no registro de outro).

### 3.2 `app.py` — Aplicação

Ponto de entrada único. Estrutura, de cima para baixo:

1. **Login** (`_tela_login`, `_validar_login`, `_criar_sessao`/`_validar_sessao`) — ver seção 5.
2. **Conexão com o banco** (`get_repository()`, cacheada com `@st.cache_resource` — uma única conexão reaproveitada entre reruns do processo).
3. **Infraestrutura de confirmação + "processando"** (`request_confirmation`, `render_pending_confirmation`, `execute_processing_action`, `render_processing_overlay`) — um padrão genérico usado em toda escrita no banco (criar/renomear/excluir Projeto ou Categoria; salvar/editar/excluir registro de atividade):
   - Toda ação de escrita primeiro abre um **modal de confirmação** (`st.dialog`).
   - Ao confirmar, a tela mostra um **overlay de "Processando..."** que ocupa a tela inteira — nada mais é desenhado nesse rerun, o que impede qualquer interação até a operação terminar.
   - Ao concluir, mostra uma notificação (`st.toast`) de sucesso ou aviso de falha (ex.: nome duplicado).
4. **4 abas principais** (`st.tabs`):
   - **Registro de Atividades** — grade de registros do usuário logado, com Novo/Editar/Excluir (cada um com confirmação); os campos Projeto/Categoria usam um seletor "criável" (`creatable_option_picker`): ao escolher "➕ Criar novo...", aparece um campo de texto que já cria e usa a opção nova, com confirmação, sem sair do formulário.
   - **Daily Scrum** — monta um resumo automático ("o que fiz ontem" / "o que farei hoje", a partir dos registros do período escolhido), com Impedimentos/Dúvidas sugeridos automaticamente (e editáveis) a partir dos registros marcados como tal. O **texto corrido** ("Ver texto corrido, para copiar e colar") é editável: por padrão fica somente leitura, um botão "✏️ Editar texto" libera edição, e só é possível **salvar** (obrigatório, não aceita ficar em branco) ou **cancelar** — enquanto em edição, os demais botões da aba (inclusive o download) ficam bloqueados, e o arquivo baixado (`daily_AAAAMMDD.txt`) sempre reflete a última versão salva desse texto.
   - **Dashboard & Relatórios** — filtro de período; tabela de registros; gráficos (barras de horas por projeto, pizza por categoria, linha de tendência, Pareto de esforço); exportação em `.csv` e `.txt`.
   - **Sincronização de Arquivo** — upload de `.txt` (log manual, formato de texto corrido com datas) ou `.csv` estruturado; o app compara com o que já existe no banco (via `merge` do Pandas, identificando o que é novo e o que seria removido) e deixa o usuário escolher, registro a registro, o que aplicar — a operação final exige digitar o nome do arquivo como confirmação.
5. **Painel de gestão de Projetos/Categorias** (barra lateral) — dropdown com todos os nomes cadastrados (padrão do sistema + customizados); para os customizados, permite renomear (com atualização em cascata dos registros que já usavam o nome antigo) e excluir; tudo com confirmação e overlay de processamento.

### 3.3 `importer_core.py` — Motor de importação/parsing

Classe `HistoryParser`, usada tanto pela tela de Sincronização quanto pelo script de linha de comando `import_history.py`. Duas entradas possíveis:

- **`.txt`** (log manual): varredura linha a linha, identificando datas (`dd/mm/aaaa`) e agrupando as tarefas sob cada data.
- **`.csv`** estruturado: aceita tanto o padrão pt-BR (`;` como separador, `,` como decimal) quanto o padrão US (`,`/`.`), com datas em `dd/mm/aaaa` ou ISO.

Para cada item de texto, infere automaticamente:
- **Projeto** e **Categoria**, por palavras-chave (dicionários `PROJECT_KEYWORDS` / `CATEGORY_KEYWORDS`)
- **Impedimento** e **Dúvida**, por palavras-chave (`IMPEDIMENT_KEYWORDS` / `QUESTION_KEYWORDS`)

> Essas listas de palavras-chave são heurísticas — ajustadas ao longo do tempo conforme o vocabulário real do histórico do usuário (ex.: adição de "Novos Planos", "Gameficação", "RH Summit" etc. como projetos próprios, e expressões como "aguardando autorização" como sinal de impedimento).

### 3.4 Scripts avulsos (rodar manualmente, uma vez)

| Script | Função |
|---|---|
| `import_history.py` | Importa `raw_history.txt` inteiro para o banco, via linha de comando |
| `fix_legacy_descriptions.py` | Remove "- " residual no início de descrições importadas antes da correção do parser |
| `fix_project_reclassification.py` | Reclassifica retroativamente registros que caíram em projetos "guarda-chuva" antigos (ex.: "Job Boards" → "Vagas") |
| `backfill_username.py` | Atribui um `username` dono aos registros gravados antes da tela de login existir (sem isso, ficariam invisíveis para todo mundo, já que toda consulta agora filtra por usuário) |

Todos os scripts (exceto os dois `fix_*`, que ainda operam só em SQLite local) usam `DatabaseConnection`/`LogRepository`, então também funcionam contra o Turso caso as variáveis de ambiente estejam definidas na sessão do terminal.

---

## 4. Banco de Dados: Turso

- **Provedor**: [Turso](https://turso.tech) — banco de dados distribuído baseado em **libSQL** (fork open-source do SQLite, com acesso via rede/HTTP).
- **Motivo da escolha**: o Streamlit Community Cloud tem disco **efêmero** (é apagado a cada redeploy/"sleep" do app) — um SQLite puro local perderia os dados a cada atualização do app. O Turso resolve isso mantendo o banco persistente na nuvem.
- **Modo de conexão**: **remoto simples** (sem réplica local/embedded replica) — cada operação vai direto para o Turso via rede. Escolhido por simplicidade: no volume de uso deste app (pessoal, poucos registros por dia), a latência extra de rede é irrelevante, e evita a complexidade de gerenciar sincronização de uma réplica local num ambiente de disco efêmero.
- **Instalação/gestão do banco**: feita via **Turso CLI**, rodando dentro do **WSL** (o CLI não tem instalação nativa para Windows — só Mac, Linux e WSL).

### 4.1 Credenciais necessárias

| Variável | Onde é usada | Como gerar |
|---|---|---|
| `TURSO_DATABASE_URL` | `database_core.py` | `turso db show <nome-do-banco> --url` (formato `libsql://...`) |
| `TURSO_AUTH_TOKEN` | `database_core.py` | `turso db tokens create <nome-do-banco>` — **token específico do banco**, não confundir com o token de sessão/login do CLI (`turso auth login`), que é para uso pessoal do CLI, não para o app se conectar |

Essas duas chaves ficam nos **Secrets** do Streamlit Cloud (Settings → Secrets), no nível raiz do TOML:
```toml
TURSO_DATABASE_URL = "libsql://SEU-BANCO-SEU-USUARIO.aws-us-east-1.turso.io"
TURSO_AUTH_TOKEN = "eyJ..."
```
Localmente, o mesmo `database_core.py` cai automaticamente para SQLite se essas variáveis não existirem — não é obrigatório ter o Turso configurado para desenvolver localmente.

---

## 5. Autenticação / Login

O Streamlit Community Cloud, no plano gratuito, só permite 1 app privado por conta (cota já usada por outro app do autor). Por isso, o controle de acesso **não** usa o mecanismo nativo do Streamlit (convite por e-mail) — o app fica público no Streamlit, e a autenticação de verdade é feita dentro do próprio `app.py`:

- Usuário/senha configurados em `[credentials]` nos Secrets:
  ```toml
  [credentials]
  "usuario1" = "senha1"
  "usuario2" = "senha2"
  ```
- Comparação de senha via `hmac.compare_digest` (evita timing attack).
- **Sessão**: um dicionário em memória do processo (`_ACTIVE_SESSIONS`, fora do `st.session_state`) guarda `token aleatório → {usuário, validade}`. O token vai para a URL (`?s=...`), o que permite sobreviver a um F5 sem pedir login de novo — válido por 12h (`SESSION_TTL_HOURS`). Ao clicar em "Sair", o token é revogado e todo o `st.session_state` é limpo (evita resíduo de dados de um usuário para o próximo, no mesmo navegador/máquina).
- **Isolamento de dados**: nada do app roda (nem a conexão com o banco) antes do login ser validado; e cada usuário só vê/edita os próprios registros (filtro por `username` em toda query).

> **Limitação conhecida**: como as sessões vivem em memória do processo (não em banco), um redeploy do Streamlit Cloud derruba todas as sessões ativas (todos precisam logar de novo) — isso é esperado e não afeta os dados salvos.

---

## 6. Hospedagem e Deploy

- **App**: [Streamlit Community Cloud](https://streamlit.io/cloud), conectado ao repositório Git do projeto — cada push atualiza o deploy automaticamente.
- **Configuração do servidor** (`.streamlit/config.toml`):
  ```toml
  [server]
  maxUploadSize = 20
  ```
  Limite de upload (usado na aba de Sincronização) de 20MB — reforçado também no código (`MAX_UPLOAD_SIZE_MB` em `app.py`), como segunda camada de segurança caso esse arquivo não seja carregado no ambiente de deploy.
- **Secrets**: gerenciados em Settings → Secrets do próprio Streamlit Cloud (não versionados no Git). Localmente, o equivalente é `.streamlit/secrets.toml` (também fora do controle de versão).
- **Banco**: Turso Cloud, região `aws-us-east-1`, independente do ciclo de vida do app — sobrevive a redeploys.

---

## 7. Ambiente de Desenvolvimento

- **Sistema operacional do autor**: Windows, com **WSL2 (Ubuntu)** instalado especificamente para rodar o Turso CLI (que não tem build nativo para Windows).
- Requisitos para configurar o ambiente do zero:
  1. WSL2 + Ubuntu (`wsl --install`, via PowerShell como administrador)
  2. Turso CLI, instalado **dentro** do WSL (`curl -sSfL https://get.tur.so/install.sh | bash`)
  3. Um "abridor de link" (`xdg-open`) configurado no WSL para o `turso auth login` conseguir abrir o navegador do Windows automaticamente — resolvido criando um script em `/usr/local/bin/xdg-open` que chama `explorer.exe` do Windows
  4. Python 3 + `pip` + `libsql` instalados no WSL, para testar a conexão com o Turso isoladamente antes de mexer no Streamlit Cloud
- **Git Bash / MINGW64**: usado no dia a dia para comandos gerais do projeto, mas **não** consegue rodar o instalador do Turso CLI (o script de instalação não reconhece o MINGW como sistema operacional suportado) — daí a necessidade do WSL à parte, só para esse CLI.

---

## 8. Decisões de Design (Resumo)

| Decisão | Motivo |
|---|---|
| Turso em vez de SQLite puro em produção | Disco efêmero do Streamlit Community Cloud apagaria os dados a cada redeploy |
| Conexão remota simples (sem réplica local) | Simplicidade > latência marginal, dado o baixo volume de uso do app |
| Fallback automático para SQLite local | Permite desenvolver/testar offline sem depender do Turso nem gastar cota |
| Teste de conexão (`SELECT 1`) logo após `connect()` | O driver `libsql` só valida credenciais na primeira query real, embrulhando qualquer erro (inclusive de autenticação) como `ValueError` genérico — sem esse teste, um token inválido derrubava o app inteiro sem tratamento |
| Login próprio (usuário/senha em Secrets), em vez do controle de acesso nativo do Streamlit | Cota gratuita do Streamlit Cloud permite só 1 app privado por conta, já usada |
| Sessão via token na URL + dicionário em memória | Sobrevive a F5 sem exigir login de novo, sem precisar de uma tabela de sessões no banco |
| Todo Projeto/Categoria customizado grava em tabela própria (`custom_options`), separada de `work_logs` | Sobrevive à sincronização por upload de `.txt`/`.csv`, que só insere/remove linhas em `work_logs` |
| Confirmação + overlay de "processando" em toda escrita no banco | Evita exclusões/edições acidentais e deixa claro quando uma operação está em andamento, bloqueando interação até terminar |
| Texto corrido da Daily com modo de edição explícito (editar → salvar/cancelar) | O texto gerado automaticamente às vezes precisa de ajuste manual antes de copiar/baixar, sem perder a rastreabilidade de que foi editado |

---

## 9. Glossário Rápido

- **libSQL**: fork open-source do SQLite, mantido pela Turso, compatível com o dialeto SQL do SQLite, mas com suporte a replicação e acesso via rede/HTTP.
- **Turso**: serviço de hospedagem de bancos libSQL na nuvem.
- **Streamlit Community Cloud**: hospedagem gratuita de apps Streamlit, com disco efêmero e reinstalação de dependências a cada deploy.
- **Secrets**: mecanismo do Streamlit para armazenar credenciais fora do código-fonte (arquivo local `.streamlit/secrets.toml`, ou editor equivalente no Community Cloud).
- **Réplica embutida (embedded replica)**: modo de conexão do libSQL em que existe uma cópia local do banco, sincronizada periodicamente com a nuvem — **não** é o modo usado neste projeto (optou-se por conexão remota simples).
