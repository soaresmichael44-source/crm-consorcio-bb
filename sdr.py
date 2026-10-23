"""Cadência de follow-up: decide quem recebe mensagem, redige e envia."""
import os
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import dotenv_values

import db
import ia

_BOT_ENV = dotenv_values(Path.home() / "claude-whatsapp" / ".env")
URL_PONTE = f"http://127.0.0.1:{_BOT_ENV.get('PORTA_ENVIO_PROATIVO', '5057')}/enviar-proativo"
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN") or _BOT_ENV.get("CRM_MOBIE_V2_BRIDGE_TOKEN", "")

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_REMETENTE_EMAIL = os.getenv("BREVO_REMETENTE_EMAIL", "")
BREVO_REMETENTE_NOME = os.getenv("BREVO_REMETENTE_NOME", "Consórcio BB - Representante")

PALAVRAS_OPTOUT = {"sair", "pare", "parar", "stop", "descadastrar", "nao quero", "não quero", "remover"}


def _cadencia(cfg):
    dias = [int(x) for x in cfg["cadencia_dias"].split(",") if x.strip().isdigit()] or [0, 1, 3, 7]
    return dias, int(cfg.get("intervalo_final") or 15)


def proximo_contato(cfg, tentativas_feitas):
    """Depois de `tentativas_feitas` envios, quando sai o próximo."""
    dias, final = _cadencia(cfg)
    n = tentativas_feitas
    gap = dias[n] - dias[n - 1] if 0 < n < len(dias) else final
    # Vence no começo do dia: a rodada diária das 9h pega o cliente mesmo que o
    # contato anterior tenha sido feito à tarde.
    dia = datetime.now() + timedelta(days=max(gap, 1))
    return dia.strftime("%Y-%m-%d 00:00:00")


def dentro_do_horario(cfg, quando=None):
    quando = quando or datetime.now()
    ini, fim = int(cfg["hora_inicio"]), int(cfg["hora_fim"])
    if quando.weekday() == 6:  # domingo: nunca
        return False
    if quando.weekday() == 5:  # sábado: só de manhã
        fim = min(fim, 13)
    return ini <= quando.hour < fim


def link_whatsapp(telefone, texto):
    return f"https://wa.me/{telefone}?text={quote(texto)}"


def envios_hoje(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM interacoes WHERE direcao = 'saida' AND status IN ('enviado', 'aberto_manual') "
        "AND criado_em >= ?", (datetime.now().strftime("%Y-%m-%d 00:00:00"),)
    ).fetchone()[0]


def fila(conn, limite=200, lote_id=None):
    """Clientes com follow-up vencido, mais antigos primeiro (opcionalmente só de um lote)."""
    filtro, args = ("AND lote_id = ? ", [lote_id]) if lote_id else ("", [])
    return conn.execute(
        "SELECT * FROM clientes WHERE pausado = 0 AND etapa NOT IN ('documentacao', 'vendido', 'perdido', 'optout') "
        "AND proximo_contato IS NOT NULL AND proximo_contato <= ? " + filtro +
        "ORDER BY (etapa = 'novo') DESC, proximo_contato LIMIT ?",
        (db.agora(), *args, limite),
    ).fetchall()


def historico(conn, cliente_id, n=8):
    rows = conn.execute(
        "SELECT * FROM interacoes WHERE cliente_id = ? AND canal != 'sistema' ORDER BY id DESC LIMIT ?",
        (cliente_id, n),
    ).fetchall()
    return list(reversed(rows))


def preparar_rascunho(conn, cliente, cfg, forcar=False):
    if cliente["rascunho"] and not forcar:
        return cliente["rascunho"]
    assunto, texto = ia.gerar_mensagem(cliente, historico(conn, cliente["id"]), cliente["tentativas"] + 1, cfg)
    conn.execute("UPDATE clientes SET rascunho = ?, rascunho_assunto = ?, atualizado_em = ? WHERE id = ?",
                 (texto, assunto, db.agora(), cliente["id"]))
    return texto


def registrar_envio(conn, cliente, cfg, canal, texto, status, assunto=None, avancar=None):
    """Grava o envio e, se for o canal principal, avança a cadência."""
    db.registrar(conn, cliente["id"], canal, "saida", texto, status=status, assunto=assunto)
    if not (canal == "whatsapp" if avancar is None else avancar):
        return
    n = cliente["tentativas"] + 1
    etapa = "contatado" if cliente["etapa"] == "novo" else cliente["etapa"]
    conn.execute(
        "UPDATE clientes SET tentativas = ?, ultimo_contato = ?, proximo_contato = ?, etapa = ?, "
        "rascunho = NULL, rascunho_assunto = NULL, atualizado_em = ? WHERE id = ?",
        (n, db.agora(), proximo_contato(cfg, n), etapa, db.agora(), cliente["id"]),
    )


def enviar_ponte(telefone, texto):
    headers = {"X-Bridge-Token": BRIDGE_TOKEN} if BRIDGE_TOKEN else {}
    r = requests.post(URL_PONTE, json={"telefone": telefone, "texto": texto}, headers=headers, timeout=60)
    dados = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not dados.get("ok"):
        raise RuntimeError(dados.get("erro") or dados.get("motivo") or f"HTTP {r.status_code}")


def enviar_email(cliente, cfg):
    if not (BREVO_API_KEY and BREVO_REMETENTE_EMAIL):
        raise RuntimeError("Brevo não configurada (BREVO_API_KEY / BREVO_REMETENTE_EMAIL no .env).")
    assunto, corpo = ia.gerar_mensagem(cliente, [], cliente["tentativas"], cfg, canal="email")
    html = "".join(f"<p>{p.strip()}</p>" for p in corpo.split("\n") if p.strip())
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json={"sender": {"name": BREVO_REMETENTE_NOME, "email": BREVO_REMETENTE_EMAIL},
              "to": [{"email": cliente["email"], "name": cliente["nome"]}],
              "subject": assunto or "Seu interesse no Consórcio BB", "htmlContent": html, "textContent": corpo},
        timeout=30,
    )
    r.raise_for_status()
    return assunto, corpo


def ciclo(conn, cfg, so_rascunhos=False, lote_id=None):
    """Uma rodada da cadência (chamada pelo n8n, pelo botão ou na chegada de uma lista).
    Respeita horário, teto diário e a trava geral `envio_automatico`."""
    resumo = {"rascunhos": 0, "enviados": 0, "emails": 0, "erros": [], "fora_do_horario": False}
    if cfg.get("envio_automatico") != "1":
        so_rascunhos = True  # trava geral desligada: só redige, nunca envia
    resumo["envio_automatico"] = not so_rascunhos
    if not dentro_do_horario(cfg) and not so_rascunhos:
        resumo["fora_do_horario"] = True
        return resumo
    restante = max(int(cfg["max_envios_dia"]) - envios_hoje(conn), 0)
    wa_auto = cfg["modo_whatsapp"] == "ponte" and not so_rascunhos
    email_auto = cfg["email_ativo"] == "1" and not so_rascunhos
    # No modo manual o WhatsApp só é redigido; o envio é o clique do consultor
    # na fila. O e-mail sai sozinho como reforço (modo ponte) ou como canal
    # único de quem não tem telefone.
    # O n8n roda uma vez por dia: a rodada precisa cobrir o limite diário inteiro.
    for c in fila(conn, limite=int(cfg["max_envios_dia"]), lote_id=lote_id):
        if restante <= 0 and (wa_auto or email_auto):
            break
        try:
            if not c["telefone"] and not c["email"]:
                continue
            if c["telefone"]:
                texto = preparar_rascunho(conn, c, cfg)
                resumo["rascunhos"] += 1
            if wa_auto and c["telefone"]:
                enviar_ponte(c["telefone"], texto)
                registrar_envio(conn, c, cfg, "whatsapp", texto, "enviado")
                resumo["enviados"] += 1
                restante -= 1
            if email_auto and c["email"] and (wa_auto or not c["telefone"]):
                c2 = conn.execute("SELECT * FROM clientes WHERE id = ?", (c["id"],)).fetchone()
                assunto, corpo = enviar_email(c2, cfg)
                # Sem telefone, o e-mail é o canal principal e avança a cadência.
                registrar_envio(conn, c2, cfg, "email", corpo, "enviado", assunto=assunto, avancar=not c["telefone"])
                resumo["emails"] += 1
                if not c["telefone"]:
                    restante -= 1
            conn.commit()
        except Exception as e:  # um cliente com problema não trava a fila
            resumo["erros"].append(f"{c['nome']}: {e}")
            db.registrar(conn, c["id"], "sistema", "interna", f"Falha no follow-up automático: {e}", status="falhou")
            conn.commit()
    return resumo


def _normalizar(txt):
    return unicodedata.normalize("NFKD", txt.lower()).encode("ascii", "ignore").decode().strip(" .!")


def registrar_resposta(conn, telefone, texto):
    """Resposta que chegou do cliente (via n8n/robô). Detecta pedido de saída."""
    tel = db.normalizar_telefone(telefone)
    c = conn.execute("SELECT * FROM clientes WHERE telefone = ?", (tel,)).fetchone() if tel else None
    if not c:
        return None
    db.registrar(conn, c["id"], "whatsapp", "entrada", texto)
    if _normalizar(texto) in {_normalizar(p) for p in PALAVRAS_OPTOUT}:
        etapa, prox = "optout", None
    else:
        etapa = c["etapa"] if c["etapa"] in ("negociando", "documentacao", "vendido", "perdido") else "respondeu"
        # Respondeu: a cadência espera 2 dias para o consultor assumir a conversa.
        prox = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE clientes SET etapa = ?, proximo_contato = ?, rascunho = NULL, atualizado_em = ? WHERE id = ?",
                 (etapa, prox, db.agora(), c["id"]))
    return {"cliente_id": c["id"], "etapa": etapa}


def processar_lote_em_segundo_plano(lote_id):
    """Chamado quando uma lista chega: primeiro contato do lote sem esperar a rodada das 9h.
    Com a trava geral desligada (ou fora do horário) só prepara as mensagens."""
    def _rodar():
        try:
            with db.conectar() as conn:
                cfg = db.config(conn)
                ciclo(conn, cfg, so_rascunhos=not dentro_do_horario(cfg), lote_id=lote_id)
        except Exception as e:  # roda solto: falha aqui não pode derrubar o upload
            print(f"[lote {lote_id}] falha ao preparar primeiro contato: {e}", flush=True)
    threading.Thread(target=_rodar, daemon=True).start()
