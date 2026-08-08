"""
Ícone de bandeja do aplicativo — o que faz "fechar" deixar de ser "encerrar".

O app foi feito para ficar aberto o dia inteiro (decisão do dono, 2026-08-02).
Com o X encerrando o processo, reabrir custa os ~36 s de boot; com ele escondendo
a janela, custa zero. E a bandeja é o único lugar onde a notificação do modo
ocioso pode aparecer com a janela minimizada — que é justamente o caso em que ela
precisa aparecer.

O desenho do ícone MUDOU DE CASA (2026-08-02): era um orbe próprio, feito aqui;
agora é a marca de `marca.py`, a MESMA da barra de tarefas e do favicon. O motivo
de desenhar em código em vez de versionar um binário continua valendo (a cor
segue a paleta da interface por construção) — o que não valia era ter dois
desenhos diferentes chamados "o ícone do app", que divergiriam na primeira
troca de paleta.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


def desenhar_icone(tamanho: int = 64, ativo: bool = True):
    """A marca, em miniatura. Puro: devolve uma imagem, sem tocar em bandeja
    nenhuma — é o que permite testá-la sem ambiente gráfico."""
    from mente_digital import marca

    return marca.desenhar_marca(tamanho, ativo=ativo)


class Bandeja:
    """Ícone na bandeja, com menu. Roda o laço do pystray numa thread própria.

    Fail-soft por inteiro: se o pystray não subir (sessão sem bandeja, política de
    grupo, servidor headless), `ativa` fica False e o app funciona exatamente como
    antes — só perde o esconder-em-vez-de-fechar. Bandeja é conveniência; nunca
    pode ser requisito para o assistente abrir."""

    def __init__(
        self,
        ao_abrir: Callable[[], None],
        ao_sair: Callable[[], None],
        ao_alternar_energia: Optional[Callable[[], None]] = None,
        ao_consolidar: Optional[Callable[[], None]] = None,
        ao_adiar: Optional[Callable[[], None]] = None,
    ) -> None:
        self._ao_abrir = ao_abrir
        self._ao_sair = ao_sair
        self._ao_alternar_energia = ao_alternar_energia
        self._ao_consolidar = ao_consolidar
        self._ao_adiar = ao_adiar
        self._icone = None
        self._thread: Optional[threading.Thread] = None
        self.ativa = False
        self.ligado = True
        self.consolidando = False
        # Frase pronta de `vez.resumo_de_uso`; vazia é o caso normal. Ver `marcar`.
        self.usando = ""

    # -- ciclo de vida ---------------------------------------------------------
    def iniciar(self) -> bool:
        try:
            import pystray

            self._icone = pystray.Icon(
                "mente_digital", desenhar_icone(64, True), "Mente Digital",
                menu=self._montar_menu(),
            )
            # `default=True` no "Abrir" faz o clique simples no ícone abrir a
            # janela, que é o que todo mundo espera de um app de bandeja.
            self._thread = threading.Thread(target=self._icone.run, daemon=True, name="bandeja")
            self._thread.start()
            self.ativa = True
        except Exception as exc:                   # noqa: BLE001 - conveniência, nunca requisito
            print(f"[APP] bandeja indisponível (o app segue sem ela): {exc}")
            self.ativa = False
        return self.ativa

    def parar(self) -> None:
        try:
            if self._icone is not None:
                self._icone.stop()
        except Exception:                          # noqa: BLE001
            pass

    # -- menu ------------------------------------------------------------------
    def _montar_menu(self):
        import pystray

        item = pystray.MenuItem
        return pystray.Menu(
            item("Abrir Mente Digital", lambda: self._ao_abrir(), default=True),
            pystray.Menu.SEPARATOR,
            item(lambda _: "Descansar (libera a VRAM)" if self.ligado else "Religar os modelos",
                 lambda: self._chamar(self._ao_alternar_energia)),
            item(lambda _: "Parar de consolidar" if self.consolidando else "Consolidar agora",
                 lambda: self._chamar(self._ao_consolidar)),
            item("Agora não (adiar o idle)", lambda: self._chamar(self._ao_adiar),
                 visible=lambda _: self.consolidando is False),
            pystray.Menu.SEPARATOR,
            item("Sair", lambda: self._ao_sair()),
        )

    @staticmethod
    def _chamar(fn: Optional[Callable[[], None]]) -> None:
        if fn is not None:
            fn()

    # -- estado e avisos -------------------------------------------------------
    def marcar(self, ligado: Optional[bool] = None, consolidando: Optional[bool] = None,
               usando: Optional[str] = None) -> None:
        """Reflete o estado no ícone e no menu. O ícone APAGA quando descansando —
        é o retorno visual de que a VRAM está livre sem precisar abrir a janela.

        `usando` é a frase pronta de `vez.resumo_de_uso` ("ana está usando agora"),
        vazia quando não há ninguém. Ela existe para o dono não abrir um jogo e
        cortar sem querer quem estava no meio de uma conversa — e mora AQUI, e não
        na janela, porque jogando a janela não está visível. É também o caminho que
        não depende do toast, que o Assistente de Foco pode engolir em tela cheia.

        ⚠ Ela VENCE "descansando" no rótulo: se há alguém usando, o assistente por
        definição não está descansando, e mostrar as duas coisas juntas faria o
        dono duvidar da que importa. Perde só para "consolidando", que é trabalho
        do próprio dono acontecendo agora."""
        mudou_cor = ligado is not None and ligado != self.ligado
        if ligado is not None:
            self.ligado = ligado
        if consolidando is not None:
            self.consolidando = consolidando
        if usando is not None:
            self.usando = usando.strip()
        if not self.ativa or self._icone is None:
            return
        try:
            if mudou_cor:
                self._icone.icon = desenhar_icone(64, self.ligado)
            self._icone.title = (
                "Mente Digital — consolidando…" if self.consolidando
                else f"Mente Digital — {self.usando}" if self.usando
                else "Mente Digital" if self.ligado else "Mente Digital — descansando"
            )
            self._icone.update_menu()
        except Exception:                          # noqa: BLE001
            pass

    def avisar(self, titulo: str, mensagem: str) -> bool:
        """Balão do sistema. Devolve se conseguiu — o chamador precisa saber:
        se a notificação NÃO apareceu, agir por conta própria depois de 15 s
        seria agir sem o dono ter tido a chance de ver a pergunta."""
        if not self.ativa or self._icone is None:
            return False
        try:
            self._icone.notify(mensagem, titulo)
            return True
        except Exception as exc:                   # noqa: BLE001
            print(f"[APP] não consegui notificar: {exc}")
            return False
