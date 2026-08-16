# k8s — Bot Discord (produto)

Manifests de **deploy deste produto**.

Plataforma GitOps (Argo CD, Gateway, cert-manager, app-of-apps):

**https://github.com/dudullsc/kingnet-k8s**

## Estrutura

```
apps/bot-discord/bot/       # bot Discord
apps/bot-discord/web/       # painel Flask
apps/bot-discord/lavalink/  # audio (Lavalink v4)
postgres/                   # banco deste produto
prd-pub.pem                 # cert público para kubeseal
```

Applications Argo (no `kingnet-k8s`):
`bot-discord-bot`, `bot-discord-web`, `bot-discord-lavalink`, `bot-discord-db`.

## Host público

- Painel: `https://bot-discord.kingbr.com.br`
- OAuth redirect Discord: `https://bot-discord.kingbr.com.br/callback`

DNS: `bot-discord.kingbr.com.br` → Gateway público (`192.168.3.11`).

## Selar um secret

```bash
kubeseal --format yaml --cert k8s/prd-pub.pem \
  < k8s/apps/.../overlays/prd/secret.yaml \
  > k8s/apps/.../overlays/prd/sealedsecret.yaml
```

Nunca commite `secret.yaml` / `*-secret.yaml` em claro.
