#!/bin/sh
# Social sites change constantly; yt-dlp/gallery-dl ship fixes within days.
# Updating on every start keeps the bot working without rebuilding the image.
if [ "${AUTO_UPDATE:-1}" = "1" ]; then
  pip install --user -U --quiet "yt-dlp[default]" gallery-dl bgutil-ytdlp-pot-provider || echo "[warn] downloader update failed, using bundled versions"
fi
exec python -m app.main
