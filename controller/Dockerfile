FROM python:3.12-slim
# operator 要 clone 仓库来读 .ci.yaml
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py reconciler.py pipeline.py ciconfig.py runstatus.py github.py ./
COPY static ./static
USER 65534
ENV HOME=/tmp
# 同一个镜像两种用法:默认跑 webhook,operator 的 Deployment 里覆盖成 kopf run
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
