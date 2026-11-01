"""Monta demo/rede_demo.db: uma rede nacional FICTÍCIA (representantes, gerentes de agência e listas
com produção), para mostrar a tela Rede nacional cheia. Não toca no banco real.

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

random.seed(2026)
REDE = [  # representante, cidade, UF, gerentes (nome, agência, cidade, UF), "força" (0–1)
    ("João Pereira", "Belo Horizonte", "MG", [("Lucas Andrade", "1615 Savassi", "Belo Horizonte", "MG"),
                                              ("Fernanda Rocha", "3380 Pampulha", "Belo Horizonte", "MG"),
                                              ("Ricardo Lima", "0442 Contagem", "Contagem", "MG"),
                                              ("Paula Menezes", "2210 Betim", "Betim", "MG")], .9),
    ("Mariana Tavares", "São Paulo", "SP", [("André Moura", "0300 Paulista", "São Paulo", "SP"),
                                            ("Juliana Prado", "4471 Moema", "São Paulo", "SP"),
                                            ("Carlos Eduardo", "1902 Campinas Centro", "Campinas", "SP")], .8),
    ("Rafael Coutinho", "Rio de Janeiro", "RJ", [("Beatriz Lemos", "0598 Copacabana", "Rio de Janeiro", "RJ"),
                                                 ("Thiago Neves", "2875 Niterói", "Niterói", "RJ")], .6),
    ("Patrícia Gomes", "Goiânia", "GO", [("Marcelo Ribeiro", "0086 Setor Bueno", "Goiânia", "GO"),
                                         ("Larissa Campos", "3521 Anápolis", "Anápolis", "GO"),
                                         ("Diego Souza", "1137 Brasília Asa Sul", "Brasília", "DF")], .7),
    ("Eduardo Farias", "Salvador", "BA", [("Camila Duarte", "0024 Barra", "Salvador", "BA"),
                                          ("Renato Alves", "2963 Feira de Santana", "Feira de Santana", "BA")], .45),
    ("Aline Martins", "Curitiba", "PR", [("Gustavo Kraus", "3046 Batel", "Curitiba", "PR"),
                                         ("Sabrina Weber", "0731 Joinville", "Joinville", "SC"),
                                         ("Felipe Bauer", "1288 Porto Alegre Centro", "Porto Alegre", "RS")], .55),
    ("Bruno Cavalcanti", "Recife", "PE", [("Tatiane Lins", "0519 Boa Viagem", "Recife", "PE")], .25),
]
NOMES = ["Ana", "Bruno", "Carla", "Daniel", "Elaine", "Fábio", "Gisele", "Hugo", "Isabela", "Jorge", "Kátia", "Leandro",
         "Mônica", "Nelson", "Olívia", "Pedro", "Quésia", "Rodrigo", "Simone", "Túlio", "Vera", "Wagner", "Yasmin", "Zeca"]
SOBRENOMES = ["Silva", "Souza", "Oliveira", "Santos", "Costa", "Pereira", "Almeida", "Ribeiro", "Carvalho", "Gomes",
              "Martins", "Araújo", "Barbosa", "Rocha", "Dias", "Teixeira", "Moreira", "Freitas", "Nunes", "Lopes"]
PRODUTOS = [("imóvel", (150_000, 600_000)), ("auto", (50_000, 160_000)), ("pesados", (250_000, 700_000)),
            ("moto", (15_000, 40_000)), ("serviços", (10_000, 30_000))]


def etapa_sorteada(forca):
    x = random.random()
    faixas = [("vendido", .07 * forca + .02), ("documentacao", .05 * forca + .01), ("negociando", .08 * forca + .02),
              ("respondeu", .12 * forca + .03), ("contatado", .38), ("perdido", .05), ("optout", .01)]
    for etapa, p in faixas:
        if x < p:
            return etapa
        x -= p
    return "novo"


def quando(dias):
    return (datetime.now() - timedelta(days=dias, hours=random.uniform(0, 8))).strftime("%Y-%m-%d %H:%M:%S")


db.iniciar()
usados = set()
with db.conectar() as conn:
    for nome_rep, cidade, uf, gerentes, forca in REDE:
        rid = conn.execute("INSERT INTO representantes (nome, telefone, cidade, uf, criado_em) VALUES (?, ?, ?, ?, ?)",
                           (nome_rep, f"55{random.randint(11, 99)}9{random.randint(10_000_000, 99_999_999)}", cidade, uf,
                            quando(90))).lastrowid
        for nome_g, agencia, cid_g, uf_g in gerentes:
            gid = conn.execute(
                "INSERT INTO gerentes (nome, agencia, token, criado_em, representante_id, cidade, uf) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nome_g, agencia, secrets.token_urlsafe(24), quando(80), rid, cid_g, uf_g)).lastrowid
            ultimo = None
            for _ in range(random.randint(1, 4) + round(3 * forca)):  # listas desse gerente
                dias = random.randint(0, 45)
                lista = []
                for _ in range(random.randint(12, 35)):
                    while True:
                        nome = f"{random.choice(NOMES)} {random.choice(SOBRENOMES)} {random.choice(SOBRENOMES)}"
                        if nome not in usados:
                            usados.add(nome)
                            break
                    produto, (lo, hi) = random.choice(PRODUTOS)
                    lista.append({"nome": nome, "produto": produto, "valor_credito": round(random.uniform(lo, hi), -3),
                                  "telefone": f"55{random.randint(11, 99)}9{random.randint(10_000_000, 99_999_999)}"})
                lote_id, _, _ = db.importar_lote(conn, lista, f"Enviado pelo celular · {nome_g}", 1, gerente_id=gid)
                conn.execute("UPDATE lotes SET criado_em = ?, visto = 1 WHERE id = ?", (quando(dias), lote_id))
                for c in lista:
                    conn.execute("UPDATE clientes SET etapa = ?, atualizado_em = ? WHERE nome = ?",
                                 (etapa_sorteada(forca), quando(random.randint(0, dias)), c["nome"].title()))
                ultimo = max(ultimo or "", quando(dias))
            conn.execute("UPDATE gerentes SET ultimo_envio = ? WHERE id = ?", (ultimo, gid))
    # Interessados já repassados aos representantes (os de hoje e de ontem ainda sem abrir).
    for c in conn.execute("SELECT id, atualizado_em FROM clientes WHERE etapa IN ('respondeu', 'negociando', 'documentacao', 'vendido')").fetchall():
        visto = 0 if c["atualizado_em"] >= quando(1) else 1
        conn.execute("UPDATE clientes SET repasse_em = ?, repasse_visto = ? WHERE id = ?", (c["atualizado_em"], visto, c["id"]))
    total = conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
print(f"ok: {len(REDE)} representantes, {sum(len(r[3]) for r in REDE)} gerentes, {total} clientes · {DEMO_DB}")
