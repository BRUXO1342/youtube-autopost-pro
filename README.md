# YouTube AutoPost PRO — versão web multicontas (MVP)

Site em Flask para grupos privados, com cadastro por convite, contas isoladas, OAuth Google por canal, upload de vídeos diferentes, títulos manuais ou derivados do arquivo, fila, publicação agendada e indicadores de conexão.

## Requisitos
- Python 3.11+ e espaço em disco para vídeos.
- Projeto no Google Cloud com YouTube Data API v3 habilitada, tela de consentimento OAuth configurada e credenciais OAuth **Web application**.
- Para amigos externos em produção: domínio HTTPS, servidor persistente e política de privacidade/termos conforme exigências do Google. Durante testes OAuth, adicione os e-mails como usuários de teste na tela de consentimento; tokens de teste podem expirar rapidamente.

## Executar no Windows

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Gere segredos com:

```powershell
python -c "import secrets; from cryptography.fernet import Fernet; print('SECRET_KEY='+secrets.token_urlsafe(48)); print('TOKEN_KEY='+Fernet.generate_key().decode())"
```

Edite `.env`: copie os valores gerados, informe `ADMIN_EMAIL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `BASE_URL=http://localhost:5000`. No Google Cloud, configure **Authorized redirect URI** como `http://localhost:5000/oauth/callback`. Execute:

```powershell
python app.py
```

Abra http://localhost:5000/register e crie a primeira conta com o `ADMIN_EMAIL`. Gere convites no painel e compartilhe os códigos apenas com amigos. Cada convite funciona uma vez.

## Hospedar para amigos

Para teste, execute em um servidor com HTTPS e disco persistente. Defina `BASE_URL=https://seu-dominio` e adicione `https://seu-dominio/oauth/callback` às URIs autorizadas do Google. Instale as dependências e inicie **um único processo**:

```bash
python app.py
```

O servidor de desenvolvimento do Flask não é adequado para produção. Para hospedagem inicial em Linux use um único worker Gunicorn:

```bash
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${PORT:-8000} app:app
```

Em serviços que suspendem instâncias gratuitas, o worker de uploads e verificações também para. Serviços com disco efêmero podem apagar vídeos e SQLite após reinício. Um serviço com disco persistente e execução contínua costuma ser pago. Não há garantia de plano gratuito compatível com vídeos grandes. Para produção séria: PostgreSQL, armazenamento de objetos, fila Redis/Celery, worker separado, HTTPS, backups, proteção contra abuso, limites por usuário e observabilidade.

## Comportamento e limites
- Cada usuário possui canais e vídeos separados; cada canal precisa autorizar OAuth individualmente.
- O painel marca `online` quando a API responde com o canal esperado, `offline` quando não é acessível e `verificando` em erros transitórios. **Offline não significa banimento confirmado.** A API não garante um indicador instantâneo de suspensão.
- Fila de uploads é **sequencial**, não simultânea. Há processamento automático enquanto o servidor está ativo.
- Um vídeo agendado é enviado para o YouTube assim que chega à frente da fila, com `privacyStatus=private` e `publishAt` futuro. A data não posterga o upload, só a publicação.
- Títulos automáticos são derivados do nome do arquivo; não há IA de análise de vídeo.
- O YouTube determina a classificação como Short; não basta marcar o arquivo como Short.
- Este MVP usa SQLite e arquivos locais, sem retomada automática segura de uploads interrompidos. Verifique manualmente no YouTube Studio antes de reenviar um vídeo com erro, para evitar duplicações.
- `selfDeclaredMadeForKids` está fixado como falso por padrão **apenas como exemplo**. Antes de usar, adapte o app para perguntar a classificação correta do público de cada vídeo. Conteúdo infantil não deve ser marcado incorretamente.
- Cotas da API, restrições de projetos não verificados e políticas de spam do YouTube continuam aplicáveis. Publicar repetidamente para contornar sanções não é permitido.
- Mantenha `TOKEN_KEY` e `SECRET_KEY` secretos e faça backup deles; perder TOKEN_KEY impede ler tokens já salvos.

## Segurança antes de produção
Este é um **MVP funcional**, não uma plataforma pública auditada. Recomenda-se adicionar: rate limiting e proteção contra força bruta, 2FA, auditoria de acessos, exclusão de dados/canais, retenção e limpeza de uploads cancelados, limite de espaço por usuário, verificação de arquivos, cookies seguros e política de privacidade. Não exponha em domínio público sem essas medidas.
