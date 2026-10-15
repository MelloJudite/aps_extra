from dataclasses import dataclass
from concurrent.futures import CancelledError
import json
from queue import Empty, SimpleQueue
import random
from threading import Event, Thread
from time import perf_counter
from typing import Iterable, Iterator
import tkinter as tk
from tkinter import messagebox, ttk
from aigyminsper.search.graph import State
from aigyminsper.search.search_algorithms import BuscaCustoUniforme


Posicao = tuple[int, int]
MOVIMENTOS = {
    "cima": (0, -1),
    "baixo": (0, 1),
    "esquerda": (-1, 0),
    "direita": (1, 0),
}
ACOES = (*MOVIMENTOS, "pegar pacote", "entregar pacote")


@dataclass(frozen=True)
class Estado:
    pos_atual: Posicao
    produto: Posicao
    carregando: bool = False


class ProblemaEntrega:
    def __init__(
        self,
        dimensao: int,
        pos_atual: Posicao,
        obstaculos: Iterable[Posicao],
        pedagios: Iterable[Posicao],
        produto: Posicao,
        destino: Posicao,
    ):
        if type(dimensao) is not int or dimensao < 1:
            raise ValueError("A dimensão deve ser um número inteiro positivo.")
        self.dimensao = dimensao
        self.obstaculos = frozenset(obstaculos)
        self.pedagios = frozenset(pedagios)
        self.destino = destino
        self.estado_inicial = Estado(pos_atual, produto)
        posicoes = [pos_atual, produto, destino, *self.obstaculos, *self.pedagios]
        if not all(self.dentro_do_grid(pos) for pos in posicoes):
            raise ValueError("Todas as posições devem ser tuplas (x, y) dentro do grid.")
        if self.obstaculos & self.pedagios:
            raise ValueError("Uma célula não pode ser obstáculo e pedágio ao mesmo tempo.")
        if any(pos in self.obstaculos for pos in (pos_atual, produto, destino)):
            raise ValueError("Agente, pacote e destino não podem ocupar obstáculos.")

    def dentro_do_grid(self, pos: Posicao) -> bool:
        return (
            isinstance(pos, tuple)
            and len(pos) == 2
            and all(type(valor) is int and 0 <= valor < self.dimensao for valor in pos)
        )

    def objetivo(self, estado: Estado) -> bool:
        return estado.produto == self.destino and not estado.carregando

    def aplicar_acao(self, estado: Estado, acao: str) -> tuple[Estado, int]:
        custo = 1 + (2 if estado.pos_atual in self.pedagios else 0)
        posicao = estado.pos_atual
        produto = estado.produto
        carregando = estado.carregando

        if acao in MOVIMENTOS:
            dx, dy = MOVIMENTOS[acao]
            candidata = (posicao[0] + dx, posicao[1] + dy)
            if self.dentro_do_grid(candidata) and candidata not in self.obstaculos:
                posicao = candidata
                if carregando:
                    produto = posicao
        elif acao == "pegar pacote":
            if not carregando and posicao == produto:
                carregando = True
        elif acao == "entregar pacote":
            if carregando:
                produto = posicao
                carregando = False
        else:
            raise ValueError(f"Ação desconhecida: {acao}")

        return Estado(posicao, produto, carregando), custo

    def sucessores(self, estado: Estado) -> Iterator[tuple[str, Estado, int]]:
        if self.objetivo(estado):
            return
        for acao in ACOES:
            novo_estado, custo = self.aplicar_acao(estado, acao)
            if novo_estado != estado:
                yield acao, novo_estado, custo


def gerar_mundo(dimensao: int, semente: int | None = None) -> ProblemaEntrega:
    if type(dimensao) is not int or dimensao < 1:
        raise ValueError("A dimensão deve ser um número inteiro positivo.")
    rng = random.Random(semente)
    total = dimensao * dimensao

    def coordenada(indice: int) -> Posicao:
        return indice % dimensao, indice // dimensao

    if dimensao == 1:
        pedagios = {(0, 0)} if rng.choice((False, True)) else set()
        return ProblemaEntrega(1, (0, 0), set(), pedagios, (0, 0), (0, 0))

    qtd_obstaculos = rng.randint(0, min(total - 3, total * 35 // 100))
    sorteados = rng.sample(range(total), 3 + qtd_obstaculos)
    agente, produto, destino = map(coordenada, sorteados[:3])
    obstaculos = set(map(coordenada, sorteados[3:]))
    livres = [coordenada(i) for i in range(total) if coordenada(i) not in obstaculos]
    qtd_pedagios = rng.randint(0, min(len(livres), total // 4))
    pedagios = rng.sample(livres, qtd_pedagios)
    return ProblemaEntrega(dimensao, agente, obstaculos, pedagios, produto, destino)


@dataclass
class ResultadoBusca:
    encontrou: bool
    caminho: list[str]
    estados: list[Estado]
    custos: list[int]
    custo_final: int | None
    expandidos: int
    tempo: float
    cancelada: bool = False


@dataclass
class ContextoBusca:
    problema: ProblemaEntrega
    cancelar: Event | None = None
    expandidos: int = 0

    def verificar_cancelamento(self):
        if self.cancelar is not None and self.cancelar.is_set():
            raise CancelledError


class EstadoEntrega(State):
    def __init__(
        self, estado: Estado, contexto: ContextoBusca,
        operador: str = "", custo_acao: int = 0,
    ):
        super().__init__(operador)
        self.estado = estado
        self.contexto = contexto
        self.custo_acao = custo_acao

    def successors(self) -> list[State]:
        self.contexto.verificar_cancelamento()
        self.contexto.expandidos += 1
        problema = self.contexto.problema
        sucessores = []
        for acao, estado, custo in problema.sucessores(self.estado):
            # Soltar fora do destino nunca melhora um plano: carregar não
            # altera os movimentos/custos e evita soltar e pegar novamente.
            if acao == "entregar pacote" and not problema.objetivo(estado):
                continue
            sucessores.append(EstadoEntrega(estado, self.contexto, acao, custo))
        return sucessores

    def is_goal(self) -> bool:
        self.contexto.verificar_cancelamento()
        return self.contexto.problema.objetivo(self.estado)

    def description(self) -> str:
        return

    def cost(self) -> int:
        return self.custo_acao

    def env(self) -> str:
        return json.dumps((self.estado.pos_atual, self.estado.produto,
                           self.estado.carregando, self.custo_acao))


def busca_custo_uniforme(problema: ProblemaEntrega, cancelar: Event | None = None) -> ResultadoBusca:
    inicio = perf_counter()
    contexto = ContextoBusca(problema, cancelar)
    inicial = EstadoEntrega(problema.estado_inicial, contexto)
    algoritmo = BuscaCustoUniforme()
    try:
        no_final = algoritmo.search(inicial, pruning="general")
    except CancelledError:
        return ResultadoBusca(False, [], [], [], None, contexto.expandidos,
                              perf_counter() - inicio, cancelada=True)

    if no_final is None:
        return ResultadoBusca(False, [], [], [], None, contexto.expandidos,
                              perf_counter() - inicio)

    caminho, estados, custos = [], [], []
    no = no_final
    while no is not None:
        estados.append(no.state.estado)
        if no.father_node is not None:
            caminho.append(no.state.operator)
            custos.append(no.state.cost())
        no = no.father_node
    return ResultadoBusca(True, caminho[::-1], estados[::-1], custos[::-1],
                          no_final.g, contexto.expandidos, perf_counter() - inicio)


class InterfaceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("APS • Robô entregador")
        self.geometry("1160x780")
        self.minsize(900, 650)
        self.configure(background="#edf2f7")
        self.problema: ProblemaEntrega | None = None
        self.resultado: ResultadoBusca | None = None
        self.estado: Estado | None = None
        self.passo = 0
        self.custo_executado = 0
        self.tamanho_celula = 48.0
        self.ajuste_automatico = True
        self.animacao_id = None
        self.ocupada = False
        self.cancelamento = Event()
        self.fila: SimpleQueue = SimpleQueue()
        self.dimensao_var = tk.StringVar(value="10")
        self.semente_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Defina a dimensão e gere um mundo.")
        self.mundo_var = tk.StringVar()
        self.resumo_var = tk.StringVar(value="Busque uma rota para calcular o plano ótimo.")
        self.execucao_var = tk.StringVar()
        self.velocidade_var = tk.DoubleVar(value=300)
        self._montar_interface()
        self.protocol("WM_DELETE_WINDOW", self.fechar)
        self.after(80, self._receber_resultados)
        self.after_idle(self.gerar)

    def _montar_interface(self):
        estilo = ttk.Style(self)
        estilo.theme_use("clam")
        estilo.configure("TFrame", background="#f7edf2")
        estilo.configure("TLabel", background="#f7edf3", foreground="#d94f84",
                         font=("Segoe UI", 10))
        estilo.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        estilo.configure("TButton", padding=(10, 7), font=("Segoe UI", 10))
        estilo.configure("Treeview", rowheight=27, font=("Segoe UI", 9))
        estilo.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        cabecalho = ttk.Frame(self, padding=(20, 14, 20, 8))
        cabecalho.grid(row=0, column=0, sticky="ew")
        ttk.Label(cabecalho, text="Robô entregador", style="Title.TLabel").pack(anchor="w")
        ttk.Label(cabecalho, text="Planeje a entrega pelo menor custo, considerando obstáculos e pedágios.").pack(anchor="w")

        controles = ttk.Frame(self, padding=(20, 4, 20, 10))
        controles.grid(row=1, column=0, sticky="ew")
        ttk.Label(controles, text="Dimensão N × N:").grid(row=0, column=0, padx=(0, 6))
        self.entrada_dimensao = ttk.Entry(controles, textvariable=self.dimensao_var, width=7)
        self.entrada_dimensao.grid(row=0, column=1)
        self.entrada_dimensao.bind("<Return>", lambda event: self.gerar())
        ttk.Label(controles, text="Semente (opcional):").grid(row=0, column=2, padx=(18, 6))
        self.entrada_semente = ttk.Entry(controles, textvariable=self.semente_var, width=12)
        self.entrada_semente.grid(row=0, column=3)
        self.botao_gerar = ttk.Button(controles, text="Gerar mundo", command=self.gerar)
        self.botao_gerar.grid(row=0, column=4, padx=(18, 8))
        self.botao_buscar = ttk.Button(controles, text="Buscar rota ótima", command=self.buscar)
        self.botao_buscar.grid(row=0, column=5)
        self.botao_cancelar = ttk.Button(controles, text="Cancelar busca", command=lambda: self.cancelamento.set())
        self.botao_cancelar.grid(row=0, column=6, padx=8)
        self.botao_cancelar.state(["disabled"])

        corpo = ttk.Panedwindow(self, orient="horizontal")
        corpo.grid(row=2, column=0, sticky="nsew", padx=20)
        esquerda = ttk.Frame(corpo)
        direita = ttk.Frame(corpo, padding=(14, 0, 0, 0), width=400)
        corpo.add(esquerda, weight=3)
        corpo.add(direita, weight=2)
        esquerda.columnconfigure(0, weight=1)
        esquerda.rowconfigure(1, weight=1)

        faixa = ttk.Frame(esquerda)
        faixa.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(faixa, textvariable=self.mundo_var).pack(side="left")
        ttk.Button(faixa, text="+", width=3, command=lambda: self._zoom(1.25)).pack(side="right")
        ttk.Button(faixa, text="−", width=3, command=lambda: self._zoom(0.8)).pack(side="right", padx=4)
        ttk.Button(faixa, text="Ajustar", command=self.ajustar).pack(side="right", padx=4)
        self.canvas = tk.Canvas(esquerda, background="#ffffff", highlightthickness=1,
                                highlightbackground="#ccd5e0", width=580, height=520)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        rolagem_y = ttk.Scrollbar(esquerda, orient="vertical", command=self._rolar_y)
        rolagem_y.grid(row=1, column=1, sticky="ns")
        rolagem_x = ttk.Scrollbar(esquerda, orient="horizontal", command=self._rolar_x)
        rolagem_x.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=rolagem_x.set, yscrollcommand=rolagem_y.set)
        self.canvas.bind("<Configure>", self._redimensionar)
        self.canvas.bind("<MouseWheel>", self._roda_mouse)

        legenda = ttk.Frame(esquerda, padding=(0, 9))
        legenda.grid(row=3, column=0, columnspan=2, sticky="ew")
        for coluna, (texto, cor) in enumerate((
            ("A: agente", "#2563eb"), ("P: pacote", "#b45309"),
            ("X: destino", "#15803d"), ("■ obstáculo", "#111827"),
            ("■ pedágio (+2)", "#666666"),
        )):
            ttk.Label(legenda, text=texto, foreground=cor).grid(
                row=coluna // 3, column=coluna % 3, sticky="w", padx=(0, 14), pady=2)
        ttk.Label(esquerda, text="Rota azul: até o pacote  •  Rota laranja: até a entrega",
                  wraplength=550).grid(row=4, column=0, columnspan=2, sticky="w")

        direita.columnconfigure(0, weight=1)
        direita.rowconfigure(3, weight=1)
        ttk.Label(direita, text="Plano de entrega", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(direita, textvariable=self.resumo_var, wraplength=370,
                  justify="left").grid(row=1, column=0, sticky="ew", pady=(10, 8))
        ttk.Label(direita, textvariable=self.execucao_var, wraplength=370,
                  justify="left").grid(row=2, column=0, sticky="ew", pady=(0, 10))
        tabela_frame = ttk.Frame(direita)
        tabela_frame.grid(row=3, column=0, sticky="nsew")
        tabela_frame.columnconfigure(0, weight=1)
        tabela_frame.rowconfigure(0, weight=1)
        colunas = ("passo", "acao", "posicao", "custo", "total")
        self.tabela = ttk.Treeview(tabela_frame, columns=colunas, show="headings", selectmode="browse")
        for nome, titulo, largura in zip(colunas, ("#", "Ação", "(x, y)", "Custo", "Total"), (34, 132, 70, 48, 48)):
            self.tabela.heading(nome, text=titulo)
            self.tabela.column(nome, width=largura, minwidth=30, stretch=nome == "acao", anchor="center")
        self.tabela.grid(row=0, column=0, sticky="nsew")
        tabela_scroll = ttk.Scrollbar(tabela_frame, orient="vertical", command=self.tabela.yview)
        tabela_scroll.grid(row=0, column=1, sticky="ns")
        self.tabela.configure(yscrollcommand=tabela_scroll.set)

        botoes = ttk.Frame(direita, padding=(0, 12, 0, 8))
        botoes.grid(row=4, column=0, sticky="ew")
        self.botao_executar = ttk.Button(botoes, text="Executar", command=self.executar)
        self.botao_executar.pack(side="left")
        self.botao_passo = ttk.Button(botoes, text="Próximo passo", command=self.proximo_passo)
        self.botao_passo.pack(side="left", padx=6)
        self.botao_reiniciar = ttk.Button(botoes, text="Reiniciar", command=self.reiniciar)
        self.botao_reiniciar.pack(side="left")
        ttk.Label(direita, text="Intervalo entre ações (ms)").grid(row=5, column=0, sticky="w")
        ttk.Scale(direita, from_=50, to=1000, variable=self.velocidade_var).grid(row=6, column=0, sticky="ew")
        ttk.Label(direita, text="Cada ação custa 1. Se começar no pedágio, custa 3.\nPegar e entregar também contam como ações.",
                  wraplength=370, justify="left").grid(row=7, column=0, sticky="w", pady=(12, 0))
        ttk.Label(self, textvariable=self.status_var, padding=(20, 12),
                  wraplength=1100).grid(row=3, column=0, sticky="ew")
        self._atualizar_botoes()

    def _atualizar_botoes(self):
        for widget in (self.botao_gerar, self.entrada_dimensao, self.entrada_semente):
            widget.state(["disabled"] if self.ocupada else ["!disabled"])
        self.botao_buscar.state(["!disabled"] if self.problema and not self.ocupada else ["disabled"])
        pode_executar = bool(self.resultado and self.resultado.encontrou
                             and self.passo < len(self.resultado.caminho) and not self.ocupada)
        self.botao_executar.state(["!disabled"] if pode_executar else ["disabled"])
        self.botao_passo.state(["!disabled"] if pode_executar and self.animacao_id is None else ["disabled"])
        self.botao_reiniciar.state(["!disabled"] if self.resultado and self.resultado.encontrou and not self.ocupada else ["disabled"])

    def _iniciar_tarefa(self, tipo, funcao):
        self.ocupada = True
        self._atualizar_botoes()

        def trabalhar():
            try:
                self.fila.put((tipo, funcao(), None))
            except Exception as erro:
                self.fila.put((tipo, None, erro))

        Thread(target=trabalhar, daemon=True).start()

    def gerar(self):
        if self.ocupada:
            return
        try:
            dimensao = int(self.dimensao_var.get())
            if dimensao < 1:
                raise ValueError
            semente_texto = self.semente_var.get().strip()
            semente = int(semente_texto) if semente_texto else None
        except ValueError:
            messagebox.showerror("Entrada inválida", "Informe uma dimensão inteira maior que zero e uma semente inteira (ou vazia).", parent=self)
            return
        self._pausar()
        self.status_var.set("Gerando o mundo aleatório…")
        self._iniciar_tarefa("mundo", lambda: gerar_mundo(dimensao, semente))

    def buscar(self):
        if self.ocupada or self.problema is None:
            return
        self._pausar()
        self._limpar_plano()
        self.cancelamento = Event()
        self.botao_cancelar.state(["!disabled"])
        self.status_var.set("Buscando o plano de menor custo…")
        problema, evento = self.problema, self.cancelamento
        self._iniciar_tarefa("busca", lambda: busca_custo_uniforme(problema, evento))

    def _receber_resultados(self):
        try:
            tipo, valor, erro = self.fila.get_nowait()
        except Empty:
            pass
        else:
            self.ocupada = False
            self.botao_cancelar.state(["disabled"])
            if erro is not None:
                self.status_var.set("Não foi possível concluir a operação.")
                messagebox.showerror("Erro", str(erro) or type(erro).__name__, parent=self)
            elif tipo == "mundo":
                self.problema = valor
                self._limpar_plano()
                self.mundo_var.set(f"{valor.dimensao} × {valor.dimensao}  |  {len(valor.obstaculos)} obstáculos  |  {len(valor.pedagios)} pedágios")
                self.ajustar()
                self.status_var.set("Mundo gerado. Busque a rota para verificar se a entrega é possível.")
            else:
                self._mostrar_resultado(valor)
            self._atualizar_botoes()
        self.after(80, self._receber_resultados)

    def _limpar_plano(self):
        self.resultado = None
        self.passo = 0
        self.custo_executado = 0
        for item in self.tabela.get_children():
            self.tabela.delete(item)
        self.estado = self.problema.estado_inicial if self.problema else None
        self.resumo_var.set("Busque uma rota para calcular o plano ótimo.")
        self._atualizar_execucao()
        self.desenhar()

    def _mostrar_resultado(self, resultado: ResultadoBusca):
        self.resultado = resultado
        estatisticas = f"{resultado.expandidos} estados expandidos em {resultado.tempo:.3f} s."
        if resultado.cancelada:
            self.resumo_var.set("Busca cancelada. Você pode tentar novamente.")
            self.status_var.set("A busca foi cancelada; a existência de solução não foi determinada.")
        elif not resultado.encontrou:
            self.resumo_var.set(f"Não existe solução para este mundo.\n{estatisticas}")
            self.status_var.set("Não existe solução: os obstáculos impedem a coleta ou a entrega do pacote.")
        else:
            self.resumo_var.set(f"Custo ótimo: {resultado.custo_final}  |  {len(resultado.caminho)} ações\n{estatisticas}")
            acumulado = 0
            for i, (acao, custo) in enumerate(zip(resultado.caminho, resultado.custos), start=1):
                acumulado += custo
                self.tabela.insert("", "end", iid=str(i), values=(
                    i, acao, str(resultado.estados[i].pos_atual), custo, acumulado))
            self.status_var.set("Plano ótimo encontrado. Execute a animação ou avance uma ação por vez."
                                if resultado.caminho else "O pacote já está no destino. Custo ótimo: 0.")
        self._atualizar_execucao()
        self.desenhar()

    def _atualizar_execucao(self):
        if self.estado is None or self.problema is None:
            return
        situacao = "com o agente" if self.estado.carregando else f"em {self.estado.produto}"
        if self.problema.objetivo(self.estado):
            situacao = "entregue no destino"
        total = len(self.resultado.caminho) if self.resultado and self.resultado.encontrou else 0
        self.execucao_var.set(f"Agente: {self.estado.pos_atual}  |  Pacote: {situacao}\nAções executadas: {self.passo}/{total}  |  Custo acumulado: {self.custo_executado}")

    def proximo_passo(self):
        if self.resultado is None or not self.resultado.encontrou or self.ocupada:
            return
        if self.passo >= len(self.resultado.caminho):
            return
        acao = self.resultado.caminho[self.passo]
        self.estado, custo = self.problema.aplicar_acao(self.estado, acao)
        self.custo_executado += custo
        self.passo += 1
        self.tabela.selection_set(str(self.passo))
        self.tabela.see(str(self.passo))
        self._atualizar_execucao()
        self._acompanhar_agente()
        self.desenhar()
        if self.passo == len(self.resultado.caminho):
            self._pausar()
            self.status_var.set(f"Entrega concluída! {self.passo} ações, custo total {self.custo_executado}.")
        else:
            self.status_var.set(f"Ação {self.passo}: {acao}. Custo da ação: {custo}.")
        self._atualizar_botoes()

    def executar(self):
        if self.animacao_id is not None:
            self._pausar()
            self._atualizar_botoes()
            return
        if not self.resultado or not self.resultado.encontrou or self.passo >= len(self.resultado.caminho):
            return
        self.botao_executar.configure(text="Pausar")
        self.animacao_id = self.after(1, self._animar)
        self._atualizar_botoes()

    def _animar(self):
        self.animacao_id = None
        self.proximo_passo()
        if self.resultado and self.passo < len(self.resultado.caminho):
            self.animacao_id = self.after(max(1, int(self.velocidade_var.get())), self._animar)
            self._atualizar_botoes()

    def _pausar(self):
        if self.animacao_id is not None:
            self.after_cancel(self.animacao_id)
            self.animacao_id = None
        self.botao_executar.configure(text="Executar")

    def reiniciar(self):
        if self.ocupada or self.problema is None:
            return
        self._pausar()
        self.passo = 0
        self.custo_executado = 0
        self.estado = self.problema.estado_inicial
        self.tabela.selection_remove(*self.tabela.selection())
        self._atualizar_execucao()
        self._acompanhar_agente()
        self.desenhar()
        self._atualizar_botoes()
        self.status_var.set("Execução reiniciada. O plano calculado foi mantido.")

    def ajustar(self):
        self.ajuste_automatico = True
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self._redimensionar()

    def _redimensionar(self, event=None):
        if self.problema and self.ajuste_automatico:
            disponivel = min(self.canvas.winfo_width(), self.canvas.winfo_height()) - 24
            self.tamanho_celula = max(14, min(72, disponivel / self.problema.dimensao))
        self.desenhar()

    def _zoom(self, fator):
        self.ajuste_automatico = False
        self.tamanho_celula = max(14, min(100, self.tamanho_celula * fator))
        self.desenhar()

    def _rolar_x(self, *args):
        self.canvas.xview(*args)
        self.desenhar()

    def _rolar_y(self, *args):
        self.canvas.yview(*args)
        self.desenhar()

    def _roda_mouse(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self.desenhar()

    def _acompanhar_agente(self):
        if self.estado is None:
            return
        tamanho = self.tamanho_celula
        lado = self.problema.dimensao * tamanho + 24
        for eixo, pos, visivel in (("x", self.estado.pos_atual[0], self.canvas.winfo_width()),
                                  ("y", self.estado.pos_atual[1], self.canvas.winfo_height())):
            inicio = self.canvas.canvasx(0) if eixo == "x" else self.canvas.canvasy(0)
            centro = 12 + (pos + 0.5) * tamanho
            if centro < inicio + tamanho / 2 or centro > inicio + visivel - tamanho / 2:
                mover = self.canvas.xview_moveto if eixo == "x" else self.canvas.yview_moveto
                mover(max(0, (centro - visivel / 2) / lado))

    def desenhar(self):
        self.canvas.delete("all")
        if self.problema is None or self.estado is None:
            return
        n, t = self.problema.dimensao, self.tamanho_celula
        lado = n * t + 24
        self.canvas.configure(scrollregion=(0, 0, lado, lado))
        # Renderiza apenas a região visível; grids grandes usam rolagem/zoom.
        x0 = max(0, int((self.canvas.canvasx(0) - 12) // t))
        y0 = max(0, int((self.canvas.canvasy(0) - 12) // t))
        x1 = min(n, int((self.canvas.canvasx(self.canvas.winfo_width()) - 12) // t) + 1)
        y1 = min(n, int((self.canvas.canvasy(self.canvas.winfo_height()) - 12) // t) + 1)

        def centro(pos):
            return 12 + (pos[0] + 0.5) * t, 12 + (pos[1] + 0.5) * t

        def visivel(pos):
            return x0 <= pos[0] < x1 and y0 <= pos[1] < y1

        for y in range(y0, y1):
            for x in range(x0, x1):
                pos = (x, y)
                cor = "#111827" if pos in self.problema.obstaculos else "#a3a3a3" if pos in self.problema.pedagios else "#f8fafc"
                self.canvas.create_rectangle(12 + x * t, 12 + y * t,
                                             12 + (x + 1) * t, 12 + (y + 1) * t,
                                             fill=cor, outline="#d6dee8")
        if self.resultado and self.resultado.encontrou:
            for anterior, seguinte in zip(self.resultado.estados, self.resultado.estados[1:]):
                if anterior.pos_atual != seguinte.pos_atual and (visivel(anterior.pos_atual) or visivel(seguinte.pos_atual)):
                    cor = "#ea8b27" if anterior.carregando else "#72a6ef"
                    self.canvas.create_line(*centro(anterior.pos_atual), *centro(seguinte.pos_atual),
                                            fill=cor, width=max(2, t * 0.09))
        destino = self.problema.destino
        if visivel(destino):
            cx, cy = centro(destino)
            raio = t * 0.32
            self.canvas.create_rectangle(cx - raio, cy - raio, cx + raio, cy + raio,
                                         outline="#15803d", width=max(2, t * 0.04))
            self.canvas.create_text(cx, cy, text="X", fill="#15803d",
                                    font=("Segoe UI", max(8, int(t * 0.32)), "bold"))
        if not self.estado.carregando and visivel(self.estado.produto):
            cx, cy = centro(self.estado.produto)
            raio = t * 0.23
            self.canvas.create_rectangle(cx - raio, cy - raio, cx + raio, cy + raio,
                                         fill="#fbbf24", outline="#92400e", width=2)
            self.canvas.create_text(cx, cy, text="P", fill="#78350f",
                                    font=("Segoe UI", max(7, int(t * 0.23)), "bold"))
        if visivel(self.estado.pos_atual):
            cx, cy = centro(self.estado.pos_atual)
            raio = t * 0.29
            self.canvas.create_oval(cx - raio, cy - raio, cx + raio, cy + raio,
                                    fill="#2563eb", outline="#ffffff", width=2)
            self.canvas.create_text(cx, cy, text="A", fill="white",
                                    font=("Segoe UI", max(7, int(t * 0.26)), "bold"))
            if self.estado.carregando:
                self.canvas.create_rectangle(cx + t * 0.1, cy + t * 0.1,
                                             cx + t * 0.36, cy + t * 0.36,
                                             fill="#fbbf24", outline="#92400e")

    def fechar(self):
        self.cancelamento.set()
        self._pausar()
        self.destroy()


if __name__ == "__main__":
    InterfaceApp().mainloop()
