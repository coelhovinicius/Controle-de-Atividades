# Task Tracker — Mudanças de Segurança, Gráficos e Responsividade

Este documento explica, um por um, os pontos que você pediu. Os arquivos já
vêm prontos — leia, teste localmente e só depois suba pro Streamlit Cloud.

---

## 1. Chave do Turso "fora do front" + obrigatória antes de continuar

### O diagnóstico
A chave do Turso **nunca esteve exposta no front** (não tem `st.secrets` sendo
impresso na tela, nem valor hardcoded no código — conferi `app.py` e
`database_core.py` inteiros). O ponto real era outro, e mais sério na
prática: se o Turso falhasse por qualquer motivo (token expirado, banco
fora do ar, variável não configurada), o app **continuava rodando** —
silenciosamente caindo para um SQLite local — e só mostrava um aviso
amarelo. No Streamlit Community Cloud, o disco é **efêmero**: qualquer
atividade registrada nesse banco "de emergência" é perdida no próximo
redeploy/sleep. Ou seja, o risco não era vazamento, era **perda de dados
sem ninguém perceber a tempo**.

### A solução implementada
- Nova chave de configuração: `REQUIRE_TURSO` (em Secrets, igual a
  `TURSO_DATABASE_URL`).
- Com `REQUIRE_TURSO = "true"`: se o Turso não conectar, o app **para
  completamente** (`st.stop()`) com uma mensagem de erro clara — **antes
  até da tela de login aparecer**. Ninguém consegue usar o app (nem
  logar) enquanto o banco persistente não estiver de pé.
- Sem essa chave (ou `"false"`): comportamento de sempre — cai pro SQLite
  local, com aviso. Isso é o que você quer **rodando local**, sem precisar
  ter Turso configurado o tempo todo.

### O que mudou no código
- `database_core.py`: nova exceção `TursoRequiredError` + variável
  `REQUIRE_TURSO`. `DatabaseConnection.get_connection()` agora levanta essa
  exceção (em vez de só logar e cair pro SQLite) quando `REQUIRE_TURSO` está
  ativo e a conexão falha.
- `app.py`: a inicialização do repositório (`get_repository()`) foi movida
  para o **topo do arquivo**, logo após `st.set_page_config`, e agora está
  num `try/except` que captura `TursoRequiredError` e chama `st.stop()`.

### Como aplicar
1. Nos Secrets do Streamlit Cloud (Settings → Secrets), adicione:
   ```toml
   REQUIRE_TURSO = "true"
   ```
2. Redeploy. Se `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` estiverem certos,
   nada muda visualmente — o app roda normal.
3. Para **testar** que a trava funciona: troque temporariamente
   `TURSO_AUTH_TOKEN` nos Secrets por um valor inválido (ex.: adicione um
   caractere) e recarregue o app — você deve ver a tela de erro vermelha
   travando o app, em vez do aviso amarelo de antes. Depois desfaça a
   alteração.

---

## 2. Credenciais de login "fora do front" (hash em vez de texto puro)

### O diagnóstico
`secrets.toml` guardava a senha em **texto puro** dentro de
`[credentials]` (ex.: `"coelhovinicius" = "senha123"`). De novo: isso não
aparece pro usuário final do app (o front), mas é uma prática frágil —
qualquer pessoa com acesso de leitura aos Secrets (um segundo administrador
do workspace, um backup do painel, um print de tela por engano) vê a senha
real, não só "se você tem acesso ou não".

### A solução implementada
- `[credentials]` agora guarda **hashes bcrypt**, não senhas.
- Criei `gerar_hash_senha.py`: você roda localmente, ele pede a senha (sem
  mostrar na tela) e imprime a linha pronta pra colar no `secrets.toml`.
- `_validar_login()` em `app.py` agora compara com `bcrypt.checkpw(...)` em
  vez de `hmac.compare_digest(...)` sobre texto puro — mantendo a mesma
  proteção contra timing attack de antes (comparação contra um hash "morto"
  quando o usuário não existe).

### Como aplicar
1. Instale a dependência nova: `pip install -r requirements.txt` (adicionei
   `bcrypt` no arquivo).
2. Para cada usuário, rode:
   ```bash
   python gerar_hash_senha.py
   ```
   Cole a senha quando pedir. Ele imprime algo como:
   ```
   "seu_usuario" = "$2b$12$J58sZ/ue9riBesvh6SQ8J.fiFIhAGT5Qc6rbM204cEDPoW7aq4Xzq"
   ```
3. Cole essa linha em `[credentials]` no `secrets.toml` (local e/ou Streamlit
   Cloud), **substituindo** a senha em texto puro que estava lá.
4. Teste o login normalmente — a senha que você digita na tela continua
   sendo a senha de sempre; só o que fica salvo mudou.

> **Atenção**: depois de trocar para hash, se você (ou o convidado) esquecer
> a senha, não tem como "recuperar" o hash de volta pra senha — é assim que
> hash funciona (de propósito). Basta gerar um hash novo com
> `gerar_hash_senha.py` e substituir a linha no secrets.

---

## 3. Gráfico de Evolução — explicação completa

### O que ele mostra, hoje
O gráfico **"Evolução Temporal do Esforço"** tem duas partes sobrepostas:

1. **Uma linha colorida por projeto** — soma de horas registradas naquele
   projeto, por período (dia, semana ou mês — a granularidade muda sozinha
   conforme o tamanho do intervalo filtrado, pra não poluir o eixo X).
2. **Uma linha branca pontilhada** — uma **regressão linear simples**
   (`numpy.polyfit`, grau 1) sobre o **total** de horas (soma de todos os
   projetos) por período. Essa reta é desenhada sobre os dados reais e
   depois **estendida 3 períodos à frente** (depois da linha vertical
   cinza tracejada) como "previsão".

### Como interpretar
- A inclinação da linha branca é a leitura mais direta: **subindo** =
  esforço total crescendo ao longo do tempo; **descendo** = diminuindo;
  **quase horizontal** = estável.
- As linhas coloridas mostram **onde** esse esforço está concentrado -
  ex.: será que o aumento de horas é geral, ou só um projeto específico
  (tipo "Sustentação") está consumindo mais tempo?
- A parte depois da linha vertical cinza é só a **mesma reta continuada**,
  não um modelo que "aprendeu" padrões de sazonalidade, dias da semana,
  feriados, etc.

### É realmente relevante?
**Parcialmente — com ressalvas importantes que valem saber:**

✅ **Pontos fortes:**
- Rápido de ler: "esforço subindo ou descendo" é uma pergunta legítima
  para retro/1:1 com gestor.
- A granularidade adaptativa (dia/semana/mês) é uma boa decisão de design —
  evita eixo ilegível em ranges longos.
- Combinar com o gráfico de barras ao lado (mesma informação, mas sem a
  linha de tendência) dá as duas leituras: volume absoluto + direção.

⚠️ **Limitações que reduzem a confiabilidade da "previsão":**
- **Regressão linear com poucos pontos é frágil.** Se você filtrar, por
  exemplo, só 4-5 semanas, a reta pode ser fortemente distorcida por uma
  única semana atípica (época de férias, projeto grande pontual, etc.).
- **Não captura sazonalidade** — ex.: se toda sexta-feira você registra
  menos horas, ou se há um padrão mensal (fechamento de sprint), a reta
  linear ignora isso completamente.
- **Extrapolação linear tende a "viajar"** em horizontes maiores — 3
  períodos à frente ainda é razoável, mas o método não tem qualquer
  intervalo de confiança/erro associado, então a linha passa uma falsa
  sensação de precisão.
- **Efeito prático**: para uma pessoa (não uma equipe/processo
  industrial), "prever" horas de trabalho com regressão linear tem valor
  mais **narrativo** ("estou acelerando ou desacelerando?") do que
  **preditivo de verdade**.

**Minha recomendação**: manter o gráfico (o valor narrativo é real e
barato), mas deixar claro na interface que é uma tendência simples, não uma
previsão robusta — **já fiz isso**: renomeei a legenda da linha para
"Tendência linear (projeção simples)" e adicionei um expander **"ℹ️ Como
ler este gráfico"** logo abaixo do gráfico, com a mesma explicação em
linguagem simples, direto pra você (ou qualquer outra pessoa que use o
app) ver sem precisar perguntar.

---

## 4. Outras melhorias nos gráficos (implementadas)

- **KPIs no topo do Dashboard** (antes de qualquer gráfico): Total de
  Horas, nº de Registros, Média de Horas/Dia (considerando só dias com
  registro), % Impedimentos, % Dúvidas. Leitura instantânea do período
  sem precisar interpretar gráfico nenhum.
- **Gráfico novo: "Impedimentos e Dúvidas ao Longo do Tempo"** — nenhum
  gráfico anterior mostrava a **evolução** de bloqueios/dúvidas, só o
  volume total (via Pareto/pizza). Esse gráfico novo (barras empilhadas,
  mesma granularidade dos outros) responde uma pergunta bem prática pra
  quem faz Daily/Retro: *os impedimentos estão aumentando, diminuindo, ou
  concentrados em algum período específico?* — útil pra apontar, por
  exemplo, uma sprint ou projeto com atrito recorrente.

## 5. Ideias de gráfico para uma próxima rodada (não implementadas ainda)

Não implementei estas agora pra não inflar demais esta entrega, mas valem
para uma próxima iteração, se fizer sentido pra você:

- **Heatmap de dia da semana** (linhas = dia da semana, colunas = semana do
  ano, cor = horas): bom pra enxergar padrões de rotina (ex.: você
  trabalha mais às terças?) — mais rico que a linha de tendência para
  achar padrão real, mas exige mais dados acumulados pra fazer sentido.
- **Horas acumuladas no período** (linha única, soma cumulativa): mais
  "estável" visualmente que o gráfico de barras/linha por período, bom
  pra ver ritmo constante vs. picos e vales.
- **Comparação período-a-período** (ex.: esta semana vs. semana anterior,
  lado a lado): útil se você for revisar o Dashboard toda semana.

Se quiser, eu implemento qualquer um desses na próxima rodada — é só
pedir.

---

## 6. Responsividade (telas grandes e mobile)

### O problema
- **Telas muito grandes** (monitor ultrawide, 4K): o `layout="wide"` do
  Streamlit estica o conteúdo de ponta a ponta — em telas muito largas,
  isso deixa gráficos/tabelas finos e esticados, difíceis de ler.
- **Mobile**: o CSS anterior fixava o texto das abas em `1.5rem` pra
  qualquer tela — ótimo em desktop, mas em celular ocupa a tela toda e
  força quebra de linha feia.

### A solução implementada (CSS puro, sem JavaScript de detecção de
dispositivo)
1. **Teto de largura centralizado** (`max-width: 1600px` em
   `.main .block-container`) — em monitores grandes, sobra respiro nas
   laterais em vez de esticar; em desktops/tablets comuns (a maioria),
   nada muda, porque a tela já é menor que esse teto.
2. **Media query para tablets** (`max-width: 992px`): só ajusta padding
   lateral.
3. **Media query para celular** (`max-width: 640px`): reduz fonte das
   abas, dos títulos (`h1`/`h2`/`h3`) e o padding geral; botões ficam com
   um pouco mais de altura de toque.
4. **Gráficos Plotly**: criei `apply_responsive_layout()`, aplicada em
   todos os 5 gráficos do Dashboard — fonte menor e mais neutra, legenda
   **horizontal embaixo** do gráfico (em vez de do lado, que rouba largura
   útil em tela estreita), margens enxutas, e rótulos do eixo X
   **inclinados automaticamente** quando há mais de 8 categorias (evita
   texto sobreposto).

### Por que não detectar o dispositivo via JavaScript
Daria um resultado mais "exato" (ex.: esconder colunas em telas < 400px),
mas exigiria um componente JS customizado rodando dentro do Streamlit —
mais uma peça pra manter, com mais chance de quebrar em atualizações do
Streamlit. A solução por CSS puro cobre os dois problemas relatados (muito
grande / muito pequeno) com um risco de manutenção bem menor.

### Como testar
- Redimensione a janela do navegador (ou abra o DevTools → modo
  responsivo) e confira: abas/títulos menores abaixo de ~640px de largura;
  conteúdo centralizado com respiro nas laterais acima de ~1600px.
- No celular de verdade, os gráficos devem caber na tela sem precisar
  fazer zoom pra ler os eixos.

---

## 7. Sobre n8n

Nada do que foi pedido aqui (Turso, login, gráficos, responsividade) exige
mudança no n8n — tudo ficou dentro do próprio Streamlit. Se em algum
momento você quiser expandir os workflows de IA (ex.: gerar automaticamente
um resumo semanal, ou análises mais elaboradas via LLM), me chama que eu
desenho o workflow do n8n prontinho pra você importar.

---

## 8. Arquivos entregues

| Arquivo | O que mudou |
|---|---|
| `app.py` | Login com bcrypt, trava de Turso obrigatório, CSS responsivo, KPIs, gráfico novo, explicação inline do gráfico de evolução |
| `database_core.py` | `REQUIRE_TURSO` + exceção `TursoRequiredError` |
| `requirements.txt` | Adicionado `bcrypt` |
| `gerar_hash_senha.py` | **Novo** — gera hash bcrypt pra colar no secrets |
| `secrets.toml.example` | Atualizado — mostra `REQUIRE_TURSO` e hash em vez de senha em texto puro |

**Não precisou mexer em**: `importer_core.py`, `import_history.py`,
`fix_legacy_descriptions.py`, `fix_project_reclassification.py`,
`backfill_username.py`, `migrate_to_turso.py`, `config.toml`,
`.gitignore` — nenhum deles tinha relação com os pontos pedidos.

## 9. Checklist de deploy

1. `pip install -r requirements.txt` (local, pra testar antes).
2. Gerar hash de cada senha com `gerar_hash_senha.py` e atualizar
   `secrets.toml` local.
3. Testar local: `streamlit run app.py` — confirmar login e navegação
   normal.
4. Nos Secrets do Streamlit Cloud: atualizar `[credentials]` com os hashes
   e adicionar `REQUIRE_TURSO = "true"`.
5. `git add app.py database_core.py requirements.txt gerar_hash_senha.py secrets.toml.example`
   `git commit` (com corpo detalhado, como você prefere) `&& git push`.
6. Depois do redeploy, testar login em produção normalmente.
