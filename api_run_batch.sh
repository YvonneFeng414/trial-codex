# export CODEX_API_KEY="sk-..."
# ./run_batch.sh --probe-rate-limits off \
#   --input-dir /Users/yixuanfeng/Desktop/web-download/downloads/bmj \
#   --out-dir bmj_result \
#   --workers 4 \
#   --limit 18 \

# ./ocr_run_batch.sh --probe-rate-limits off \
#   --pdf-list need_ocr.csv \
#   --ignore-stop \
#   --force-ocr \
#   --workers 4 \
#   --progress verbose \
#   --limit 20 \
#   --ocr-dir ocr_2_pdfs \
#   --out-dir ocr_2_result \

./ocr_run_batch.sh --probe-rate-limits on \
  --pdf-list need_ocr_3.csv \
  --ignore-stop \
  --force-ocr \
  --workers 4 \
  --progress verbose \
  --limit 20 \
  --ocr-dir ocr_3_pdfs \
  --out-dir ocr_3_result \