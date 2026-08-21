#!/usr/bin/env bash
# install.sh — развернуть радар и прописать в cron.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3)"

echo "== Каталог: $DIR"
echo "== Python:  $PY  ($($PY -V))"
$PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' \
  || { echo "Нужен Python 3.8+"; exit 1; }

mkdir -p "$DIR/state"
chmod +x "$DIR"/*.py 2>/dev/null || true

if [[ ! -f "$DIR/config.json" ]]; then
  cp "$DIR/config.example.json" "$DIR/config.json"
  chmod 600 "$DIR/config.json"
  echo "!! Создан config.json — заполни его (telegram, ключи, кошельки) и перезапусти."
  exit 0
fi
chmod 600 "$DIR/config.json"

echo "== Инициализация журнала"
$PY "$DIR/aml_journal.py" init

echo "== Первичный прогон (baseline, алертов не будет)"
$PY "$DIR/ofac_radar.py"          || echo "  ofac: ошибка, разберись"
$PY "$DIR/opensanctions_radar.py" || echo "  opensanctions: ошибка, разберись"
$PY "$DIR/hacks_radar.py"         || echo "  hacks: ошибка, разберись"

CRON=$(cat <<EOF
# --- aml-radar ---
*/20 * * * *  cd $DIR && $PY hacks_radar.py         >> state/radar.log 2>&1
*/30 * * * *  cd $DIR && $PY exposure_watch.py      >> state/radar.log 2>&1
7    * * * *  cd $DIR && $PY ofac_radar.py          >> state/radar.log 2>&1
23 */6 * * *  cd $DIR && $PY opensanctions_radar.py >> state/radar.log 2>&1
0    9 * * 1  cd $DIR && $PY aml_journal.py due     >> state/radar.log 2>&1
# --- /aml-radar ---
EOF
)

echo
echo "== Добавь в crontab (crontab -e):"
echo "$CRON"
echo
read -rp "Прописать автоматически? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
  ( crontab -l 2>/dev/null | grep -v 'aml-radar' | grep -v "$DIR"; echo "$CRON" ) | crontab -
  echo "Готово. Проверь: crontab -l"
fi
