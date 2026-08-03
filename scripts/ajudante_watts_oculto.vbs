' Sobe o ajudante de watts SEM janela de console.
'
' Existe porque a Tarefa Agendada roda na sessao INTERATIVA do dono (e nao em
' sessao 0): e de la que o ajudante consegue abrir o processo do jogo para
' fixar a afinidade, que foi como o recurso foi testado. Rodar python.exe
' direto pela tarefa piscaria um console a cada logon.
'
' O projeto ja usa este padrao em mente_digital/inicializacao.py -- .lnk exigiria
' COM/pywin32 (ausente nesta env) e .cmd piscaria o console.
'
' ⚠ ASCII PURO E SEM ACENTO de proposito: o WSH le .vbs como ANSI, e um arquivo
' gravado em UTF-8 com acento chega com byte trocado e falha em silencio.
' ⚠ Caminho com espaco SEM ASPAS falha calado -- o Windows nao reporta erro de
' script de inicializacao em lugar nenhum visivel. Por isso tudo vai entre aspas
' (no VBScript, aspas dentro de string se escapam DOBRANDO-AS).

Option Explicit

Dim python, script, projeto, log, shell, comando

python  = "C:\ProgramData\miniconda3\envs\llama-omni\python.exe"
script  = "D:\projetos\mente_digital\scripts\ajudante_watts.py"
projeto = "D:\projetos\mente_digital"
log     = "D:\projetos\mente_digital\dados\ajudante_watts.log"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = projeto

' -u para o stdout nao ficar preso em buffer de BLOCO: sem ele o log so aparece
' aos pedacos e da para ler uma linha velha achando que e o estado de agora.
comando = "cmd /c """"" & python & """ -u """ & script & """ >> """ & log & """ 2>&1"""

' 0 = janela oculta; False = nao espera (o ajudante e um plantao, nao termina).
shell.Run comando, 0, False
