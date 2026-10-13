#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# Database backups: create, prune, restore, and prove a restore works.
#
#   sh scripts/backup.sh backup                 # dump now, prune old dumps
#   sh scripts/backup.sh restore-test <file>    # restore into a scratch DB, check, drop
#   sh scripts/backup.sh restore <file>         # DESTRUCTIVE: replace the live DB
#
# Dumps go to ./backups (git-ignored). Set BACKUP_PASSPHRASE to encrypt them
# with AES-256 (openssl); keep the passphrase somewhere other than this host.
#
# What is in a dump: employees, face templates (biometric data), presence
# history and the audit log. Camera passwords are inside it too, but
# encrypted with FIELD_ENCRYPTION_KEY, which is NOT in the dump - back that
# key up separately, or a restore cannot open any camera.
#
# Snapshots (the media volume) are deliberately not backed up: they are
# short-retention evidence, and copying them would outlive their purge.
# ---------------------------------------------------------------------------
set -eu

BACKUP_DIR="${BACKUP_DIR:-backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
COMPOSE="${COMPOSE:-docker compose}"

log() { printf '[backup] %s\n' "$*" >&2; }

dump() {
  mkdir -p "$BACKUP_DIR"
  stamp=$(date +%Y%m%d-%H%M%S)
  target="$BACKUP_DIR/cctv-$stamp.dump"
  log "dumping database to $target"
  $COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$target.part"
  if [ ! -s "$target.part" ]; then
    rm -f "$target.part"
    log "FAILED: empty dump"
    exit 1
  fi
  if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
    openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE \
      -in "$target.part" -out "$target.enc"
    rm -f "$target.part"
    target="$target.enc"
    log "encrypted"
  else
    mv "$target.part" "$target"
    log "WARNING: not encrypted (set BACKUP_PASSPHRASE). This file holds biometric data."
  fi
  log "done: $target ($(wc -c < "$target") bytes)"

  # Retention applies to backups too: a backup kept forever defeats the purge.
  find "$BACKUP_DIR" -name 'cctv-*.dump*' -type f -mtime +"$RETENTION_DAYS" -print -delete \
    | sed 's/^/[backup] pruned /' >&2 || true
}

plain_dump_of() {
  # Prints the path of an unencrypted copy of $1 (decrypting to a temp file).
  case "$1" in
    *.enc)
      [ -n "${BACKUP_PASSPHRASE:-}" ] || { log "BACKUP_PASSPHRASE is needed to decrypt $1"; exit 1; }
      tmp="$1.decrypted.tmp"
      openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$1" -out "$tmp"
      echo "$tmp"
      ;;
    *) echo "$1" ;;
  esac
}

restore_test() {
  file="$1"
  [ -f "$file" ] || { log "no such file: $file"; exit 1; }
  plain=$(plain_dump_of "$file")
  log "restoring $file into scratch database cctv_restore_test"
  $COMPOSE exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists cctv_restore_test && createdb -U "$POSTGRES_USER" cctv_restore_test'
  $COMPOSE exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d cctv_restore_test --no-owner --exit-on-error' < "$plain"
  log "row counts in the restored copy:"
  $COMPOSE exec -T db sh -c 'psql -U "$POSTGRES_USER" -d cctv_restore_test -At -F " "' <<'SQL' | sed 's/^/  /' >&2
SELECT 'employees', count(*) FROM employees_employee
UNION ALL SELECT 'face_templates', count(*) FROM employees_faceenrollment
UNION ALL SELECT 'presence_intervals', count(*) FROM tracking_presenceinterval
UNION ALL SELECT 'audit_rows', count(*) FROM accounts_auditlog
UNION ALL SELECT 'users', count(*) FROM accounts_user;
SQL
  $COMPOSE exec -T db sh -c 'dropdb -U "$POSTGRES_USER" cctv_restore_test'
  [ "$plain" = "$file" ] || rm -f "$plain"
  log "restore test PASSED - the backup is usable. Scratch database dropped."
}

restore_live() {
  file="$1"
  [ -f "$file" ] || { log "no such file: $file"; exit 1; }
  printf 'This REPLACES the live database with %s. Type "restore" to continue: ' "$file"
  read -r answer
  [ "$answer" = "restore" ] || { log "aborted"; exit 1; }
  plain=$(plain_dump_of "$file")
  log "stopping services that write to the database"
  $COMPOSE stop web worker beat aggregator vision 2>/dev/null || true
  $COMPOSE exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < "$plain"
  [ "$plain" = "$file" ] || rm -f "$plain"
  log "restored. Starting services again."
  $COMPOSE up -d web worker beat aggregator
}

case "${1:-}" in
  backup) dump ;;
  restore-test) restore_test "${2:?usage: backup.sh restore-test <file>}" ;;
  restore) restore_live "${2:?usage: backup.sh restore <file>}" ;;
  latest) ls -1t "$BACKUP_DIR"/cctv-*.dump* 2>/dev/null | head -1 ;;
  *) echo "usage: $0 backup | restore-test <file> | restore <file> | latest" >&2; exit 2 ;;
esac
