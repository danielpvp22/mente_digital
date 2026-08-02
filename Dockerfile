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
#
# O lock entra como CONSTRAINT (-c), não como -r: o requirements.txt usa ranges
# (>=) e já quebrou por resolução nova — o caso documentado no cabeçalho do lock é
# coqui-tts x transformers>=5 ("isin_mps_friendly" removido). Sem o lock, um build
# feito hoje e outro daqui a um mês instalam árvores diferentes, e a imagem deixa
# de ser reprodutível justamente onde ela deveria ser o retrato do que funciona.
# Vale para o llama-cpp-python também: o requirements.txt pede >=0.2.70, então um
# build limpo pegaria a MAIS NOVA — o oposto do que o comentário lá manda fazer
# (o prompt-lookup crashava em contexto longo até a 0.3.34, e subir a versão exige
# passar no eval/retest_speculative.py primeiro). O lock prende em 0.3.34.
# Constraint é INERTE para pacote que ninguém instala, então os pins de origem
# Windows do lock não atrapalham em Linux — e os 206 pins foram conferidos um a um
# na API do PyPI: todos têm artefato para cp310/Linux (201 wheel, 5 sdist).
COPY requirements.txt /tmp/requirements.txt
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH}" \
    FORCE_CMAKE=1 \
    pip install --no-cache-dir -c /tmp/requirements.lock.txt -r /tmp/requirements.txt

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
# O app é o pacote mente_digital/; o entrypoint main.py mora na RAIZ (com guard
# __main__) e importa o pacote por caminho absoluto. Rodado a partir do WORKDIR /app.
CMD ["python", "main.py"]
