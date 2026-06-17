# Central de E-mails Novaprint

MVP em Python + Streamlit para rastrear e organizar e-mails da Novaprint usando IMAP/SMTP. Ele usa a mesma conta configurada no Thunderbird, mas não depende do Thunderbird aberto.

## O que foi corrigido nesta versão

- Textos em português que estavam corrompidos por codificação.
- Criação do arquivo `.env.example`.
- Busca IMAP mais compatível: procura e-mails não lidos e e-mails dos últimos 7 dias separadamente.
- Interface sem `st.badge`, para funcionar melhor em versões diferentes do Streamlit.
- Remoção da dependência obrigatória de `pandas`.
- Fallback quando `OPENAI_API_KEY` não está configurada.
- Parsing de HTML e fallback de encoding.

## Arquivos

- `app.py`: tela Streamlit.
- `email_client.py`: leitura IMAP e envio SMTP.
- `ai_classifier.py`: classificação por IA ou regras locais.
- `database.py`: banco SQLite.
- `response_generator.py`: geração e armazenamento de respostas.
- `task_manager.py`: tarefas.
- `utils.py`: utilitários de texto, encoding e detecção de dados.
- `requirements.txt`: dependências.
- `.env.example`: modelo de configuração.

## Instalação no Windows

Entre na pasta do projeto e rode:

```powershell
py -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Se o comando `py` não existir, instale o Python em https://www.python.org/downloads/ e marque a opção de adicionar ao PATH.

## Configuração

Copie `.env.example` para `.env`:

```powershell
copy .env.example .env
```

Edite o `.env`:

```env
EMAIL_IMAP_HOST=imap.seudominio.com
EMAIL_IMAP_PORT=993
EMAIL_SMTP_HOST=smtp.seudominio.com
EMAIL_SMTP_PORT=587
EMAIL_USER=seuemail@seudominio.com
EMAIL_PASSWORD=sua_senha_ou_senha_de_app
OPENAI_API_KEY=
GESTAOCLICK_BASE_URL=https://api.gestaoclick.com/api
GESTAOCLICK_ACCESS_TOKEN=
GESTAOCLICK_SECRET_ACCESS_TOKEN=
GESTAOCLICK_DEFAULT_SITUACAO_ORCAMENTO_ID=
```

Nunca coloque senha dentro do código. A senha deve ficar somente no `.env`.

## Configuração no Streamlit Cloud

Se o app estiver online no Streamlit Cloud, não envie `.env` para o GitHub.

No painel do app, vá em **Settings > Secrets** e cole:

```toml
EMAIL_IMAP_HOST = "mail.seudominio.com.br"
EMAIL_IMAP_PORT = "993"
EMAIL_SMTP_HOST = "mail.seudominio.com.br"
EMAIL_SMTP_PORT = "587"
EMAIL_USER = "seuemail@seudominio.com.br"
EMAIL_PASSWORD = "sua_senha_do_email"
OPENAI_API_KEY = ""
GESTAOCLICK_BASE_URL = "https://api.gestaoclick.com/api"
GESTAOCLICK_ACCESS_TOKEN = "seu_access_token"
GESTAOCLICK_SECRET_ACCESS_TOKEN = "seu_secret_access_token"
GESTAOCLICK_DEFAULT_LOJA_ID = "id_da_loja_novaprint"
GESTAOCLICK_DEFAULT_SITUACAO_ORCAMENTO_ID = "id_da_situacao_em_aberto"
```

Depois clique em **Save** e reinicie o app. O código aceita tanto `.env` local quanto os Secrets do Streamlit Cloud.

Use sempre o `GESTAOCLICK_DEFAULT_LOJA_ID` da **Novaprint**. Sem ele, o Gestão Click pode retornar dados de outra loja da conta, como Techtoner.

## Como rodar

Com o ambiente virtual ativado:

```powershell
streamlit run app.py
```

Abra o endereço mostrado no terminal, normalmente:

```text
http://localhost:8501
```

## Como rastrear os e-mails

1. Abra a aba **Configurações** e veja se as variáveis aparecem preenchidas.
2. Vá em **Caixa de Entrada Inteligente**.
3. Clique em **Processar novos e-mails**.
4. O sistema busca e-mails não lidos e recentes dos últimos 7 dias.
5. Cada e-mail é salvo no SQLite e não duplica pelo `message_id`.
6. Use filtros por categoria, urgência, remetente e status.

## Envio de respostas

O sistema não envia nada automaticamente.

Para enviar:

1. Clique em **Gerar resposta** em um e-mail.
2. Vá em **Respostas Geradas**.
3. Revise e edite o texto.
4. Marque **Tem certeza que deseja enviar este e-mail?**.
5. Clique em **Enviar resposta**.

## Gmail, Outlook e e-mails corporativos

Alguns provedores não aceitam a senha normal da conta em IMAP/SMTP.

Nesses casos:

- Ative IMAP no provedor.
- Ative autenticação em duas etapas.
- Gere uma senha de aplicativo.
- Use essa senha em `EMAIL_PASSWORD`.

No Outlook/Microsoft 365, pode ser necessário liberar SMTP AUTH no painel administrativo.

## Testar sem enviar e-mail real

- Configure somente IMAP para buscar e-mails.
- Deixe `EMAIL_SMTP_HOST` vazio para impedir envio.
- Não marque a confirmação de envio.
- Sem `OPENAI_API_KEY`, a classificação funciona por regras locais.

## Gestão Click

Preencha no `.env`:

```env
GESTAOCLICK_BASE_URL=https://api.gestaoclick.com/api
GESTAOCLICK_ACCESS_TOKEN=seu_access_token
GESTAOCLICK_SECRET_ACCESS_TOKEN=seu_secret_access_token
GESTAOCLICK_DEFAULT_SITUACAO_ORCAMENTO_ID=id_da_situacao_em_aberto
```

Na aba **Pedidos de Orçamento**, o sistema:

- detecta CNPJ quando possível;
- deixa você informar CNPJ, cliente ID e número do orçamento manualmente;
- tenta detectar produtos e quantidades no e-mail;
- monta uma prévia do payload do orçamento;
- só cria no Gestão Click se você marcar a aprovação manual.

Na aba **Financeiro**, o sistema:

- busca cliente pelo CNPJ;
- consulta recebimentos e notas fiscais pelo cliente ID;
- permite gerar rascunho financeiro;
- não envia nota, boleto nem resposta sem aprovação manual.

## Banco local

O banco é criado automaticamente:

```text
central_emails_novaprint.sqlite3
```

Tabelas:

- `emails`
- `clientes_detectados`
- `tarefas`
- `respostas_geradas`
- `logs`
