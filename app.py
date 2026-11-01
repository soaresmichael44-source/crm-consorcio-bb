"""CRM BB — operação exclusiva de clientes do Consórcio BB (listas do Banco do Brasil)."""
import io
import json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
# Carrega o .env ANTES de importar os módulos que leem variáveis na importação
# (mesma armadilha que já mordeu o CRM Mobie).
load_dotenv(BASE / ".env")

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for  # noqa: E402
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

import armazenamento  # noqa: E402
import db  # noqa: E402
import ia  # noqa: E402
import sdr  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
API_TOKEN = os.getenv("CRM_BB_API_TOKEN", "")
# Endereço público do app do gerente (gerente.py). Fixo pelo .env (nuvem/domínio próprio) ou, enquanto
# roda no Mac, o endereço atual do serviço com.michaelsoares.crm-bb-tunel (lido do log a cada uso).
_URL_FIXA = os.getenv("URL_PUBLICA_GERENTE", "").rstrip("/")
_LOG_TUNEL = BASE / "logs" / "tunel-fixo.log"


def url_publica_gerente():
    if _URL_FIXA:
        return _URL_FIXA
    try:
        achados = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", _LOG_TUNEL.read_text(errors="ignore"))
        return achados[-1] if achados else ""
    except OSError:
        return ""
MIMES_ACEITOS = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}

app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(24)
# Na nuvem (FORCAR_LOGIN=1) ninguém entra sem senha, nem "de dentro"; e o cookie só anda por https.
FORCAR_LOGIN = os.getenv("FORCAR_LOGIN") == "1"
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=30 * 86400,
                  SESSION_COOKIE_SECURE=FORCAR_LOGIN)
SENHA = os.getenv("CRM_BB_SENHA", "")
# Senha extra, só para apresentações: dá para apagar a variável no Render e ela para de valer na hora.
SENHA_APRESENTACAO = os.getenv("CRM_BB_SENHA_APRESENTACAO", "")
# Cópia de demonstração (banco vazio, link de teste): abre sem senha.
DEMO = os.getenv("CRM_BB_DEMO") == "1"
_tentativas = defaultdict(list)  # ip -> horários de senhas erradas

db.iniciar()


@app.before_request
def _zerar_demo():
    db._banco_demo.set(None)  # o CRM nunca usa o banco de demonstração do Portal


@app.before_request
def _exigir_login():
    """Neste Mac abre direto. De outro aparelho, ou sempre na nuvem, pede a senha (CRM_BB_SENHA).
    Representante entra com e-mail e senha própria e fica preso à carteira dele (g.rep)."""
    g.rep = None
    if session.get("rep_id"):
        with db.conectar() as conn:
            r = conn.execute("SELECT * FROM representantes WHERE id = ? AND ativo = 1", (session["rep_id"],)).fetchone()
        if r:
            g.rep = r
            return None
        session.clear()  # representante desativado: perde o acesso na hora
    if DEMO or session.get("logado") or (not FORCAR_LOGIN and request.remote_addr in ("127.0.0.1", "::1")):
        return None
    if request.path.startswith(("/static/", "/api/n8n/", "/acesso/")) or request.path in ("/login", "/app.webmanifest"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(erro="Sessão expirada. Entre de novo."), 401
    return redirect(url_for("login", proximo=request.full_path))


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = None
    proximo = request.values.get("proximo") or "/"
    if not proximo.startswith("/") or proximo.startswith("//"):
        proximo = "/"
    if request.method == "POST":
        ip = request.remote_addr
        _tentativas[ip] = [t for t in _tentativas[ip] if time.time() - t < 900]
        email = request.form.get("email", "").strip().lower()
        if len(_tentativas[ip]) >= 8:
            erro = "Muitas tentativas. Espere 15 minutos."
        elif email:
            with db.conectar() as conn:
                r = conn.execute("SELECT * FROM representantes WHERE LOWER(email) = ? AND ativo = 1", (email,)).fetchone()
                if r and r["senha_hash"] and check_password_hash(r["senha_hash"], request.form.get("senha", "")):
                    conn.execute("UPDATE representantes SET ultimo_acesso = ? WHERE id = ?", (db.agora(), r["id"]))
                    session.clear()
                    session.permanent = True
                    session["rep_id"] = r["id"]
                    return redirect(proximo)
            _tentativas[ip].append(time.time())
            erro = "E-mail ou senha incorretos."
        elif any(x and secrets.compare_digest(request.form.get("senha", ""), x) for x in (SENHA, SENHA_APRESENTACAO)):
            session.clear()
            session.permanent = True
            session["logado"] = True
            return redirect(proximo)
        else:
            _tentativas[ip].append(time.time())
            erro = "Senha incorreta." if SENHA else "Acesso de fora do Mac está desligado (sem CRM_BB_SENHA no .env)."
    return render_template("login.html", erro=erro, proximo=proximo)


@app.get("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/acesso/<token>", methods=["GET", "POST"])
def acesso(token):
    """Link de convite: o representante cria a própria senha (vale 7 dias e uma vez só)."""
    with db.conectar() as conn:
        r = conn.execute("SELECT * FROM representantes WHERE convite_token = ? AND ativo = 1", (token,)).fetchone()
        if not r or (r["convite_expira"] or "") < db.agora():
            return render_template("acesso.html", r=None)
        erro = None
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            senha = request.form.get("senha", "")
            dono = conn.execute("SELECT id FROM representantes WHERE LOWER(email) = ? AND id <> ?", (email, r["id"])).fetchone()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                erro = "Digite um e-mail válido."
            elif dono:
                erro = "Este e-mail já é de outro representante."
            elif len(senha) < 8:
                erro = "A senha precisa de pelo menos 8 caracteres."
            elif senha != request.form.get("senha2", ""):
                erro = "As duas senhas não são iguais."
            else:
                conn.execute("UPDATE representantes SET email = ?, senha_hash = ?, convite_token = NULL, convite_expira = NULL, "
                             "ultimo_acesso = ? WHERE id = ?", (email, generate_password_hash(senha, method="pbkdf2:sha256:600000"), db.agora(), r["id"]))
                session.clear()
                session.permanent = True
                session["rep_id"] = r["id"]
                return redirect(url_for("painel"))
    return render_template("acesso.html", r=r, erro=erro)


def so_admin(f):
    """Telas da operação inteira (configurações, rede, importação, cadência): representante não entra."""
    @wraps(f)
    def _f(*a, **k):
        if g.rep:
            if request.path.startswith("/api/"):
                return jsonify(erro="Sem permissão."), 403
            return redirect(url_for("painel"))
        return f(*a, **k)
    return _f


def _rep_id():
    return g.rep["id"] if g.rep else None


@app.context_processor
def _globais():
    novos = 0
    if g.get("rep"):  # selo no menu: interessados que chegaram e o representante ainda não abriu
        filtro, args = db.filtro_rep(g.rep["id"], "lote_id")
        with db.conectar() as conn:
            novos = conn.execute("SELECT COUNT(*) AS n FROM clientes WHERE repasse_em IS NOT NULL AND repasse_visto = 0 "
                                 "AND etapa NOT IN ('vendido', 'perdido', 'optout')" + filtro, args).fetchone()["n"]
    return {"ETAPAS": db.ETAPAS, "ETAPAS_DICT": db.ETAPAS_DICT, "fmt_tel": db.telefone_formatado, "fmt_cpf": db.cpf_formatado,
            "rep_logado": g.get("rep"), "repasses_novos": novos}


@app.template_filter("compacto")
def _compacto(v):
    """R$ 1,2 mi / R$ 350 mil: para os placares da Rede."""
    v = v or 0
    if v >= 1_000_000:
        return f"R$ {v / 1_000_000:.1f} mi".replace(".", ",")
    if v >= 1000:
        return f"R$ {v / 1000:.0f} mil"
    return f"R$ {v:.0f}"


@app.template_filter("dinheiro")
def _dinheiro(v):
    if v in (None, ""):
        return "—"
    return "R$ " + f"{v:,.0f}".replace(",", ".")


@app.template_filter("data")
def _data(v):
    if not v:
        return "—"
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").strftime("%d/%m %H:%M")
    except ValueError:
        return v


def _cliente_ou_404(conn, cid):
    filtro, args = db.filtro_rep(_rep_id(), "lote_id")
    c = conn.execute("SELECT * FROM clientes WHERE id = ?" + filtro, (cid, *args)).fetchone()
    if not c:
        abort(404)
    return c


# ---------------------------------------------------------------- páginas

@app.route("/")
def painel():
    rid = _rep_id()
    fc, ac = db.filtro_rep(rid, "c.lote_id")
    fl, al = db.filtro_rep(rid, "l.id")
    with db.conectar() as conn:
        cfg = db.config(conn)
        por_etapa = {r["etapa"]: r["n"] for r in conn.execute(
            "SELECT c.etapa, COUNT(*) n FROM clientes c WHERE 1=1" + fc + " GROUP BY c.etapa", ac)}
        credito_fechado = conn.execute(
            "SELECT COALESCE(SUM(c.valor_credito), 0) AS v FROM clientes c WHERE c.etapa = 'vendido'" + fc, ac).fetchone()["v"]
        fila = sdr.fila(conn, limite=5000, rep_id=rid)
        # Uma linha por representante: o que está na fila, com quem e quanto já fechou.
        reps = []
        if not rid:
            prod = db.producao_gerentes(conn)
            novos = {r["rid"]: r["n"] for r in conn.execute(
                "SELECT g.representante_id AS rid, COUNT(*) AS n FROM clientes c JOIN lotes l ON l.id = c.lote_id "
                "JOIN gerentes g ON g.id = l.gerente_id WHERE c.repasse_em IS NOT NULL AND c.repasse_visto = 0 "
                "AND c.etapa NOT IN ('vendido', 'perdido', 'optout') GROUP BY g.representante_id")}
            for r in conn.execute("SELECT * FROM representantes WHERE ativo = 1 ORDER BY nome"):
                ger = [x for x in prod if x["representante_id"] == r["id"]]
                reps.append({**dict(r), "agencias": len(ger), "prod": db.somar_producao(ger),
                             "fila": sum(1 for i in fila if i["rep_id"] == r["id"]), "novos": novos.get(r["id"], 0)})
        # Movimentação recente: quem mudou de etapa por último (o painel "respira" com a operação).
        recentes = conn.execute(
            "SELECT c.id, c.nome, c.etapa, c.valor_credito, c.atualizado_em, g.agencia, r.nome AS rep_nome FROM clientes c "
            "LEFT JOIN lotes l ON l.id = c.lote_id LEFT JOIN gerentes g ON g.id = l.gerente_id "
            "LEFT JOIN representantes r ON r.id = g.representante_id WHERE c.etapa <> 'novo'" + fc +
            " ORDER BY c.atualizado_em DESC LIMIT 8", ac).fetchall()
        # Interessado que chegou para o representante e ninguém abriu há mais de 24 h: precisa de cobrança.
        parados = conn.execute(
            "SELECT c.id, c.nome, c.repasse_em, r.nome AS rep_nome FROM clientes c JOIN lotes l ON l.id = c.lote_id "
            "JOIN gerentes g ON g.id = l.gerente_id JOIN representantes r ON r.id = g.representante_id "
            "WHERE c.repasse_visto = 0 AND c.repasse_em < ? AND c.etapa NOT IN ('vendido', 'perdido', 'optout')" + fc +
            " ORDER BY c.repasse_em LIMIT 6", [(datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"), *ac]).fetchall()
        docs_clientes = conn.execute(
            "SELECT p.id, p.cliente_id, p.cliente_nome FROM pacotes_docs p JOIN gerentes g ON g.id = p.gerente_id "
            "WHERE p.origem = 'cliente' AND p.visto_consultor = 0" + (" AND g.representante_id = ?" if rid else "") +
            " ORDER BY p.id DESC", [rid] if rid else []).fetchall()
        recebidos = conn.execute(
            "SELECT g.nome, COUNT(*) AS lotes, SUM(l.qtd_clientes) AS contatos FROM lotes l "
            "JOIN gerentes g ON g.id = l.gerente_id WHERE l.visto = 0" + (" AND g.representante_id = ?" if rid else "") +
            " GROUP BY g.id, g.nome", [rid] if rid else []).fetchall()
        lotes = [] if rid else conn.execute(
            "SELECT l.*, g.nome AS gerente FROM lotes l LEFT JOIN gerentes g ON g.id = l.gerente_id WHERE 1=1" + fl +
            " ORDER BY l.id DESC LIMIT 6", al).fetchall()
        gerentes = [] if rid else conn.execute("SELECT id, nome FROM gerentes WHERE ativo = 1 ORDER BY nome").fetchall()
        # Clientes repetidos entre listas de gerentes diferentes: só o consultor decide de quem fica.
        duplicados = [] if rid else conn.execute(
            "SELECT id, nome, alerta_duplicidade FROM clientes WHERE alerta_duplicidade IS NOT NULL "
            "ORDER BY atualizado_em DESC LIMIT 20").fetchall()
        return render_template("painel.html", duplicados=duplicados, por_etapa=por_etapa, total=sum(por_etapa.values()), fila_n=len(fila),
                               credito_fechado=credito_fechado, reps=reps, recentes=recentes, parados=parados,
                               ha=_ha_quanto, cfg=cfg, lotes=lotes, recebidos=recebidos, gerentes=gerentes,
                               docs_clientes=docs_clientes, envios_hoje=sdr.envios_hoje(conn),
                               horario_ok=sdr.dentro_do_horario(cfg))


@app.route("/importar")
@so_admin
def importar():
    return render_template("importar.html")


@app.route("/clientes")
def clientes():
    etapa = request.args.get("etapa", "")
    busca = request.args.get("q", "").strip()
    rep_id = _rep_id() or request.args.get("rep", type=int)
    ger_id = request.args.get("gerente", type=int)
    sql, args = ("SELECT c.*, g.nome AS gerente_nome, r.nome AS rep_nome FROM clientes c "
                 "LEFT JOIN lotes l ON l.id = c.lote_id LEFT JOIN gerentes g ON g.id = l.gerente_id "
                 "LEFT JOIN representantes r ON r.id = g.representante_id WHERE 1=1"), []
    if rep_id == 0:  # operação direta: gerentes sem representante
        sql += " AND g.id IS NOT NULL AND g.representante_id IS NULL"
    elif rep_id:
        sql += " AND g.representante_id = ?"
        args.append(rep_id)
    if ger_id:
        sql += " AND g.id = ?"
        args.append(ger_id)
    if etapa:
        sql += " AND c.etapa = ?"
        args.append(etapa)
    if busca:
        sql += " AND (c.nome LIKE ? OR c.telefone LIKE ? OR c.email LIKE ? OR c.cidade LIKE ?)"
        args += [f"%{busca}%"] * 4
    with db.conectar() as conn:
        rows = conn.execute(sql + " ORDER BY c.atualizado_em DESC LIMIT 500", args).fetchall()
        reps = [] if g.rep else conn.execute("SELECT id, nome FROM representantes ORDER BY nome").fetchall()
        gers = conn.execute("SELECT id, nome, agencia, representante_id FROM gerentes" +
                            (" WHERE representante_id = ?" if g.rep else "") + " ORDER BY nome",
                            [g.rep["id"]] if g.rep else []).fetchall()
    return render_template("clientes.html", clientes=rows, etapa=etapa, busca=busca, reps=reps, gers=gers,
                           rep_id=rep_id, ger_id=ger_id)


@app.route("/cliente/<int:cid>")
def cliente(cid):
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
        if g.rep and c["repasse_em"] and not c["repasse_visto"]:
            conn.execute("UPDATE clientes SET repasse_visto = 1 WHERE id = ?", (cid,))
        inter = conn.execute("SELECT * FROM interacoes WHERE cliente_id = ? ORDER BY id DESC", (cid,)).fetchall()
        extras = json.loads(c["extras"]) if c["extras"] else {}
        pacotes = conn.execute(
            "SELECT p.*, g.nome AS gerente FROM pacotes_docs p JOIN gerentes g ON g.id = p.gerente_id "
            "WHERE p.cliente_id = ? OR (p.cliente_telefone IS NOT NULL AND p.cliente_telefone = ?) ORDER BY p.id DESC",
            (cid, c["telefone"] or "-")).fetchall()
        # Documentos enviados pelo próprio cliente ficam só com o gerente: aqui aparece apenas o status.
        docs = {p["id"]: [] if p["origem"] == "cliente" else
                conn.execute("SELECT * FROM documentos WHERE pacote_id = ? ORDER BY id", (p["id"],)).fetchall() for p in pacotes}
        return render_template("cliente.html", c=c, interacoes=inter, extras=extras, cfg=db.config(conn),
                               pacotes=pacotes, docs=docs)


@app.route("/atender")
def atender():
    """Interessados de agências com representante: é o representante quem atende (o consultor acompanha tudo)."""
    rid = _rep_id() or request.args.get("rep", type=int)
    filtro, args = db.filtro_rep(rid, "c.lote_id")
    with db.conectar() as conn:
        itens = conn.execute(
            "SELECT c.*, g.nome AS gerente_nome, g.agencia AS agencia, g.cidade AS gerente_cidade, g.uf AS gerente_uf, "
            "r.nome AS rep_nome FROM clientes c JOIN lotes l ON l.id = c.lote_id JOIN gerentes g ON g.id = l.gerente_id "
            "JOIN representantes r ON r.id = g.representante_id WHERE c.repasse_em IS NOT NULL" + filtro +
            " ORDER BY c.repasse_visto, (c.etapa IN ('vendido', 'perdido', 'optout')), c.repasse_em DESC LIMIT 500",
            args).fetchall()
        reps = [] if g.rep else conn.execute("SELECT id, nome FROM representantes ORDER BY nome").fetchall()
    return render_template("atender.html", itens=itens, reps=reps, rep_id=rid)


COLUNAS_NEGOCIACAO = [("novo", "Na fila"), ("contatado", "Em contato"), ("respondeu", "Interessado"),
                      ("negociando", "Negociando"), ("documentacao", "Documentação"), ("vendido", "Fechado")]


def _ha_quanto(ts):
    try:
        seg = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return ""
    if seg < 3600:
        return f"{max(int(seg // 60), 1)} min"
    if seg < 86400:
        return f"{int(seg // 3600)} h"
    return f"{int(seg // 86400)} d"


@app.route("/negociacoes")
def negociacoes():
    """Todos os clientes por etapa, lado a lado: o consultor acompanha a rede inteira (ou um representante)."""
    rid = _rep_id() or request.args.get("rep", type=int)
    filtro, args = ("", [])
    if rid == 0:
        filtro = " AND (g.representante_id IS NULL)"
    elif rid:
        filtro, args = " AND g.representante_id = ?", [rid]
    with db.conectar() as conn:
        rows = conn.execute(
            "SELECT c.id, c.nome, c.etapa, c.produto, c.valor_credito, c.atualizado_em, c.repasse_em, c.repasse_visto, "
            "g.nome AS gerente_nome, g.agencia, r.nome AS rep_nome FROM clientes c LEFT JOIN lotes l ON l.id = c.lote_id "
            "LEFT JOIN gerentes g ON g.id = l.gerente_id LEFT JOIN representantes r ON r.id = g.representante_id "
            "WHERE 1=1" + filtro + " ORDER BY c.atualizado_em DESC", args).fetchall()
        reps = [] if g.rep else conn.execute("SELECT id, nome FROM representantes WHERE ativo = 1 ORDER BY nome").fetchall()
    colunas = []
    for etapa, titulo in COLUNAS_NEGOCIACAO:
        itens = [dict(r, ha=_ha_quanto(r["atualizado_em"])) for r in rows if r["etapa"] == etapa]
        colunas.append({"etapa": etapa, "titulo": titulo, "n": len(itens),
                        "credito": sum(i["valor_credito"] or 0 for i in itens), "itens": itens[:60]})
    fora = sum(1 for r in rows if r["etapa"] in ("perdido", "optout"))
    return render_template("negociacoes.html", colunas=colunas, fora=fora, reps=reps, rep_id=rid, total=len(rows))


@app.route("/fila")
def fila():
    with db.conectar() as conn:
        cfg = db.config(conn)
        itens = sdr.fila(conn, limite=2000, rep_id=_rep_id())
        reps = [] if g.rep else conn.execute("SELECT id, nome FROM representantes WHERE ativo = 1 ORDER BY nome").fetchall()
        # Uma aba por representante (e "Direto" para gerentes sem representante), com a contagem de cada uma.
        abas = [{"id": "", "nome": "Todos", "n": len(itens)}]
        abas += [{"id": str(r["id"]), "nome": r["nome"].split(" ")[0], "n": sum(1 for i in itens if i["rep_id"] == r["id"])}
                 for r in reps]
        diretos = sum(1 for i in itens if not i["rep_id"])
        if reps and diretos:
            abas.append({"id": "0", "nome": "Direto", "n": diretos})
        aba = request.args.get("rep", "")
        if aba:
            itens = [i for i in itens if (i["rep_id"] or 0) == int(aba)]
        return render_template("fila.html", itens=itens[:200], total=len(itens), cfg=cfg, envios_hoje=sdr.envios_hoje(conn),
                               horario_ok=sdr.dentro_do_horario(cfg), abas=abas if reps else [], aba=aba)


@app.route("/config", methods=["GET", "POST"])
@so_admin
def configuracoes():
    with db.conectar() as conn:
        if request.method == "POST":
            dados = request.form.to_dict()
            dados["email_ativo"] = "1" if request.form.get("email_ativo") else "0"
            dados["envio_automatico"] = "1" if request.form.get("envio_automatico") else "0"
            db.salvar_config(conn, dados)
            return redirect(url_for("configuracoes", ok=1))
        gerentes = conn.execute(
            "SELECT g.*, (SELECT COALESCE(SUM(qtd_clientes), 0) FROM lotes WHERE gerente_id = g.id) AS contatos "
            "FROM gerentes g ORDER BY g.ativo DESC, g.id DESC").fetchall()
        return render_template("config.html", cfg=db.config(conn), api_token=bool(API_TOKEN),
                               gerentes=gerentes, url_gerente=url_publica_gerente(),
                               brevo_ok=bool(sdr.BREVO_API_KEY and sdr.BREVO_REMETENTE_EMAIL),
                               ia_ok=bool(ia.API_KEY), url_ponte=sdr.URL_PONTE)


# ------------------------------------------------------------ Rede nacional

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI",
       "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO"]


def _voltar(padrao):
    destino = request.form.get("voltar") or padrao
    return destino if destino.startswith("/") and not destino.startswith("//") else padrao


@app.route("/rede")
@so_admin
def rede():
    uf = request.args.get("uf", "")
    with db.conectar() as conn:
        reps = [dict(r) for r in conn.execute("SELECT * FROM representantes ORDER BY ativo DESC, nome")]
        gers = db.producao_gerentes(conn)
    if uf:
        gers = [g for g in gers if g["uf"] == uf]
    for r in reps:
        r["gerentes"] = [g for g in gers if g["representante_id"] == r["id"]]
        r["prod"] = db.somar_producao(r["gerentes"])
    if uf:
        reps = [r for r in reps if r["gerentes"] or r["uf"] == uf]
    diretos = [g for g in gers if not g["representante_id"]]
    total = db.somar_producao(gers)
    estados = sorted({g["uf"] for g in gers if g["uf"]})
    reps.sort(key=lambda r: (-r["prod"]["credito_fechado"], -r["prod"]["interessados"], r["nome"]))
    top_gerentes = sorted((g for g in gers if g["clientes"]), key=lambda g: (-g["credito_fechado"], -g["interessados"]))[:10]
    nomes_rep = {r["id"]: r["nome"] for r in reps}
    return render_template("rede.html", reps=reps, diretos=diretos, diretos_prod=db.somar_producao(diretos),
                           total=total, estados=estados, uf=uf, UFS=UFS, top_gerentes=top_gerentes, nomes_rep=nomes_rep,
                           qtd_gerentes=len(gers), qtd_reps=sum(1 for r in reps if r["ativo"]))


@app.post("/rede/representantes/novo")
@so_admin
def representante_novo():
    nome = " ".join(request.form.get("nome", "").split())
    if nome:
        with db.conectar() as conn:
            conn.execute("INSERT INTO representantes (nome, telefone, email, cidade, uf, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                         (nome, db.normalizar_telefone(request.form.get("telefone")) or None,
                          request.form.get("email", "").strip() or None, request.form.get("cidade", "").strip() or None,
                          request.form.get("uf", "").strip().upper() or None, db.agora()))
    return redirect(url_for("rede"))


@app.route("/rede/representante/<int:rid>", methods=["GET", "POST"])
@so_admin
def representante(rid):
    with db.conectar() as conn:
        r = conn.execute("SELECT * FROM representantes WHERE id = ?", (rid,)).fetchone()
        if not r:
            abort(404)
        if request.method == "POST":  # editar dados, ativar/desativar ou gerar link de acesso
            if request.form.get("acao") == "convite":
                token = secrets.token_urlsafe(24)
                conn.execute("UPDATE representantes SET convite_token = ?, convite_expira = ? WHERE id = ?",
                             (token, (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"), rid))
                return redirect(url_for("representante", rid=rid) + "#acesso")
            if request.form.get("acao") == "ativo":
                conn.execute("UPDATE representantes SET ativo = ? WHERE id = ?", (0 if r["ativo"] else 1, rid))
            else:
                conn.execute("UPDATE representantes SET nome = ?, telefone = ?, email = ?, cidade = ?, uf = ? WHERE id = ?",
                             (" ".join(request.form.get("nome", "").split()) or r["nome"],
                              db.normalizar_telefone(request.form.get("telefone")) or None,
                              request.form.get("email", "").strip() or None, request.form.get("cidade", "").strip() or None,
                              request.form.get("uf", "").strip().upper() or None, rid))
            return redirect(url_for("representante", rid=rid))
        gers = [g for g in db.producao_gerentes(conn) if g["representante_id"] == rid]
        token_docs = {g["id"]: db.token_docs_do_gerente(conn, g["id"]) for g in gers if g["ativo"]}
        outros = conn.execute("SELECT id, nome FROM representantes WHERE ativo = 1 AND id <> ? ORDER BY nome", (rid,)).fetchall()
    gers.sort(key=lambda g: (-g["ativo"], -g["credito_fechado"], -g["interessados"], g["nome"]))
    convite = (request.url_root.rstrip("/") + url_for("acesso", token=r["convite_token"])
               if r["convite_token"] and (r["convite_expira"] or "") >= db.agora() else None)
    return render_template("representante.html", r=r, gerentes=gers, prod=db.somar_producao(gers), UFS=UFS,
                           url_gerente=url_publica_gerente(), token_docs=token_docs, outros=outros, convite=convite)


@app.post("/gerentes/novo")
def gerente_novo():
    """Admin cadastra para qualquer representante; o representante cadastra só para ele mesmo."""
    nome = " ".join(request.form.get("nome", "").split())
    rid = _rep_id() or request.form.get("representante_id") or None
    if nome:
        with db.conectar() as conn:
            conn.execute(
                "INSERT INTO gerentes (nome, agencia, token, criado_em, representante_id, cidade, uf, telefone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (nome, request.form.get("agencia", "").strip(), secrets.token_urlsafe(24), db.agora(), rid,
                 request.form.get("cidade", "").strip() or None, request.form.get("uf", "").strip().upper() or None,
                 db.normalizar_telefone(request.form.get("telefone")) or None))
    if g.rep:
        return redirect(url_for("meus_gerentes", novo=1))
    return redirect(_voltar(url_for("configuracoes") + "#gerentes"))


@app.post("/gerentes/<int:gid>/representante")
@so_admin
def gerente_representante(gid):
    """Passa um gerente para outro representante (ou para a operação direta)."""
    with db.conectar() as conn:
        conn.execute("UPDATE gerentes SET representante_id = ? WHERE id = ?", (request.form.get("representante_id") or None, gid))
    return redirect(_voltar(url_for("rede")))


@app.post("/gerentes/<int:gid>/ativo")
def gerente_ativo(gid):
    """Desativar corta o link na hora; reativar gera um link novo (o antigo nunca volta).
    O representante só mexe nos gerentes dele."""
    with db.conectar() as conn:
        ger = conn.execute("SELECT ativo, representante_id FROM gerentes WHERE id = ?", (gid,)).fetchone()
        if ger and g.rep and ger["representante_id"] != g.rep["id"]:
            abort(404)
        if ger and ger["ativo"]:
            conn.execute("UPDATE gerentes SET ativo = 0 WHERE id = ?", (gid,))
        elif ger:
            conn.execute("UPDATE gerentes SET ativo = 1, token = ? WHERE id = ?", (secrets.token_urlsafe(24), gid))
    if g.rep:
        return redirect(url_for("meus_gerentes"))
    return redirect(_voltar(url_for("configuracoes") + "#gerentes"))


@app.get("/meus-gerentes")
def meus_gerentes():
    """Os gerentes de agência que o representante trouxe: produção de cada um, links e cadastro de novos."""
    if not g.rep:
        return redirect(url_for("rede"))
    rid = g.rep["id"]
    with db.conectar() as conn:
        gers = [x for x in db.producao_gerentes(conn) if x["representante_id"] == rid]
        token_docs = {x["id"]: db.token_docs_do_gerente(conn, x["id"]) for x in gers if x["ativo"]}
    gers.sort(key=lambda x: (-x["ativo"], -x["id"] if request.args.get("novo") else 0, -x["credito_fechado"], x["nome"]))
    return render_template("meus_gerentes.html", gerentes=gers, prod=db.somar_producao(gers), UFS=UFS,
                           url_gerente=url_publica_gerente(), token_docs=token_docs, novo=request.args.get("novo"))


@app.post("/rede/representante/<int:rid>/ver-como")
@so_admin
def ver_como(rid):
    """Você abre o CRM exatamente como o representante vê (para testar e conferir), sem precisar da senha dele."""
    with db.conectar() as conn:
        if not conn.execute("SELECT 1 FROM representantes WHERE id = ? AND ativo = 1", (rid,)).fetchone():
            abort(404)
    session["rep_id"] = rid
    session["ver_como"] = True
    return redirect(url_for("painel"))


@app.get("/voltar-operacao")
def voltar_operacao():
    if session.pop("ver_como", None):
        rid = session.pop("rep_id", None)
        return redirect(url_for("representante", rid=rid) if rid else url_for("painel"))
    return redirect(url_for("painel"))


@app.get("/app.webmanifest")
def manifest_crm():
    """Deixa o CRM instalável na tela inicial do celular (abre como aplicativo)."""
    return jsonify(
        name="CRM Consórcio BB", short_name="Consórcio BB", start_url="/", scope="/",
        display="standalone", background_color="#16245c", theme_color="#16245c",
        icons=[{"src": "/static/icone-192.png", "sizes": "192x192", "type": "image/png"},
               {"src": "/static/icone-512.png", "sizes": "512x512", "type": "image/png"}])


@app.post("/api/lote/<int:lid>/gerente")
@so_admin
def lote_gerente(lid):
    """Vincula uma lista já importada ao gerente que a indicou (aparece no placar dele)."""
    gid = request.get_json(force=True).get("gerente_id") or None
    with db.conectar() as conn:
        conn.execute("UPDATE lotes SET gerente_id = ? WHERE id = ?", (gid, lid))
    return jsonify(ok=True)


@app.post("/api/cliente/<int:cid>/duplicidade-vista")
@so_admin
def duplicidade_vista(cid):
    with db.conectar() as conn:
        conn.execute("UPDATE clientes SET alerta_duplicidade = NULL WHERE id = ?", (cid,))
        db.registrar(conn, cid, "sistema", "interna", "Aviso de duplicidade conferido pelo consultor.")
    return jsonify(ok=True)


@app.post("/api/docs-clientes/vistos")
def docs_clientes_vistos():
    rid = _rep_id()
    with db.conectar() as conn:
        conn.execute("UPDATE pacotes_docs SET visto_consultor = 1 WHERE origem = 'cliente' AND visto_consultor = 0" +
                     (" AND gerente_id IN (SELECT id FROM gerentes WHERE representante_id = ?)" if rid else ""),
                     [rid] if rid else [])
    return jsonify(ok=True)


@app.post("/api/lotes/vistos")
def lotes_vistos():
    rid = _rep_id()
    with db.conectar() as conn:
        conn.execute("UPDATE lotes SET visto = 1 WHERE visto = 0" +
                     (" AND gerente_id IN (SELECT id FROM gerentes WHERE representante_id = ?)" if rid else ""),
                     [rid] if rid else [])
    return jsonify(ok=True)


# ------------------------------------------- documentos de clientes ao gerente

@app.route("/documentos")
def documentos():
    rid = _rep_id()
    fg, ag = (" AND g.representante_id = ?", [rid]) if rid else ("", [])
    with db.conectar() as conn:
        gerentes = conn.execute("SELECT g.id, g.nome, g.agencia FROM gerentes g WHERE g.ativo = 1" + fg + " ORDER BY g.nome", ag).fetchall()
        cid = request.args.get("cliente", type=int)
        cliente_sel = _cliente_ou_404(conn, cid) if cid else None
        pacotes = conn.execute(
            "SELECT p.*, g.nome AS gerente, (SELECT COUNT(*) FROM documentos d WHERE d.pacote_id = p.id) AS qtd "
            "FROM pacotes_docs p JOIN gerentes g ON g.id = p.gerente_id WHERE 1=1" + fg + " ORDER BY p.id DESC LIMIT 50", ag).fetchall()
        docs = {}
        if pacotes:
            ids = [p["id"] for p in pacotes]
            for d in conn.execute(f"SELECT * FROM documentos WHERE pacote_id IN ({','.join('?' * len(ids))})", ids):
                docs.setdefault(d["pacote_id"], []).append(d)
        base = url_publica_gerente()
        links_cliente = [{"gerente": g, "url": f"{base or 'http://localhost:8096'}/d/{db.token_docs_do_gerente(conn, g['id'])}"}
                         for g in gerentes]
    return render_template("documentos.html", gerentes=gerentes, pacotes=pacotes, docs=docs, cliente_sel=cliente_sel,
                           links_cliente=links_cliente, url_publica=bool(base))


@app.post("/documentos/enviar")
def documentos_enviar():
    gid = request.form.get("gerente_id", type=int)
    cliente_id = request.form.get("cliente_id", type=int)
    if g.rep:
        with db.conectar() as conn:
            ok = conn.execute("SELECT 1 FROM gerentes WHERE id = ? AND representante_id = ?", (gid, g.rep["id"])).fetchone()
            if not ok or (cliente_id and not _cliente_ou_404(conn, cliente_id)):
                abort(404)
    nome = request.form.get("cliente_nome", "").strip()
    arquivos = []
    for f in request.files.getlist("arquivos"):
        if not f.filename:
            continue
        if Path(f.filename).suffix.lower() not in db.EXTENSOES_DOCS:
            return redirect(url_for("documentos", erro=f"Formato não aceito: {f.filename}. Use PDF, foto ou Word."))
        arquivos.append((f.filename, f.read(), f.mimetype))
    with db.conectar() as conn:
        if cliente_id and not nome:
            c = conn.execute("SELECT nome FROM clientes WHERE id = ?", (cliente_id,)).fetchone()
            nome = c["nome"] if c else ""
        if not gid or not nome or not arquivos:
            return redirect(url_for("documentos", erro="Preencha o nome do cliente e anexe pelo menos um documento."))
        db.salvar_pacote_docs(conn, gid, nome, arquivos, request.form.get("produto", ""),
                              request.form.get("observacao", ""), cliente_id)
    return redirect(url_for("documentos", ok=1))


@app.get("/documentos/arquivo/<int:doc_id>")
def documentos_arquivo(doc_id):
    with db.conectar() as conn:
        d = conn.execute(
            "SELECT d.* FROM documentos d JOIN pacotes_docs p ON p.id = d.pacote_id JOIN gerentes g ON g.id = p.gerente_id "
            "WHERE d.id = ? AND COALESCE(p.origem, 'consultor') != 'cliente'" +
            (" AND g.representante_id = ?" if g.rep else ""), (doc_id, *([g.rep["id"]] if g.rep else []))).fetchone()
    if not d:
        abort(404)  # documentos enviados pelo cliente ficam só no Portal do Gerente
    return send_file(io.BytesIO(armazenamento.ler("documentos", d["caminho"])), download_name=d["nome"],
                     mimetype=d["mime"] or "application/octet-stream")


# ------------------------------------------------------- importação por foto

@app.post("/api/ler")
@so_admin
def api_ler():
    """Recebe 1+ imagens de lista do BB e devolve os clientes lidos pela IA (sem gravar)."""
    arquivos = request.files.getlist("imagens")
    if not arquivos:
        return jsonify(erro="Nenhuma imagem enviada."), 400
    imagens = []
    for f in arquivos:
        mime = (f.mimetype or "").lower()
        if mime not in MIMES_ACEITOS:
            return jsonify(erro=f"Formato não aceito: {f.filename} ({mime})."), 400
        dados = f.read()
        armazenamento.salvar("uploads", f"{datetime.now():%Y-%m-%d/%H%M%S}-{secrets.token_hex(3)}-{Path(f.filename or 'img').name}",
                             dados, mime)
        imagens.append((dados, mime))
    try:
        lidos, obs = ia.ler_listas(imagens)
    except ia.ErroIA as e:
        return jsonify(erro=str(e)), 502
    with db.conectar() as conn:
        for c in lidos:
            tel = db.normalizar_telefone(c.get("telefone"))
            c["telefone_ok"] = bool(tel)
            ja = conn.execute("SELECT id, etapa FROM clientes WHERE telefone = ?", (tel,)).fetchone() if tel else None
            c["ja_existe"] = dict(ja) if ja else None
    return jsonify(clientes=lidos, observacao=obs)


@app.post("/api/importar")
@so_admin
def api_importar():
    corpo = request.get_json(force=True)
    lista = [c for c in corpo.get("clientes", []) if (c.get("nome") or "").strip()]
    if not lista:
        return jsonify(erro="Nenhum cliente para importar."), 400
    with db.conectar() as conn:
        lote_id, novos, atualizados = db.importar_lote(
            conn, lista, corpo.get("descricao") or "", int(corpo.get("qtd_imagens") or 0))
    sdr.processar_lote_em_segundo_plano(lote_id)
    return jsonify(ok=True, lote_id=lote_id, novos=novos, atualizados=atualizados)


# ---------------------------------------------------------- ações no cliente

@app.post("/api/cliente/<int:cid>/rascunho")
def api_rascunho(cid):
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
        try:
            texto = sdr.preparar_rascunho(conn, c, db.config(conn), forcar=True)
        except ia.ErroIA as e:
            return jsonify(erro=str(e)), 502
    return jsonify(texto=texto)


@app.post("/api/cliente/<int:cid>/enviado")
def api_enviado(cid):
    """Consultor abriu o WhatsApp com a mensagem (modo manual): conta como contato feito."""
    texto = (request.get_json(force=True).get("texto") or "").strip()
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
        sdr.registrar_envio(conn, c, db.config(conn), "whatsapp", texto, "aberto_manual")
    return jsonify(ok=True, link=sdr.link_whatsapp(c["telefone"], texto))


@app.post("/api/cliente/<int:cid>/enviar-auto")
def api_enviar_auto(cid):
    texto = (request.get_json(force=True).get("texto") or "").strip()
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
        try:
            sdr.enviar_ponte(c["telefone"], texto)
        except Exception as e:
            return jsonify(erro=f"Robô do WhatsApp não enviou: {e}"), 502
        sdr.registrar_envio(conn, c, db.config(conn), "whatsapp", texto, "enviado")
    return jsonify(ok=True)


@app.post("/api/cliente/<int:cid>/atualizar")
def api_atualizar(cid):
    d = request.get_json(force=True)
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
        sets = {}
        for campo in ("nome", "email", "cidade", "produto", "situacao_bb", "observacoes"):
            if campo in d:
                sets[campo] = (d[campo] or "").strip()
        if "telefone" in d:
            sets["telefone"] = db.normalizar_telefone(d["telefone"])
        if "cpf" in d:
            cpf = db.normalizar_cpf(d["cpf"])
            if d["cpf"].strip() and not cpf:
                return jsonify(erro="CPF inválido. Confira os números."), 400
            sets["cpf"] = cpf or None
        if "etapa" in d and d["etapa"] in db.ETAPAS_DICT and d["etapa"] != c["etapa"]:
            sets["etapa"] = d["etapa"]
            db.registrar(conn, cid, "sistema", "interna",
                         f"Etapa: {db.ETAPAS_DICT[c['etapa']]} → {db.ETAPAS_DICT[d['etapa']]}")
            if d["etapa"] in db.ETAPAS_FINAIS:
                sets["proximo_contato"] = None
        if "pausado" in d:
            sets["pausado"] = 1 if d["pausado"] else 0
        if "proximo_contato" in d:
            v = (d["proximo_contato"] or "").replace("T", " ")
            sets["proximo_contato"] = (v + ":00" if len(v) == 16 else v) or None
        if not sets:
            return jsonify(ok=True)
        sets["atualizado_em"] = db.agora()
        conn.execute(f"UPDATE clientes SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), cid))
        if "etapa" in sets:
            db.repassar_se_interessado(conn, cid)
    return jsonify(ok=True)


@app.post("/api/cliente/<int:cid>/nota")
def api_nota(cid):
    d = request.get_json(force=True)
    texto = (d.get("texto") or "").strip()
    if not texto:
        return jsonify(erro="Nota vazia."), 400
    with db.conectar() as conn:
        _cliente_ou_404(conn, cid)
        if d.get("tipo") == "resposta":
            c = conn.execute("SELECT telefone FROM clientes WHERE id = ?", (cid,)).fetchone()
            res = sdr.registrar_resposta(conn, c["telefone"], texto) if c["telefone"] else None
            if not res:
                db.registrar(conn, cid, "whatsapp", "entrada", texto)
        else:
            db.registrar(conn, cid, "nota", "interna", texto)
    return jsonify(ok=True)


@app.post("/api/ciclo")
@so_admin
def api_ciclo_manual():
    with db.conectar() as conn:
        return jsonify(sdr.ciclo(conn, db.config(conn), so_rascunhos=request.args.get("rascunhos") == "1"))


# ------------------------------------------------------------ API para o n8n

def _exigir_token():
    if not API_TOKEN or request.headers.get("X-Token") != API_TOKEN:
        abort(403)


@app.post("/api/n8n/ciclo")
def n8n_ciclo():
    _exigir_token()
    if request.args.get("fundo") == "1":
        sdr.ciclo_em_segundo_plano()
        return jsonify(ok=True, em_andamento=True), 202
    with db.conectar() as conn:
        return jsonify(sdr.ciclo(conn, db.config(conn)))


@app.post("/api/n8n/resposta")
def n8n_resposta():
    """O cliente respondeu (vindo do robô de WhatsApp ou de outro fluxo do n8n)."""
    _exigir_token()
    d = request.get_json(force=True)
    with db.conectar() as conn:
        res = sdr.registrar_resposta(conn, d.get("telefone"), (d.get("texto") or "").strip())
    return jsonify(res or {"ignorado": True, "motivo": "telefone não é de cliente BB"})


@app.get("/api/n8n/resumo")
def n8n_resumo():
    _exigir_token()
    with db.conectar() as conn:
        por_etapa = {r["etapa"]: r["n"] for r in conn.execute("SELECT etapa, COUNT(*) n FROM clientes GROUP BY etapa")}
        quentes = [dict(r) for r in conn.execute(
            "SELECT id, nome, telefone, etapa, atualizado_em FROM clientes WHERE etapa IN ('respondeu', 'negociando', 'documentacao') "
            "ORDER BY atualizado_em DESC LIMIT 20")]
        return jsonify(por_etapa=por_etapa, fila=len(sdr.fila(conn)), envios_hoje=sdr.envios_hoje(conn), quentes=quentes)


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORTA", "8095")), debug=False)
