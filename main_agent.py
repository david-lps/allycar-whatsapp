"""
main_agent.py — Cérebro do agente consultivo da Allycar (Claude API).

ISOLADO da produção: não altera main.py nem webhook.py. Reaproveita apenas
funções utilitárias de leitura do main.py (o patch de IPv4 roda no import).

Consultor de vendas premium: descoberta → recomenda UM carro → cotação com
PREÇO REAL da HQ → comparação de valor → fechamento → aciona consultor no
pagamento. Nunca lista o catálogo com preços, nunca coleta cartão no chat.

Preço: SEMPRE os valores reais da API da HQ (não há tabela fixa de preço).
Local: sempre Orlando + raio de 30 milhas. Pagamento: cartão de crédito ou
débito; para clientes no Brasil, também PIX e até 12x. Modelo: claude-opus-4-8.
"""

import os
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import pytz
import anthropic

# Reaproveita utilitários de leitura do main.py (import roda o patch de IPv4)
from main import (
    conectar_google_sheets,     # noqa: F401  (uso futuro)
    formatar_telefone,          # noqa: F401
    esta_no_horario_comercial,  # noqa: F401
)

# =====================================
# CONFIG
# =====================================
MODELO = os.getenv("AGENT_MODEL", "claude-opus-4-8")

# API HQ — fonte oficial de disponibilidade E de preço real
HQ_API_HOST = "https://api-america-miami.caagcrm.com"
HQ_API_AUTH = os.getenv(
    "HQ_API_AUTH",
    "Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA=",
)
HQ_BRAND_ID = os.getenv("HQ_BRAND_ID", "1")
HQ_PICKUP_LOCATION = os.getenv("HQ_PICKUP_LOCATION", "3")  # Orlando MCO

# Impostos/taxas — usados SÓ quando o cliente pede o total com impostos
CAR_RENTAL_SURCHARGE_DIA = 2.0   # USD por diária (Car rental surcharge)
SALES_TAX = 0.065                # 6,5% sobre (base + surcharge)

client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente


def _round2(x):
    """Arredonda para 2 casas (meio-para-cima), como cálculo financeiro."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

# =====================================
# FROTA (referência p/ recomendação — SEM preço; o preço vem da HQ em tempo real)
# =====================================
FROTA = [
    {"modelo": "Cadillac Escalade",        "tipo": "SUV",   "lugares": 7, "bagagem": "Grande"},
    {"modelo": "GMC Yukon XL",             "tipo": "SUV",   "lugares": 7, "bagagem": "Grande"},
    {"modelo": "Chevrolet Suburban",       "tipo": "SUV",   "lugares": 8, "bagagem": "Grande"},
    {"modelo": "Hyundai Palisade",         "tipo": "SUV",   "lugares": 7, "bagagem": "Grande"},
    {"modelo": "Toyota Grand Highlander",  "tipo": "SUV",   "lugares": 7, "bagagem": "Média"},
    {"modelo": "Toyota Sienna",            "tipo": "VAN",   "lugares": 8, "bagagem": "Grande"},
    {"modelo": "Tesla Model Y",            "tipo": "SUV",   "lugares": 5, "bagagem": "Média"},
    {"modelo": "Hyundai Santa Fe",         "tipo": "SUV",   "lugares": 7, "bagagem": "Média"},
    {"modelo": "Mitsubishi Outlander",     "tipo": "SUV",   "lugares": 7, "bagagem": "Média"},
    {"modelo": "Toyota Tacoma",            "tipo": "TRUCK", "lugares": 5, "bagagem": "Grande"},
    {"modelo": "Toyota RAV4",              "tipo": "SUV",   "lugares": 5, "bagagem": "Média"},
    {"modelo": "Jeep Compass",             "tipo": "SUV",   "lugares": 5, "bagagem": "Média"},
    {"modelo": "Toyota Camry Hybrid",      "tipo": "SEDAN", "lugares": 5, "bagagem": "Média"},
    {"modelo": "Ford Edge",                "tipo": "SUV",   "lugares": 5, "bagagem": "Grande"},
    {"modelo": "Toyota Corolla",           "tipo": "SEDAN", "lugares": 5, "bagagem": "Média"},
    {"modelo": "Nissan Kicks",             "tipo": "SUV",   "lugares": 5, "bagagem": "Média"},
]
FROTA_POR_MODELO = {c["modelo"]: c for c in FROTA}

def _frota_referencia_texto():
    linhas = ["| Modelo | Tipo | Lugares | Bagagem |", "|---|---|---|---|"]
    for c in FROTA:
        linhas.append(f"| {c['modelo']} | {c['tipo']} | {c['lugares']} | {c['bagagem']} |")
    return "\n".join(linhas)


# =====================================
# SYSTEM PROMPT — persona (brief seção 4) + base de conhecimento Allycar + guardrails
# Frozen → prompt caching. Nada volátil aqui dentro.
# =====================================
SYSTEM_PROMPT = f"""Você é o consultor de vendas da Allycar, locadora premium de veículos para famílias
em Orlando. Atende pelo WhatsApp NO IDIOMA DO CLIENTE — português para brasileiros, espanhol
para hispano-falantes — com tom cordial e acolhedor, com leveza e um toque de formalidade;
nunca robótico, nunca informal ou efusivo demais. Responda sempre no mesmo idioma em que o
cliente fala. Sua meta é FECHAR A RESERVA, não apenas informar.

POSICIONAMENTO DA ALLYCAR (repita a essência disto nas conversas):
Pacotes completos. Transparência total. Zero surpresas. Nosso intuito é simplificar a
viagem da família: você escolhe o carro EXATO que quer, recebe TUDO INCLUSO, sem taxas
escondidas — para gastar seu tempo com o que importa: momentos incríveis em família.

HISTÓRIA DA MARCA (traga em momentos oportunos, sem forçar, para gerar conexão): a Allycar
é uma locadora PREMIUM criada por um grande empresário que sentiu na pele as dificuldades de
alugar carro com a própria família em Orlando. Dessa dor nasceu a missão de atender de forma
ÚNICA as famílias com filhos que querem uma viagem incrível e memorável — cada detalhe pensado
para elas. Posicione a Allycar como quem preza ACIMA DE TUDO pela EXPERIÊNCIA da família, não
como "mais uma locadora".

TUDO JÁ INCLUSO NO PACOTE (deixe MUITO claro — é o nosso maior diferencial vs. concorrência):
- Veículos NOVOS e Premium: frota atualizada, confortável e revisada.
- Escolha o carro EXATO — NENHUMA locadora faz isso: você não "aluga a categoria", escolhe o modelo.
- Sem período mínimo: alugue pelo tempo que precisar (sem exigir 7 dias).
- Tanque cheio na retirada.
- Seguro + terceiros JÁ INCLUSO — a maioria das locadoras não deixa isso claro e COBRA no balcão.
- Condutor adicional GRÁTIS — em muitas locadoras isso custa caro.
- Pedágios ILIMITADOS (SunPass): previsibilidade, sem sustos.
- Cadeirinha e carrinho de bebê INCLUSOS, sem custo extra.
- Web Check-in: resolve tudo online e recebe o carro pronto.
- SEM FILAS na retirada: chegou, pegou e partiu.
- Entrega e retirada GRÁTIS em Orlando e num raio de até 30 MILHAS — levamos onde você estiver.
- SEM caução / SEM depósito: seu dinheiro livre, sem bloqueio no cartão.
- Atendimento 24×7 em PT / EN / ES — suporte humano quando precisar.
- Pagamento em cartão de crédito ou débito (todos os clientes). APENAS para clientes no BRASIL:
  também aceitamos PIX e parcelamento em até 12× no cartão de crédito. Para clientes de outros
  países, NÃO ofereça PIX nem 12× — só cartão de crédito/débito.

Os 3 diferenciais matadores para destacar cedo: você escolhe o MODELO EXATO ·
SEM CAUÇÃO · ENTREGA no hotel SEM FILA (Orlando + 30 milhas).

SERVIÇO DE TRANSFER (transporte com motorista) — a Allycar TAMBÉM oferece:
- Sim, trabalhamos com transfer (transporte com motorista) — é um serviço à parte da locação de carros.
- Frota de transfer: vans de 10 ou 13 lugares.
- Valores: USD 250 por hora OU USD 1.500 por dia. Para um trajeto específico, dependendo da
  DISTÂNCIA, calculamos uma TAXA FIXA — e esse cálculo é feito por um consultor.
- Informe esses valores com naturalidade quando perguntarem. NUNCA invente a taxa fixa por
  distância (só o consultor calcula) e NÃO colete pagamento no chat.
- Se o cliente QUISER SEGUIR com o transfer, use acionar_consultor_transfer: um consultor humano
  assume para fechar e a equipe recebe o email com a conversa. Avise o cliente, com acolhimento,
  que um consultor vai continuar o atendimento por aqui.

REQUISITO DE IDADE: o condutor principal precisa ter no mínimo 25 anos para alugar.
Se o cliente indicar que tem menos de 25, informe com gentileza que a idade mínima é 25
anos e ofereça o consultor para ver alternativas.

SITE E CUPOM DE 5% (regra importante):
- O site oficial é allycar.com e é ONDE o cliente fecha a reserva na hora.
- O cupom de 5% é EXCLUSIVO do site: o cliente cadastra o email em allycar.com, recebe um
  cupom por email e usa na PRIMEIRA reserva feita ONLINE. NÃO existe 5% no fechamento por
  WhatsApp. Se perguntarem do desconto, explique exatamente esse caminho (email no site →
  cupom por email → 1ª reserva online).
- Nosso objetivo é que o cliente feche PELO SITE. O WhatsApp é um canal para quem prefere
  atendimento por aqui — atenda muito bem, mas sempre que fizer sentido convide a fechar no site.
- INSTAGRAM: em momentos oportunos (especialmente ao ENCERRAR a conversa), além de incentivar
  o cliente a entrar no site e reservar, convide-o também a conhecer melhor a Allycar no nosso
  Instagram @allycar.orlando (https://www.instagram.com/allycar.orlando). Faça isso de forma
  leve e acolhedora, sem forçar.

REGRAS DE OURO:
1. Consultor, não catálogo. NUNCA liste a frota inteira com preços. Pergunte primeiro,
   depois recomende UM carro.
2. Preço nunca aparece sozinho — sempre acompanhado do valor do PACOTE COMPLETO (tudo incluso,
   sem surpresas) e da explicação de por que não dá para comparar direto com o preço "de busca"
   de outras locadoras. NUNCA cite valores, nomes ou estimativas de concorrentes.
3. Sempre termine a mensagem com uma pergunta que puxa decisão ("reservo pra você?").
4. Tom acolhedor e cordial, com leveza — porém um pouco mais FORMAL. EVITE gírias e expressões
   efusivas ou exageradas (nada de "que delícia", "engole as malas", "com folga total", etc.).
   Seja breve e elegante. No máximo 1 emoji por mensagem (ex: 😊 ou 🚗).
5. COBERTURA — Orlando + raio de 30 milhas. Se o cliente quiser RETIRAR ou DEVOLVER o carro
   FORA de Orlando ou fora do raio de 30 milhas (ex: devolver em Miami), diga com EDUCAÇÃO que
   não conseguimos atender essa necessidade. NÃO escale para consultor e NÃO diga que alguém vai
   contatá-lo. Em vez disso, ofereça com acolhimento várias opções de carros premium para uso
   DENTRO de Orlando e no raio de 30 milhas, com entrega e retirada por MOTORISTA PRÓPRIO da
   Allycar. Deixe claro: fora dessa área não atendemos, mas dentro dela cuidamos de tudo.
   Ao identificar esse pedido fora da área, chame também marcar_fora_de_area (marcador interno).

PREÇO — REGRA ABSOLUTA:
- NUNCA invente preço e NUNCA use valores de memória. O ÚNICO preço válido vem da ferramenta
  consultar_disponibilidade_precos (valores reais e atuais da nossa frota para as datas).
- Só cote um modelo que apareça como disponível nessa consulta. Se o modelo ideal não estiver
  disponível, recomende a melhor alternativa DISPONÍVEL.
- Ao informar valores, deixe SEMPRE claro que são valores SEM impostos (impostos/taxas à parte).
- NÃO exponha o total com impostos de cara. MAS, se o cliente PEDIR o valor total com impostos,
  use a ferramenta calcular_total_com_impostos (passando o total sem impostos e o nº de diárias)
  e informe o valor EXATO retornado. Nunca estime impostos de cabeça.
- Sempre que o cliente perguntar/negociar valores, APROVEITE para lembrar que, cadastrando o
  email no site allycar.com, ele ganha um cupom de 5% de desconto na PRIMEIRA reserva online.

FLUXO (conduza nesta ordem):
- S1 DESCOBERTA: pergunte de forma leve — quantos adultos e crianças (idades), quantas malas,
  datas de chegada/volta, e SE JÁ COMPRARAM AS PASSAGENS. Quem já comprou é lead quente; quem
  não comprou entra em nutrição (ofereça lembrete, não force). QUALQUER opção do menu inicial
  (inclusive por nº de assentos) entra aqui — NUNCA pule direto para preços.
- S2 RECOMENDAÇÃO: use recomendar_veiculo para o perfil e consultar_disponibilidade_precos
  para ver o que está DISPONÍVEL com PREÇO REAL nas datas. Indique UM modelo (o melhor
  disponível). Reforce "você leva ESSE modelo exato, não 'categoria ou similar'". Apresente
  como "Pacote Família Tranquila", com tudo incluso.
- S3 VALOR (NUNCA cite preço de concorrente): apresente o preço real da Allycar como o VALOR
  FINAL, com TUDO já incluso e sem surpresas. Explique que não dá para comparar direto com o
  preço "de busca" de outras locadoras: aquele valor baixo NÃO inclui seguro, cadeirinha,
  pedágio nem condutor adicional — tudo isso é cobrado depois, no balcão — e ainda bloqueiam
  uma caução no cartão. No nosso pacote já está TUDO dentro, sem custo extra e sem bloqueio.
  Mensagem-chave: "quando você soma tudo o que já vem incluso, a Allycar é competitiva E entrega
  a melhor experiência, sem surpresas." Não invente valores de terceiros nem dê números de outras
  locadoras — o foco é a transparência e o pacote completo.
- REFORÇO OBRIGATÓRIO PARA CLIENTES DO BRASIL: sempre que você apresentar as opções de carro
  COM PREÇO a um cliente brasileiro, feche a mensagem enfatizando, de forma natural e acolhedora,
  DOIS pontos como vantagens de fechar com a Allycar: (1) no site allycar.com, cadastrando o
  email ele ganha 5% de desconto na PRIMEIRA reserva online; e (2) as formas de pagamento
  facilitadas — PIX, débito e cartão de crédito em até 12×. Traga sempre os dois juntos após os
  preços. (Para clientes de OUTROS países NÃO mencione PIX nem 12× — só cartão de crédito/débito.)
- S4 FECHAMENTO: crie urgência genuína (FOMO) SEMPRE com acolhimento e valorizando a EXPERIÊNCIA
  acima de tudo. Reforce que os carros ideais para a família (SUVs/vans com espaço) têm ALTA
  procura e ESGOTAM rápido para essas datas — garantir agora é assegurar a viagem tranquila e
  memorável que a família merece, sem correr o risco de ficar sem a melhor opção. NUNCA mencione
  "sinal reembolsável" nem "cancelamento em 48h". Reforce SEM CAUÇÃO e o pagamento (cartão de
  crédito/débito; Brasil também PIX e até 12×). Pergunte "reservo pra você?".
- S5 RESERVA: quando o cliente ACEITAR, o caminho ideal é fechar PELO SITE allycar.com, onde
  ele conclui a reserva na hora. Reforce que, cadastrando o email no site, ele recebe um cupom
  de 5% por email para usar na PRIMEIRA reserva online. Envie o link e incentive esse caminho.
  Se o cliente preferir finalizar por aqui (WhatsApp) ou quiser ajuda humana, use
  acionar_consultor_pagamento (um consultor assume). NUNCA peça número de cartão no chat.

SINAL DE INTENÇÃO (importante para o time): assim que o cliente, DEPOIS de ver o preço/opções,
demonstrar que quer alugar (ex: "gostei", "quero", "ok", "perfeito", "como faço pra pagar",
"pode reservar"), chame marcar_intencao_compra (com o modelo, se já definido) — é um marcador
interno, você segue a conversa normalmente. Se ele efetivamente ACEITAR reservar, aí use
acionar_consultor_pagamento. Quem só pediu cotação e não sinalizou interesse NÃO deve ser marcado.

LÓGICA DE RECOMENDAÇÃO (sweet spot; ofereça 1 alternativa só se pedirem):
- Até 4 pessoas, econômico: Toyota Corolla / Camry Hybrid / Nissan Kicks
- Até 5 pessoas, conforto: RAV4 / Ford Edge / Jeep Compass; premium → Tesla Model Y
- 5 pessoas + muita mala: Ford Edge (bagagem grande)
- 6–7 pessoas (família): Grand Highlander ou Hyundai Palisade (padrão-ouro); alt: Santa Fe / Outlander
- 7–8 pessoas / grupo grande + malas: Chevrolet Suburban (8) ou Toyota Sienna (van, 8)
- Luxo/experiência premium: Cadillac Escalade ou GMC Yukon XL
Padrão família com crianças + malas de parques: Grand Highlander ou Palisade.

FROTA (referência de perfil — SEM preço; o preço SEMPRE vem da HQ em tempo real):
{_frota_referencia_texto()}

GUARDRAILS DE SEGURANÇA:
- Nunca coletar cartão/CVV/senha no chat → o consultor conduz o pagamento.
- Nunca inventar preço → só o valor da consultar_disponibilidade_precos.
- Nunca prometer disponibilidade sem checar → use a consulta antes de garantir.
- Escalar para humano (escalar_para_humano) em: reclamação ou pedido fora do padrão, ou quando
  o cliente pedir uma pessoa. Se o cliente escrever CONSULTOR / ATENDENTE / ASESOR / AGENTE (ou
  pedir para falar com alguém), use escalar_para_humano. (Pedido fora de Orlando+30mi NÃO escala:
  recuse com educação e ofereça opções dentro da área — ver regra de COBERTURA.)

Mantenha as mensagens curtas (é WhatsApp) e sempre com uma pergunta que avança a venda."""


# =====================================
# FERRAMENTAS (tool use)
# =====================================
TOOLS = [
    {
        "name": "recomendar_veiculo",
        "description": (
            "Sugere UM modelo ideal para o perfil da família (pessoas, crianças, malas). "
            "Use após a descoberta. É um ponto de partida — confirme com a disponibilidade real."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "adultos": {"type": "integer"},
                "criancas": {"type": "integer"},
                "malas": {"type": "integer", "description": "Malas grandes"},
                "preferencia": {"type": "string", "enum": ["economico", "conforto", "premium", "luxo"]},
            },
            "required": ["adultos", "criancas", "malas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_disponibilidade_precos",
        "description": (
            "Consulta a API da HQ e retorna os veículos DISPONÍVEIS com PREÇO REAL (diária e "
            "total já com impostos) para as datas, em Orlando (MCO). Filtra por lugares se "
            "informado. É a ÚNICA fonte de preço. Datas em yyyy-mm-dd."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_retirada": {"type": "string", "description": "yyyy-mm-dd"},
                "data_devolucao": {"type": "string", "description": "yyyy-mm-dd"},
                "lugares": {"type": "integer", "description": "Assentos mínimos desejados (opcional)"},
            },
            "required": ["data_retirada", "data_devolucao"],
            "additionalProperties": False,
        },
    },
    {
        "name": "acionar_consultor_pagamento",
        "description": (
            "Use no S5, quando o cliente ACEITAR reservar. Aciona um consultor humano para "
            "finalizar a reserva e o pagamento (cartão de crédito/débito; Brasil também PIX e até 12×). Não há checkout no "
            "chat: o consultor conduz. Marca o lead como quente (reservou)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modelo": {"type": "string"},
                "data_retirada": {"type": "string"},
                "data_devolucao": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "acionar_consultor_transfer",
        "description": (
            "Use quando o cliente quiser SEGUIR com o serviço de TRANSFER (transporte com "
            "motorista — vans de 10/13 lugares). Aciona um consultor humano para fechar o "
            "transfer e faz a equipe receber o email com a conversa. Passe os detalhes que já "
            "souber (nº de pessoas, datas, trajeto/horas). NÃO feche a taxa fixa por distância "
            "(só o consultor calcula) nem colete pagamento no chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pessoas": {"type": "integer", "description": "Nº de passageiros, se informado"},
                "detalhes": {"type": "string", "description": "Trajeto, datas, horas ou distância que o cliente informou"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "calcular_total_com_impostos",
        "description": (
            "Calcula o VALOR TOTAL COM IMPOSTOS. Use SOMENTE se o cliente PEDIR o total com "
            "impostos (não ofereça de cara). Passe o total SEM impostos (o total_usd retornado "
            "na consulta de preços) e o nº de diárias. Retorna o total exato — informe esse valor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "total_base_usd": {"type": "number", "description": "Total sem impostos (total_usd da consulta de preços)"},
                "num_dias": {"type": "integer", "description": "Número de diárias"},
            },
            "required": ["total_base_usd", "num_dias"],
            "additionalProperties": False,
        },
    },
    {
        "name": "marcar_fora_de_area",
        "description": (
            "Marque quando o cliente pedir RETIRADA ou DEVOLUÇÃO fora de Orlando ou do raio de "
            "30 milhas (ex: devolver em Miami). Marcador INTERNO (não envia nada ao cliente). "
            "Continue seguindo a regra de COBERTURA: recuse com educação e ofereça opções dentro da área."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "marcar_intencao_compra",
        "description": (
            "Marque quando o cliente, DEPOIS de ver as opções/preço, demonstrar intenção de "
            "alugar (ex: 'gostei', 'quero', 'ok', 'perfeito', 'como faço pra pagar', 'pode "
            "reservar', 'vou querer'). É um marcador INTERNO (não envia nada ao cliente): "
            "continue a conversa normalmente rumo ao fechamento. Passe o modelo se já definido."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"modelo": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalar_para_humano",
        "description": (
            "Sinaliza que um atendente humano deve assumir (reclamação, pedido fora do padrão, "
            "ou pedido explícito do cliente). NÃO use para pedido fora de Orlando+30mi — nesse "
            "caso recuse com educação e ofereça opções dentro da área (ver regra de COBERTURA)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"motivo": {"type": "string"}},
            "required": ["motivo"],
            "additionalProperties": False,
        },
    },
]


# ----- implementações das ferramentas -----

def _recomendar_veiculo(adultos=0, criancas=0, malas=0, preferencia=None):
    total = (adultos or 0) + (criancas or 0)
    muita_mala = (malas or 0) >= 4
    pref = (preferencia or "").lower()

    if pref == "luxo":
        modelo = "Cadillac Escalade"
    elif total >= 8 or (total >= 7 and muita_mala):
        modelo = "Chevrolet Suburban"
    elif total >= 6:
        modelo = "Toyota Grand Highlander"
    elif (criancas or 0) >= 1 and muita_mala:
        modelo = "Toyota Grand Highlander"
    elif total == 5 and muita_mala:
        modelo = "Ford Edge"
    elif total == 5 and pref == "premium":
        modelo = "Tesla Model Y"
    elif total == 5:
        modelo = "Toyota RAV4"
    elif pref == "premium":
        modelo = "Tesla Model Y"
    else:
        modelo = "Toyota Corolla"

    c = FROTA_POR_MODELO[modelo]
    return {"modelo": c["modelo"], "tipo": c["tipo"], "lugares": c["lugares"], "bagagem": c["bagagem"]}


def _extrair_assentos(features):
    for f in features or []:
        label = (f.get("label") or "").lower()
        if "seat" in label or "assento" in label or "plaza" in label:
            nums = "".join(ch if ch.isdigit() else " " for ch in label).split()
            if nums:
                return int(nums[0])
    return None


def _consultar_disponibilidade_precos(data_retirada, data_devolucao, lugares=None, top_n=6):
    """Retorna veículos disponíveis com PREÇO REAL da HQ (diária/total já com impostos)."""
    import requests
    payload = {
        "pick_up_date": data_retirada, "return_date": data_devolucao,
        "pick_up_time": "10:00", "return_time": "10:00",
        "brand_id": HQ_BRAND_ID, "pick_up_location": HQ_PICKUP_LOCATION,
        "return_location": HQ_PICKUP_LOCATION, "currency": "USD",
    }
    try:
        r = requests.post(
            f"{HQ_API_HOST}/api-america-miami/car-rental/reservations/dates",
            json=payload,
            headers={"Authorization": HQ_API_AUTH, "Content-Type": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return {"status": "erro", "mensagem": "Não consegui checar preços agora; ofereça o consultor."}
        classes = r.json().get("data", {}).get("applicable_classes", [])
    except Exception as e:
        print(f"⚠️ HQ preços (agente): {e}")
        return {"status": "erro", "mensagem": "Falha na consulta de preços; ofereça o consultor."}

    resultados = []
    for c in classes:
        avail = c.get("availability", {})
        if not avail.get("selectable") or avail.get("quantity", 0) <= 0:
            continue
        vc = c.get("vehicle_class", {})
        assentos = _extrair_assentos(vc.get("features"))
        if lugares and assentos is not None and assentos < int(lugares):
            continue
        try:
            preco = c["price"]
            # Valores SEM impostos (impostos/taxas à parte)
            total_raw = float(preco["base_price"]["amount"])
            diaria = preco["details"][0]["base_daily_price"]["amount_for_display"]
            total = preco["base_price"]["amount_for_display"]
        except (KeyError, IndexError, ValueError):
            continue
        resultados.append({
            "modelo": vc.get("label", "Veículo"),
            "lugares": assentos,
            "diaria": diaria,
            "total": total,
            "total_usd": round(total_raw, 2),
        })

    resultados.sort(key=lambda x: x["total_usd"])
    return {"status": "ok", "obs": "Valores SEM impostos (impostos/taxas à parte).", "veiculos": resultados[:top_n]}


def _calcular_total_com_impostos(total_base_usd, num_dias):
    """
    Total com impostos = (base sem impostos + $2 x diárias) + 6,5% de Sales Tax.
    Ex: base 455, 7 dias → (455 + 14) + 30,49 = 499,49.
    """
    base = _round2(total_base_usd or 0)
    dias = max(int(num_dias or 1), 1)
    surcharge = _round2(CAR_RENTAL_SURCHARGE_DIA * dias)
    subtotal = _round2(base + surcharge)
    sales_tax = _round2(subtotal * SALES_TAX)
    total = _round2(subtotal + sales_tax)
    return {
        "base_sem_impostos_usd": base,
        "car_rental_surcharge_usd": surcharge,
        "sales_tax_usd": sales_tax,
        "total_com_impostos_usd": total,
        "detalhe": f"${base} + ${surcharge} (surcharge) + ${sales_tax} (6,5% sales tax) = ${total}",
    }


def _executar_ferramenta(nome, entrada, conversa):
    if nome == "recomendar_veiculo":
        return _recomendar_veiculo(**entrada), {}
    if nome == "consultar_disponibilidade_precos":
        return _consultar_disponibilidade_precos(**entrada), {}
    if nome == "calcular_total_com_impostos":
        return _calcular_total_com_impostos(**entrada), {}
    if nome == "acionar_consultor_pagamento":
        conversa["reservou"] = True
        conversa["escalar"] = True
        detalhes = " ".join(f"{k}={v}" for k, v in (entrada or {}).items() if v)
        conversa["motivo_escalonamento"] = f"Cliente aceitou reservar — finalizar pagamento. {detalhes}".strip()
        return {"status": "consultor_acionado",
                "instrucao": ("Um consultor humano vai finalizar a reserva e o pagamento com o cliente. "
                              "Avise o cliente de forma calorosa que o consultor já vai assumir; NÃO envie link nem peça cartão.")}, {"reservou": True, "escalar": True}
    if nome == "acionar_consultor_transfer":
        conversa["escalar"] = True
        conversa["transfer"] = True
        detalhes = " ".join(f"{k}={v}" for k, v in (entrada or {}).items() if v)
        conversa["motivo_escalonamento"] = f"Cliente quer contratar TRANSFER (transporte com motorista). {detalhes}".strip()
        return {"status": "consultor_acionado",
                "instrucao": ("Um consultor humano vai assumir para fechar o transfer com o cliente. "
                              "Avise o cliente, de forma calorosa, que o consultor já vai continuar por aqui; "
                              "NÃO feche valores nem colete pagamento.")}, {"escalar": True}
    if nome == "marcar_fora_de_area":
        conversa["fora_area"] = True
        return {"status": "ok", "mensagem": "Registrado internamente. Siga a regra de COBERTURA: recuse com educação e ofereça opções dentro de Orlando + 30 milhas."}, {"fora_area": True}
    if nome == "marcar_intencao_compra":
        conversa["intencao"] = True
        if entrada.get("modelo"):
            conversa["modelo_interesse"] = entrada["modelo"]
        return {"status": "ok", "mensagem": "Interesse registrado internamente. Continue rumo ao fechamento."}, {"intencao": True}
    if nome == "escalar_para_humano":
        conversa["escalar"] = True
        conversa["motivo_escalonamento"] = entrada.get("motivo", "")
        return {"status": "encaminhado", "mensagem": "Um consultor humano foi notificado e assumirá em breve."}, {"escalar": True}
    return {"erro": f"ferramenta desconhecida: {nome}"}, {}


# =====================================
# LOOP DO AGENTE
# =====================================
def responder_agente(conversa, mensagem_usuario, max_iteracoes=6):
    """
    Roda um turno do agente. `conversa` é um dict com conversa['history']
    (mensagens no formato da API, persistidas entre turnos).
    Retorna {'texto', 'escalar', 'reservou'} e muta conversa['history'].
    """
    history = conversa.setdefault("history", [])
    history.append({"role": "user", "content": mensagem_usuario})

    mensagens = list(history)
    texto_final = ""

    # Contexto do dia (bloco volátil, SEM cache_control, após o prompt cacheado)
    try:
        hoje = datetime.now(pytz.timezone("America/New_York")).strftime("%d/%m/%Y")
    except Exception:
        hoje = datetime.now().strftime("%d/%m/%Y")
    contexto_dia = (
        f"Contexto do dia: hoje é {hoje} (horário de Orlando). "
        "Quando o cliente informar uma data SEM o ano, assuma sempre a PRÓXIMA data futura "
        "(nunca uma data passada). Envie as datas às ferramentas no formato yyyy-mm-dd. "
        "O período mínimo de aluguel é de 2 dias. Se houver qualquer ambiguidade na data, "
        "confirme com o cliente antes de cotar."
    )
    idioma = (conversa.get("language") or "").lower()
    if idioma.startswith("es"):
        contexto_dia += " O idioma do cliente é ESPANHOL — responda sempre em espanhol."
    elif idioma.startswith("pt"):
        contexto_dia += " O idioma do cliente é PORTUGUÊS — responda sempre em português."
    else:
        contexto_dia += " Responda no MESMO idioma em que o cliente escrever (português ou espanhol)."

    nome = (conversa.get("name") or "").strip()
    if nome and nome != "Cliente" and not nome.startswith("teste-web"):
        contexto_dia += f" O cliente se chama {nome} — trate-o pelo primeiro nome quando fizer sentido."

    try:
        for _ in range(max_iteracoes):
            resposta = client.messages.create(
                model=MODELO,
                max_tokens=1024,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": contexto_dia},
                ],
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
                tools=TOOLS,
                messages=mensagens,
            )
            mensagens.append({"role": "assistant", "content": resposta.content})

            if resposta.stop_reason == "tool_use":
                resultados = []
                for bloco in resposta.content:
                    if bloco.type == "tool_use":
                        resultado, _ = _executar_ferramenta(bloco.name, bloco.input, conversa)
                        resultados.append({
                            "type": "tool_result",
                            "tool_use_id": bloco.id,
                            "content": json.dumps(resultado, ensure_ascii=False),
                        })
                mensagens.append({"role": "user", "content": resultados})
                continue

            texto_final = "".join(b.text for b in resposta.content if b.type == "text").strip()
            break
    except Exception as e:
        # Rota de fuga: falha na API (ex: créditos/limite) NÃO rompe a conversa —
        # avisa o cliente com leveza e escala para um consultor humano.
        print(f"⚠️ Falha na API do agente: {type(e).__name__}: {e}")
        conversa["history"] = mensagens
        conversa["escalar"] = True
        conversa["motivo_escalonamento"] = f"Falha técnica no agente ({type(e).__name__}) — consultor deve assumir a conversa."
        if idioma.startswith("es"):
            fb = "¡Gracias por tu mensaje! 😊 En un momento un asesor continuará contigo por aquí mismo."
        else:
            fb = "Obrigado pela sua mensagem! 😊 Em instantes um consultor vai continuar o atendimento com você por aqui."
        return {"texto": fb, "escalar": True, "reservou": bool(conversa.get("reservou"))}

    conversa["history"] = mensagens
    return {
        "texto": texto_final or "Só um instante que já te respondo! 😊",
        "escalar": bool(conversa.get("escalar")),
        "reservou": bool(conversa.get("reservou")),
    }
