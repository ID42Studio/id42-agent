# Agent instructions

nanobot loads this file, `SOUL.md`, and `USER.md` into the system prompt
from the workspace directory.

- Keep durable facts about the user in `USER.md`.
- Keep tone and boundaries in `SOUL.md`.
- Long-term memory belongs in `memory/MEMORY.md`, which nanobot creates.
- Shell execution is off in the id42-agent default (`tools.exec.enable` is false). Do not assume a shell is available.
- Stay inside this workspace when using file tools.

Edit this file in the WebUI or in the data volume. A later `make init` does not overwrite it.
