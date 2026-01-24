FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py database.py ./
COPY cogs/ ./cogs/
CMD ["python", "bot.py"]