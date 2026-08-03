"""Testes de `jogo_ativo` — a guarda que solta o kernel quando um jogo abre."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mente_digital import jogo_ativo
from mente_digital.jogo_ativo import Acao, comando_pnputil, decidir, detectar, normalizar


class TestNormalizar:
    def test_baixa_caixa_e_apara(self):
        assert normalizar("  EscapeFromTarkov.EXE ") == "escapefromtarkov.exe"


class TestDetectar:
    def test_acha_tarkov_independente_da_caixa(self):
        # O Windows devolve o nome como o disco guarda; a lista não pode depender disso.
        assert detectar(["chrome.exe", "EscapeFromTarkov.exe"]) == "escapefromtarkov.exe"

    def test_acha_o_arena(self):
        assert detectar(["EscapeFromTarkovArena.exe"]) == "escapefromtarkovarena.exe"

    def test_acha_o_proprio_battleye(self):
        # O serviço do anti-cheat sobe ANTES do jogo. Pegá-lo é o que dá margem
        # para soltar o driver antes de o BattlEye varrer o kernel.
        assert detectar(["BEService.exe"]) == "beservice.exe"

    def test_sem_jogo_devolve_none(self):
        assert detectar(["explorer.exe", "python.exe"]) is None

    def test_lista_vazia(self):
        assert detectar([]) is None

    def test_alvos_customizados_substituem_o_padrao(self):
        assert detectar(["meujogo.exe"], alvos={"MeuJogo.exe"}) == "meujogo.exe"
        assert detectar(["escapefromtarkov.exe"], alvos={"outro.exe"}) is None

    def test_devolve_o_nome_e_nao_um_booleano(self):
        # O motivo vai para o log; "pausei por causa de X" se audita, "detectei
        # um jogo" não.
        assert isinstance(detectar(["EscapeFromTarkov.exe"]), str)


class TestDecidir:
    def test_jogo_abriu_pausa(self):
        v = decidir("escapefromtarkov.exe", pausado=False)
        assert v.acao is Acao.PAUSAR
        assert v.jogo == "escapefromtarkov.exe"
        assert "escapefromtarkov.exe" in v.motivo

    def test_jogo_continua_nao_repete_a_acao(self):
        # Sem isto, cada tique tentaria desabilitar um dispositivo já desabilitado.
        assert decidir("escapefromtarkov.exe", pausado=True).acao is Acao.NADA

    def test_jogo_fechou_retoma(self):
        v = decidir(None, pausado=True)
        assert v.acao is Acao.RETOMAR
        assert v.jogo is None

    def test_sem_jogo_e_medindo_nao_faz_nada(self):
        assert decidir(None, pausado=False).acao is Acao.NADA

    @pytest.mark.parametrize("jogo,pausado", [
        ("x.exe", False), ("x.exe", True), (None, True), (None, False),
    ])
    def test_sempre_tem_motivo(self, jogo, pausado):
        assert decidir(jogo, pausado).motivo.strip()


class TestComandoPnputil:
    def test_desabilitar(self):
        assert comando_pnputil(False) == [
            "pnputil", "/disable-device", r"ROOT\PAWNIO\0000"]

    def test_habilitar(self):
        assert comando_pnputil(True) == [
            "pnputil", "/enable-device", r"ROOT\PAWNIO\0000"]

    def test_nao_usa_sc_stop(self):
        # Medido em 2026-08-03: `sc stop PawnIO` retorna sucesso e o driver
        # continua RUNNING (é dispositivo PnP raiz). Trocar por `sc` parece
        # funcionar e deixa o kernel ocupado no meio da partida.
        for habilitar in (True, False):
            assert comando_pnputil(habilitar)[0] == "pnputil"


class TestAfinidade:
    def test_mascara_e_o_ccd_de_baixo(self):
        # 0xFFFF = CPUs 0-15. Medido: esse CCD perdeu o teste de clock por 11,2%
        # e ganhou o de cache por 44,1%, e no jogo deu +28% de FPS.
        assert jogo_ativo.MASCARA_VCACHE == 0xFFFF
        assert bin(jogo_ativo.MASCARA_VCACHE).count("1") == 16

    def test_anticheat_nao_vai_para_o_vcache(self):
        """A diferença entre as duas listas é deliberada: o BEService dispara a
        PAUSA (sobe antes do jogo, é o gatilho certo para soltar o kernel a
        tempo), mas não renderiza nada — prendê-lo no V-Cache só tiraria núcleo
        de quem precisa."""
        assert "beservice.exe" in jogo_ativo.JOGOS_PADRAO
        assert "beservice.exe" not in jogo_ativo.JOGOS_AFINIDADE
        assert "battleye.exe" not in jogo_ativo.JOGOS_AFINIDADE
        assert jogo_ativo.JOGOS_AFINIDADE < jogo_ativo.JOGOS_PADRAO

    def test_alvos_filtra_por_nome_e_devolve_pid(self):
        procs = [(10, "chrome.exe"), (20, "EscapeFromTarkovArena.exe"),
                 (30, "BEService.exe"), (40, "escapefromtarkov.exe")]
        assert jogo_ativo.alvos_de_afinidade(procs) == [
            (20, "escapefromtarkovarena.exe"), (40, "escapefromtarkov.exe")]

    def test_alvos_sem_jogo(self):
        assert jogo_ativo.alvos_de_afinidade([(1, "explorer.exe")]) == []

    def test_precisa_fixar_compara_mascara(self):
        assert jogo_ativo.precisa_fixar(0xFFFFFFFF, 0xFFFF) is True
        assert jogo_ativo.precisa_fixar(0xFFFF, 0xFFFF) is False

    def test_afinidade_ilegivel_conta_como_precisa(self):
        # Tentar é barato e o pior caso é um erro logado; NÃO tentar deixaria o
        # jogo no CCD errado em silêncio, que é o defeito a corrigir.
        assert jogo_ativo.precisa_fixar(None, 0xFFFF) is True


class TestProcessosEmExecucao:
    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp é do Windows")
    def test_enxerga_o_proprio_interpretador(self):
        nomes = jogo_ativo.processos_em_execucao()
        assert nomes, "nenhum processo listado"
        assert all(n == n.lower() for n in nomes), "nomes devem vir normalizados"
        assert any("python" in n for n in nomes)

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp é do Windows")
    def test_com_pid_traz_o_proprio_processo(self):
        import os

        procs = jogo_ativo.processos_com_pid()
        assert any(pid == os.getpid() for pid, _ in procs)
        assert all(isinstance(pid, int) and pid >= 0 for pid, _ in procs)

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp é do Windows")
    def test_afinidade_do_proprio_processo_e_legivel(self):
        import os

        atual = jogo_ativo.afinidade_atual(os.getpid())
        assert atual is not None and atual > 0

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp é do Windows")
    def test_afinidade_de_pid_inexistente_nao_estoura(self):
        # Fail-soft é contrato: o plantão não pode morrer porque um jogo fechou
        # entre a enumeração e a leitura.
        assert jogo_ativo.afinidade_atual(0x7FFFFFFF) is None
        ok, msg = jogo_ativo.fixar_afinidade(0x7FFFFFFF)
        assert ok is False and msg


class TestPurezaStdlib:
    """Quem importa este módulo roda como ADMINISTRADOR. Um import de `config`
    traria o pydantic — e o `.env` — para dentro do processo elevado."""

    def test_nao_arrasta_pydantic_nem_torch(self, tmp_path):
        raiz = Path(jogo_ativo.__file__).resolve().parent.parent
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(raiz)!r})
            import mente_digital.jogo_ativo  # noqa: F401
            proibidos = [m for m in sys.modules
                         if m.split(".")[0] in {{"pydantic", "torch", "psutil",
                                                 "numpy", "chromadb"}}]
            print(",".join(sorted(proibidos)))
        """)
        arq = tmp_path / "prova_pureza.py"
        arq.write_text(script, encoding="utf-8")
        r = subprocess.run([sys.executable, str(arq)], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "", (
            f"jogo_ativo arrastou dependências pesadas: {r.stdout.strip()}")

    def test_import_nao_toca_wintypes(self, tmp_path):
        """`ctypes.wintypes` NÃO EXISTE fora do Windows — importá-lo no Linux
        levanta ValueError na hora. Com o import no topo do módulo, este arquivo
        quebraria no IMPORT em qualquer runner Linux e derrubaria a suíte
        inteira no CI, enquanto passa verde no Windows do dono. Já custou um CI
        vermelho neste repo com `os.name`/pathlib; a régua é: onde o código toca
        API de plataforma, verde local não prova nada.

        Roda em subprocesso porque outro teste pode já ter importado wintypes.
        """
        raiz = Path(jogo_ativo.__file__).resolve().parent.parent
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(raiz)!r})
            import mente_digital.jogo_ativo as j
            print("VAZOU" if "ctypes.wintypes" in sys.modules else "ok")
            # e as funções puras têm de responder sem nunca tocar no Windows
            assert j.detectar(["EscapeFromTarkov.exe"]) == "escapefromtarkov.exe"
            assert j.comando_pnputil(False)[1] == "/disable-device"
        """)
        arq = tmp_path / "prova_wintypes.py"
        arq.write_text(script, encoding="utf-8")
        r = subprocess.run([sys.executable, str(arq)], capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ok", (
            "importar jogo_ativo puxou ctypes.wintypes — isso quebra o CI no Linux")
