FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei \
    USERS_FILE=/data/users.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 單一 worker:問答狀態存在記憶體,不可多實例
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
