# Bot Discord de música (Python)

Bot com slash commands para tocar YouTube por **link** ou **nome**, com fila, volume, loop e now playing.

Stack: `discord.py` + `Wavelink` + `Lavalink v4` + **PostgreSQL** + painel web (Docker).

## Estrutura

```
bot_discord/
├── backend/          # Bot Discord, DB, Lavalink, Docker
│   ├── bot/
│   ├── lavalink/
│   ├── docker-compose.yml
│   └── requirements.txt
├── frontend/         # Painel web (Flask)
│   ├── web/
│   └── requirements.txt
├── .env
└── requirements.txt  # instala backend + frontend
```

## Pré-requisitos

- Python 3.11+
- [Docker](https://docs.docker.com/get-docker/) (Lavalink + PostgreSQL)
- Conta no [Discord Developer Portal](https://discord.com/developers/applications)
- Um servidor Discord onde você possa convidar bots

## 1. Criar o bot no Discord

1. Abra o [Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Em **Bot** → **Add Bot** → copie o **Token**.
3. Em **OAuth2 → General**, copie o **Client ID** (ID do aplicativo).
4. Em **OAuth2 → Redirects**, adicione exatamente:
   `http://127.0.0.1:8080/callback`

## 2. Configurar o projeto

```bash
cd bot_discord
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edite `.env` na raiz do projeto:

```env
DISCORD_TOKEN=cole_o_token_aqui
DISCORD_CLIENT_ID=id_do_aplicativo
DISCORD_REDIRECT_URI=http://127.0.0.1:8080/callback
DISCORD_GUILD_ID=
DATABASE_URL=postgresql://music:music@127.0.0.1:5433/music
LAVALINK_HOST=127.0.0.1
LAVALINK_PORT=2333
LAVALINK_PASSWORD=youshallnotpass
```

## 3. Subir PostgreSQL + Lavalink

```bash
mkdir -p backend/lavalink/plugins
chmod 777 backend/lavalink/plugins
cd backend
docker compose up -d
cd ..
```

Isso sobe:
- Postgres em `127.0.0.1:5432` (user/senha/db: `music`)
- Lavalink em `127.0.0.1:2333`

## 4. Site de convite + painel

```bash
source .venv/bin/activate
cd frontend
python -m web
```

- Início / convite: [http://127.0.0.1:8080](http://127.0.0.1:8080)
- Painel (online, uptime, servidores): [http://127.0.0.1:8080/dashboard](http://127.0.0.1:8080/dashboard)
- API JSON: [http://127.0.0.1:8080/api/status](http://127.0.0.1:8080/api/status)

Clique em **Adicionar ao Discord**, escolha o servidor e autorize. O callback grava `DISCORD_GUILD_ID` no `.env`.

Se a home ainda pedir `DISCORD_CLIENT_ID`, confira o `.env` e recarregue a página (o site relê o arquivo a cada request).

## 5. Rodar o bot

```bash
source .venv/bin/activate
cd backend
python -m bot
```

O bot grava no PostgreSQL: online/offline, heartbeat, uptime e lista de servidores.

## Comandos

| Comando | Descrição |
|---|---|
| `/play <link ou nome>` | Toca ou enfileira (YouTube) |
| `/pause` | Pausa |
| `/resume` | Continua |
| `/skip` | Pula a faixa atual |
| `/stop` | Para e limpa a fila |
| `/queue` | Mostra a fila |
| `/volume <0-100>` | Ajusta o volume |
| `/loop` | Alterna: off → faixa → fila |
| `/nowplaying` | Mostra o que está tocando |
| `/leave` | Sai do canal de voz |

## Teste rápido

1. Entre em um canal de voz no servidor.
2. `/play never gonna give you up`
3. `/play https://www.youtube.com/watch?v=dQw4w9WgXcQ`
4. `/queue`, `/nowplaying`, `/skip`, `/volume 30`, `/loop`, `/stop`, `/leave`

## Deploy Kubernetes (Kingnet)

Mesmo modelo do `controle_contas`: manifests neste repo + Applications Argo no [`kingnet-k8s`](https://github.com/dudullsc/kingnet-k8s).

| App Argo | Path | Namespace |
|---|---|---|
| `bot-discord-db` | `k8s/postgres/overlays/prd` | `bot-discord-db` |
| `bot-discord-lavalink` | `k8s/apps/bot-discord/lavalink/overlays/prd` | `bot-discord` |
| `bot-discord-bot` | `k8s/apps/bot-discord/bot/overlays/prd` | `bot-discord` |
| `bot-discord-web` | `k8s/apps/bot-discord/web/overlays/prd` | `bot-discord` |

- Painel: `https://bot-discord.kingbr.com.br`
- Imagens: `ghcr.io/dudullsc/bot-discord-bot` e `bot-discord-web` (CI em `.github/workflows/build-images.yml`)
- Deploy automático de imagem: Argo CD Image Updater (em `kingnet-k8s`) observa o digest de `:latest` e atualiza `bot-discord-bot` / `bot-discord-web`

Checklist pós-merge:
1. DNS `bot-discord.kingbr.com.br` → `192.168.3.11`
2. Discord OAuth2 Redirect: `https://bot-discord.kingbr.com.br/callback`
3. Deploy key SSH do repo no Argo (`bot-discord-ssh` no `kingnet-k8s`)
4. Push `main` para buildar imagens no GHCR
5. (Se packages privadas) selar `ghcr-pull-secret` e descomentar no kustomize do bot

Detalhes: [`k8s/README.md`](k8s/README.md).

## Rodar no VPS depois

No servidor, clone o projeto, configure `.env`, suba o Docker (`cd backend && docker compose up -d`) e rode o bot (systemd, screen ou `cd backend && python -m bot`).

Se o bot e o Lavalink forem containers na mesma rede Docker, use `LAVALINK_HOST=lavalink` (nome do serviço) em vez de `127.0.0.1`.

## Troubleshooting

### Comandos `/` não aparecem

- Confirme que o bot foi convidado pela página web (`cd frontend && python -m web`) com os scopes `bot` + `applications.commands`.
- Confirme `DISCORD_GUILD_ID` no `.env` e reinicie o bot (sync por guild é imediato; global pode demorar até ~1h).
- Em OAuth2 → Redirects, a URL deve ser exatamente `http://127.0.0.1:8080/callback`.

### “Falha ao buscar” / Lavalink offline

```bash
cd backend
docker compose ps
docker compose logs lavalink
curl -s -H "Authorization: youshallnotpass" http://127.0.0.1:2333/version
```

### YouTube bloqueia (“sign in to confirm you're not a bot”)

1. Atualize o plugin em `backend/lavalink/application.yml` (versão do [youtube-source](https://github.com/lavalink-devs/youtube-source/releases)).
2. Reinicie: `cd backend && docker compose restart lavalink`.
3. Se continuar, ative OAuth no plugin (conta secundária recomendada):

```yaml
plugins:
  youtube:
    oauth:
      enabled: true
```

Reinicie o Lavalink, veja o log (`docker compose logs -f lavalink`) e conclua o fluxo OAuth no [google.com/device](https://www.google.com/device) (conta secundária). Não grave o `refreshToken` neste repositório.

### Bot entra no canal mas não toca

Confirme permissões **Connect** e **Speak** no canal de voz e que ninguém silenciou o bot no servidor.
