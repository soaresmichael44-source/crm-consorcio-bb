"""App do gerente do banco: só tira foto da lista e manda para o CRM BB.

Roda num processo e numa porta separados do CRM (app.py) de propósito: é a
única parte que vai para a internet, e aqui não existe rota que mostre
clientes, fila ou configurações. Cada gerente entra por um link com token
próprio (/g/<token>), que o consultor cria e desativa em Configurações.
"""
import io
import os
import re
import secrets
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

from flask import Flask, abort, jsonify, render_template, request, send_file  # noqa: E402

import armazenamento  # noqa: E402
import db  # noqa: E402
import ia  # noqa: E402
import sdr  # noqa: E402

app = Flask(__name__)
app.jinja_env.globals["fmt_tel"] = db.telefone_formatado
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
MIMES_ACEITOS = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}
MAX_FOTOS_POR_ENVIO = 8
MAX_ENVIOS_POR_HORA = 20
_envios = defaultdict(deque)  # token -> horários dos últimos envios
# Demonstração (banco de exemplo): a comemoração aparece sempre e nenhuma
# mensagem é preparada para os contatos enviados.
DEMO_PROCESSO = os.getenv("GERENTE_DEMO") == "1"   # processo inteiro de demonstração (uso no Mac)
TOKEN_DEMO = os.getenv("TOKEN_DEMO_GERENTE", "demonstracao-gerente-carlos-2026")
PASTA_DEMO = Path(os.getenv("PASTA_DEMO", "/tmp/crm-bb-demo"))


def _eh_demo():
    return DEMO_PROCESSO or db.usando_demo() is not None
if os.getenv("PASTA_DOCS"):
    db.PASTA_DOCS = Path(os.environ["PASTA_DOCS"])

db.iniciar()


def _gerente(token):
    if not token or len(token) < 20:
        abort(404)
    with db.conectar() as conn:
        g = conn.execute("SELECT * FROM gerentes WHERE token = ? AND ativo = 1", (token,)).fetchone()
    if not g:
        abort(404)
    return g


def _limite_ok(token):
    agora = time.time()
    fila = _envios[token]
    while fila and agora - fila[0] > 3600:
        fila.popleft()
    if len(fila) >= MAX_ENVIOS_POR_HORA:
        return False
    fila.append(agora)
    return True


def _montar_demo():
    """Recria o banco de exemplo (gerente "Carlos", clientes fictícios) num processo à parte, sem
    nenhuma variável da nuvem: nada disso encosta no banco real nem no Supabase."""
    import subprocess, sys
    PASTA_DEMO.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY")}
    env.update(DEMO_DB=str(PASTA_DEMO / "gerente_demo.db"), DEMO_DOCS=str(PASTA_DEMO / "documentos_demo"))
    subprocess.run([sys.executable, str(BASE / "demo" / "montar_demo_gerente.py")], env=env, cwd=str(BASE), check=True,
                   capture_output=True, timeout=120)


@app.before_request
def _talvez_demo():
    """O link de demonstração usa o banco de exemplo, refeito a cada 6 h para os horários ("há 20 min") seguirem vivos."""
    db._banco_demo.set(None)  # nunca herdar a marca de um acesso anterior na mesma linha de execução
    if TOKEN_DEMO and request.path.startswith((f"/g/{TOKEN_DEMO}", f"/manifest/{TOKEN_DEMO}")) and not DEMO_PROCESSO:
        arq = PASTA_DEMO / "gerente_demo.db"
        if not arq.exists() or time.time() - arq.stat().st_mtime > 6 * 3600:
            _montar_demo()
        db._banco_demo.set({"db": str(arq), "docs": str(PASTA_DEMO / "documentos_demo")})


@app.teardown_request
def _fim_demo(_erro=None):
    db._banco_demo.set(None)


@app.after_request
def _cabecalhos(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/")
def raiz():
    abort(404)


# Como o gerente enxerga cada etapa: sempre verdade, no tom de um extrato.
# Perdido e "pediu para sair" viram um discreto "fora do momento", sem destaque.
ETAPAS_GERENTE = [  # (etapas do CRM, chave visual, título do grupo)
    (("vendido",), "fechado", "Cartas fechadas"),
    (("documentacao",), "documentacao", "Documentação recebida"),
    (("negociando",), "negociando", "Em negociação"),
    (("respondeu",), "interessado", "Interessados"),
    (("contatado",), "contato", "Em contato"),
    (("novo",), "fila", "Na fila de contato"),
    (("perdido", "optout"), "fora", "Fora do momento"),
]
CHAVE_DA_ETAPA = {e: chave for etapas, chave, _ in ETAPAS_GERENTE for e in etapas}
# Frases do feed de movimentações, no tom de quem acompanha a negociação de perto.
MOVIMENTO = {
    "vendido": ("🏆", "fechou a carta{p}! Venda concretizada"),
    "documentacao": ("📄", "enviou a documentação{p}, pronta para análise de crédito"),
    "negociando": ("🔥", "está negociando{p} — proposta na mesa"),
    "respondeu": ("✨", "respondeu e quer saber mais{p}"),
}  # "Em contato" fica de fora: o feed mostra só quem já evoluiu na conversa.
# Tipo da carta de consórcio, como o gerente fala na agência.
TIPOS = {"imovel": ("🏠", "Imóvel"), "auto": ("🚗", "Automóvel"), "pesados": ("🚛", "Pesados"),
         "moto": ("🏍️", "Moto"), "servicos": ("🎓", "Serviços")}


def _tipo(produto):
    chave = (produto or "").strip().lower().translate(str.maketrans("áâãéêíóôõúç", "aaaeeiooouc"))
    return TIPOS.get(chave, ("💼", (produto or "Consórcio").capitalize()))


PESO_CALOR = {"vendido": 4, "documentacao": 4, "negociando": 3, "respondeu": 2, "contatado": 1}


def _primeiro_nome(nome):
    partes = (nome or "").split()
    if not partes:
        return "Cliente"
    # "Maria S." — o gerente reconhece o cliente sem expor o nome completo na tela.
    return partes[0] + (f" {partes[-1][0]}." if len(partes) > 1 else "")


def _ha_quanto(ts):
    try:
        seg = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return ""
    if seg < 3600:
        return f"há {max(int(seg // 60), 1)} min"
    if seg < 86400:
        return f"há {int(seg // 3600)} h"
    dias = int(seg // 86400)
    return "ontem" if dias == 1 else f"há {dias} dias"


def _temperatura(rows):
    """0–100: quanto da carteira indicada já está conversando, negociando ou fechou."""
    if not rows:
        return 0, "Aguardando a primeira lista"
    t = round(100 * sum(PESO_CALOR.get(r["etapa"], 0) for r in rows) / (4 * len(rows)))
    if t >= 40:
        return t, "Pegando fogo"
    if t >= 22:
        return t, "Esquentando"
    if t >= 8:
        return t, "Aquecendo"
    return t, "Começando"


def _posicao_na_barra(t):
    """A barra tem 4 faixas iguais (Começando, Aquecendo, Esquentando, Fogo) com limites 8/22/40:
    o marcador cai dentro da faixa que dá nome ao clima."""
    faixas = [(0, 8), (8, 22), (22, 40), (40, 100)]
    for i, (ini, fim) in enumerate(faixas):
        if t <= fim:
            return round(25 * i + 25 * (t - ini) / (fim - ini))
    return 100


@app.get("/g/<token>")
def pagina(token):
    g = _gerente(token)
    with db.conectar() as conn:
        cfg = db.config(conn)
        rows = conn.execute(
            "SELECT c.nome, c.etapa, c.produto, c.valor_credito, c.atualizado_em, l.criado_em AS lote_em, l.descricao AS lote_desc FROM clientes c "
            "JOIN lotes l ON l.id = c.lote_id WHERE l.gerente_id = ? ORDER BY c.atualizado_em DESC", (g["id"],)).fetchall()
        pacotes = [dict(p) for p in conn.execute(
            "SELECT * FROM pacotes_docs WHERE gerente_id = ? ORDER BY id DESC LIMIT 30", (g["id"],))]
        for p in pacotes:
            p["docs"] = [dict(d) for d in conn.execute("SELECT * FROM documentos WHERE pacote_id = ? ORDER BY id", (p["id"],))]
            p["quando"] = _ha_quanto(p["criado_em"])
        if not _eh_demo():
            conn.execute("UPDATE gerentes SET ultima_visita = ? WHERE id = ?", (db.agora(), g["id"]))

    ultima = g["ultima_visita"] or ""

    def _item(r):
        icone, tipo = _tipo(r["produto"])
        return {"nome": _primeiro_nome(r["nome"]), "produto": r["produto"] or "", "tipo": tipo, "tipo_icone": icone,
                "credito": r["valor_credito"] or 0}

    # Feed: o que andou na carteira, mais recente primeiro; "novo" = depois da última visita.
    movimentos = []
    for r in rows:
        if r["etapa"] in MOVIMENTO:
            icone, frase = MOVIMENTO[r["etapa"]]
            movimentos.append({**_item(r), "icone": icone, "etapa": r["etapa"],
                               "texto": frase.format(p=f" de {r['produto']}" if r["produto"] else ""),
                               "valor": r["valor_credito"] or 0,
                               "quando": _ha_quanto(r["atualizado_em"]), "novo": r["atualizado_em"] > ultima})
    movimentos = movimentos[:10]
    vendas_novas = [m["nome"] for m in movimentos if m["novo"] and m["etapa"] == "vendido"]
    na_mesa = [{**_item(r), "etapa": r["etapa"], "quando": _ha_quanto(r["atualizado_em"])}
               for r in sorted(rows, key=lambda r: r["etapa"] != "documentacao")
               if r["etapa"] in ("documentacao", "negociando", "respondeu")][:8]
    grupos = []
    for etapas, chave, titulo in ETAPAS_GERENTE:
        itens = sorted((_item(r) for r in rows if r["etapa"] in etapas), key=lambda c: c["nome"])
        if itens:
            grupos.append({"chave": chave, "titulo": titulo, "itens": itens})
    regua = [g_["chave"] for g_ in grupos for _ in g_["itens"]]
    # Listas por dia de envio (últimos 7 dias): só números, para o gerente ver o andamento sem a aba encher de nomes.
    hoje = datetime.now().date()
    por_dia = {}
    for r in rows:
        if (r["lote_desc"] or "").startswith("Documentos enviados pelo cliente"):
            continue
        try:
            dia = datetime.strptime(r["lote_em"][:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if (hoje - dia).days > 6:
            continue
        d = por_dia.setdefault(dia, {"total": 0, "contatados": 0, "interessados": 0, "fechados": 0})
        d["total"] += 1
        d["contatados"] += r["etapa"] != "novo"
        d["interessados"] += r["etapa"] in ("respondeu", "negociando", "documentacao", "vendido")
        d["fechados"] += r["etapa"] == "vendido"
    listas_dia = []
    for dia in sorted(por_dia, reverse=True):
        dias = (hoje - dia).days
        rotulo = "Hoje" if dias == 0 else ("Ontem" if dias == 1 else f"{dia:%d/%m}")
        listas_dia.append({"rotulo": rotulo, **por_dia[dia],
                           "pct": round(100 * por_dia[dia]["contatados"] / por_dia[dia]["total"])})
    vendidos = [r for r in rows if r["etapa"] == "vendido"]
    temperatura, clima = _temperatura(rows)

    def _soma(etapa):
        return sum(r["valor_credito"] or 0 for r in rows if r["etapa"] == etapa)
    # Crédito por tipo de carta: o que já fechou e o que está em negociação.
    por_tipo = {}
    for r in rows:
        if r["etapa"] in ("vendido", "documentacao", "negociando", "respondeu"):
            icone, tipo = _tipo(r["produto"])
            t = por_tipo.setdefault(tipo, {"tipo": tipo, "icone": icone, "fechado": 0, "cartas": 0, "andamento": 0})
            if r["etapa"] == "vendido":
                t["fechado"] += r["valor_credito"] or 0
                t["cartas"] += 1
            else:
                t["andamento"] += r["valor_credito"] or 0
    por_tipo = sorted(por_tipo.values(), key=lambda t: (-t["fechado"], -t["andamento"]))
    maior = max((t["fechado"] + t["andamento"] for t in por_tipo), default=0) or 1
    for t in por_tipo:
        t["pct_fechado"] = round(100 * t["fechado"] / maior)
        t["pct_andamento"] = round(100 * t["andamento"] / maior)
    placar = {
        "indicados": len(rows),
        "vendas": len(vendidos),
        "valor_vendido": sum(r["valor_credito"] or 0 for r in vendidos),
        "andamento": sum(r["etapa"] in ("documentacao", "negociando", "respondeu") for r in rows),
        "em_negociacao_valor": sum(r["valor_credito"] or 0 for r in rows if r["etapa"] in ("documentacao", "negociando", "respondeu")),
        "ultima_venda": _ha_quanto(max((r["atualizado_em"] for r in vendidos), default=None)),
        "temperatura": temperatura, "clima": clima, "posicao": _posicao_na_barra(temperatura),
        "faixas": [("📄", "Documentação", _soma("documentacao")), ("🔥", "Proposta na mesa", _soma("negociando")),
                   ("✨", "Interessados", _soma("respondeu"))],
        "ticket_medio": round(sum(r["valor_credito"] or 0 for r in vendidos) / len(vendidos)) if vendidos else 0,
        "por_tipo": por_tipo,
    }
    docs_novos = sum(1 for p in pacotes if not p["visto_em"])
    return render_template("gerente.html", g=g, token=token, consultor=cfg["nome_consultor"],
                           grupos=grupos, regua=regua, listas_dia=listas_dia, placar=placar, movimentos=movimentos,
                           na_mesa=na_mesa, vendas_novas=vendas_novas, pacotes=pacotes, docs_novos=docs_novos,
                           movs_novos=sum(1 for m in movimentos if m["novo"]), carregado_em=db.agora())


def _pacote_do_gerente(conn, g, pacote_id):
    p = conn.execute("SELECT * FROM pacotes_docs WHERE id = ? AND gerente_id = ?", (pacote_id, g["id"])).fetchone()
    if not p:
        abort(404)
    return p


def _nome_arquivo(cliente_nome, nome):
    """'Ana Paula Ribeiro - Documento com foto 1.pdf': o gerente sabe de quem é ao anexar no sistema do banco."""
    limpo = "".join(ch for ch in cliente_nome if ch.isalnum() or ch in " -").strip()
    return f"{limpo} - {nome}" if limpo and not nome.lower().startswith(limpo.lower()) else nome


@app.get("/g/<token>/doc/<int:doc_id>")
def baixar_doc(token, doc_id):
    """Só abre documento de pacote endereçado a este gerente. ?baixar=1 salva o arquivo em vez de abrir."""
    g = _gerente(token)
    with db.conectar() as conn:
        d = conn.execute(
            "SELECT d.*, p.cliente_nome FROM documentos d JOIN pacotes_docs p ON p.id = d.pacote_id "
            "WHERE d.id = ? AND p.gerente_id = ?", (doc_id, g["id"])).fetchone()
        if not d:
            abort(404)
        if not _eh_demo():
            conn.execute("UPDATE documentos SET baixado_em = COALESCE(baixado_em, ?) WHERE id = ?", (db.agora(), doc_id))
    resp = send_file(io.BytesIO(armazenamento.ler("documentos", d["caminho"])), download_name=_nome_arquivo(d["cliente_nome"], d["nome"]),
                     mimetype=d["mime"] or "application/octet-stream", as_attachment=request.args.get("baixar") == "1")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.get("/g/<token>/pacote/<int:pacote_id>/todos.zip")
def baixar_pacote(token, pacote_id):
    """Todos os documentos do cliente num .zip só, para anexar de uma vez no sistema do banco ou num e-mail."""
    g = _gerente(token)
    with db.conectar() as conn:
        p = _pacote_do_gerente(conn, g, pacote_id)
        docs = conn.execute("SELECT * FROM documentos WHERE pacote_id = ? ORDER BY id", (pacote_id,)).fetchall()
        if not _eh_demo():
            conn.execute("UPDATE documentos SET baixado_em = COALESCE(baixado_em, ?) WHERE pacote_id = ?", (db.agora(), pacote_id))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in docs:
            z.writestr(_nome_arquivo(p["cliente_nome"], d["nome"]), armazenamento.ler("documentos", d["caminho"]))
    buf.seek(0)
    resp = send_file(buf, download_name=_nome_arquivo(p["cliente_nome"], "documentos.zip"), mimetype="application/zip", as_attachment=True)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.get("/g/<token>/novidades")
def novidades(token):
    """Consultado pelo portal aberto a cada minuto: documentos não vistos e movimentações desde `desde`."""
    g = _gerente(token)
    desde = request.args.get("desde") or db.agora()
    with db.conectar() as conn:
        docs = conn.execute("SELECT COUNT(*) FROM pacotes_docs WHERE gerente_id = ? AND visto_em IS NULL", (g["id"],)).fetchone()[0]
        movs = conn.execute(
            "SELECT COUNT(*) FROM clientes c JOIN lotes l ON l.id = c.lote_id WHERE l.gerente_id = ? "
            "AND c.etapa IN ('vendido', 'documentacao', 'negociando', 'respondeu', 'contatado') AND c.atualizado_em > ?", (g["id"], desde)).fetchone()[0]
    return jsonify(docs=docs, movimentos=movs)


@app.post("/g/<token>/docs/vistos")
def docs_vistos(token):
    g = _gerente(token)
    if not _eh_demo():
        with db.conectar() as conn:
            conn.execute("UPDATE pacotes_docs SET visto_em = ? WHERE gerente_id = ? AND visto_em IS NULL", (db.agora(), g["id"]))
    return jsonify(ok=True)


@app.post("/g/<token>/pacote/<int:pacote_id>/recebido")
def pacote_recebido(token, pacote_id):
    g = _gerente(token)
    with db.conectar() as conn:
        _pacote_do_gerente(conn, g, pacote_id)
        conn.execute("UPDATE pacotes_docs SET recebido_em = COALESCE(recebido_em, ?) WHERE id = ?", (db.agora(), pacote_id))
    return jsonify(ok=True)


PRODUTOS = {"auto", "imóvel", "moto", "pesados", "serviços"}


@app.post("/g/<token>/contato")
def contato_manual(token):
    """O gerente digita um cliente que está vendo na tela do banco (nome, telefone, e-mail, CPF)."""
    g = _gerente(token)
    if not _limite_ok(token):
        return jsonify(erro="Muitos envios na última hora. Tente de novo mais tarde."), 429
    d = request.get_json(force=True, silent=True) or {}
    nome = " ".join((d.get("nome") or "").split())[:120]
    telefone, email = (d.get("telefone") or "").strip(), (d.get("email") or "").strip()[:120]
    cpf = (d.get("cpf") or "").strip()
    if len(nome) < 3:
        return jsonify(erro="Digite o nome do cliente."), 400
    if not email:
        return jsonify(erro="Digite o e-mail do cliente."), 400
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(erro="E-mail incompleto. Confira, ex.: nome@gmail.com"), 400
    if not telefone:
        return jsonify(erro="Digite o telefone do cliente com DDD."), 400
    if not db.normalizar_telefone(telefone):
        return jsonify(erro="Telefone incompleto. Use DDD + número, ex.: (31) 99999-8888."), 400
    if cpf and not db.normalizar_cpf(cpf):
        return jsonify(erro="CPF inválido. Confira os números."), 400
    produto = d.get("produto") if d.get("produto") in PRODUTOS else ""
    cliente = {"nome": nome, "telefone": telefone, "email": email, "cpf": cpf, "produto": produto,
               "observacoes": (d.get("observacao") or "").strip()[:500], "situacao_bb": "Indicado pelo gerente"}
    with db.conectar() as conn:
        lote_id, novos, _ = db.importar_lote(conn, [cliente], f"Digitado pelo gerente · {g['nome']}", 0, gerente_id=g["id"])
        conn.execute("UPDATE gerentes SET ultimo_envio = ? WHERE id = ?", (db.agora(), g["id"]))
    if not _eh_demo():
        sdr.processar_lote_em_segundo_plano(lote_id)
    return jsonify(ok=True, novo=bool(novos), nome=_primeiro_nome(nome.title()))


@app.post("/g/<token>/enviar")
def enviar(token):
    g = _gerente(token)
    if not _limite_ok(token):
        return jsonify(erro="Muitos envios na última hora. Tente de novo mais tarde."), 429
    fotos = request.files.getlist("fotos")
    if not fotos:
        return jsonify(erro="Nenhum arquivo recebido."), 400
    if len(fotos) > MAX_FOTOS_POR_ENVIO:
        return jsonify(erro=f"Mande no máximo {MAX_FOTOS_POR_ENVIO} arquivos por vez."), 400
    imagens = []
    for f in fotos:
        mime = (f.mimetype or "").lower()
        if mime not in MIMES_ACEITOS:
            return jsonify(erro="Envie fotos (JPG, PNG, HEIC) ou PDF."), 400
        dados = f.read()
        ext = "pdf" if mime == "application/pdf" else "jpg"
        armazenamento.salvar("uploads", f"{datetime.now():%Y-%m-%d/%H%M%S}-g{g['id']}-{secrets.token_hex(3)}.{ext}", dados, mime)
        imagens.append((dados, mime))
    try:
        lidos, obs = ia.ler_listas(imagens)
    except ia.ErroIA:
        return jsonify(erro="Não consegui ler os arquivos agora. Espere um minuto e envie de novo."), 502
    lidos = [c for c in lidos if (c.get("nome") or "").strip()]
    if not lidos:
        return jsonify(erro="Não encontrei nenhum contato. Se for foto, tente mais de perto e sem reflexo."), 422
    desc = f"Enviado pelo celular · {g['nome']}" + (f" · ag. {g['agencia']}" if g["agencia"] else "")
    with db.conectar() as conn:
        lote_id, novos, atualizados = db.importar_lote(conn, lidos, desc, len(imagens), gerente_id=g["id"])
        conn.execute("UPDATE gerentes SET ultimo_envio = ? WHERE id = ?", (db.agora(), g["id"]))
    if not _eh_demo():
        sdr.processar_lote_em_segundo_plano(lote_id)
    sem_tel = sum(1 for c in lidos if not db.normalizar_telefone(c.get("telefone")))
    return jsonify(
        ok=True, lote=lote_id, novos=novos, atualizados=atualizados, sem_telefone=sem_tel,
        # O gerente vê só os nomes que ele mesmo fotografou, para conferir a leitura.
        nomes=[(c.get("nome") or "").strip().title() for c in lidos],
        observacao=obs,
    )


@app.template_filter("reais")
def _reais(v):
    if v >= 1_000_000:
        return f"R$ {v / 1_000_000:.1f} mi".replace(".", ",")
    if v >= 1000:
        return f"R$ {v / 1000:.0f} mil"
    return f"R$ {v:.0f}"


@app.template_filter("reais_partes")
def _reais_partes(v):
    """525000 -> ("525", "mil"); 1250000 -> ("1,25", "milhão"): número grande e unidade pequena no cartão."""
    if v >= 1_000_000:
        n = f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        return n, "milhão" if v < 2_000_000 else "milhões"
    if v >= 1000:
        return f"{v / 1000:.0f}", "mil"
    return f"{v:.0f}", ""


# ─────────── Link do cliente: manda documentos direto ao gerente ───────────
TIPOS_DOC = [  # campo do formulário, nome que o gerente vê
    ("identidade", "Documento com foto"),
    ("renda", "Comprovante de renda"),
    ("endereco", "Comprovante de endereço"),
]
MIMES_DOC_CLIENTE = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


def _gerente_por_link_cliente(token_docs):
    if not token_docs or len(token_docs) < 20:
        abort(404)
    with db.conectar() as conn:
        g = conn.execute("SELECT * FROM gerentes WHERE token_docs = ? AND ativo = 1", (token_docs,)).fetchone()
    if not g:
        abort(404)
    return g


@app.get("/d/<token_docs>")
def pagina_cliente(token_docs):
    g = _gerente_por_link_cliente(token_docs)
    with db.conectar() as conn:
        cfg = db.config(conn)
    return render_template("cliente_docs.html", g=g, token=token_docs, tipos=TIPOS_DOC,
                           consultor=cfg["nome_consultor"], empresa=cfg["nome_empresa"])


@app.post("/d/<token_docs>/enviar")
def enviar_docs_cliente(token_docs):
    g = _gerente_por_link_cliente(token_docs)
    if not _limite_ok("docs:" + token_docs):
        return jsonify(erro="Muitos envios por este link na última hora. Tente de novo mais tarde."), 429
    nome = " ".join((request.form.get("nome") or "").split())[:120]
    telefone = db.normalizar_telefone(request.form.get("telefone"))
    if len(nome) < 3:
        return jsonify(erro="Digite o seu nome completo."), 400
    if not telefone:
        return jsonify(erro="Digite o seu telefone com DDD, ex.: (31) 99999-8888."), 400
    if request.form.get("aceite") != "1":
        return jsonify(erro="Marque a autorização para enviarmos os documentos ao gerente."), 400
    arquivos = []
    for campo, rotulo in TIPOS_DOC:
        for i, f in enumerate(request.files.getlist(campo)[:4], 1):
            mime = (f.mimetype or "").lower()
            if mime not in MIMES_DOC_CLIENTE:
                return jsonify(erro="Envie só fotos ou PDF."), 400
            ext = ".pdf" if mime == "application/pdf" else ".jpg"
            arquivos.append((f"{rotulo} {i}{ext}", f.read(), mime))
    if not arquivos:
        return jsonify(erro="Anexe pelo menos um documento."), 400
    with db.conectar() as conn:
        cliente_id = db.cliente_enviou_documentos(conn, g["id"], nome.title(), telefone, len(arquivos))
        db.salvar_pacote_docs(conn, g["id"], nome.title(), arquivos, observacao="Enviado pelo próprio cliente, pelo link de documentos.",
                              cliente_id=cliente_id, origem="cliente", cliente_telefone=telefone)
    return jsonify(ok=True, qtd=len(arquivos), nome=nome.split()[0].title())


@app.get("/manifest/<token>.webmanifest")
def manifest(token):
    g = _gerente(token)
    return jsonify(
        name="Listas Consórcio BB", short_name="Listas BB", start_url=f"/g/{token}",
        display="standalone", background_color="#16245c", theme_color="#16245c",
        icons=[{"src": "/static/icone-192.png", "sizes": "192x192", "type": "image/png"},
               {"src": "/static/icone-512.png", "sizes": "512x512", "type": "image/png"}],
        description=f"Envio de listas de {g['nome']}",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORTA_GERENTE", "8096")), debug=False)
