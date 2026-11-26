FROM python:3.12-slim

ENV PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r /app/requirements.txt

COPY app.py generate.py scorer.py /app/

EXPOSE 9800
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9800", "--workers", "1"]
