#!/usr/bin/env bash
# Mirror every repo in the platform's HF org, including large files, to a disk outside Hugging Face.
# Fetch-only: a mirror never loses a commit it has seen, even if the remote history is rewritten or deleted.
set -euo pipefail
ORG=${AIC_ORG:-AI-science-sheggle}
DEST=${1:-/mnt/big_data/backups/ai-science}
mkdir -p "$DEST"
for kind in datasets models spaces; do
  for id in $(curl -sf "https://huggingface.co/api/$kind?author=$ORG&limit=1000" | python3 -c "import json,sys; print(*[r['id'] for r in json.load(sys.stdin)])"); do
    url="https://huggingface.co/$( [ $kind = models ] || echo "$kind/" )$id"
    dir="$DEST/$kind/$id.git"
    if [ ! -d "$dir" ]; then git clone -q --bare "$url" "$dir"; fi
    # refs/backup/* keeps every head ever seen, so a force-push upstream can't drop old commits here
    git -C "$dir" fetch -q "$url" "+refs/heads/*:refs/backup/$(date -u +%Y%m%d)/*"
    git -C "$dir" fetch -q "$url" "+refs/heads/*:refs/heads/*"
    git -C "$dir" lfs fetch --all "$url" >/dev/null
    echo "$(date -u +%FT%TZ) $kind/$id ok"
  done
done
