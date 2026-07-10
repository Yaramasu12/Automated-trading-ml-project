#!/usr/bin/env bash
# Nightly backup: PostgreSQL dump + SQLite/data tar → S3 (14-day retention).
# Requires: aws cli v2 on the host, an S3 bucket, and an EC2 instance role
# (or aws configure) with PutObject/ListObjects/DeleteObject on that bucket.
set -euo pipefail

REPO_DIR=/opt/trading
ENV_FILE="$REPO_DIR/deploy/trading.env"
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${BACKUP_S3_BUCKET:?BACKUP_S3_BUCKET not set in trading.env}"

STAMP=$(date +%Y%m%d_%H%M%S)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# 1. PostgreSQL/TimescaleDB logical dump (via the running container)
docker exec trading-timescaledb pg_dump -U "${POSTGRES_USER:-trading}" \
  -d "${POSTGRES_DB:-trading}" --no-owner -Fc > "$WORK/pg_${STAMP}.dump"

# 2. SQLite + runtime state (exit plans, OMS events, caches, models)
tar -czf "$WORK/data_${STAMP}.tar.gz" -C "$REPO_DIR" data models

# 3. Ship to S3
aws s3 cp "$WORK/pg_${STAMP}.dump"      "s3://${BACKUP_S3_BUCKET}/pg/pg_${STAMP}.dump"
aws s3 cp "$WORK/data_${STAMP}.tar.gz"  "s3://${BACKUP_S3_BUCKET}/data/data_${STAMP}.tar.gz"

# 4. Retention: delete objects older than 14 days
CUTOFF=$(date -d '14 days ago' +%s 2>/dev/null || date -v-14d +%s)
for prefix in pg data; do
  aws s3api list-objects-v2 --bucket "$BACKUP_S3_BUCKET" --prefix "$prefix/" \
    --query 'Contents[].[Key,LastModified]' --output text 2>/dev/null | \
  while read -r key modified; do
    [ -z "$key" ] && continue
    obj_epoch=$(date -d "$modified" +%s 2>/dev/null || date -jf '%Y-%m-%dT%H:%M:%S' "${modified%%+*}" +%s)
    if [ "$obj_epoch" -lt "$CUTOFF" ]; then
      aws s3 rm "s3://${BACKUP_S3_BUCKET}/${key}"
    fi
  done
done

echo "backup OK ${STAMP}"
