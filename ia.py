"""Chamadas ao Gemini: leitura das fotos das listas do BB e redação das mensagens."""
import base64
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

_FALLBACK = dotenv_values(Path.home() / "claude-whatsapp" / ".env")
API_KEY = os.getenv("GEMINI_API_KEY") or _FALLBACK.get("GEMINI_API_KEY", "")
MODELO = os.getenv("GEMINI_MODELO") or "gemini-flash-latest"
MODELO_RESERVA = os.getenv("GEMINI_MODELO_RESERVA") or _FALLBACK.get("MODELO_GEMINI_RESERVA") or "gemini-flash-lite-latest"
MODELO_LEITURA = os.getenv("GEMINI_MODELO_LEITURA") or "gemini-flash-lite-latest"
URL = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"


def _cadeia(var, padrao):
    return [m.strip() for m in (os.getenv(var) or padrao).split(",") if m.strip()]


# Cadeias de modelos: quando o Google sobrecarrega um modelo (503 "high demand", às vezes por vários minutos),
# o próximo assume em segundos. Teste de 05/11/2026: flash-latest e lite-latest caíram juntos, enquanto
# 3.5-flash-lite, 3.1-flash-lite e 3.5/3.6-flash respondiam em 2–4 s e contavam a lista certa.
MODELOS_RAPIDOS = _cadeia("GEMINI_MODELOS_RAPIDOS", f"{MODELO_LEITURA},gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.5-flash")
MODELOS_MINUCIOSOS = _cadeia("GEMINI_MODELOS_MINUCIOSOS", f"gemini-3.6-flash,gemini-3.5-flash,{MODELO},gemini-3.5-flash-lite")
TIMEOUT_MODELO = 45  # segundos por tentativa: modelo travado não segura a lista do gerente


# Letras de outros alfabetos (cirílico, grego, árabe, hebraico, chinês, japonês, coreano...): o modelo às vezes
# solta uma palavra assim no meio do português, e uma mensagem dessas não pode chegar ao cliente.
_OUTRO_ALFABETO = re.compile(r"[\u0370-\u06ff\u0900-\u0dff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


class ErroIA(RuntimeError):
    pass


def _chamar(partes, max_tokens=8192, temperatura=0.2, modelos=None):
    if not API_KEY:
        raise ErroIA("GEMINI_API_KEY não configurada (nem no .env do CRM BB, nem em ~/claude-whatsapp/.env).")
    corpo = {
        "contents": [{"role": "user", "parts": partes}],
        # O flash é um modelo "thinking": precisa de folga no maxOutputTokens,
        # senão o JSON sai cortado (aconteceu no CRM Mobie com 300 tokens).
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_tokens,
            "temperature": temperatura,
        },
    }
    # 503/429 de "alta demanda" são frequentes e passageiros: tenta de novo e
    # cai para o modelo reserva antes de desistir.
    for tentativa, modelo in enumerate(modelos or [MODELO, MODELO, MODELO_RESERVA, MODELO_RESERVA]):
        if tentativa:
            time.sleep(1)  # a cadeia já troca de modelo: espera curta
        try:
            r = requests.post(URL.format(m=modelo), params={"key": API_KEY}, json=corpo, timeout=TIMEOUT_MODELO)
        except requests.RequestException as e:
            erro = str(e)
            continue
        if r.status_code == 200:
            break
        erro = f"Gemini respondeu {r.status_code}: {r.text[:200]}"
        # 404 = modelo aposentado/indisponível nesta conta: só passa para o próximo da cadeia.
        if r.status_code not in (404, 429, 500, 502, 503, 504):
            raise ErroIA(erro)
    else:
        raise ErroIA(erro)
    try:
        texto = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(texto, strict=False)  # aceita quebra de linha crua dentro do texto
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ErroIA(f"Resposta da IA em formato inesperado: {e}")


PROMPT_LEITURA = """Estas imagens são fotos/prints de listas de clientes enviadas pelo Banco do
Brasil a um representante autorizado do Consórcio BB. Extraia TODOS os clientes
que aparecem, um por linha da lista, sem pular nenhum e sem inventar nada.

Devolva JSON: {"clientes": [ {...}, ... ], "observacao_leitura": "..."}
Campos de cada cliente (use "" quando não existir na imagem):
- nome: nome completo como aparece
- telefone: com DDD, só como aparece (não complete dígitos faltando)
- email
- cidade
- produto: um de "auto", "imóvel", "moto", "pesados", "serviços" ou "" se não der para saber
- valor_credito: número em reais, sem símbolo (ex.: 85000), ou ""
- situacao_bb: a situação/status do cliente como está escrito (ex.: "simulou no app",
  "proposta não finalizada", "visitou a página"), ou ""
- observacoes: qualquer anotação da linha que não caiba acima
- extras: objeto com as demais colunas da lista (nome da coluna -> valor), EXCETO
  CPF, RG, conta, agência e senha — esses dados NUNCA devem ser copiados.
- confianca: "alta", "media" ou "baixa" para a leitura daquela linha
Se a mesma pessoa aparecer em duas imagens, liste só uma vez.
Em observacao_leitura diga, em uma frase, se alguma parte estava ilegível ou cortada."""


def ler_listas(imagens):
    """`imagens` = lista de (bytes, mime). Devolve (clientes, observacao)."""
    partes = [{"text": PROMPT_LEITURA}]
    for dados, mime in imagens:
        partes.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(dados).decode()}})
    # Leitura de lista usa o modelo leve primeiro: lê uma página em ~5 s, enquanto o flash
    # com raciocínio passava de 50 s e vivia em "alta demanda" (teste de 29/10/2026).
    res = _chamar(partes, max_tokens=16384, temperatura=0, modelos=MODELOS_RAPIDOS + MODELOS_RAPIDOS[:1])
    return res.get("clientes", []), res.get("observacao_leitura", "")


def ler_listas_minucioso(imagens):
    """Segunda leitura, com calma: modelo maior primeiro, linha por linha. Roda em segundo plano depois da
    leitura rápida (que já colocou a lista na tela) e acha quem ela deixou passar."""
    partes = [{"text": PROMPT_LEITURA + "\n\nLEIA COM MUITA ATENÇÃO: percorra a imagem linha por linha, do topo ao "
                       "fim, conte as linhas da lista e confira se o número de clientes devolvidos bate com elas."}]
    for dados, mime in imagens:
        partes.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(dados).decode()}})
    res = _chamar(partes, max_tokens=32768, temperatura=0, modelos=MODELOS_MINUCIOSOS + MODELOS_RAPIDOS[1:])
    return res.get("clientes", []), res.get("observacao_leitura", "")


PROMPT_MENSAGEM = """Você escreve mensagens de {canal} para {consultor}, da {empresa}, que é
REPRESENTANTE AUTORIZADO do Consórcio BB (Banco do Brasil). O cliente abaixo veio de
uma lista que o próprio Banco do Brasil repassou ao representante — ele demonstrou
interesse em consórcio pelos canais do banco. O assunto é EXCLUSIVAMENTE Consórcio BB.

DADOS DO CLIENTE
{dados}

HISTÓRICO RECENTE (mais recente por último)
{historico}

ESTE É O CONTATO Nº {tentativa} DA CADÊNCIA. Objetivo desta etapa: {objetivo}

REGRAS OBRIGATÓRIAS (compliance):
- No 1º contato, apresente-se: "{consultor}, da {empresa}, representante autorizado do
  Consórcio BB" e diga de onde veio o contato (interesse demonstrado no Banco do Brasil).
  Nunca diga que é funcionário ou gerente do banco.
- Nunca prometa contemplação, prazo de contemplação, valor de lance ou rentabilidade.
- Não diga "sem juros" sem completar que existe taxa de administração.
- Não invente valores, parcelas, prazos, grupos ou promoções que não estão nos dados.
- Nunca peça CPF, senha, dados bancários ou código por mensagem.
- Termine com uma pergunta simples que convide resposta.
- Inclua no final, em linha separada, exatamente: "{optout}"
- Tom: pessoal, educado, consultivo; nada de caixa alta, emoji em excesso ou urgência falsa.
{regras_canal}

Devolva JSON: {{"assunto": "...", "texto": "..."}} (assunto vazio se for WhatsApp)."""

OBJETIVOS = [
    "apresentação e confirmar se o interesse em consórcio continua",
    "retomar de forma leve e oferecer tirar dúvidas sobre como funciona o consórcio",
    "trazer um benefício concreto do consórcio ligado ao produto de interesse",
    "propor uma simulação personalizada, sem compromisso",
    "contar como funciona lance e sorteio, de forma educativa",
    "perguntar se o momento mudou ou se prefere ser contatado mais para frente",
    "mensagem curta de lembrete, deixando a porta aberta",
]


def gerar_mensagem(cliente, historico, tentativa, cfg, canal="whatsapp"):
    dados = {k: cliente[k] for k in ("nome", "cidade", "produto", "valor_credito", "situacao_bb", "observacoes") if cliente[k]}
    if cliente["extras"]:
        dados["outras_informacoes"] = json.loads(cliente["extras"])
    hist = "\n".join(f"[{h['criado_em']}] {h['canal']}/{h['direcao']}: {h['texto'][:300]}" for h in historico) or "(nenhum contato ainda)"
    optout = ("Se não quiser mais receber mensagens sobre isso, é só responder SAIR."
              if canal == "whatsapp" else
              "Se não quiser mais receber estes e-mails, basta responder com SAIR.")
    regras = ("- WhatsApp: no máximo 4 frases curtas, pode ter 1 emoji discreto."
              if canal == "whatsapp" else
              "- E-mail: assunto até 60 caracteres, corpo de 3 a 6 frases, assinatura com nome e empresa.")
    prompt = PROMPT_MENSAGEM.format(
        canal="WhatsApp" if canal == "whatsapp" else "e-mail",
        consultor=cfg["nome_consultor"], empresa=cfg["nome_empresa"],
        dados=json.dumps(dados, ensure_ascii=False, indent=1),
        historico=hist, tentativa=tentativa,
        objetivo=OBJETIVOS[min(tentativa - 1, len(OBJETIVOS) - 1)],
        optout=optout, regras_canal=regras,
    )
    for _ in range(3):
        # Mensagem curta não precisa do modelo maior, que às vezes fica minutos sobrecarregado: o rápido vem primeiro.
        res = _chamar([{"text": prompt}], max_tokens=4096, temperatura=0.7,
                      modelos=MODELOS_RAPIDOS)
        texto = (res.get("texto") or "").strip()
        if texto and not _OUTRO_ALFABETO.search(texto + (res.get("assunto") or "")):
            break
    else:
        raise ErroIA("A IA escreveu a mensagem com palavras de outro idioma três vezes; tente redigir de novo.")
    if optout not in texto:
        texto = f"{texto}\n\n{optout}"
    return (res.get("assunto") or "").strip(), texto


PROMPT_RESUMO = """Você prepara um representante autorizado do Consórcio BB para atender um cliente que acabou de
demonstrar interesse no follow-up da operação central. Use SOMENTE os dados e o histórico abaixo.

DADOS DO CLIENTE
{dados}

HISTÓRICO (mais recente por último)
{historico}

REGRAS:
- Não invente nada: se o histórico não diz o que o cliente quer, diga que não ficou claro.
- Nunca afirme contemplação, aprovação de crédito, taxa, prazo ou condição que não esteja nos dados.
- Português do Brasil, frases curtas, direto ao ponto.

Devolva JSON:
{{"resumo": "1 a 2 frases: o que aconteceu na conversa",
  "quer": "o que o cliente parece querer (bem, valor, prazo) ou 'não ficou claro'",
  "proximo_passo": "uma ação concreta para o representante fazer agora, sem prometer condições"}}"""


def resumir_para_atendimento(cliente, historico):
    """Resumo da conversa + próximo passo sugerido, para o representante chegar ao cliente com contexto."""
    dados = {k: cliente[k] for k in ("nome", "cidade", "produto", "valor_credito", "situacao_bb", "observacoes") if cliente[k]}
    hist = "\n".join(f"[{h['criado_em']}] {'cliente' if h['direcao'] == 'entrada' else 'nós'}: {h['texto'][:500]}"
                     for h in historico) or "(sem mensagens registradas)"
    prompt = PROMPT_RESUMO.format(dados=json.dumps(dados, ensure_ascii=False, indent=1), historico=hist)
    for _ in range(3):
        res = _chamar([{"text": prompt}], max_tokens=2048, temperatura=0.3,
                      modelos=MODELOS_RAPIDOS)
        campos = {k: (res.get(k) or "").strip() for k in ("resumo", "quer", "proximo_passo")}
        if campos["resumo"] and not _OUTRO_ALFABETO.search(" ".join(campos.values())):
            return campos
    raise ErroIA("A IA não conseguiu resumir o atendimento; tente de novo.")
