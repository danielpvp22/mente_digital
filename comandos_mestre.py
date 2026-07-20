"""
Os COMANDOS do plano-mestre — a metade "age" do Agent.

Mixin com o `_fluxo_mestre` (a máquina de estados da palavra-mestre: expansão de
atalho, meta-comandos de sessão — desfazer/corrigir/confirmar/confidencial —,
parse rápido composto sem LLM e a queda controlada pro roteador) e TODOS os
executores de comando das três ondas: agenda/listas, SRS, hábitos, rotinas,
gatilhos, pomodoro, navegação, fio, ajuda, revisão diária, conexões,
contradições e auditoria de fontes.

Extraído VERBATIM do agent.py na modularização — mixin de propósito: os métodos
continuam sendo o MESMO objeto Agent em runtime (self._falar, self._rotear e o
estado vivem no núcleo), só o arquivo mudou. Nada aqui roda sem um Agent.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional, Tuple

import agenda
import calendario
import diapasao
import fio
import grafo
import habitos
import mestre
import srs
import textutils
import tools
import verbosidade
from config import settings
from state import SessionMemory
from telemetry import LatencyTracker, db, telemetry

Sender = Callable[[dict], Awaitable[bool]]


class ComandosMestre:
    async def _executar_acoes_rapidas(
        self, acoes: List["tools.Decisao"], send: Sender, auditar: bool = True,
        mem: Optional[SessionMemory] = None, prefixo: str = "",
    ) -> str:
        """Executa uma ou mais ferramentas determinísticas e FALA o resultado direto —
        sem passar pelo LLM (o texto de retorno da ferramenta já é amigável). É o que
        torna 'mestre, põe pão na lista' instantâneo e barato.

        `auditar` (False em modo confidencial) controla o registro na trilha (#27).
        `mem` (quando dado) recebe a reversão desta ação p/ o "mestre, desfaça" (#8);
        `prefixo` prefixa a fala (ex.: "Desfeito. " ao executar a própria reversão)."""
        resultados: List[str] = []
        executadas: List[tuple] = []   # (Decisao, resultado) p/ computar a reversão
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is None:
                continue
            try:
                obs = await tool.executar(dec.args, self.ctx)
            except Exception as exc:
                telemetry.error("MESTRE", f"Falha na ferramenta '{dec.tool}'", exc)
                obs = f"não consegui executar {dec.tool}"
            if auditar and tool.auditavel:
                await asyncio.to_thread(db.registrar_auditoria, dec.tool, obs)
            resultados.append(obs)
            executadas.append((dec, obs))
        self._lembrar_reversao(mem, executadas)
        final = prefixo + (" ".join(r for r in resultados if r) or "Pronto.")
        await self._emitir_falado(send, final)
        # GATILHOS (#11): um item adicionado à lista é um EVENTO interno — dispara as regras
        # que casam. Rodam DEPOIS da fala principal e por um caminho direto (sem re-emitir).
        for dec, obs in executadas:
            if dec.tool == "adicionar_item" and textutils.normaliza(obs or "").startswith("adicionei"):
                await self._disparar_gatilhos("lista_add", str(dec.args.get("item", "")), send)
        return final

    def _lembrar_reversao(self, mem: Optional[SessionMemory], executadas: List[tuple]) -> None:
        """Guarda na sessão as ações que DESFAZEM o que acabou de rodar (#8).

        Só sobrescreve o alvo de undo quando há de fato algo reversível — assim uma
        leitura (ler_lista) ou uma ação que falhou não apaga o undo pendente anterior."""
        if mem is None or not executadas:
            return
        rev = mestre.reverter(executadas)
        if rev:
            mem.ultima_reversivel = rev
            # guarda também as ações FORWARD p/ o "corrige" refazer com o valor certo (#9).
            mem.ultima_acao = [dec for dec, _ in executadas]

    async def _desfazer(self, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Executa a reversão da última ação da sessão (#8). Devolve (fala, rota)."""
        revs = mem.ultima_reversivel
        if not revs:
            fala = "Não tenho nenhuma ação recente para desfazer."
            await self._emitir_falado(send, fala)
            return fala, "mestre:desfazer_vazio"
        # Consome ANTES de executar: a própria reversão não vira novo alvo de undo
        # (mem=None abaixo), então "desfaça, desfaça" não fica num ping-pong.
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        fala = await self._executar_acoes_rapidas(
            revs, send, auditar=auditar, mem=None, prefixo="Desfeito. "
        )
        return fala, "mestre:desfazer"

    async def _corrigir(self, comando: str, send: Sender, mem: SessionMemory, auditar: bool) -> tuple:
        """Corta-e-corrige (#9): desfaz a última adição e refaz com o valor certo.

        "mestre, corrige para leite" (após "adiciona pão") = remover pão + adicionar leite.
        Reaproveita a reversão do #8 (undo) e refaz a ação forward trocando o valor."""
        certo = mestre.parse_correcao(comando)
        if not certo:
            fala = "Não entendi a correção. Diga, por exemplo, 'corrige para leite'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_incompleto"
        redo = mestre.refazer_com(mem.ultima_acao or [], certo)
        if not redo:
            fala = "Não tenho uma ação recente de lista para corrigir."
            await self._emitir_falado(send, fala)
            return fala, "mestre:corrige_vazio"
        undo = list(mem.ultima_reversivel or [])
        mem.ultima_reversivel = None
        mem.ultima_acao = None
        # Executa undo + redo SEM registrar reversão automática (mem=None): abaixo
        # definimos o alvo de undo manualmente como só o REDO — assim correções
        # ENCADEADAS ("corrige para leite", depois "para água") ficam limpas, sem
        # ressuscitar o valor original a cada nova correção.
        fala = await self._executar_acoes_rapidas(
            undo + list(redo), send, auditar=auditar, mem=None, prefixo="Corrigido. "
        )
        mem.ultima_acao = list(redo)
        mem.ultima_reversivel = [tools.Decisao("remover_item", dict(d.args)) for d in redo]
        return fala, "mestre:corrige"

    async def _atalhos_cache(self) -> dict:
        """Atalhos-mestre (#2) carregados do DB uma vez por processo (apelido -> comando)."""
        if self._atalhos is None:
            self._atalhos = await asyncio.to_thread(db.listar_atalhos)
        return self._atalhos

    async def _criar_atalho(self, nome: str, send: Sender, mem: SessionMemory) -> tuple:
        """Grava um atalho nomeado para o ÚLTIMO comando-mestre resolvido (#2)."""
        alvo = mem.ultimo_comando_mestre
        if not alvo:
            fala = "Não tenho um comando recente para transformar em atalho."
            await self._emitir_falado(send, fala)
            return fala, "mestre:atalho_vazio"
        chave = textutils.normaliza(nome)
        await asyncio.to_thread(db.salvar_atalho, chave, alvo)
        # Já tem atalho -> não ofereça atalho de novo para essa intenção.
        await asyncio.to_thread(db.marcar_sugerido, textutils.normaliza(alvo))
        (await self._atalhos_cache())[chave] = alvo
        fala = f"Pronto. Agora é só dizer '{settings.palavra_mestre}, {nome}' que eu faço isso."
        await self._emitir_falado(send, fala)
        return fala, "mestre:atalho_criado"

    async def _talvez_sugerir_atalho(self, comando: str, send: Sender) -> Optional[str]:
        """Conta a intenção e, se ela virou hábito (e ainda não foi sugerida), OFERECE um
        atalho — uma vez só (#2). Devolve a fala da sugestão (p/ anexar ao histórico) ou None."""
        minimo = settings.atalho_sugestao_min
        if minimo <= 0:
            return None
        assinatura = textutils.normaliza(comando)
        n, ja_sugerido = await asyncio.to_thread(db.registrar_frequencia, assinatura, comando)
        if n < minimo or ja_sugerido:
            return None
        await asyncio.to_thread(db.marcar_sugerido, assinatura)
        fala = (
            f"Aliás, você já pediu isso {n} vezes. Se quiser um atalho, diga "
            f"'{settings.palavra_mestre}, atalho' e um apelido curto."
        )
        await self._emitir_falado(send, fala)
        return fala

    def _instrucao_com_perfil(self, base: str) -> str:
        """#36 Diapasão: anexa a diretriz de estilo do usuário (COMO ele prefere ser
        respondido) à instrução de resposta. No-op se desligado ou sem perfil."""
        if not settings.diapasao_habilitado:
            return base
        extra = diapasao.instrucao_do_perfil(self.ctx.perfil_conversa)
        if not extra:
            return base
        return f"{base}\n{extra}" if base else extra

    async def _descobrir_conexoes(self, send: Sender) -> tuple:
        """G8 (descobridor de conexões): acha PONTES no vault — notas que ligam dois temas
        ESTABELECIDOS que quase nunca co-ocorrem — e as fala. Sob demanda, sem push. O grafo
        roda em thread (só CPU/RAM) para não travar o loop. Malha vazia -> nada a conectar."""
        malha = self.ctx.vectorstore.malha
        pontes = await asyncio.to_thread(
            malha.pontes,
            settings.conexao_df_min,
            settings.conexao_coocorrencia_max,
            settings.conexao_limite,
        )
        if not pontes:
            fala = "Não achei conexões novas no seu vault por enquanto."
        else:
            def _titulo(src: str) -> str:
                base = src.replace("\\", "/").rsplit("/", 1)[-1]
                return base[:-3] if base.endswith(".md") else base

            partes = [
                f"sua nota '{_titulo(p.source)}' liga {p.conceito_a} e {p.conceito_b}"
                for p in pontes
            ]
            fala = "Achei algumas conexões: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:conexoes"

    async def _reportar_contradicoes(self, send: Sender) -> tuple:
        """#24: fala as contradições que o idle já achou entre notas do vault. Sob
        demanda, sem push, sem LLM aqui (só lê a tabela — a detecção rodou no idle)."""
        achadas = await asyncio.to_thread(db.contradicoes_abertas, settings.conexao_limite)
        if not achadas:
            fala = "Não encontrei contradições entre as suas notas por enquanto."
        else:
            def _titulo(src: str) -> str:
                base = str(src or "").replace("\\", "/").rsplit("/", 1)[-1]
                return base[:-3] if base.endswith(".md") else base

            partes = [
                f"entre '{_titulo(c['a'])}' e '{_titulo(c['b'])}': {c['resumo']}"
                for c in achadas
            ]
            fala = "Achei possíveis contradições — " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:contradicoes"

    async def _navegar(self, nav: dict, send: Sender, mem: SessionMemory) -> tuple:
        """#14: executa uma ação de navegação da UI. Manda {tipo:"navegar"} para o
        front (que faz a mudança de tela) e uma confirmação falada curta. Para
        'carregar_conversa', resolve o TEMA falado num id de conversa antes."""
        acao = nav["acao"]
        if acao == "carregar_conversa":
            conversas = await asyncio.to_thread(db.get_conversations, 30)
            alvo = fio.casar_conversa(conversas, nav.get("tema", ""))
            if alvo is None:
                fala = f"Não achei uma conversa sobre '{nav.get('tema', '')}'."
                await self._emitir_falado(send, fala)
                return fala, "mestre:nav_nao_achou"
            await send({"tipo": "navegar", "acao": "carregar_conversa", "id": alvo["id"]})
            fala = f"Abrindo a conversa sobre '{alvo['titulo'].strip().rstrip('?.!')}'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:nav_carregar"
        await send({"tipo": "navegar", "acao": acao})
        fala = {
            "nova_conversa": "Pronto, comecei uma conversa nova.",
            "abrir_historico": "Abri o seu histórico de conversas.",
            "fechar_historico": "Fechei o histórico.",
        }.get(acao, "Feito.")
        await self._emitir_falado(send, fala)
        return fala, f"mestre:nav_{acao}"

    async def _retomar_fio(self, send: Sender, mem: SessionMemory) -> tuple:
        """#35: resgata o assunto de uma conversa ANTERIOR (a mais recente que não é a
        atual e teve substância) e oferece continuar. Depende do #34 (Malha): se o tema
        casa um conceito estabelecido do vault, enriquece a deixa com o que já se sabe."""
        conversas = await asyncio.to_thread(db.get_conversations, 10)
        escolhido = fio.escolher_fio(conversas, mem.conversa_id, settings.fio_min_turnos)
        if escolhido is None:
            fala = "Não achei uma conversa anterior pra retomar — acho que estamos começando agora."
            await self._emitir_falado(send, fala)
            return fala, "mestre:fio_vazio"
        titulo = escolhido["titulo"].strip().rstrip("?.!")
        # Toque da Malha (#34): um conceito relacionado ao tema, se o vault o conhece.
        relacionado = ""
        try:
            malha = self.ctx.vectorstore.malha
            chaves = textutils.palavras_chave(titulo)
            concs = [k for k in chaves if malha.idf_palavra(k) >= settings.aterramento_idf_min]
            if concs:
                relacionado = f" A gente pode puxar pelo lado de {concs[0]}, que você já tem anotado."
        except Exception:
            relacionado = ""
        fala = f"Da última vez a gente estava falando sobre '{titulo}'. Quer continuar daí?{relacionado}"
        await self._emitir_falado(send, fala)
        return fala, "mestre:fio"

    async def _reportar_perfil(self, send: Sender) -> tuple:
        """#36: fala o perfil de conversa aprendido (só lê o cache em ctx)."""
        perfil = self.ctx.perfil_conversa
        if not perfil:
            fala = "Ainda estou te conhecendo — não formei um perfil de como você gosta de conversar."
        else:
            fala = f"Pelo que percebi de como você gosta de conversar: {perfil}"
        await self._emitir_falado(send, fala)
        return fala, "mestre:perfil"

    # -- SRS (#43): repetição espaçada, manual + sob demanda --------------------
    async def _srs_marcar(self, send: Sender, mem: SessionMemory) -> tuple:
        """"mestre, revisa isso": cria um card da ÚLTIMA troca (pergunta->resposta). O card
        nasce VENCIDO (revisável já), então "mestre, revisão" o pega; o acerto o empurra."""
        if not mem.chat_history:
            fala = "Não há nada recente pra marcar pra revisão."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_marca_vazia"
        pergunta, resposta = mem.chat_history[-1]
        await asyncio.to_thread(db.srs_criar_card, pergunta, resposta, datetime.now().isoformat())
        fala = "Guardei pra revisão. Diga 'mestre, revisão' quando quiser revisar."
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_marca"

    async def _srs_iniciar(self, send: Sender, mem: SessionMemory) -> tuple:
        """"mestre, revisão": puxa os cards vencidos e abre a sessão de revisão na RAM."""
        cards = await asyncio.to_thread(
            db.srs_vencidos, datetime.now().isoformat(), settings.srs_max_por_sessao
        )
        if not cards:
            fala = "Você não tem nada pra revisar agora. Bom trabalho!"
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_vazio"
        mem.revisao = {"pendentes": cards, "atual": None, "revelado": False}
        fala = self._srs_proximo_card(mem, abertura=f"Você tem {len(cards)} pra revisar. ")
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_inicio"

    def _srs_proximo_card(self, mem: SessionMemory, abertura: str = "") -> str:
        """Avança para o próximo card da fila (ou encerra). Puro em relação à fala."""
        rev = mem.revisao
        if not rev["pendentes"]:
            mem.revisao = None
            return abertura + "Revisão concluída. Até a próxima!"
        rev["atual"] = rev["pendentes"].pop(0)
        rev["revelado"] = False
        return abertura + (
            f"Lembra disto? {rev['atual']['frente']}. "
            "Diga 'mestre, mostra' pra ver a resposta, ou 'acertei' / 'errei'."
        )

    async def _srs_responder(self, comando: str, send: Sender, mem: SessionMemory) -> tuple:
        """Conduz uma revisão EM ANDAMENTO: mostra a resposta, ou registra acerto/erro
        (reagenda pela Leitner) e vai ao próximo card. Só chamado com mem.revisao ativa."""
        rev = mem.revisao
        if mestre.comando_srs_parar(comando):
            mem.revisao = None
            fala = "Ok, parei a revisão. Voltamos quando quiser."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_parar"
        if mestre.comando_srs_mostrar(comando) and not rev["revelado"]:
            rev["revelado"] = True
            fala = f"{rev['atual']['verso']} — acertou? Diga 'acertei' ou 'errei'."
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_mostra"
        if mestre.comando_srs_acertei(comando) or mestre.comando_srs_errei(comando):
            acertou = mestre.comando_srs_acertei(comando)
            novo_estagio, dias = srs.proximo(
                rev["atual"].get("estagio", 0), acertou, settings.srs_intervalos_dias
            )
            proxima = (datetime.now() + timedelta(days=dias)).isoformat()
            await asyncio.to_thread(db.srs_reagendar, rev["atual"]["id"], novo_estagio, proxima)
            fala = self._srs_proximo_card(
                mem, abertura=("Boa! " if acertou else "Sem problema, ela volta em breve. ")
            )
            await self._emitir_falado(send, fala)
            return fala, "mestre:srs_resposta"
        # sub-comando não acionável agora (ex.: "mostra" com a resposta já revelada)
        fala = "Diga 'acertei' ou 'errei' pra esta, ou 'mestre, mostra' pra ver a resposta."
        await self._emitir_falado(send, fala)
        return fala, "mestre:srs_reprompt"

    async def _agenda_hoje(self, send: Sender, mem: SessionMemory) -> tuple:
        """#40: lê os .ics locais e fala os compromissos de HOJE. Só leitura, 100% local."""
        from datetime import date

        eventos = await asyncio.to_thread(calendario.ler_pasta, str(settings.dir_agenda), date.today())
        if not eventos:
            fala = "Você não tem compromissos na agenda para hoje."
        else:
            partes = [f"{dt.strftime('%H:%M')} {titulo}" for dt, titulo in eventos]
            fala = "Hoje você tem: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:agenda"

    # -- Ajuda (/help falável): lista de capacidades ---------------------------
    async def _ajuda(self, send: Sender) -> tuple:
        """Fala um resumo das capacidades — o /help do assistente."""
        fala = (
            "Posso te ajudar de várias formas, sempre começando com 'mestre'. "
            "Conhecimento: eu respondo perguntas com as suas notas e a web, e faço a síntese "
            "de 'o que eu sei sobre um tema'. "
            "Listas e lembretes: 'adiciona pão na lista', 'me lembra de algo às 8 horas', "
            "'me avise quando tal coisa acontecer'. "
            "Agenda: 'o que tenho hoje' lê o seu calendário local. "
            "Estudo: 'revisa isso' e depois 'revisão' pra repetição espaçada; 'modo tutor' "
            "pra eu te ensinar fazendo perguntas. "
            "Produtividade: 'inicia um pomodoro', 'fiz treino' pra marcar hábitos, "
            "'resumo do dia' pro fechamento, e 'rotina manhã: comando e comando' pra criar macros. "
            "Automação: 'quando eu adicionar algo na lista, faça tal ação'. "
            "Conexões: 'alguma conexão nova' acha pontes entre os seus temas. "
            "E ainda: 'desfaça', 'corrige para', 'modo confidencial' e 'atalho' pra encurtar comandos. "
            "É só pedir."
        )
        await self._emitir_falado(send, fala)
        return fala, "mestre:ajuda"

    # -- Revisão diária (#21): fechamento do dia --------------------------------
    @staticmethod
    def _contar_inbox() -> int:
        """Itens pendentes na Inbox de captura (linhas com conteúdo, ignora cabeçalhos)."""
        caminho = str(settings.arquivo_inbox)
        if not os.path.exists(caminho):
            return 0
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return sum(1 for l in f if l.strip() and not l.lstrip().startswith("#"))
        except Exception:
            return 0

    async def _revisao_diaria(self, send: Sender, mem: SessionMemory) -> tuple:
        """#21: fala um fechamento do dia — o que você fez (auditoria), a inbox a
        processar e o que vem amanhã (agenda .ics + lembretes). Só leitura/agregação."""
        from datetime import date

        hoje = date.today()
        amanha = hoje + timedelta(days=1)
        inicio_dia = datetime(hoje.year, hoje.month, hoje.day).isoformat()
        acoes = await asyncio.to_thread(db.get_auditoria, inicio_dia, 50)
        inbox_n = await asyncio.to_thread(self._contar_inbox)
        eventos = await asyncio.to_thread(calendario.ler_pasta, str(settings.dir_agenda), amanha)
        lembretes = await asyncio.to_thread(db.listar_agendamentos, ("lembrete",))
        amanha_iso = amanha.isoformat()
        lembretes_amanha = [l for l in lembretes if (l.get("proximo_disparo") or "").startswith(amanha_iso)]

        partes = [f"hoje você fez {len(acoes)} ação(ões) registrada(s)" if acoes
                  else "hoje você não registrou ações"]
        if inbox_n:
            partes.append(f"tem {inbox_n} item(ns) na inbox pra processar")
        if eventos:
            partes.append("amanhã na agenda: " + "; ".join(f"{dt.strftime('%H:%M')} {t}" for dt, t in eventos))
        if lembretes_amanha:
            partes.append("lembretes de amanhã: " + "; ".join(l["mensagem"] for l in lembretes_amanha))
        fala = "Fechamento do dia. " + ". ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:revisao_diaria"

    # -- Pomodoro (#19): ciclo foco/pausa via scheduler ------------------------
    async def _pomodoro_iniciar(self, send: Sender, mem: SessionMemory) -> tuple:
        """Cria o agendamento 'pomodoro' (1ª transição = fim do foco). O SchedulerService
        cuida do ciclo foco<->pausa. Só um ativo por vez: cancela outro antes."""
        ativos = await asyncio.to_thread(db.listar_agendamentos, ("pomodoro",))
        for a in ativos:
            await asyncio.to_thread(db.cancelar_agendamento, a["id"])
        prox = (datetime.now() + timedelta(minutes=settings.pomodoro_foco_min)).isoformat()
        await asyncio.to_thread(
            db.criar_agendamento, "pomodoro", "Pomodoro", prox, None,
            json.dumps({"fase": "foco"}), mem.conversa_id,
        )
        fala = (f"Pomodoro iniciado! Foco por {settings.pomodoro_foco_min} minutos — "
                "eu aviso quando for a pausa.")
        await self._emitir_falado(send, fala)
        return fala, "mestre:pomodoro_inicia"

    async def _pomodoro_parar(self, send: Sender) -> tuple:
        ativos = await asyncio.to_thread(db.listar_agendamentos, ("pomodoro",))
        for a in ativos:
            await asyncio.to_thread(db.cancelar_agendamento, a["id"])
        fala = "Pomodoro encerrado." if ativos else "Você não tem um pomodoro ativo."
        await self._emitir_falado(send, fala)
        return fala, "mestre:pomodoro_para"

    # -- Rotinas compostas (#10): macros nomeadas ------------------------------
    async def _criar_rotina(self, nome: str, comando: str, send: Sender) -> tuple:
        await asyncio.to_thread(db.rotina_salvar, nome, comando)
        fala = f"Rotina '{nome}' salva. Diga 'mestre, rotina {nome}' pra executar."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotina_criada"

    async def _rotinas_listar(self, send: Sender) -> tuple:
        rs = await asyncio.to_thread(db.rotinas_listar)
        if not rs:
            fala = "Você não tem rotinas salvas."
        else:
            fala = "Suas rotinas: " + "; ".join(f"{r['nome']} ({r['comando']})" for r in rs) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotinas_listar"

    async def _rotina_remover(self, nome: str, send: Sender) -> tuple:
        ok = await asyncio.to_thread(db.rotina_remover, nome)
        fala = f"Rotina '{nome}' removida." if ok else f"Não achei a rotina '{nome}'."
        await self._emitir_falado(send, fala)
        return fala, "mestre:rotina_removida"

    # -- Diário de hábitos (#37): fluxo independente ---------------------------
    async def _habito_marcar(self, nome: str, send: Sender, mem: SessionMemory) -> tuple:
        """Marca o hábito HOJE e fala a sequência (streak) atual."""
        from datetime import date

        hoje = date.today()
        await asyncio.to_thread(db.habito_marcar, nome, hoje.isoformat())
        datas_iso = await asyncio.to_thread(db.habito_datas, nome)
        datas = {datetime.fromisoformat(d).date() for d in datas_iso}
        seq = habitos.streak(datas, hoje)
        fala = f"Marquei '{nome}'. {seq} dia(s) seguido(s)!" if seq > 1 else f"Marquei '{nome}'. Começou a sequência!"
        await self._emitir_falado(send, fala)
        return fala, "mestre:habito_marca"

    async def _habitos_listar(self, send: Sender, mem: SessionMemory) -> tuple:
        from datetime import date

        hoje = date.today()
        nomes = await asyncio.to_thread(db.habitos_nomes)
        if not nomes:
            fala = "Você ainda não registrou nenhum hábito."
        else:
            partes = []
            for nome in nomes:
                datas_iso = await asyncio.to_thread(db.habito_datas, nome)
                datas = {datetime.fromisoformat(d).date() for d in datas_iso}
                partes.append(f"{nome}: {habitos.streak(datas, hoje)} dia(s)")
            fala = "Seus hábitos: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:habitos_listar"

    # -- Gatilhos condicionais internos (#11) ----------------------------------
    async def _criar_gatilho(self, evento: str, filtro: str, acao_txt: str, send: Sender, mem: SessionMemory) -> tuple:
        """Cria a regra "quando <evento/filtro>, <ação>". A ação é parseada agora (rápido
        ou roteador LLM) e ARMAZENADA como JSON de Decisões, para reexecutar no disparo."""
        decisoes = mestre.parse_composto(acao_txt, datetime.now())
        if not decisoes:
            decisao = await self._rotear(acao_txt)
            if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                decisoes = [decisao]
        if not decisoes:
            fala = ("Entendi a condição, mas não a ação. Tente algo simples, como adicionar "
                    "a outra lista ou criar um lembrete.")
            await self._emitir_falado(send, fala)
            return fala, "mestre:gatilho_acao_invalida"
        acao_json = json.dumps([{"tool": d.tool, "args": d.args} for d in decisoes], ensure_ascii=False)
        descricao = f"quando adicionar '{filtro}' à lista → {mestre.descrever_acao(decisoes[0])}"
        await asyncio.to_thread(db.gatilho_criar, evento, filtro, acao_json, descricao)
        fala = f"Combinado: {descricao}."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilho_criado"

    async def _disparar_gatilhos(self, evento: str, contexto: str, send: Sender) -> None:
        """Executa as regras que casam um EVENTO. As ações rodam DIRETO (não via
        _executar_acoes_rapidas), então NÃO re-emitem eventos — sem risco de loop."""
        gatilhos = await asyncio.to_thread(db.gatilhos_por_evento, evento)
        ctx_norm = textutils.normaliza(contexto)
        for g in gatilhos:
            filtro = (g.get("filtro") or "").strip()
            if filtro and textutils.normaliza(filtro) not in ctx_norm:
                continue
            try:
                decs = json.loads(g["acao"])
            except (ValueError, TypeError):
                continue
            for d in decs:
                tool = self.tools.get(d.get("tool"))
                if tool is None:
                    continue
                try:
                    obs = await tool.executar(d.get("args") or {}, self.ctx)
                except Exception as exc:
                    telemetry.error("GATILHO", f"Falha na ação do gatilho {g['id']}", exc)
                    continue
                await self._emitir_falado(send, f"Gatilho automático: {obs}")

    async def _gatilhos_listar(self, send: Sender) -> tuple:
        gs = await asyncio.to_thread(db.gatilhos_listar)
        if not gs:
            fala = "Você não tem gatilhos configurados."
        else:
            partes = [f"{g['id']}: {g['descricao']}" for g in gs]
            fala = "Seus gatilhos: " + "; ".join(partes) + "."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilhos_listar"

    async def _gatilho_remover(self, gatilho_id: int, send: Sender) -> tuple:
        ok = await asyncio.to_thread(db.gatilho_remover, gatilho_id)
        fala = f"Gatilho {gatilho_id} removido." if ok else f"Não achei o gatilho {gatilho_id}."
        await self._emitir_falado(send, fala)
        return fala, "mestre:gatilho_removido"

    def _acao_confirmavel(self, acoes: List["tools.Decisao"]) -> Optional["tools.Decisao"]:
        """A 1ª ação DESTRUTIVA que exige confirmação (#25), ou None. Respeita o botão
        `confirmacao_habilitada` — desligado, nada é gateado (executa direto)."""
        if not settings.confirmacao_habilitada:
            return None
        for dec in acoes:
            tool = self.tools.get(dec.tool)
            if tool is not None and tool.confirmavel:
                return dec
        return None

    async def _fluxo_mestre(
        self, comando: str, send: Sender, tracker: LatencyTracker, mem: SessionMemory
    ) -> None:
        """Fluxo ISOLADO da palavra-mestre: comando de agente, não pergunta.

        1) `parse_rapido` resolve os comandos regulares SEM LLM. 2) Senão, o roteador
        LLM tenta mapear numa ferramenta. 3) Se nem o roteador reconhece uma AÇÃO, o
        comando é RECUSADO (isolamento rígido, decidido com o dono) e REGISTRADO como
        melhoria a revisar — nunca vira uma resposta de conhecimento."""
        if not comando.strip():
            fala = f"Sim, {settings.palavra_mestre.capitalize()}? Pode falar."
            await self._emitir_falado(send, fala)
            return

        # ATALHO (#2): se o comando INTEIRO casa um apelido salvo, expande para o comando
        # original antes de qualquer coisa — daí segue o fluxo normal como se o usuário
        # tivesse dito o comando completo.
        atalhos = await self._atalhos_cache()
        expandido = atalhos.get(textutils.normaliza(comando))
        if expandido:
            telemetry.track("MESTRE", f"Atalho '{comando}' -> '{expandido}'.")
            comando = expandido

        # ROTINAS (#10): "rotina <nome>" (sem ':') EXPANDE para o comando composto salvo, e
        # o fluxo normal (parse_composto abaixo) executa os passos. Criar/listar/remover não
        # expandem (têm ':'/'=' ou verbo de remoção — ver parse_rotina_rodar).
        nome_rotina = mestre.parse_rotina_rodar(comando)
        if nome_rotina:
            salva = await asyncio.to_thread(db.rotina_get, nome_rotina)
            if salva:
                telemetry.track("MESTRE", f"Rotina '{nome_rotina}' -> '{salva}'.")
                comando = salva

        # COFRE DE CONFIRMAÇÃO (#25): se há uma ação destrutiva PENDENTE, "confirma" a
        # executa e "não/deixa" a aborta (abort tem precedência — na dúvida, não faz).
        # Qualquer OUTRO comando ABANDONA a pendência e segue normal (não prende o
        # usuário). Os gatilhos só valem com algo pendente (#15: sem gatilho global).
        pend = mem.confirmacao_pendente
        abortando = pend is not None and mestre.comando_abortar(comando)
        confirmando = pend is not None and not abortando and mestre.comando_confirmar(comando)
        if pend is not None and not abortando and not confirmando:
            mem.confirmacao_pendente = None   # comando novo supera a pendência

        # SRS (#43): um comando NÃO-relacionado durante uma revisão em andamento a abandona
        # (não prende o usuário) — mesmo espírito da pendência de confirmação acima. Os
        # sub-comandos (mostra/acertei/errei/parar) seguem para o handler de revisão abaixo.
        if mem.revisao is not None and not mestre.comando_srs_sub(comando):
            mem.revisao = None

        # GATILHOS (#11): "quando eu adicionar X na lista, <ação>". Só vira gatilho se a
        # condição casa um evento CONHECIDO; senão None e segue o fluxo normal (o watcher
        # "me avise quando ..." e o roteador continuam intactos).
        gatilho_spec = mestre.parse_gatilho(comando, datetime.now())
        # HÁBITOS (#37): nome do hábito a marcar ("fiz X" / "marca que ..."), ou None.
        habito_nome = mestre.parse_habito_marcar(comando)
        # ROTINAS (#10): criação "rotina <nome>: <cmd>" (a execução já foi expandida acima).
        rotina_nova = mestre.parse_rotina_criar(comando)

        # MODO CONFIDENCIAL (#5): meta-comando que mexe no estado da SESSÃO (por isso é
        # tratado aqui, não numa ferramenta). Liga/desliga e NÃO registra o próprio turno.
        modo = mestre.modo_confidencial(comando)
        if modo is not None:
            mem.confidencial = modo
            fala = (
                "Modo confidencial ativado. O que falarmos agora fica só nesta sessão — "
                "não salvo nada nem transformo em conhecimento."
                if modo else
                "Voltando ao normal. As conversas voltam a ser salvas e aprendidas."
            )
            await self._emitir_falado(send, fala)
            return

        # TUTOR SOCRÁTICO (#44): meta-comando de SESSÃO (como o confidencial). Liga/desliga
        # o modo em que as respostas viram perguntas guiadas (via verbosidade.aplicar_tutor).
        tutor = mestre.comando_tutor(comando)
        if tutor is not None:
            mem.tutor = tutor
            fala = (
                "Modo tutor ativado. Vou te fazer perguntas pra você raciocinar, em vez de "
                "dar a resposta pronta. Quando quiser parar, diga 'mestre, sai do tutor'."
                if tutor else
                "Modo tutor desligado. Volto a responder direto."
            )
            await self._emitir_falado(send, fala)
            return

        # MODO ECONÔMICO (#30): meta-comando de SESSÃO (como confidencial/tutor). Liga/
        # desliga o bypass do gate de relevância (responde local em vez de escalar web).
        economico = mestre.comando_economico(comando)
        if economico is not None:
            mem.economico = economico and settings.modo_economico_habilitada
            fala = (
                "Modo econômico ligado. Vou responder do que já tenho no vault sempre que "
                "der, evitando a web — mais rápido, porém menos preciso em temas que não domino."
                if mem.economico else
                "Modo econômico desligado. Volto a escalar pra web quando o local não bastar."
            )
            await self._emitir_falado(send, fala)
            return

        # REVISÃO EM ANDAMENTO (#43): se há uma revisão ativa, o comando aqui é um
        # sub-comando dela (mostra/acertei/errei/parar) — os não-relacionados já a
        # abandonaram acima. Tem prioridade sobre o resto.
        if mem.revisao is not None:
            texto_final, rota = await self._srs_responder(comando, send, mem)
        # CONFIRMAÇÃO PENDENTE (#25) tem prioridade sobre um comando novo.
        elif confirmando:
            dec = mem.confirmacao_pendente
            mem.confirmacao_pendente = None
            texto_final = await self._executar_acoes_rapidas(
                [dec], send, auditar=not mem.confidencial, mem=mem
            )
            rota = "mestre:confirmado"
        elif abortando:
            mem.confirmacao_pendente = None
            texto_final = "Ok, não vou fazer isso."
            await self._emitir_falado(send, texto_final)
            rota = "mestre:confirmacao_abortada"
        # ATALHO (#2): "mestre, atalho <nome>" nomeia o último comando resolvido.
        elif (nome_atalho := mestre.parse_atalho(comando)) is not None:
            texto_final, rota = await self._criar_atalho(nome_atalho, send, mem)
        # DESFAZER (#8): meta-comando que reverte a última ação reversível da sessão.
        # Vem antes do parse_rapido: "desfaça" não é uma ação nova a rotear.
        elif mestre.comando_desfazer(comando):
            texto_final, rota = await self._desfazer(send, mem, auditar=not mem.confidencial)
        elif mestre.comando_conexoes(comando):
            # DESCOBRIDOR DE CONEXÕES (G8): "mestre, alguma conexão nova?" — insight sob
            # demanda, não ação. Lê a malha em ctx (como o desfazer lê o estado da sessão).
            texto_final, rota = await self._descobrir_conexoes(send)
        elif mestre.comando_fonte(comando):
            # "FONTE?" (painel 2026-07): proveniência da última resposta, por TEMPLATE —
            # auditoria não paga decode na GPU serializada (nada de LLM aqui).
            texto_final, rota = await self._falar_fontes(send, mem)
        elif mestre.comando_contradicoes(comando):
            # DETECTOR DE CONTRADIÇÃO (#24): "mestre, alguma contradição?" — só lê a
            # tabela; a detecção rodou no idle. Insight sob demanda, não ação.
            texto_final, rota = await self._reportar_contradicoes(send)
        elif mestre.comando_perfil(comando):
            # DIAPASÃO (#36): "mestre, como você me vê?" — diz o perfil de estilo
            # aprendido no idle. Só lê o cache em ctx; insight sob demanda.
            texto_final, rota = await self._reportar_perfil(send)
        elif settings.navegacao_voz_habilitada and (nav := mestre.parse_navegacao(comando)):
            # NAVEGAÇÃO POR VOZ (#14): opera a UI (nova conversa, histórico, abrir uma
            # conversa por tema). ANTES do #35: "retoma a conversa SOBRE X" é o pedido
            # específico (abrir X) e vence o genérico "onde paramos" — parse_navegacao
            # só casa carregar quando há o tema, então "retoma o fio" ainda cai no #35.
            texto_final, rota = await self._navegar(nav, send, mem)
        elif mestre.comando_retomar_fio(comando):
            # FIO DA CONVERSA (#35): "mestre, onde paramos?" — resgata o assunto de
            # uma conversa anterior. Momento oportuno = o usuário pediu.
            texto_final, rota = await self._retomar_fio(send, mem)
        elif mestre.comando_revisao_diaria(comando):
            # #21: "resumo/fechamento do dia" — ANTES do SRS, pois "revisão do dia" conteria
            # "revisão" e cairia na revisão de cards.
            texto_final, rota = await self._revisao_diaria(send, mem)
        elif mestre.comando_srs_marcar(comando):
            # SRS (#43): "revisa isso" — ANTES do iniciar, pois a frase contém "revisa".
            texto_final, rota = await self._srs_marcar(send, mem)
        elif mestre.comando_srs_iniciar(comando):
            texto_final, rota = await self._srs_iniciar(send, mem)
        elif mestre.comando_agenda(comando):
            # #40: "o que tenho hoje" — leitura da agenda .ics local.
            texto_final, rota = await self._agenda_hoje(send, mem)
        elif mestre.comando_pomodoro_parar(comando):
            # #19: parar ANTES de iniciar (a frase de parar contém "pomodoro").
            texto_final, rota = await self._pomodoro_parar(send)
        elif mestre.comando_pomodoro_iniciar(comando):
            texto_final, rota = await self._pomodoro_iniciar(send, mem)
        elif gatilho_spec is not None:
            # #11: cria a regra "quando eu adicionar X na lista, <ação>".
            evento, filtro, acao_txt = gatilho_spec
            texto_final, rota = await self._criar_gatilho(evento, filtro, acao_txt, send, mem)
        elif (gid := mestre.comando_gatilho_remover(comando)) is not None:
            texto_final, rota = await self._gatilho_remover(gid, send)
        elif mestre.comando_gatilhos_listar(comando):
            texto_final, rota = await self._gatilhos_listar(send)
        elif rotina_nova is not None:
            # #10: cria a rotina "rotina <nome>: <comando composto>".
            texto_final, rota = await self._criar_rotina(rotina_nova[0], rotina_nova[1], send)
        elif (rot_rem := mestre.comando_rotina_remover(comando)) is not None:
            texto_final, rota = await self._rotina_remover(rot_rem, send)
        elif mestre.comando_rotinas_listar(comando):
            texto_final, rota = await self._rotinas_listar(send)
        elif mestre.comando_habitos_listar(comando):
            texto_final, rota = await self._habitos_listar(send, mem)
        elif habito_nome is not None:
            # #37: marca um hábito cumprido hoje.
            texto_final, rota = await self._habito_marcar(habito_nome, send, mem)
        elif mestre.tem_correcao(comando):
            # CORTA-E-CORRIGE (#9): "corrige para X" — antes do parse_rapido, pois um
            # "corrige ... na lista" tem gatilho de lista mas é correção, não add.
            texto_final, rota = await self._corrigir(comando, send, mem, auditar=not mem.confidencial)
        elif acoes := mestre.parse_composto(comando, datetime.now()):
            # #12 já pode ter devolvido VÁRIAS ações (comando encadeado "faz X e faz Y").
            # COFRE (#25): se alguma é destrutiva não-desfazível, executa as SEGURAS já e
            # deixa a destrutiva aguardando "confirma".
            confirmavel = self._acao_confirmavel(acoes)
            if confirmavel is not None:
                seguras = [a for a in acoes if a is not confirmavel]
                if seguras:
                    await self._executar_acoes_rapidas(
                        seguras, send, auditar=not mem.confidencial, mem=mem
                    )
                mem.confirmacao_pendente = confirmavel
                texto_final = f"Confirma que quer {mestre.descrever_acao(confirmavel)}? Diga 'mestre, confirma'."
                await self._emitir_falado(send, texto_final)
                rota = "mestre:aguarda_confirmacao"
            else:
                texto_final = await self._executar_acoes_rapidas(
                    acoes, send, auditar=not mem.confidencial, mem=mem
                )
                rota = "mestre:rapido"
        elif mestre.comando_ajuda(comando):
            # /help falável: descoberta de comandos. TARDE no fluxo (depois do parse_composto)
            # pra os comandos estruturados ganharem de um "me ajuda a adicionar na lista".
            texto_final, rota = await self._ajuda(send)
        else:
            decisao = await self._rotear(comando)
            if decisao and decisao.tool != "responder" and self.tools.get(decisao.tool):
                tool = self.tools.get(decisao.tool)
                if settings.confirmacao_habilitada and tool.confirmavel:
                    mem.confirmacao_pendente = decisao
                    texto_final = f"Confirma que quer {mestre.descrever_acao(decisao)}? Diga 'mestre, confirma'."
                    await self._emitir_falado(send, texto_final)
                    rota = "mestre:aguarda_confirmacao"
                else:
                    texto_final = await self._pipeline_tools(
                        comando, send, decisao, auditar=not mem.confidencial, mem=mem
                    )
                    rota = f"mestre:tool:{decisao.tool}"
            else:
                # Não é ação reconhecida: recusa + registra para revisão.
                await asyncio.to_thread(db.registrar_comando_desconhecido, comando)
                fala = (
                    "Não reconheci um comando de agente aí. Posso, por exemplo, adicionar "
                    "itens a uma lista, criar um lembrete ou avisar quando algo acontecer."
                )
                await self._emitir_falado(send, fala)
                texto_final = fala
                rota = "mestre:desconhecido"

        # ATALHO DE INTENÇÃO FREQUENTE (#2): só AÇÕES resolvidas contam como "intenção"
        # (meta-comandos como desfazer/confirmar/atalho não). Guarda o último comando p/
        # o "atalho <nome>" e, se a intenção virou hábito, OFERECE um atalho (uma vez).
        if rota == "mestre:rapido" or rota.startswith("mestre:tool:"):
            mem.ultimo_comando_mestre = comando
            if not mem.confidencial:
                sugestao = await self._talvez_sugerir_atalho(comando, send)
                if sugestao and texto_final:
                    texto_final = f"{texto_final} {sugestao}"   # anexa ao histórico

        # Comando de agente NÃO alimenta o dump (não é conhecimento). Segue no SQLite
        # (histórico) e na RAM (contexto de follow-up), como as demais ações — salvo
        # em modo confidencial, onde nada é persistido (só a RAM).
        if texto_final:
            mem.registrar_turno(comando, texto_final)
            if not mem.confidencial:
                await asyncio.to_thread(db.save_chat, comando, texto_final, mem.conversa_id)
            await self._registrar_latencia(tracker, rota)
