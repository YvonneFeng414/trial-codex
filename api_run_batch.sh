export CODEX_API_KEY="sk-..."
# ./run_batch.sh --probe-rate-limits off \
#   --input-dir /Users/yixuanfeng/Desktop/web-download/downloads/bmj \
#   --out-dir bmj_result \
#   --workers 4 \
#   --limit 18 \
./ocr_run_batch.sh --probe-rate-limits off \
  --pdf-list need_ocr.csv \
  --ignore-stop \
  --force-ocr \
  --workers 4 \
  --progress verbose \
  --limit 20 \