"""CRM BB — operação exclusiva de clientes do Consórcio BB (listas do Banco do Brasil)."""
import io
import json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
# Carrega o .env ANTES de importar os módulos que leem variáveis na importação
# (mesma armadilha que já mordeu o CRM Mobie).
load_dotenv(BASE / ".env")

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for  # noqa: E402

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
    """Neste Mac abre direto. De outro aparelho, ou sempre na nuvem, pede a senha (CRM_BB_SENHA)."""
    if DEMO or session.get("logado") or (not FORCAR_LOGIN and request.remote_addr in ("127.0.0.1", "::1")):
        return None
    if request.path.startswith(("/static/", "/api/n8n/")) or request.path == "/login":
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
        if len(_tentativas[ip]) >= 8:
            erro = "Muitas tentativas. Espere 15 minutos."
        elif any(x and secrets.compare_digest(request.form.get("senha", ""), x) for x in (SENHA, SENHA_APRESENTACAO)):
            session.permanent = True
            session["logado"] = True
            return redirect(proximo)
        else:
            _tentativas[ip].append(time.time())
            erro = "Senha incorreta." if SENHA else "Acesso de fora do Mac está desligado (sem CRM_BB_SENHA no .env)."
    return render_template("login.html", erro=erro, proximo=proximo)


@app.context_processor
def _globais():
    return {"ETAPAS": db.ETAPAS, "ETAPAS_DICT": db.ETAPAS_DICT, "fmt_tel": db.telefone_formatado, "fmt_cpf": db.cpf_formatado}


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
    c = conn.execute("SELECT * FROM clientes WHERE id = ?", (cid,)).fetchone()
    if not c:
        abort(404)
    return c


# ---------------------------------------------------------------- páginas

@app.route("/")
def painel():
    with db.conectar() as conn:
        cfg = db.config(conn)
        por_etapa = {r["etapa"]: r["n"] for r in conn.execute("SELECT etapa, COUNT(*) n FROM clientes GROUP BY etapa")}
        total = sum(por_etapa.values())
        fila_n = len(sdr.fila(conn))
        lotes = conn.execute(
            "SELECT l.*, g.nome AS gerente FROM lotes l LEFT JOIN gerentes g ON g.id = l.gerente_id "
            "ORDER BY l.id DESC LIMIT 6").fetchall()
        gerentes = conn.execute("SELECT id, nome FROM gerentes WHERE ativo = 1 ORDER BY nome").fetchall()
        docs_clientes = conn.execute(
            "SELECT id, cliente_id, cliente_nome FROM pacotes_docs WHERE origem = 'cliente' AND visto_consultor = 0 "
            "ORDER BY id DESC").fetchall()
        recebidos = conn.execute(
            "SELECT g.nome, COUNT(*) AS lotes, SUM(l.qtd_clientes) AS contatos FROM lotes l "
            "JOIN gerentes g ON g.id = l.gerente_id WHERE l.visto = 0 GROUP BY g.id").fetchall()
        ultimas = conn.execute(
            "SELECT i.*, c.nome FROM interacoes i JOIN clientes c ON c.id = i.cliente_id "
            "WHERE i.canal != 'sistema' ORDER BY i.id DESC LIMIT 8").fetchall()
        return render_template("painel.html", por_etapa=por_etapa, total=total, fila_n=fila_n,
                               envios_hoje=sdr.envios_hoje(conn), cfg=cfg, lotes=lotes, ultimas=ultimas, recebidos=recebidos, gerentes=gerentes, docs_clientes=docs_clientes,
                               horario_ok=sdr.dentro_do_horario(cfg))


@app.route("/importar")
def importar():
    return render_template("importar.html")


@app.route("/clientes")
def clientes():
    etapa = request.args.get("etapa", "")
    busca = request.args.get("q", "").strip()
    sql, args = "SELECT * FROM clientes WHERE 1=1", []
    if etapa:
        sql += " AND etapa = ?"
        args.append(etapa)
    if busca:
        sql += " AND (nome LIKE ? OR telefone LIKE ? OR email LIKE ? OR cidade LIKE ?)"
        args += [f"%{busca}%"] * 4
    with db.conectar() as conn:
        rows = conn.execute(sql + " ORDER BY atualizado_em DESC LIMIT 500", args).fetchall()
    return render_template("clientes.html", clientes=rows, etapa=etapa, busca=busca)


@app.route("/cliente/<int:cid>")
def cliente(cid):
    with db.conectar() as conn:
        c = _cliente_ou_404(conn, cid)
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


@app.route("/fila")
def fila():
    with db.conectar() as conn:
        cfg = db.config(conn)
        itens = sdr.fila(conn)
        return render_template("fila.html", itens=itens, cfg=cfg, envios_hoje=sdr.envios_hoje(conn),
                               horario_ok=sdr.dentro_do_horario(cfg))


@app.route("/config", methods=["GET", "POST"])
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


@app.post("/gerentes/novo")
def gerente_novo():
    nome = request.form.get("nome", "").strip()
    if nome:
        with db.conectar() as conn:
            conn.execute("INSERT INTO gerentes (nome, agencia, token, criado_em) VALUES (?, ?, ?, ?)",
                         (nome, request.form.get("agencia", "").strip(), secrets.token_urlsafe(24), db.agora()))
    return redirect(url_for("configuracoes") + "#gerentes")


@app.post("/gerentes/<int:gid>/ativo")
def gerente_ativo(gid):
    """Desativar corta o link na hora; reativar gera um link novo (o antigo nunca volta)."""
    with db.conectar() as conn:
        g = conn.execute("SELECT ativo FROM gerentes WHERE id = ?", (gid,)).fetchone()
        if g and g["ativo"]:
            conn.execute("UPDATE gerentes SET ativo = 0 WHERE id = ?", (gid,))
        elif g:
            conn.execute("UPDATE gerentes SET ativo = 1, token = ? WHERE id = ?", (secrets.token_urlsafe(24), gid))
    return redirect(url_for("configuracoes") + "#gerentes")


@app.post("/api/lote/<int:lid>/gerente")
def lote_gerente(lid):
    """Vincula uma lista já importada ao gerente que a indicou (aparece no placar dele)."""
    gid = request.get_json(force=True).get("gerente_id") or None
    with db.conectar() as conn:
        conn.execute("UPDATE lotes SET gerente_id = ? WHERE id = ?", (gid, lid))
    return jsonify(ok=True)


@app.post("/api/docs-clientes/vistos")
def docs_clientes_vistos():
    with db.conectar() as conn:
        conn.execute("UPDATE pacotes_docs SET visto_consultor = 1 WHERE origem = 'cliente' AND visto_consultor = 0")
    return jsonify(ok=True)


@app.post("/api/lotes/vistos")
def lotes_vistos():
    with db.conectar() as conn:
        conn.execute("UPDATE lotes SET visto = 1 WHERE visto = 0")
    return jsonify(ok=True)


# ------------------------------------------- documentos de clientes ao gerente

@app.route("/documentos")
def documentos():
    with db.conectar() as conn:
        gerentes = conn.execute("SELECT id, nome, agencia FROM gerentes WHERE ativo = 1 ORDER BY nome").fetchall()
        cid = request.args.get("cliente", type=int)
        cliente_sel = conn.execute("SELECT id, nome, produto FROM clientes WHERE id = ?", (cid,)).fetchone() if cid else None
        pacotes = conn.execute(
            "SELECT p.*, g.nome AS gerente, (SELECT COUNT(*) FROM documentos d WHERE d.pacote_id = p.id) AS qtd "
            "FROM pacotes_docs p JOIN gerentes g ON g.id = p.gerente_id ORDER BY p.id DESC LIMIT 50").fetchall()
        docs = {}
        for d in conn.execute("SELECT * FROM documentos WHERE pacote_id IN (SELECT id FROM pacotes_docs ORDER BY id DESC LIMIT 50)"):
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
            "SELECT d.* FROM documentos d JOIN pacotes_docs p ON p.id = d.pacote_id "
            "WHERE d.id = ? AND COALESCE(p.origem, 'consultor') != 'cliente'", (doc_id,)).fetchone()
    if not d:
        abort(404)  # documentos enviados pelo cliente ficam só no Portal do Gerente
    return send_file(io.BytesIO(armazenamento.ler("documentos", d["caminho"])), download_name=d["nome"],
                     mimetype=d["mime"] or "application/octet-stream")


# ------------------------------------------------------- importação por foto

@app.post("/api/ler")
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
