clean:
  rm -rf .venv
  rm -rf tmp
  rm -f data/processed_links.txt data/last_github_checked.txt || true

backup-db:
  mkdir -p tmp
  cp -R data/ tmp/data/

pull-db:
  docker --context orangepi cp orange-listmonk-newsletter-1:/app/data/processed_links.txt data/
