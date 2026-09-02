FROM python:3.12-slim

# منع كتابة ملفات .pyc وتفعيل الإخراج المباشر للسجلات
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# تثبيت المتطلبات أولاً للاستفادة من طبقات الكاش الخاصة بـ Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود التطبيق
COPY main.py .

# مستخدم غير جذري (non-root) لأسباب أمنية
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
