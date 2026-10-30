#!/usr/bin/env bash
# Push the files the last radar.py command changed (out/changed.txt) to the "inbox" branch.
# GitHub Actions then saves them to S3. No AWS key is needed here.
set -e
cd "$(dirname "$0")"
[ -s out/changed.txt ] || { echo "Nothing changed; nothing to push."; exit 0; }
tmp=$(mktemp -d)
mkdir -p "$tmp/out"; cp out/changed.txt "$tmp/out/"
while read -r k; do [ -n "$k" ] && mkdir -p "$tmp/store/$(dirname "$k")" && cp "store/$k" "$tmp/store/$k"; done < out/changed.txt
cd "$tmp"
git init -q -b inbox
git -c user.name="radar-bot" -c user.email="radar-bot@users.noreply.github.com" add -A
git -c user.name="radar-bot" -c user.email="radar-bot@users.noreply.github.com" commit -qm "radar data $(date -u +%FT%TZ)"
git push -q -f https://github.com/Tarantula-Research-Labs/earnings-radar.git inbox
echo "Pushed $(wc -l < out/changed.txt) file(s) to the inbox branch; GitHub Actions saves them to S3."
