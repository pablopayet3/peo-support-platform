FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY intake_agent ./intake_agent
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "-c", "uvicorn intake_agent.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
