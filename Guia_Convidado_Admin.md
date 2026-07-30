# Área de Convidado com Aprovação — Guia

## 1. Visão geral

Antes, "dar acesso a alguém" significava compartilhar a senha fixa do
usuário `convidado`. Agora existe um fluxo de verdade:

1. Quem não tem login preenche um formulário (nome, e-mail, justificativa)
   na própria tela de login.
2. Você (admin) vê a solicitação numa área administrativa própria,
   acessível por um botão na barra lateral.
3. Você aprova, rejeita ou exclui.
4. Ao aprovar, o app gera um **link único** — você copia e manda pra
   pessoa por qualquer canal (WhatsApp, e-mail, etc.).
5. Quem recebe o link entra direto, **sem usuário/senha**, em modo
   **somente leitura**: vê Registro de Atividades (sem os botões de
   Novo/Editar/Excluir) e o Dashboard completo, com filtro de datas livre.
   Não vê Daily Scrum, Sincronização, nem nada da barra lateral de
   administração.

Você pode **revogar o acesso a qualquer momento** — o link para de
funcionar na hora, sem precisar esperar nenhum prazo.

---

## 2. Configuração necessária

No `secrets.toml`, adicione:
```toml
ADMIN_USERNAME = "seu_usuario"
```
Use exatamente o mesmo nome que já está em `[credentials]`. É isso que diz
ao app **de quem** são os dados que o convidado deve enxergar (o convidado
não tem work_logs próprios — ele vê os seus).

Sem essa chave configurada, o formulário de solicitação de acesso aparece
desativado ("Solicitação de acesso desativada no momento").

Reinicie o app depois de editar.

---

## 3. Testando o fluxo (lado convidado)

1. Abra o app **sem estar logado** (ou clique em Sair primeiro).
2. Na tela de login, abra "Não tem acesso? Solicitar acesso de convidado".
3. Preencha nome, e-mail e justificativa, envie e confirme.
4. Deve aparecer "✅ Solicitação enviada! Aguarde a aprovação".

Duas validações automáticas:
- **E-mail duplicado**: uma segunda solicitação com o mesmo e-mail, enquanto
  a primeira ainda estiver pendente ou aprovada, é bloqueada.
- **Limite de 5**: a partir da 6ª solicitação **ativa** (pendente + aprovada
  somadas), aparece "Não há mais solicitações disponíveis no momento."
  Rejeitar ou excluir uma solicitação libera vaga na hora.

---

## 4. Testando o fluxo (lado admin)

1. Logue normalmente com seu usuário/senha.
2. Na barra lateral, clique em **"🔐 Solicitações de Acesso"** — abre uma
   tela própria (o resto do app some enquanto ela estiver aberta; "← Voltar
   para o app" fecha).
3. Na solicitação pendente, clique **"✅ Aprovar"**.
4. Aparece um campo tipo `?g=AbCdEf123...` — copie esse texto.
5. Monte o link completo: `<URL do seu app>?g=AbCdEf123...` (ex.:
   `https://seu-app.streamlit.app/?g=AbCdEf123...`) e mande pra pessoa.
6. Abra esse link **numa aba anônima/outro navegador** (pra simular a
   pessoa que recebeu) — deve entrar direto, mostrando "👁️ Convidado
   (somente leitura)" na barra lateral, só com as abas Registro de
   Atividades e Dashboard & Relatórios.
7. Pra revogar: volte na área admin, clique **"🚫 Revogar"** na mesma
   solicitação — recarregue a aba anônima: deve cair pra tela de login com
   o aviso "Este link de convidado não é válido ou o acesso foi revogado."

---

## 5. Recomendação: remover o `convidado` fixo do secrets.toml

Como esse sistema novo substitui a lógica de "compartilhar uma senha", a
entrada antiga em `[credentials]`:
```toml
"convidado" = "$2b$12$..."
```
fica redundante — e continua sendo um acesso **completo** (não
somente-leitura), então vale considerar removê-la depois que migrar todo
mundo que usava essa senha pro fluxo novo.

---

## 6. O que ficou de fora desta entrega (ideias pra depois, se quiser)

- Envio automático do link por e-mail (via n8n) quando aprovar — você
  preferiu copiar/mandar manualmente por agora, mas dá pra automatizar
  depois se mudar de ideia.
- Um botão para o admin **criar e aprovar direto** um convidado, sem
  esperar ele preencher o formulário (útil se você já sabe que vai dar
  acesso a alguém específico).
- Expiração automática por tempo do link aprovado (hoje ele vale até você
  revogar manualmente, sem prazo).