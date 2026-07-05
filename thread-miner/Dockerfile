FROM python:3.12-slim

WORKDIR /app
COPY . .
# Editable install keeps report.py's repo-relative templates/ path working.
RUN pip install --no-cache-dir -e ".[web]"

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz')" || exit 1
CMD ["sh", "-c", "uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
