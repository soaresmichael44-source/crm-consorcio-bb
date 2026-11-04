"""Monta demo/rede_demo.db: um ambiente de TESTE enxuto da operação em rede (4 representantes, 2 agências
cada, clientes em todas as etapas, follow-up vencido e interessados já repassados). Não toca no banco real.

Uso:  python3 demo/montar_demo_rede.py
      CRM_BB_DB=demo/rede_demo.db CRM_BB_DEMO=1 PORTA=8098 python3 app.py
"""
import os
import random
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEMO_DB = Path(os.getenv("DEMO_DB") or Path(__file__).resolve().parent / "rede_demo.db")
for sufixo in ("", "-wal", "-shm"):
    Path(str(DEMO_DB) + sufixo).unlink(missing_ok=True)
os.environ["CRM_BB_DB"] = str(DEMO_DB)
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

random.seed(7)
REDE = [  # representante, cidade, UF, gerentes (nome, agência, cidade, UF)
    ("João Pereira", "Belo Horizonte", "MG", [("Lucas Andrade", "1615 Savassi", "Belo Horizonte", "MG"),
                                              ("Paula Menezes", "2210 Betim", "Betim", "MG")]),
    ("Mariana Tavares", "São Paulo", "SP", [("André Moura", "0300 Paulista", "São Paulo", "SP"),
                                            ("Juliana Prado", "1902 Campinas", "Campinas", "SP")]),
    ("Rafael Coutinho", "Rio de Janeiro", "RJ", [("Beatriz Lemos", "0598 Copacabana", "Rio de Janeiro", "RJ"),
                                                 ("Thiago Neves", "2875 Niterói", "Niterói", "RJ")]),
    ("Patrícia Gomes", "Goiânia", "GO", [("Marcelo Ribeiro", "0086 Setor Bueno", "Goiânia", "GO"),
                                         ("Diego Souza", "1137 Asa Sul", "Brasília", "DF")]),
    ("Carina Albuquerque", "Curitiba", "PR", [("Renato Ferraz", "3041 Batel", "Curitiba", "PR"),
                                              ("Luciana Prates", "1288 Centro", "Londrina", "PR")]),
]
# Quantos clientes de cada etapa cada agência tem: dá um funil realista sem encher a tela.
FUNIL = ["novo"] * 3 + ["contatado"] * 4 + ["respondeu"] * 2 + ["negociando"] * 2 + ["documentacao", "vendido", "perdido"]
NOMES = ["Ana", "Bruno", "Carla", "Daniel", "Elaine", "Fábio", "Gisele", "Hugo", "Isabela", "Jorge", "Kátia", "Leandro",
         "Mônica", "Nelson", "Olívia", "Pedro", "Rodrigo", "Simone", "Túlio", "Vera", "Wagner", "Yasmin"]
SOBRENOMES = ["Silva", "Souza", "Oliveira", "Santos", "Costa", "Almeida", "Ribeiro", "Carvalho", "Gomes",
              "Martins", "Araújo", "Barbosa", "Rocha", "Dias", "Teixeira", "Moreira", "Freitas", "Nunes", "Lopes"]
PRODUTOS = [("imóvel", (150_000, 500_000)), ("auto", (50_000, 150_000)), ("pesados", (250_000, 600_000)),
            ("moto", (15_000, 40_000))]


def ts(horas_atras):
    return (datetime.now() - timedelta(hours=horas_atras)).strftime("%Y-%m-%d %H:%M:%S")


db.iniciar()
usados = set()
with db.conectar() as conn:
    for nome_rep, cidade, uf, gerentes in REDE:
        rid = conn.execute("INSERT INTO representantes (nome, telefone, cidade, uf, criado_em) VALUES (?, ?, ?, ?, ?)",
                           (nome_rep, f"55319{random.randint(10_000_000, 99_999_999)}", cidade, uf, ts(24 * 60))).lastrowid
        for nome_g, agencia, cid_g, uf_g in gerentes:
            gid = conn.execute(
                "INSERT INTO gerentes (nome, agencia, token, criado_em, representante_id, cidade, uf, ultimo_envio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (nome_g, agencia, secrets.token_urlsafe(24), ts(24 * 40), rid, cid_g, uf_g, ts(random.randint(2, 30)))).lastrowid
            lista = []
            for _ in FUNIL:
                while True:
                    nome = f"{random.choice(NOMES)} {random.choice(SOBRENOMES)} {random.choice(SOBRENOMES)}"
                    if nome not in usados:
                        usados.add(nome)
                        break
                produto, (lo, hi) = random.choice(PRODUTOS)
                lista.append({"nome": nome, "produto": produto, "valor_credito": round(random.uniform(lo, hi), -3),
                              "telefone": f"55{random.choice([31, 11, 21, 62, 61])}9{random.randint(10_000_000, 99_999_999)}"})
            lote_id, _, _ = db.importar_lote(conn, lista, f"Enviado pelo celular · {nome_g}", 1, gerente_id=gid)
            conn.execute("UPDATE lotes SET criado_em = ?, visto = 1 WHERE id = ?", (ts(random.randint(30, 200)), lote_id))
            for c, etapa in zip(lista, FUNIL):
                cid = conn.execute("SELECT id FROM clientes WHERE nome = ?", (c["nome"].title(),)).fetchone()["id"]
                mexeu = random.uniform(1, 120)  # horas desde a última movimentação
                tentativas = {"novo": 0, "contatado": random.randint(1, 3)}.get(etapa, random.randint(1, 2))
                # Follow-up vencido para os frios (entram na fila de hoje); interessados seguem com o representante.
                proximo = ts(random.uniform(0, 20)) if etapa in ("novo", "contatado") else None
                conn.execute(
                    "UPDATE clientes SET etapa = ?, tentativas = ?, ultimo_contato = ?, proximo_contato = ?, atualizado_em = ? "
                    "WHERE id = ?", (etapa, tentativas, ts(mexeu) if tentativas else None, proximo, ts(mexeu), cid))
                if etapa in ("respondeu", "negociando", "documentacao", "vendido"):
                    conn.execute("UPDATE clientes SET repasse_em = ?, repasse_visto = ? WHERE id = ?",
                                 (ts(mexeu), 0 if mexeu < 30 and etapa != "vendido" else 1, cid))
    total = conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
print(f"ok: {len(REDE)} representantes, {sum(len(r[3]) for r in REDE)} agências, {total} clientes · {DEMO_DB}")
