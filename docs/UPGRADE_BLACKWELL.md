# Upgrade da toolchain CUDA para Blackwell (sm_120 / RTX 50xx)

Roteiro para fazer a stack deste projeto rodar numa GPU Blackwell (RTX 5070/5080/5090,
arquitetura `sm_120`), partindo do estado de **2026-07-29** (RTX 3080, `sm_86`).

> **A ideia que organiza tudo:** este upgrade pode ser **inteiramente preparado e
> verificado com a 3080 ainda na máquina**, antes de a placa nova chegar. Você compila
> os binários para `86-real;120a-real` (as duas arquiteturas no mesmo artefato), o
> `verificar_stack_cuda.py` prova que o código `sm_120` está lá dentro, e o app +
> a suíte provam que nada regrediu em `sm_86`. Só então trocar a placa.
> Isso transforma "compra e reza" em "prepara, verifica, compra".

---

## 0. Por que não é só instalar driver

Quatro binários **independentes** carregam kernels CUDA compilados por arquitetura, e
nenhum deles vinha com `sm_120` (medido por parse de fatbin, não por changelog):

| Componente | Estado em 2026-07-29 | Caminho de JIT? |
|---|---|---|
| `llama-cpp-python` 0.3.34 (wheel CUDA 12.4) | cubins `sm_60`…`sm_90`, PTX só `sm_90` | **Sim** (139/139 fatbins JITaram p/ `sm_120` neste driver) |
| `torch` 2.5.1+cu121 | cubins até `sm_90`, **zero PTX** | **Não** — bloqueio duro |
| `ctranslate2` 4.8.1 | cubins até `sm_86`, PTX `sm_86` | Sim, mas o **cuBLAS** é o elo fraco |
| `llama-server` do OCR (`llama-b10107`) | cubins `sm_86`/`sm_89`, CUDA 12.4 | Sim, degradado |
| `cuBLAS` 12.4.5 | — | **Não** — libs math da NVIDIA não são forward-compatible |

**O sintoma de falha é silencioso.** `audio.py` só arma o pára-quedas de CPU dentro do
`load`; o `transcribe` faz `telemetry.error` + `return ""`, e `ws.py:371` descarta texto
com menos de 3 chars sem avisar. O app sobe "saudável" e o microfone fica mudo para
sempre. É por isso que cada passo aqui tem um **gate verificável**.

---

## 1. Princípio de segurança: env nova, produção intacta

**Nunca faça este upgrade na env `llama-omni`.** Ela é a produção e o seu rollback.
Crie uma env paralela e só promova depois do aceite:

```bash
conda create -n mente-blackwell python=3.10 -y
```

O canal conda `pytorch` **morreu no 2.5.1** e nunca teve CUDA 12.8 (verificado na API do
Anaconda: `latest_version` 2.5.1, `pytorch-cuda` para no 12.4). Este upgrade **abandona o
torch do conda** e migra para o índice pip `cu128` — é o único ponto que merece a palavra
"risco", e é risco de migração de ambiente, com caminho conhecido e reversível.

---

## 2. Passo 0 — baseline (faça antes de tocar em nada)

```bash
python scripts/verificar_stack_cuda.py --arch 120 --json docs/stack_antes.json
```

Esperado hoje: **FAIL**, com `torch` e `cuBLAS` como `NAO COBERTO` e os outros como `JIT`.
Guarde o JSON. No fim, `diff` contra o `depois` é a prova do upgrade.

Rode também o controle positivo, que deve passar:

```bash
python scripts/verificar_stack_cuda.py --arch 86
```

E registre o estado funcional atual, para saber o que "não regrediu" significa:

```bash
python -m pytest -q
```

---

## 3. Passo 1 — limpar a armadilha de PATH (**faça antes de tudo**)

`D:\projetos\llama-omni\llamacpp` está no seu `PATH` e fornece `cudart64_12.dll` e
`cublas64_12.dll` na **versão 12.4** (março/2024). No Windows a DLL é resolvida por
**nome** e o módulo é **global ao processo**: quem carrega primeiro define para todos.
Essa pasta **vence o conda** — se ela ficar no `PATH`, você faz todo o rebuild e ele
**não pega**, de forma silenciosa.

Essa mesma pasta tem uma segunda instalação de llama.cpp (`ggml-cuda.dll` de 576 MB,
também sem `sm_120`). Se algum script seu usa esses binários, ele precisa do mesmo upgrade.

**Ação:** tire a pasta do `PATH` (ou atualize as DLLs dela para ≥12.8).

**Gate:** depois de ajustar, confirme de qual caminho as DLLs saem:

```bash
python scripts/verificar_stack_cuda.py --arch 120 --json docs/stack_pos_path.json
```

A linha `cuBLAS / cudart carregados no processo` mostra o caminho real de cada DLL.

---

## 4. Passo 2 — CUDA Toolkit 12.8 ou 12.9 (**não 13.x**)

`sm_120` só existe a partir do **CUDA 12.8**. E **evite 13.x**: há segfault documentado de
MMQ em Blackwell, e o wheel atual do llama-cpp tem `GGML_CUDA_FORCE_MMQ=ON` (o script
avisa disso). Hoje não existe nenhum `nvcc` nesta máquina (`CUDA_PATH` vazio, sem
`cuda-nvcc` no conda-meta), então compilar é impossível sem instalar o toolkit.

Instale o CUDA Toolkit 12.8/12.9 + Visual Studio Build Tools (workload C++).

**Gate:**

```bash
nvcc --version
```

Deve reportar 12.8 ou 12.9. Se disser 13.x, você instalou a versão errada.

---

## 5. Passo 3 — torch 2.8.0+cu128 (**não 2.9+**)

O `--index-url` **não é opcional**: sem ele o wheel padrão de Windows é CPU-only e o XTTS
cai para CPU em silêncio.

```bash
pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0+cu128 torchaudio==2.8.0+cu128 torchvision==0.23.0+cu128
```

**Por que 2.8.0 e não a mais nova:** `coqui-tts` 0.27.5 tem gate **duro** em
`TTS/__init__.py` — a partir de `torch>=2.9` ele exige `torchcodec`, que não está
instalado, não está no `requirements.txt` e não tem wheel no índice `cu128` para Windows.
O 2.8.0 é o maior `<2.9`, tem `cp310-win_amd64`, e o build script oficial de Windows
(`.ci/pytorch/windows/cuda128.bat`) lista `8.6` **e** `12.0` — **as duas placas no mesmo
wheel**.

Boa notícia já verificada: a quebra clássica de XTTS com `torch>=2.6` (o flip de
`weights_only=True`) **já está paga** no coqui 0.27.5 — ele chama `add_safe_globals` para
`XttsConfig`/`XttsAudioConfig`/`XttsArgs`/`RAdam` e passa `weights_only=is_pytorch_at_least_2_4()`.

**Gate:**

```bash
python -c "import torch; print(torch.__version__, torch.version.cuda); print(torch.cuda.get_arch_list())"
```

A lista **precisa** conter `sm_120` **e** `sm_86`.

---

## 6. Passo 4 — llama-cpp-python com as duas arquiteturas

`0.3.34` é a **última versão no PyPI** — não existe "bump de versão" que resolva. O
problema é o **build**, não o número. Duas rotas:

**Rota A — wheel pré-compilada `cu128`.** O índice oficial do `abetlen` **não** publica
`cu128`; existem índices comunitários para Windows/py3.10 (não auditados). Se achar um,
valide com o gate abaixo antes de confiar.

**Rota B — compilar local** (a receita que o `README.md:191-194` já documenta, só mudando
a lista de arquiteturas):

```bash
CMAKE_ARGS="-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=86-real;120a-real" FORCE_CMAKE=1 pip install --force-reinstall --no-cache-dir --no-binary llama-cpp-python llama-cpp-python==0.3.34
```

> Em PowerShell: `$env:CMAKE_ARGS="..."; $env:FORCE_CMAKE="1"` antes do `pip install`.
> Se for **vender a 3080** e ficar só com a placa nova, use `-DCMAKE_CUDA_ARCHITECTURES=120`
> — mais simples e mais rápido de compilar.

**Corrija a documentação enquanto está aqui:** `requirements.lock.txt:5-6` e
`README.md:194` afirmam que o llama-cpp-python foi compilado local com
`-DCMAKE_CUDA_ARCHITECTURES=86`. O binário instalado **refuta** isso — é wheel de CI do
GitHub Actions (caminho de build `D:/a/llama-cpp-python/...`), multi-arch com 8
arquiteturas e `FORCE_MMQ=ON`, flag que a receita do README não passa. Quem seguir o
README para "reproduzir a env" produz um binário diferente do que roda hoje.

**Gate:**

```bash
python -c "import llama_cpp; print(llama_cpp.llama_cpp.llama_print_system_info())" 2>&1 | grep -o "ARCHS = [0-9,]*"
```

Deve listar `860` e `1200`.

**Gate extra, já documentado no projeto:** como o upgrade força um binário novo de
qualquer jeito, é o momento de rodar o gate do speculative decoding — e ele exige env
isolada, nunca a produção:

```bash
python eval/retest_speculative.py
```

PASS exige nenhum crash no prompt longo e `tok/s(spec) >= tok/s(baseline)` em algum
regime. Ver o docstring de `eval/retest_speculative.py` para o histórico do crash de shape.

---

## 7. Passo 5 — cuBLAS ≥12.8 onde o CTranslate2 enxerga

`ctranslate2` 4.8.1 e `faster-whisper` 1.2.1 são as **últimas do PyPI** e não têm cubin
`sm_120` — o caminho é PTX JIT, e isso é design upstream, não "por enquanto". O CT2 usa
apenas 9 símbolos legados de cuBLAS e **zero** de `cublasLt`, ABI estável em todo o 12.x,
então trocar o cuBLAS é substituição limpa, sem recompilar nada.

**Atenção — `pip install nvidia-cublas-cu12` NÃO resolve.** Verificado no código:
`site-packages/ctranslate2/__init__.py:13-21` faz `os.add_dll_directory` **só do próprio
package_dir** e depois `ctypes.CDLL` em todo `*.dll` de lá. O diretório do pacote pip da
NVIDIA nunca entra na resolução.

**O que funciona de forma determinista:** copiar `cublas64_12.dll` e `cublasLt64_12.dll`
(≥12.8, do toolkit do Passo 2) **para dentro de** `site-packages/ctranslate2/`. O glob do
`__init__` pré-carrega e o `LoadLibrary` posterior do CT2 reusa o módulo já carregado.

Efeito colateral **benigno** a esperar: com o guard de `sm_120`, o
`MENTE_WHISPER_COMPUTE_TYPE=int8` do seu `.env` é auto-convertido para `float16` com log —
mais VRAM, e em Blackwell provavelmente mais rápido. Não é falha.

**Gate:**

```bash
python scripts/verificar_stack_cuda.py --arch 120
```

A linha do cuBLAS deve virar `COBERTO`.

---

## 8. Passo 6 — binário do OCR (Fase 3)

O quarto binário CUDA não vem do pip: `MENTE_OCR_BIN` aponta para
`C:\Users\User\Downloads\llama-b10107\llama-server.exe` (o comentário do `.env:175` diz
"b9977", divergente do caminho — corrija ao passar por aqui). Ele tem cubins só
`sm_86`/`sm_89` e traz `cudart64_12.dll` / `cublas64_12.dll` **12.4** na própria pasta.

E aqui há uma pegadinha específica: `ocr.py:210` faz `subprocess.Popen(..., cwd=Path(comando[0]).parent)`
**sem `env=`**. No Windows o diretório do EXE vence o `PATH`, logo **consertar o cuBLAS da
env conda não conserta o OCR** — as DLLs da pasta do executável mandam nesse subprocesso.

**Ação:** baixe uma release do llama.cpp com CUDA **12.8+** (é download, não compilação),
aponte `MENTE_OCR_BIN` para ela e confirme que as DLLs CUDA da nova pasta são ≥12.8.

**Gate:** o script audita esse binário junto com os outros e lista as DLLs locais que
vencem o PATH:

```bash
python scripts/verificar_stack_cuda.py --arch 120 --json docs/stack_depois.json
```

---

## 9. Gate final — aceite

1. **Cobertura medida** (o diff que é a prova do upgrade):

```bash
python scripts/verificar_stack_cuda.py --arch 120 --json docs/stack_depois.json
```

Precisa sair **PASS** e exit 0. Compare com `docs/stack_antes.json`.

2. **Nada regrediu em `sm_86`** — ainda com a 3080 na máquina:

```bash
python scripts/verificar_stack_cuda.py --arch 86
```

3. **A suíte continua verde:**

```bash
python -m pytest -q
```

4. **O app sobe inteiro** — e este é o teste que pega a falha silenciosa. Não basta o
   servidor responder: confirme os quatro serviços de GPU, um por um.

```bash
python main.py
```

- `/api/metrics` respondendo;
- **fale no microfone** e veja se a transcrição volta com conteúdo (o modo de falha do
  STT é devolver `""` para sempre, sem erro visível);
- **ouça uma resposta** (XTTS com `ready=True`; se falhar, o app fica sem voz mas não cai);
- **rode um ciclo de OCR** (`python scripts/ocr_agora.py`), que exercita o quarto binário;
- confira que `prefill_ms` e `decode_tok_s_gpu` estão sendo gravados em
  `metricas_latencia` — se o `vram_peak_mb` vier nulo, algo mudou de device.

5. **Só então** promova: renomeie/aponte a env de produção e atualize
   `requirements.lock.txt` com o que de fato ficou instalado (`pip freeze`).

---

## 10. Rollback

A env `llama-omni` continua intacta durante todo o processo — é o rollback. Se algo der
errado, volte a apontar para ela.

O único passo com efeito **fora** da env nova é o **Passo 1** (mexer no `PATH`) e o
**Passo 6** (`MENTE_OCR_BIN`). Anote o valor original dos dois antes de mudar:

```bash
echo "$PATH" > docs/path_antes.txt
```

---

## 11. O que este roteiro NÃO garante

Seja explícito com você mesmo aqui — nada abaixo foi medido em hardware Blackwell, porque
não existe nenhum nesta máquina:

- **O JIT foi provado *compilar*, não *rodar correto*.** A medição real: os 139 fatbins do
  `ggml-cuda.dll` foram passados ao compilador JIT do driver (`cuLinkAddData` com
  `CU_JIT_TARGET=120`) e **139/139 compilaram**, com o `e_flags` do cubin gerado
  confirmando `sm_120` e o controle negativo (target 130) sendo rejeitado. Isso prova que
  existe caminho. Não prova que cada kernel produz o resultado certo.
- **Há bugs `sm_120` conhecidos no ggml recente**: store fora de faixa no epílogo MMA do
  `mul_mat_q` Q8_0 e Xid 43 no `flash_attn_stream_k_fixup`. Compilar com cubin nativo
  (`120a-real`) em vez de depender do JIT reduz a exposição, não a elimina.
- **Custo do JIT não medido em segundos.** No pior caso absoluto medido, compilar os 932 MB
  inteiros levou 300 s — mas o CUDA usa lazy module loading e o driver tem cache de JIT
  persistente, então na prática é parcial e pago uma vez. Se você compilar nativo, não paga
  nada disso.
- **Perda de performance no caminho de fallback é grande**: medições públicas em 5090 dão
  ~5,7× no prompt entre o caminho MMQ nativo e o caminho cuBLAS. Mais uma razão para
  compilar nativo em vez de aceitar o JIT.
- **O XTTS não foi executado sobre torch 2.7/2.8** — só os gates de versão foram lidos no
  fonte. Como o XTTS é opt-in e fail-soft, uma regressão ali custa a voz, não o app.
- **Se você ficar com as DUAS placas**, este roteiro não é suficiente: há 11 pontos do repo
  que fixam o device 0 (o pior: `llm.py:248` não passa `main_gpu`/`split_mode`, e o default
  do llama.cpp é fatiar o modelo entre todas as GPUs visíveis). Ver a memória
  `blackwell-5080-toolchain-e-device0`.
