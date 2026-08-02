# Guia de Deploy: Streamlit Community Cloud + Turso

Este guia coloca seu app no ar em `SEU-APP.streamlit.app`, com acesso restrito
por e-mail e dados persistidos com segurança no Turso (banco compatível com
SQLite, hospedado na nuvem, camada gratuita).

Sua VM da Oracle Cloud **não é necessária** para este caminho — o Community
Cloud cuida do servidor pra você. Se um dia quiser ter controle total (sem
depender de terceiros), a VM continua aí como opção B.

---

## 1. Criar conta e banco no Turso

1. Crie uma conta em https://turso.tech (tem opção de login com GitHub).
2. Instale o CLI do Turso na sua máquina:
   ```bash
   curl -sSfL https://get.tur.so/install.sh | bash
   ```
3. Faça login e crie o banco:
   ```bash
   turso auth login
   turso db create qa-tracker
   ```
4. Pegue a URL de conexão e gere um token:
   ```bash
   turso db show qa-tracker --url
   turso db tokens create qa-tracker
   ```
   Guarde os dois valores — você vai precisar deles no próximo passo.

---

## 2. Configurar os segredos localmente e testar

1. Copie o arquivo `.streamlit/secrets.toml.example` para
   `.streamlit/secrets.toml` (sem o `.example` no nome).
2. Preencha com os valores reais que você pegou no passo anterior:
   ```toml
   TURSO_DATABASE_URL = "libsql://qa-tracker-seu-usuario.turso.io"
   TURSO_AUTH_TOKEN = "eyJ..."
   ```
3. Instale as dependências (inclusive o `libsql`, novo no `requirements.txt`):
   ```bash
   pip install -r requirements.txt
   ```
4. Rode o app localmente para confirmar que ele conecta no Turso:
   ```bash
   streamlit run app.py
   ```
   Se abrir normalmente e a tela "Suas Atividades" aparecer vazia (banco
   novo, ainda sem dados), está tudo certo — os dados de verdade ainda estão
   no seu `personal_tracker.db` local, e vamos copiá-los no próximo passo.

---

## 3. Migrar seus dados para o Turso (rodar uma única vez)

Você tem 2.610 registros no `personal_tracker.db` atual. Este passo copia
todos eles para o Turso, de uma vez:

```bash
export TURSO_DATABASE_URL="libsql://qa-tracker-seu-usuario.turso.io"
export TURSO_AUTH_TOKEN="eyJ..."
python migrate_to_turso.py
```

Você deve ver algo como:
```
'work_logs': 2610 registro(s) copiado(s).
'custom_options': 1 registro(s) copiado(s).
Migração concluída! 2611 registro(s) no total copiado(s) para o Turso.
```

Rode o app local de novo (`streamlit run app.py`) e confirme que agora seus
registros aparecem normalmente — só que vindo do Turso, não mais do arquivo
local.

---

## 4. Subir o código para o GitHub

Como você já tem o repositório, só falta garantir que esses arquivos novos
estejam nele (o `.gitignore` já impede que `secrets.toml` e o `.db` local
sejam enviados):

```bash
git add app.py database_core.py importer_core.py import_history.py \
        fix_legacy_descriptions.py fix_project_reclassification.py \
        migrate_to_turso.py requirements.txt .gitignore \
        .streamlit/config.toml .streamlit/secrets.toml.example
git commit -m "Preparar app para deploy no Streamlit Community Cloud com Turso"
git push
```

Confirme no GitHub que o `secrets.toml` (o de verdade, sem `.example`) **não**
aparece no repositório.

---

## 5. Deploy no Streamlit Community Cloud

1. Acesse https://share.streamlit.io e faça login com sua conta GitHub.
2. Clique em **"New app"**.
3. Escolha seu repositório, a branch (geralmente `main`) e o arquivo principal
   (`app.py`).
4. Antes de clicar em "Deploy", clique em **"Advanced settings"**:
   - Cole o conteúdo do seu `secrets.toml` (com os valores reais do Turso)
     na caixa de **Secrets**.
5. Clique em **Deploy**. Em alguns minutos, seu app estará em
   `https://SEU-APP.streamlit.app` (ou um nome parecido — você pode
   personalizar o subdomínio nas configurações do app).

---

## 6. Restringir o acesso só a e-mails convidados

1. No seu workspace em share.streamlit.io, abra o menu (⋮) do app e vá em
   **Settings > Sharing**.
2. Mude de "Public" para **"Only specific people can view this app"**.
3. Adicione os e-mails das pessoas que devem ter acesso (inclusive o seu).
4. Quem não estiver na lista verá uma tela pedindo login e será bloqueado
   mesmo tendo o link.

---

## 7. Uso no dia a dia

- Como você pediu, o app **não** fica "sempre no ar" de propósito — o
  Community Cloud já se comporta assim sozinho: se ninguém acessar por 12h,
  ele dorme, e quem acessar o link depois só precisa clicar em "acordar o
  app" (leva uns 30 segundos).
- Toda vez que você mudar o código e der `git push`, o Community Cloud
  redesenha o app automaticamente — sem risco de perder dados, porque eles
  agora vivem no Turso, não mais no container do app.
- Seu `personal_tracker.db` local continua existindo na sua máquina como
  backup do estado até a migração — não precisa apagar, mas o app publicado
  não lê mais dele.

---

## Resumo do que mudou no código

- **`database_core.py`**: passou a conectar no Turso quando
  `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` existem (via secrets), com fallback
  automático para o SQLite local se essas variáveis não estiverem definidas
  — então continua funcionando 100% offline se você rodar sem essas
  variáveis configuradas.
- **`migrate_to_turso.py`** (novo): script de migração única.
- **`requirements.txt`** (novo): dependências do app, incluindo `libsql`.
- **`.streamlit/config.toml`**: limite de upload de 20MB (já existia).
- **`.streamlit/secrets.toml.example`** (novo): modelo para você preencher.
- **`.gitignore`** (novo): mantém `secrets.toml` e o `.db` local fora do
  Git.
