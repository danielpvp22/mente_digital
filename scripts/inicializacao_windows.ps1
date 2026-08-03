<#
.SYNOPSIS
    Faz o Mente Digital subir junto com o Windows (e desfaz).

.DESCRIPTION
    Registra uma TAREFA AGENDADA que dispara no logon. Por que tarefa agendada e
    nao atalho na pasta Inicializar nem servico do Windows:

      - Servico esta ERRADO por construcao: servico nao tem sessao de desktop, e o
        app abre uma janela. Ele subiria e a janela nunca apareceria.
      - Atalho na pasta Inicializar funciona, mas nao sabe atrasar o inicio nem
        reiniciar se cair, e nao roda escondido sem gambiarra de VBScript (que a
        Microsoft esta descontinuando).
      - Tarefa agendada faz os tres nativamente.

    O atraso de 40s existe por medicao: o boot carrega ~5 GB para a GPU, e disputar
    isso com o resto do logon do Windows atrasa os dois. Melhor deixar a area de
    trabalho assentar primeiro.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\inicializacao_windows.ps1
    powershell -ExecutionPolicy Bypass -File scripts\inicializacao_windows.ps1 -Remover
#>
param(
    [switch]$Remover,
    [int]$AtrasoSegundos = 40,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$NomeTarefa = "Mente Digital"
$Raiz = Split-Path -Parent $PSScriptRoot

if ($Remover) {
    if (Get-ScheduledTask -TaskName $NomeTarefa -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $NomeTarefa -Confirm:$false
        Write-Host "Removido: '$NomeTarefa' nao sobe mais com o Windows." -ForegroundColor Green
    } else {
        Write-Host "Nada a remover -- a tarefa '$NomeTarefa' nao existe." -ForegroundColor Yellow
    }
    return
}

# pythonw.exe, nao python.exe: o 'w' e o que evita a janela preta de console
# aparecendo junto do app. O do PATH do Windows costuma ser o atalho falso da
# Microsoft Store, entao procuramos o do ambiente conda do projeto primeiro.
if (-not $Python) {
    $candidatos = @(
        "C:\ProgramData\miniconda3\envs\llama-omni\pythonw.exe",
        "$env:USERPROFILE\miniconda3\envs\llama-omni\pythonw.exe",
        "$env:USERPROFILE\anaconda3\envs\llama-omni\pythonw.exe"
    )
    $Python = $candidatos | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "Nao achei o pythonw.exe do ambiente llama-omni. Passe o caminho: -Python C:\...\pythonw.exe"
}
if (-not (Test-Path (Join-Path $Raiz "app.py"))) {
    throw "Nao achei app.py em '$Raiz'. Rode este script de dentro de scripts\ do projeto."
}

Write-Host "Python : $Python"
Write-Host "Projeto: $Raiz"

$acao = New-ScheduledTaskAction -Execute $Python -Argument "app.py" -WorkingDirectory $Raiz
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$gatilho.Delay = "PT${AtrasoSegundos}S"
$config = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # sem teto: o app roda o dia inteiro
# Interactive, nao ServiceAccount: a janela precisa da sessao de desktop do dono.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho `
    -Settings $config -Principal $principal -Force `
    -Description "Assistente local Mente Digital -- sobe no logon, janela nativa." | Out-Null

Write-Host ""
Write-Host "Pronto. O Mente Digital sobe ${AtrasoSegundos}s apos o logon." -ForegroundColor Green
Write-Host "Testar agora sem reiniciar:  Start-ScheduledTask -TaskName '$NomeTarefa'"
Write-Host "Desfazer:                    ...\inicializacao_windows.ps1 -Remover"
