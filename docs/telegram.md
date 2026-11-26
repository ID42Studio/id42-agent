# Telegram

Telegram is off until `TELEGRAM_ENABLED=true`. When it is on, the first boot writes `channels.telegram` with `enabled`, `token`, `allowFrom`, and `mode: "polling"`. Those fields are `TelegramConfig` in `nanobot/channels/telegram/runtime.py` at the pinned commit (`enabled` at line 453, `token` at 454, `mode` at 455, `allow_from` at 456). `mode` defaults to `"polling"`. The channel class states that long polling is the default (`TelegramChannel` docstring, `runtime.py:564`), and upstream `docs/chat-apps.md` says the same ("Telegram uses long polling by default").

The image is built with `NANOBOT_CHANNELS=telegram`, which preinstalls the channel dependency (`Dockerfile` `ARG NANOBOT_CHANNELS`, default `whatsapp`). You do not need a separate `nanobot plugins enable telegram` inside this image.

## Create the bot

1. In Telegram, open `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the token into `TELEGRAM_BOT_TOKEN` in `.env`. Do not commit `.env`.

## Allowlist

Set `TELEGRAM_ALLOWED_USERS` to your numeric Telegram user id, or to your username without `@`. Separate several people with commas.

```bash
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:replace-me
TELEGRAM_ALLOWED_USERS=123456789
```

`*` is rejected. Upstream treats `allowFrom: ["*"]` as allow-everyone (`nanobot/channels/base.py` `is_allowed`). This personal distribution does not write that.

Matching rules at this pin (`TelegramChannel.is_allowed`, `runtime.py:641`): an entry must equal the sender id or, for Telegram's `id|username` form, the id or the username. A leading `@` does not match. Pairing approval is a separate upstream path and is not configured here. An empty allowlist does not mean "allow everyone"; bootstrap refuses to enable Telegram without at least one entry.

Find your numeric id with a tool you trust (BotFather does not show it). A username entry also works, but the numeric id survives a username change.

Set these values before `make init`. After init, change the token or allowlist in the WebUI or in `config.json`, then `make restart`.

## Long polling, and one consumer per token

`mode` is `polling`. The gateway calls Telegram `getUpdates`. No public HTTPS endpoint is required, and none is published.

Do not use the same bot token in another program, webhook, or second nanobot at the same time. Telegram delivers each update to one long-poll consumer. A second `getUpdates` (or a webhook set on that token) steals updates, and this gateway looks idle. One token, one running bot.

If you later want a webhook, that is an upstream feature (`webhookUrl`, `webhookSecretToken`, and related fields on `TelegramConfig`) and it needs a public HTTPS URL. This phase does not render webhook settings. Keep the published Compose ports on loopback either way.

## Check without printing the token

With the gateway running and Telegram enabled:

```bash
make doctor
```

Doctor calls `getMe` from inside the container, prints the bot id and username, and checks that `allowFrom` is a non-empty list without `*`. The token stays in `.env` and in `config.json` on the volume.
