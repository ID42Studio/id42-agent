# Ollama

The gateway talks to Ollama through the OpenAI-compatible API. Bootstrap writes:

- `providers.ollama.apiBase` from `OLLAMA_BASE_URL`
- `modelPresets.primary.model` from `OLLAMA_MODEL`
- `agents.defaults.modelPreset` = `primary`

Those fields exist at the pinned commit. See [architecture.md](architecture.md#keys-bootstrap-writes). The upstream default base URL is `http://localhost:11434/v1` (`nanobot/providers/registry.py` `default_api_base` for `name="ollama"`). Inside this container, `localhost` is the container, not the host. The default in `.env.example` is therefore `http://host.docker.internal:11434/v1`. Compose adds `extra_hosts: host.docker.internal:host-gateway`, which upstream's own Compose file does not set.

The URL must use the `/v1` path. `scripts/bootstrap.sh` rejects any other path.

## The host server must be reachable, and not public

`host.docker.internal` arrives on a Docker bridge address (often `172.17.0.1`), not on the host's loopback interface. An Ollama process bound only to `127.0.0.1` will refuse that connection.

Bind Ollama on an interface the container can reach, and keep that port off the public internet.

A typical host setup:

```bash
# Listen on all interfaces on the machine. Then firewall the port.
# Do not forward 11434 from your router.
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Then restrict the host firewall so `11434/tcp` is accepted from the Docker bridge and rejected from LAN and WAN. With `ufw`, that is "allow from the bridge subnet, deny everywhere else", and no router port-forward. Binding only to the bridge address (`OLLAMA_HOST=172.17.0.1:11434`) is stricter when that address is stable on your machine.

Check from the gateway, not from the host browser:

```bash
make doctor
```

Doctor GETs `{apiBase}/models` with the container's network and prints the ids it sees. The configured model must appear as that id, or as `{name}:latest`.

`ollama pull` still runs on the host (or inside the optional `ollama` profile). The gateway does not download models.

## Cloud models are opt-in

Bootstrap writes only the Ollama provider. It does not set an API key for OpenAI, Anthropic, OpenRouter, or any other provider.

To use a cloud model later, add that provider in the WebUI (**Settings → Models**) or in `config.json` under a key nanobot already defines, for example `providers.openai` / `providers.anthropic` (`nanobot/config/schema.py`, `ProvidersConfig`). Restart the gateway after editing the file. Treat that as an explicit choice: prompts and tool traffic then leave the machine.

Upstream also recognizes `OLLAMA_API_KEY` (`ProviderSpec.env_key` for Ollama). This distribution does not set it. Leave it unset for a normal local Ollama.

## Bundled Ollama profile

`docker compose --profile ollama` starts `ollama/ollama:0.40.18` with a pinned digest, a private volume, and `127.0.0.1:11434:11434`. It is off unless you select the profile.

Point `OLLAMA_BASE_URL` at `http://ollama:11434/v1` before the first init. After init, the URL lives in `config.json`. Changing `.env` does not rewrite it.

The profile does not pass a GPU through. CPU inference is the behavior you get from this file.
