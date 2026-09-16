FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --uid 1000 --create-home agentnexus \
    && mkdir -p /app/data \
    && chmod -R a+rX /app/app /app/web /app/scripts \
    && chown -R 1000:1000 /app/data
ENV PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
