FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core_platform ./core_platform
RUN useradd --create-home appuser
USER appuser
CMD ["sh", "-c", "uvicorn core_platform.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
