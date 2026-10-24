# dev-decide HTTP API: Python standard library only, plus the bundled 30 MB meaning model.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --chown=app:app skill/ skill/
COPY --chown=app:app repo/ repo/
# The state folder must exist and belong to the app user, so a volume mounted on it is writable.
RUN mkdir -p repo/.claude/dev-decide && chown -R app:app repo
USER app
RUN python3 -m compileall -q skill/scripts

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8765'), timeout=4)"
# Render and Koyeb set PORT; elsewhere it defaults to 8765. serve.py needs DEV_DECIDE_TOKEN to listen on 0.0.0.0.
CMD ["sh", "-c", "exec python3 skill/scripts/serve.py --host 0.0.0.0 --port \"${PORT:-8765}\" --cwd /app/repo"]
