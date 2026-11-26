#!/bin/sh
# First-boot copy into the data volume. Run as uid 1000 when config.json is absent.
# The rendered config is read from stdin so a host file mode of 0600 still works.
set -eu

dest=/home/nanobot/.nanobot
mkdir -p "$dest/workspace/skills"
cat >"$dest/config.json"
chmod 600 "$dest/config.json"

for name in AGENTS.md SOUL.md USER.md; do
    if [ ! -e "$dest/workspace/$name" ]; then
        cp "/id42-workspace/$name" "$dest/workspace/$name"
    fi
done

if [ ! -e "$dest/workspace/skills/README.md" ]; then
    cp /id42-workspace/skills/README.md "$dest/workspace/skills/README.md"
fi

echo initialized
