# Watts na faixa do app — o que já funciona e o que exige a sua mão

O chip de energia da janela passou a mostrar **watts** ao lado de VRAM e RAM.

| Número | Precisa de quê | Estado |
|---|---|---|
| **Watt da GPU** | nada | **já funciona** — a `nvml.dll` vem com o driver NVIDIA e o servidor a lê por ctypes |
| **Watt da CPU** | um driver em modo kernel | **depende de você** — os passos abaixo |

Se você não fizer nada, o app continua igual: o watt da GPU aparece, o da CPU
vem vazio, e nada quebra. O campo nunca sai como `0 W` — vazio é vazio, e "medi
e não há consumo" é uma afirmação que este projeto não faz por engano.

---

## Por que a CPU exige driver (e por que o servidor não pode ser o elevado)

No Windows não existe caminho em modo usuário para a potência do pacote da CPU.
Seis foram medidos em 2026-08-03 e os seis vieram vazios: `Win32_PerfFormatted
Data_PowerMeterCounter_EnergyMeter` (sem instância), `CIM_PowerSupply` (sem
instância), `CallNtPowerInformation` (sem campo de potência), `root\WMI` (sem
classe de energia), `psutil` (sem API no Windows) e `kernel32.ReadMsr` /
`ntdll.NtReadMsr` (não existem). Sobra o MSR `0xC001029B` via **RDMSR**, que é
instrução privilegiada.

O `main.py` **não** vira o processo elevado. Ele escuta em `0.0.0.0`, tem rota
que escreve no vault e é alcançado pelo celular; rodá-lo como administrador para
ler um watt trocaria um número por uma superfície de ataque. Em vez disso há um
**ajudante mínimo** (`scripts/ajudante_watts.py`) que é a única coisa elevada da
máquina: sem porta, sem rota, sem parser. Ele mede e **escreve** um arquivo; o
servidor **lê**. Nada que o lado sem privilégio produza chega ao lado com
privilégio.

---

## Os passos

> Um bloco por passo, na ordem. Cada um traz o risco em uma linha.
> **Nada aqui foi executado por mim** — os passos 1, 2, 4 e 5 mudam a máquina ou
> instalam software, e essa decisão é sua.

### Passo 0 — a checagem que decide se o passo 4 pode funcionar

Antes de tudo, veja se o Windows vai deixar o driver subir (leitura de registro,
não muda nada):

```powershell
(Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" -EA SilentlyContinue).Enabled
```

Vazio ou `0` = Integridade de Memória desligada, o driver sobe. `1` = **o passo 4
vai falhar** e você precisa decidir se quer desligar o Isolamento de Núcleo (é uma
proteção real; não desligue só por um número na tela).

> Medido nesta máquina em 2026-08-03: vazio — não há bloqueio.

### ~~Passo 1 — instalar a ponte para .NET~~ (JÁ FEITO nesta máquina)

```powershell
C:\ProgramData\miniconda3\envs\llama-omni\python.exe -m pip install pythonnet
```

O `pythonnet` **já está instalado** na env `llama-omni` (conferido em 2026-08-03),
e ele carrega o **.NET Framework 4.x** — o que decide qual zip pegar no passo 2.

**Risco:** baixo. Pacote comum do PyPI, entra só na env `llama-omni`, e o
**servidor nunca o importa** — quem usa é o ajudante. Não entra em
`requirements.txt` nem no CI de propósito.

### ~~Passo 2 — pegar a DLL~~ (JÁ FEITO — as DLLs estão em `dados\lhm\`)

Feito em 2026-08-03 a partir da release **oficial v0.9.6**
(`github.com/LibreHardwareMonitor/LibreHardwareMonitor`), asset
`LibreHardwareMonitor.zip`, SHA-256 do zip
`086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001`.

⚠ **Duas correções em relação ao que este doc dizia antes**, e as duas custam
tempo se descobertas na hora:

1. **O asset `LibreHardwareMonitor-net472.zip` não existe mais.** A v0.9.6 publica
   `LibreHardwareMonitor.zip` (.NET Framework — **este**) e
   `LibreHardwareMonitor.NET.10.zip` (**errado** para esta env: o `pythonnet` daqui
   carrega o .NET Framework 4.x, conferido).
2. **Copiar "só o `LibreHardwareMonitorLib.dll`" NÃO funciona.** Ele depende de
   assemblies de apoio, e a falha aparece só quando o sensor é lido:
   `Não foi possível carregar … 'System.Memory, Version=4.0.5.0'`. São **8**
   arquivos em `dados\lhm\`:

   ```
   LibreHardwareMonitorLib.dll          HidSharp.dll
   System.Memory.dll                    Microsoft.Bcl.HashCode.dll
   System.Buffers.dll                   System.Numerics.Vectors.dll
   System.Runtime.CompilerServices.Unsafe.dll
   System.Threading.Tasks.Extensions.dll
   ```

A `LibreHardwareMonitorLib.dll` **não é assinada** — o normal para a lib
gerenciada desse projeto; quem precisa de assinatura é o `.sys` que ela extrai,
senão o próprio Windows recusa carregar. A garantia aqui é a ORIGEM (release
oficial por HTTPS) e o hash acima.

**Risco:** é binário de terceiro que vai rodar elevado no passo 4 — pegue da
página de releases oficial e de nenhum espelho.

### Passo 3 — conferir SEM elevação (nada muda na máquina)

```powershell
C:\ProgramData\miniconda3\envs\llama-omni\python.exe scripts\ajudante_watts.py --uma-vez
```

Com as DLLs já no lugar, o esperado agora é:

```
[WATTS] nenhum sensor de potência da CPU respondeu — quase sempre é falta de
privilégio (o driver não subiu) ou CPU sem o sensor exposto
```

É **exatamente** o que se quer ver: significa que a cadeia de DLLs está completa e
o único bloqueio que resta é a elevação. (Se aparecer `LibreHardwareMonitorLib.dll
não encontrada`, falta o passo 2; se aparecer `não consegui carregar … System.
Memory`, faltam as assemblies de apoio do passo 2.)

**Risco:** nenhum. Este passo não eleva nada e não carrega driver nenhum.

### Passo 4 — a primeira medição de verdade (aqui o driver sobe)

Abra o PowerShell **como administrador** e rode:

```powershell
C:\ProgramData\miniconda3\envs\llama-omni\python.exe D:\projetos\mente_digital\scripts\ajudante_watts.py --uma-vez
```

Esperado: `[WATTS] CPU Package: 142.3 W  ->  ...\dados\potencia_cpu.json`

**Risco — o maior de todos, leia antes de rodar:** a LibreHardwareMonitorLib
carrega o **`WinRing0x64.sys`**, um driver em modo kernel que expõe leitura e
escrita de MSR e de portas de I/O a quem falar com ele. Enquanto ele estiver
carregado, qualquer processo local capaz de abrir esse dispositivo tem um
primitivo de escalada de privilégio. É por isso que este passo é seu e não meu, e
por isso o passo 6 (desligar) existe. Se o **Isolamento de núcleo / Integridade
de memória** estiver ligado no Windows, o driver provavelmente será **bloqueado**
— e aí o passo falha com uma mensagem, sem estrago.

### Passo 5 — deixar de plantão (opcional)

```powershell
C:\ProgramData\miniconda3\envs\llama-omni\python.exe D:\projetos\mente_digital\scripts\ajudante_watts.py
```

Também no PowerShell elevado. Ele publica a cada 2 s e imprime uma linha ao
subir; `Ctrl+C` encerra. Para subir no logon, crie uma tarefa no Agendador de
Tarefas com "Executar com privilégios mais altos" apontando para o mesmo comando.

**Risco:** deixa o `WinRing0` carregado o tempo todo, em vez de só durante um
teste — é o passo 4 permanente. Uma tarefa de logon elevada também é um alvo:
se alguém puder editar o comando dela, ganha execução como administrador.

### Passo 6 — desligar / desfazer

Feche o ajudante (`Ctrl+C`), remova a tarefa do Agendador se criou uma, e apague
`dados\lhm\`. O servidor volta a mostrar o watt da CPU vazio em até 15 segundos
(é o prazo de validade da publicação) e nada mais muda. Opcional:
`pip uninstall pythonnet`.

---

## Como saber se funcionou

- **Na janela:** o chip de energia passa de `5.2 GB VRAM · 7.1 GB RAM · 96/320 W GPU`
  para `... · 96/320 W GPU + 142 W CPU`.
- **Pela API:** `POST /api/energia` com `{"acao": "estado"}` — o campo
  `cpu_watts` deixa de ser `null`.

O til (`~96 W`) marca leitura **instantânea** — só a primeira depois de o app
subir. Sem til, o número é a **média dos ~20 s** desde a leitura anterior, medida
por integral de energia. Numa placa que salta de 96 W parada para 320 W
decodificando, os dois contam histórias diferentes.

---

## Quando não aparecer: o campo `cpu_watts_motivo` responde

A resposta de `/api/energia` traz o motivo justamente para você não ter de
reencenar a instalação inteira:

| `cpu_watts_motivo` | O que é | O que fazer |
|---|---|---|
| `ausente` | o ajudante nunca publicou (não está de pé) | passo 4 ou 5 |
| `vencido` | ele publicou e **parou** — morreu ou travou | veja a janela dele; o número não fica congelado na tela de propósito |
| `invalido` | o arquivo existe e não presta | apague `dados\potencia_cpu.json` e rode o passo 4 |

---

## ⚠ O rótulo é honesto de propósito: estes watts NÃO são "o assistente"

O watt da GPU é o do **dispositivo**, não deste processo. Quando isto foi medido,
**22 programas** tinham contexto na GPU — o compositor do Windows, o navegador,
o reprodutor de música, a plataforma de jogos. Os 96 W de repouso são de todos
eles somados. Ler "96 W" logo depois de "VRAM" e concluir "o assistente gasta
96 W parado" é falso, e é por isso que o tooltip do próprio número diz isso.

O watt da CPU é o mesmo caso, um degrau pior: é o **pacote inteiro**, todos os
núcleos e o IO die, de tudo que estiver rodando na máquina.

É a mesma ressalva que o `energia.py` já documenta para a VRAM. Se um dia a
pergunta for "quanto o assistente consome", a resposta exige medir com o
assistente parado e com ele decodificando, e reportar a **diferença** — não este
número.

---

## O que ficou por medir

- **O caminho da CPU nunca rodou.** Não instalei `pythonnet`, não baixei a DLL e
  não elevei nada — R2. O que está provado é a lógica em volta (escolha do
  sensor, publicação atômica, prazo de validade, leitura defensiva, e o ajudante
  saindo com mensagem quando a lib falta, que rodei de verdade). A conversa com
  a LibreHardwareMonitorLib em si só será exercitada no seu passo 4.
- **O nome do sensor no 7950X3D.** O código prefere quem se chame "Package" e
  cai para quem fale de "CPU"; se a sua placa expuser outro nome, o rótulo na
  tela vai dizer qual sensor foi usado — confira que não é `CPU Cores`, que
  **exclui o IO die** e daria um número plausível e errado.
