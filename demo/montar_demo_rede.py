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


# O que o cliente respondeu no WhatsApp, por produto (a IA resume isso para o representante).
RESPOSTAS = {
    "imóvel": ["Oi! Tenho interesse sim, quero sair do aluguel. Quanto ficaria uma carta de {v}?",
               "Boa tarde. Estou pensando em comprar um apartamento ano que vem, como funciona o lance?"],
    "auto": ["Tenho sim, quero trocar de carro. Dá pra usar meu carro atual no lance?",
             "Oi, quero um carro em uns 12 meses. Qual a parcela de {v}?"],
    "pesados": ["Bom dia. Preciso renovar a frota, um caminhão de {v}. Vocês fazem?",
                "Tenho interesse, mas quero entender se consigo contemplar rápido."],
    "moto": ["Oi! Quero uma moto pra trabalhar, qual a parcela mais baixa?",
             "Tenho interesse sim, mas só posso pagar até 400 por mês."],
}
MOTIVOS = ["Achou a parcela alta", "Comprou em outro lugar", "Não atende / sumiu", "Prefere financiamento"]


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
                if tentativas:
                    db.registrar(conn, cid, "whatsapp", "saida",
                                 f"Olá, {c['nome'].split()[0]}! Aqui é o Michael, da Mobie, representante autorizado do "
                                 f"Consórcio BB. Vi que você demonstrou interesse em consórcio de {c['produto']} pelo Banco do "
                                 "Brasil. Esse interesse continua?", status="aberto_manual")
                if etapa in ("respondeu", "negociando", "documentacao", "vendido"):
                    valor = f"R$ {c['valor_credito']:,.0f}".replace(",", ".")
                    db.registrar(conn, cid, "whatsapp", "entrada", random.choice(RESPOSTAS[c["produto"]]).format(v=valor))
                    # Chegou para o representante: os mais recentes ainda sem contato (alguns já atrasados),
                    # os mais antigos já atendidos; um ou outro devolvido à central por falta de contato.
                    chegou = mexeu + (random.uniform(1, 30) if etapa != "respondeu" else 0)
                    atendido = None if etapa == "respondeu" and chegou < 30 else ts(chegou - random.uniform(0.2, 3))
                    devolvido = ts(chegou - 24) if etapa == "respondeu" and chegou > 60 and random.random() < .5 else None
                    conn.execute("UPDATE clientes SET repasse_em = ?, repasse_visto = ?, atendido_em = ?, devolvido_em = ?, "
                                 "proximo_contato = ? WHERE id = ?", (ts(chegou), 0 if not atendido else 1,
                                                  None if devolvido else atendido, devolvido, ts(1) if devolvido else None, cid))
                if etapa == "perdido":
                    conn.execute("UPDATE clientes SET motivo_perda = ? WHERE id = ?", (random.choice(MOTIVOS), cid))
    total = conn.execute("SELECT COUNT(*) FROM clientes").fetchone()[0]
print(f"ok: {len(REDE)} representantes, {sum(len(r[3]) for r in REDE)} agências, {total} clientes · {DEMO_DB}")
