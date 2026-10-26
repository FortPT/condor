#!/bin/sh
set -eu
umask 077
cd /var/lib/condor
# Native MCP children use the project cwd. Keep their config beside the parent's
# while code remains in the image, so an image update cannot retain stale source.
for source in /opt/condor/* /opt/condor/.mcp.json /opt/condor/.venv; do
    name=$(basename "$source")
    if [ -e "$name" ] || [ -L "$name" ]; then
        if [ ! -L "$name" ] || [ "$(readlink "$name")" != "$source" ]; then
            printf 'Refusing conflicting workspace path: %s\n' "$name" >&2
            exit 1
        fi
    else
        ln -s "$source" "$name"
    fi
done
exec "$@"
