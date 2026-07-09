# MyMusicBox is a fully static, client-side app -- this container just
# serves frontend/ over HTTP. It has zero effect on which music files the
# app can see: the File System Access API / drag-and-drop always operate on
# whatever machine's browser is open, never on this container's filesystem.
FROM python:3.12-alpine

WORKDIR /app
COPY start.py .
COPY frontend/ frontend/

ENV PORT=8765
EXPOSE 8765

CMD ["python", "start.py"]
