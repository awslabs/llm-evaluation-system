FROM python:3.12-slim

WORKDIR /workspace
COPY agent.py .
RUN pip install --no-cache-dir openai

CMD ["tail", "-f", "/dev/null"]
