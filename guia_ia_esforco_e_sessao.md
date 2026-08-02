# Estimativa de Esforço por IA + Sessão Persistente — Guia Completo

Este documento explica as mudanças novas: estimativa de duração de tarefas
por IA (via n8n), classificação dinâmica de Projeto/Categoria, dias de
OVERTIME/plantão, e sessão que sobrevive a F5 (e opcionalmente a restarts).

---

## 1. Visão geral do que foi implementado

| Pedido | Como foi resolvido |
|---|---|
| Não achar que 15 tarefas = 15 horas | IA estima duração REAL de cada tarefa; depois normaliza o total do dia pra ~8h (dias comuns) |
| Atribuição lógica projeto/categoria | IA classifica contra os projetos/categorias que **já existem no seu banco**, sem lista fixa no código |
| Sem chave paga da Anthropic | Cascata **Gemini → Groq → Mistral** (nessa ordem) -- todos com camada gratuita, sem cartão de crédito; só cai pro próximo se o anterior falhar |
| OVERTIME / plantão | Detecção por palavra-chave (não IA) marca o **dia inteiro** como exceção -- nesses dias, sem normalização, soma crua da IA |
| Reunião/Daily ~20min, 1x/semana mais longa | Não virou regra fixa -- é uma calibração no prompt da IA ("reuniões rápidas tendem a ser curtas"), então a IA julga cada descrição |
| Novo projeto/categoria detectado → cadastrar sozinho | `repo.add_custom_option()` chamado automaticamente quando a IA sinaliza `is_new_project`/`is_new_category` |
| "Produto e Tecnologia" não pode sumir/duplicar | Resolvido estruturalmente: a IA usa `get_project_options()` (base + banco) como lista de referência -- nunca precisa reescrever nem duplicar nada |
| n8n pronto pra importar | `n8n_workflow_estimativa_ia.json` |
| Ephemeral / qualquer empresa | Nenhuma lista de projeto/categoria nova ficou hardcoded -- tudo vem do banco em tempo real |
| F5 mantém logado | Já funcionava dentro do mesmo processo; token virou assinado (HMAC), então também sobrevive a restart do servidor se você configurar `SESSION_SECRET_KEY` |

**Sobre a cascata**: ela troca de provedor quando a chamada HTTP falha de
verdade (credencial errada, rate limit, servidor fora do ar, timeout). Se
um provedor responder 200 OK mas devolver um conteúdo que não dá pra
interpretar como JSON válido, isso conta como uma falha "de conteúdo", não
"de conexão" -- nesse caso específico (raro, mas possível) o workflow
devolve o erro direto em vez de tentar o próximo provedor. Se isso virar um
problema no seu uso real, me avisa que eu estendo a cascata pra cobrir
esse caso também.

---

## 2. Como montar o workflow no n8n (cascata Gemini → Groq → Mistral)

O workflow tenta os provedores **nessa ordem**: se o Gemini falhar (rate
limit, credencial ausente, fora do ar), tenta o Groq; se esse também
falhar, tenta o Mistral; se os 3 falharem, devolve um erro claro (e o app
cai pro modo sem IA, como sempre).

### 2.1. Importar

1. Abra seu n8n → **Import from File** → `n8n_workflow_estimativa_ia.json`.

### 2.2. Selecionar as credenciais (você já tem as 3 no n8n)

Como Gemini, Groq e Mistral já existem nas suas credenciais do n8n, não
precisa criar nada novo -- só abrir cada node e **selecionar a credencial
certa** no dropdown:

| Node | Tipo de credencial esperado | Selecione |
|---|---|---|
| **"1) Chamar Gemini"** | Header Auth (header `x-goog-api-key`) | sua credencial da Gemini |
| **"2) Chamar Groq"** | Bearer Token | sua credencial da Groq |
| **"3) Chamar Mistral"** | Bearer Token | sua credencial da Mistral |

Se alguma das suas credenciais existentes for de um **tipo diferente**
desses (ex.: só guarda a chave como texto solto, sem ser Header Auth/Bearer
Token de verdade), o dropdown do node não vai oferecer ela como opção --
nesse caso, me avisa o tipo que você já usa que eu ajusto o node pra
combinar, em vez de você recriar a credencial.

### 2.3. Confirmar que a cascata está ligada (passo importante)

Os 3 nodes de chamada (`1) Chamar Gemini`, `2) Chamar Groq`, `3) Chamar
Mistral`) precisam ter **duas saídas** (sucesso e erro) -- é isso que faz a
cascata funcionar. O JSON já vem configurado assim, mas **confira depois de
importar**: clique em cada um desses 3 nodes → aba **Settings** → campo
**"On Error"** → deve estar em **"Continue (using error output)"**. Se
aparecer só uma saída no node (em vez de duas, uma verde e uma vermelha),
ajuste esse campo manualmente.

### 2.4. Modelos usados (ajustáveis)

No node **"Configuracoes (AJUSTE OS MODELS AQUI)"**:
- `gemini_model` = `gemini-2.5-flash` (free tier)
- `groq_model` = `llama-3.3-70b-versatile` (free tier, sem cartão de crédito -- `llama-3.1-8b-instant` é uma opção mais rápida/leve se preferir)
- `mistral_model` = `mistral-small-latest` (tier gratuito "Experiment")

Esses free tiers mudam com frequência -- se algum parar de funcionar,
confira o painel de cada provedor pelo modelo/tier atual e atualize aqui.

### 2.5. Ativar e pegar a URL

1. Ative o workflow (toggle no canto superior direito).
2. Clique no node **Webhook** → copie a **Production URL** (não a "Test
   URL" -- essa só funciona enquanto o editor está aberto ouvindo um clique
   de "Listen for test event", e dá erro 404 fora disso).

### Testar o workflow isoladamente (antes de plugar no app)

```powershell
curl -X POST "https://SEU-DOMINIO.duckdns.org/webhook/estimar-esforco-ia" `
  -H "Content-Type: application/json" `
  -d '{\"known_projects\": [\"Passaporte\", \"360\"], \"known_categories\": [\"Reuniao\", \"Execucao de Testes\"], \"tasks\": [{\"id\": 0, \"description\": \"Daily Scrum - alinhamento de sprint\"}, {\"id\": 1, \"description\": \"Investigacao de bug no checkout do Passaporte\"}]}'
```

Resposta esperada (formato):
```json
{"results": [
  {"id": 0, "estimated_minutes": 20, "project": "Passaporte", "category": "Reuniao", "is_new_project": false, "is_new_category": false},
  {"id": 1, "estimated_minutes": 90, "project": "Passaporte", "category": "Resolucao/Testes de BUG/Problema", "is_new_project": false, "is_new_category": false}
]}
```

Se der erro, confira (nessa ordem): workflow **ativo**, URL usando
`/webhook/` (não `/webhook-test/`), as 3 credenciais configuradas, e a aba
**Executions** do n8n para ver em qual dos 3 provedores travou e por quê.

---

## 3. Configurar o app

No `secrets.toml` (local e/ou Streamlit Cloud), adicione:
```toml
N8N_AI_ESTIMATE_WEBHOOK_URL = "https://seu-n8n.duckdns.org/webhook/estimar-esforco-ia"
SESSION_SECRET_KEY = "gere com: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
```

Ambas são **opcionais** -- sem `N8N_AI_ESTIMATE_WEBHOOK_URL`, os recursos de
IA ficam desativados e tudo funciona como antes (esforço manual, botão "🤖
Sugerir com IA" desabilitado). Sem `SESSION_SECRET_KEY`, a sessão sobrevive
a F5 mas não a um restart do servidor.

Reinicie o app (`streamlit run app.py`) depois de editar o secrets.toml.

---

## 4. Como funciona, na prática

### Sincronização em massa (upload de .txt/.csv)

1. Você envia o arquivo e clica em "Analisar Arquivo Enviado", como sempre.
2. Se `N8N_AI_ESTIMATE_WEBHOOK_URL` estiver configurado, o app chama o
   workflow **uma vez, com todas as tarefas novas do arquivo de uma vez**
   (mesma lição da sincronização lenta de antes -- nada de round-trip por
   linha).
3. A IA devolve, para cada tarefa: duração estimada, projeto, categoria, e
   se algum desses dois é "novo" (não existia antes).
4. O app:
   - Sobrescreve `effort_hours`/`project`/`category` na tabela de revisão.
   - Detecta dias com "OVERTIME"/"plantão" na descrição (day inteiro).
   - Normaliza as horas de cada dia **comum** pra somar ~8h (mantendo a
     proporção relativa entre as tarefas); dias de plantão ficam com a
     soma crua da IA, sem teto.
   - Cadastra automaticamente qualquer projeto/categoria novo.
5. Você ainda revisa tudo na tabela antes de confirmar -- agora Projeto,
   Categoria e Horas também são **editáveis** ali (antes só dava pra
   marcar/desmarcar aplicar ou não).
6. Se o n8n estiver fora do ar ou responder errado, aparece um aviso e o
   app cai de volta pro comportamento antigo (esforço fixo de 1h +
   classificação por palavra-chave) -- nada trava.

### Formulário manual ("Novo Registro")

1. Escreva a descrição da tarefa.
2. Clique em **"🤖 Sugerir com IA"** (abaixo da descrição).
3. O campo "Esforço (Horas)" e os dropdowns de Projeto/Categoria são
   preenchidos com a sugestão -- você pode mudar qualquer um deles antes
   de salvar, normalmente.
4. **Não há normalização de 8h aqui** -- faz sentido só quando você já tem
   o dia inteiro de tarefas por perto (a sincronização em massa); no
   registro manual, é só a estimativa da IA para aquela tarefa isolada.

### Dias de OVERTIME / plantão

- Escreva `OVERTIME` (ou `plantão`/`plantao`, para registros antigos) em
  **qualquer** descrição daquele dia -- não precisa estar em toda tarefa,
  uma vez já marca o dia inteiro.
- Detecção por palavra-chave, não por IA -- é um marcador literal que você
  escreve, não precisa de julgamento.

---

## 5. Sessão persistente (F5)

- **Dentro do mesmo processo do servidor** (o caso normal do dia a dia):
  já funcionava, e continua funcionando -- F5 mantém logado.
- **Depois de um restart do servidor** (Ctrl+C + rodar de novo, ou um
  redeploy/sleep no Streamlit Cloud): só sobrevive se você configurar
  `SESSION_SECRET_KEY` nos Secrets (veja seção 3).
- **Fechar a aba/navegador**: tecnicamente, o token continua válido até
  você clicar em "Sair" ou até as 12h de validade passarem -- não existe
  um jeito confiável de "avisar o servidor" no exato instante que uma aba
  fecha. Na prática, para um uso pessoal, isso funciona exatamente como
  você quer: fechar e reabrir a mesma aba/link (ou um F5) mantém você
  logado; "Sair" desloga de propósito; depois de 12h, desloga sozinho.

---

## 6. Arquivos entregues

| Arquivo | O que é |
|---|---|
| `n8n_workflow_estimativa_ia.json` | Workflow pronto para importar no n8n |
| `app.py` | Integração da IA na Sincronização + formulário manual; sessão HMAC |
| `database_core.py` | (sem mudança nesta entrega) |
| `requirements.txt` | Adicionado `requests` explícito |
| `secrets.toml.example` | Atualizado com `SESSION_SECRET_KEY` e `N8N_AI_ESTIMATE_WEBHOOK_URL` |

---

## 7. Ideias para depois (não implementadas ainda)

- Um botão "Reestimar com IA" na tela de edição de um registro já salvo
  (hoje o recurso só existe na criação/sincronização).
- Guardar o "motivo"/raciocínio da IA junto do registro (hoje só o
  resultado final é salvo).
- Um modo "dry run" na Sincronização que mostra a estimativa da IA sem
  gastar chamada nenhuma no banco, só pra conferência.