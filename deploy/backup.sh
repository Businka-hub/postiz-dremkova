#!/bin/sh
# Nightly backup of the Postiz database and uploaded media, kept for $KEEP_DAYS days.
# Install: cp deploy/backup.sh /opt/postiz/backup.sh && chmod 700 /opt/postiz/backup.sh
#          echo '15 3 * * * root /opt/postiz/backup.sh >> /var/log/postiz-backup.log 2>&1' > /etc/cron.d/postiz-backup
# Restore: docker exec -i postiz-postgres pg_restore -U postiz -d postiz --clean --if-exists < postiz-<stamp>.dump
set -eu
DIR=${BACKUP_DIR:-/opt/postiz/backups}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p "$DIR" && chmod 700 "$DIR"

docker exec postiz-postgres pg_dump -U postiz -d postiz -Fc > "$DIR/postiz-$STAMP.dump.part"
mv "$DIR/postiz-$STAMP.dump.part" "$DIR/postiz-$STAMP.dump"
docker run --rm -v postiz_postiz-uploads:/uploads:ro -v "$DIR":/backup alpine \
  tar czf "/backup/uploads-$STAMP.tgz" -C /uploads .

find "$DIR" -name 'postiz-*.dump' -mtime +"$KEEP_DAYS" -delete
find "$DIR" -name 'uploads-*.tgz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) backup ok $(du -sh "$DIR/postiz-$STAMP.dump" | cut -f1) db, $(du -sh "$DIR/uploads-$STAMP.tgz" | cut -f1) uploads"
