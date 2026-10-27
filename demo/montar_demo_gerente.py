"""Monta demo/gerente_demo.db: um gerente de exemplo com clientes FICTÍCIOS em todas as etapas,
para mostrar a tela do gerente cheia (placar, termômetro, comemoração). Não toca no banco real."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEMO_DB = Path(os.getenv("DEMO_DB") or Path(__file__).resolve().parent / "gerente_demo.db")
for sufixo in ("", "-wal", "-shm"):
    Path(str(DEMO_DB) + sufixo).unlink(missing_ok=True)
os.environ["CRM_BB_DB"] = str(DEMO_DB)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

TOKEN = "demonstracao-gerente-carlos-2026"
CLIENTES = [  # nome, produto, crédito, etapa, horas atrás da última movimentação
    # fechados
    ("Juliana Ferreira Lopes", "imóvel", 320000, "vendido", 1.2),
    ("Ricardo Monteiro Alves", "pesados", 410000, "vendido", 20),
    ("Rogério Pacheco Viana", "moto", 35000, "vendido", 30),
    ("Roberto Almeida Costa", "auto", 95000, "vendido", 50),
    ("André Luiz Carvalho", "imóvel", 520000, "vendido", 70),
    ("Cristiane Moura Lacerda", "auto", 145000, "vendido", 96),
    ("Patrícia Gomes Rocha", "auto", 110000, "vendido", 120),
    ("Simone Batista Rezende", "imóvel", 280000, "vendido", 150),
    ("Márcio Tadeu Fonseca", "pesados", 650000, "vendido", 200),
    ("Daniela Souto Maia", "serviços", 25000, "vendido", 230),
    # documentação recebida
    ("Mariana Costa Vieira", "imóvel", 280000, "documentacao", 0.6),
    ("Leonardo Brito Sales", "pesados", 480000, "documentacao", 3.5),
    ("Tatiane Coelho Ramos", "auto", 120000, "documentacao", 9),
    ("Helena Duarte Campos", "moto", 32000, "documentacao", 26),
    # proposta na mesa
    ("Fernando Dias Moreira", "imóvel", 450000, "negociando", 0.3),
    ("Rafael Antunes Brandão", "imóvel", 600000, "negociando", 1.8),
    ("Camila Ribeiro Santos", "moto", 28000, "negociando", 2.5),
    ("Paulo Sérgio Andrade", "auto", 135000, "negociando", 7),
    ("Gilberto Siqueira Paiva", "pesados", 520000, "negociando", 11),
    ("Priscila Neves Torres", "auto", 160000, "negociando", 18),
    ("Jorge Henrique Barros", "imóvel", 380000, "negociando", 33),
    # responderam / interessados
    ("Marcos Vinícius Prado", "pesados", 380000, "respondeu", 0.15),
    ("Aline Cardoso Mendes", "auto", 85000, "respondeu", 4),
    ("Bruno César Macedo", "imóvel", 350000, "respondeu", 5.5),
    ("Elaine Cristina Pires", "auto", 98000, "respondeu", 14),
    ("Thiago Barbosa Lima", "serviços", 20000, "respondeu", 26),
    ("Wagner Luiz Bastos", "pesados", 300000, "respondeu", 40),
    ("Vanessa Rocha Duarte", "imóvel", 300000, "respondeu", 49),
    # em contato
    ("Renata Souza Pires", "imóvel", 250000, "contatado", 1),
    ("Eduardo Nunes Teixeira", "auto", 70000, "contatado", 5),
    ("Cláudia Regina Faria", "imóvel", 220000, "contatado", 12),
    ("Luciana Martins Araújo", "auto", 60000, "contatado", 28),
    ("Henrique Matos Leal", "pesados", 290000, "contatado", 44),
    ("Gustavo Henrique Reis", "moto", 22000, "contatado", 54),
    ("Felipe Augusto Nogueira", "auto", 98000, "contatado", 75),
    # na fila
    ("Bianca Oliveira Castro", "imóvel", 200000, "novo", 0.5),
    ("Diego Fernandes Melo", "auto", 90000, "novo", 0.5),
    ("Larissa Pinto Correia", "moto", 18000, "novo", 0.5),
    ("Igor Santana Rios", "auto", 75000, "novo", 0.5),
    ("Natália Prates Vidal", "imóvel", 240000, "novo", 0.5),
    # não compraram agora
    ("Sandra Lúcia Freitas", "auto", 55000, "perdido", 160),
    ("Otávio Ramos Silveira", "imóvel", 190000, "optout", 200),
]


def pdf_exemplo(titulo):
    """PDF mínimo de uma página com um título: só para a demonstração ter o que abrir."""
    texto = f"BT /F1 20 Tf 60 760 Td ({titulo}) Tj 0 -34 Td /F1 12 Tf (DOCUMENTO DE EXEMPLO - DEMONSTRACAO) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>stream\n" % len(texto) + texto + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return bytes(out)

db.iniciar()
with db.conectar() as conn:
    conn.execute("INSERT INTO gerentes (nome, agencia, token, criado_em, ultima_visita) VALUES (?, ?, ?, ?, ?)",
                 ("Carlos (exemplo)", "Savassi", TOKEN, db.agora(), "2026-01-01 00:00:00"))
    gid = conn.execute("SELECT id FROM gerentes WHERE token = ?", (TOKEN,)).fetchone()["id"]
    lote_id, _, _ = db.importar_lote(
        conn, [{"nome": n, "produto": p, "valor_credito": v} for n, p, v, _, _ in CLIENTES],
        "Exemplo", 2, gerente_id=gid)
    for nome, _, _, etapa, horas in CLIENTES:
        quando = (datetime.now() - timedelta(hours=horas)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE clientes SET etapa = ?, atualizado_em = ? WHERE nome = ?", (etapa, quando, nome.title()))
    # Gerente "visitou" há 3 horas: o que aconteceu depois aparece como NOVO e a venda de pouco atrás vira comemoração.
    conn.execute("UPDATE gerentes SET ultima_visita = ? WHERE id = ?",
                 ((datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"), gid))
    db.PASTA_DOCS = Path(os.getenv("DEMO_DOCS") or Path(__file__).resolve().parent / "documentos_demo")
    import shutil
    shutil.rmtree(db.PASTA_DOCS, ignore_errors=True)
    def pacote(nome, produto, recado, arquivos, dias, visto=False, recebido=False):
        pid = db.salvar_pacote_docs(conn, gid, nome, [(n, pdf_exemplo(t), "application/pdf") for n, t in arquivos], produto, recado)
        quando = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE pacotes_docs SET criado_em = ?, visto_em = ?, recebido_em = ? WHERE id = ?",
                     (quando, quando if visto else None, quando if recebido else None, pid))

    pacote("Ricardo Tavares Neto", "auto", "", [("CNH_Ricardo.pdf", "CNH - Ricardo Tavares"),
           ("Comprovante_renda.pdf", "Comprovante de renda")], 3, visto=True, recebido=True)
    pacote("Helena Duarte Campos", "moto", "Cliente da agência do Barro Preto, já tem conta. Só falta assinar.",
           [("RG_Helena.pdf", "RG - Helena Duarte"), ("Comprovante_residencia.pdf", "Comprovante de residencia")], 1, visto=True)
    pacote("Ana Paula Ribeiro", "imóvel", "Quer abrir conta e já fechar o consórcio de imóvel na mesma visita. Prefere atendimento de manhã.",
           [("RG_Ana_Paula.pdf", "RG - Ana Paula Ribeiro"), ("Comprovante_residencia.pdf", "Comprovante de residencia"),
            ("Holerite_setembro.pdf", "Holerite - setembro")], 0.02)
print(f"ok: {len(CLIENTES)} clientes de exemplo · token {TOKEN}")
