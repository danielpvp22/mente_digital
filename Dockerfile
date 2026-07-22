# syntax=docker/dockerfile:1
# Dois estágios de propósito: a imagem -devel (com nvcc) existe SÓ para compilar
# o llama-cpp-python com CUDA; o estágio final usa a -runtime, bem menor. O que
# NÃO entra na imagem: modelos, vault e bancos — ficam no host via bind mount
# (ver docker-compose.yml), então rebuild nunca toca os seus dados.
ARG CUDA_VERSION=12.4.1

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS builder
# Arquitetura CUDA alvo da compilação — 86 = Ampere (RTX 3080). Passe outro valor
# no build para outra GPU (89 = Ada/RTX 40xx): docker compose build --build-arg CUDA_ARCH=89
ARG CUDA_ARCH=86
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 python3.10-venv python3-pip build-essential cmake ninja-build git \
    && rm -rf /var/lib/apt/lists/*
RUN python3.10 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip install --no-cache-dir --upgrade pip
# Camada própria para a parte cara: o llama-cpp-python compila por vários minutos
# e só precisa recompilar quando o requirements.txt mudar.
COPY requirements.txt /tmp/requirements.txt
RUN CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}" \
    FORCE_CMAKE=1 \
    pip install --no-cache-dir -r /tmp/requirements.txt

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive
# libgomp1: exigido por llama.cpp/CTranslate2; curl: healthcheck do compose.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.10 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY . .
EXPOSE 8000
# O app agora é o pacote mente_digital/ (main.py mora em mente_digital/main.py, com
# guard __main__). `-m` roda o módulo como __main__ a partir do WORKDIR /app.
CMD ["python", "-m", "mente_digital.main"]
