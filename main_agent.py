"""
main_agent.py — Cérebro do agente consultivo da Allycar (Claude API).

ISOLADO da produção: não altera main.py nem webhook.py. Reaproveita apenas
funções utilitárias de leitura do main.py (conexão com planilha, formatação
de telefone, horário comercial — o patch de IPv4 do main.py roda no import).

Implementa o BRIEF: consultor de vendas que faz descoberta → recomenda UM
carro → compara valor → fecha a reserva → envia link de pagamento. Nunca
lista o catálogo inteiro com preços, nunca coleta cartão no chat.

Modelo: claude-opus-4-8 (adaptive thinking). System prompt com prompt caching.
"""

import os
import json
from datetime import datetime

import anthropic

# Reaproveita utilitários de leitura do main.py (import roda o patch de IPv4)
from main import (
    conectar_google_sheets,   # noqa: F401  (disponível para uso futuro)
    formatar_telefone,        # noqa: F401
    esta_no_horario_comercial,  # noqa: F401
)

# =====================================
# CONFIG
# =====================================
MODELO = os.getenv("AGENT_MODEL", "claude-opus-4-8")
PAYMENT_LINK_URL = os.getenv(
    "PAYMENT_LINK_URL", "https://www.allycar.com/checkout"
)  # checkout oficial (BrazaBank) — configurar no Railway

# API HQ (mesma credencial usada no restante do sistema) — só para checar estoque
HQ_API_HOST = "https://api-america-miami.caagcrm.com"
HQ_API_AUTH = os.getenv(
    "HQ_API_AUTH",
    "Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA=",
)
HQ_BRAND_ID = os.getenv("HQ_BRAND_ID", "1")
HQ_PICKUP_LOCATION = os.getenv("HQ_PICKUP_LOCATION", "3")

client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente

# =====================================
# TABELA OFICIAL DE FROTA E PREÇOS (BRIEF seção 5) — diária em USD
# =====================================
FROTA = [
    {"modelo": "Cadillac Escalade",        "tipo": "SUV",   "lugares": 7, "bagagem": "Grande", "diaria": 290},
    {"modelo": "GMC Yukon XL",             "tipo": "SUV",   "lugares": 7, "bagagem": "Grande", "diaria": 190},
    {"modelo": "Chevrolet Suburban",       "tipo": "SUV",   "lugares": 8, "bagagem": "Grande", "diaria": 180},
    {"modelo": "Hyundai Palisade",         "tipo": "SUV",   "lugares": 7, "bagagem": "Grande", "diaria": 120},
    {"modelo": "Toyota Grand Highlander",  "tipo": "SUV",   "lugares": 7, "bagagem": "Média",  "diaria": 120},
    {"modelo": "Toyota Sienna",            "tipo": "VAN",   "lugares": 8, "bagagem": "Grande", "diaria": 120},
    {"modelo": "Tesla Model Y",            "tipo": "SUV",   "lugares": 5, "bagagem": "Média",  "diaria": 100},
    {"modelo": "Hyundai Santa Fe",         "tipo": "SUV",   "lugares": 7, "bagagem": "Média",  "diaria": 100},
    {"modelo": "Mitsubishi Outlander",     "tipo": "SUV",   "lugares": 7, "bagagem": "Média",  "diaria": 100},
    {"modelo": "Toyota Tacoma",            "tipo": "TRUCK", "lugares": 5, "bagagem": "Grande", "diaria": 129},
    {"modelo": "Toyota RAV4",              "tipo": "SUV",   "lugares": 5, "bagagem": "Média",  "diaria": 80},
    {"modelo": "Jeep Compass",             "tipo": "SUV",   "lugares": 5, "bagagem": "Média",  "diaria": 75},
    {"modelo": "Toyota Camry Hybrid",      "tipo": "SEDAN", "lugares": 5, "bagagem": "Média",  "diaria": 70},
    {"modelo": "Ford Edge",                "tipo": "SUV",   "lugares": 5, "bagagem": "Grande", "diaria": 65},
    {"modelo": "Toyota Corolla",           "tipo": "SEDAN", "lugares": 5, "bagagem": "Média",  "diaria": 65},
    {"modelo": "Nissan Kicks",             "tipo": "SUV",   "lugares": 5, "bagagem": "Média",  "diaria": 50},
]
FROTA_POR_MODELO = {c["modelo"]: c for c in FROTA}

# Constantes editáveis da comparação de valor (BRIEF seção 7)
COMP = {
    "base_suv_dia": 90,
    "seguro_dia": 45,
    "cadeirinha_dia": 16,   # por criança (cap ~85 por cadeirinha no total)
    "cadeirinha_cap": 85,
    "condutor_add_dia": 14,
    "pedagio_dia": 15,
    "taxa_percentual": 0.30,
    "caucao_hold": "US$ 300–500 bloqueados no cartão",
}


def _tabela_frota_texto():
    linhas = ["| Modelo | Tipo | Lugares | Bagagem | Diária (USD) |",
              "|---|---|---|---|---|"]
    for c in FROTA:
        linhas.append(f"| {c['modelo']} | {c['tipo']} | {c['lugares']} | {c['bagagem']} | ${c['diaria']} |")
    return "\n".join(linhas)


# =====================================
# SYSTEM PROMPT (BRIEF seção 4 + guardrails seção 11 + dados)
# Frozen → prompt caching. Nada volátil aqui dentro.
# =====================================
SYSTEM_PROMPT = f"""Você é o consultor de vendas da Allycar, locadora premium de veículos para famílias
brasileiras em Orlando. Atende pelo WhatsApp, em português, com tom caloroso, próximo e
humano — nunca robótico. Sua meta é FECHAR A RESERVA, não apenas informar.

REGRAS DE OURO:
1. Consultor, não catálogo. NUNCA liste a frota inteira com preços. Pergunte primeiro,
   depois recomende UM carro.
2. Preço nunca aparece sozinho — sempre com a comparação de custo total vs. locadora de
   aeroporto (use a ferramenta calcular_comparacao_valor).
3. Sempre termine a mensagem com uma pergunta que puxa decisão ("reservo pra você?").
4. Destaque cedo os 3 diferenciais matadores: SEM CAUÇÃO · você escolhe o MODELO EXATO ·
   ENTREGA no hotel SEM FILA.
5. Seja breve e caloroso. Use emojis com moderação (😊 🚗 💙 👶).

FLUXO (máquina de estados — conduza nesta ordem):
- S1 DESCOBERTA: pergunte de forma leve — quantos adultos e crianças (idades), quantas malas,
  datas de chegada/volta, e SE JÁ COMPRARAM AS PASSAGENS. Quem já comprou é lead quente;
  quem não comprou entra em nutrição (ofereça lembrete, não force). QUALQUER opção do menu
  inicial (inclusive por nº de assentos) entra aqui — NUNCA pule direto para preços.
- S2 RECOMENDAÇÃO: indique UM modelo adequado (use recomendar_veiculo). Reforce "você leva
  ESSE modelo exato, não 'categoria ou similar'". Apresente como "Pacote Família Tranquila".
- S3 COMPARAÇÃO: mostre a conta lado a lado (Allycar tudo incluído vs. aeroporto com extras).
  Conclua: "vocês pagam menos E ganham a experiência premium."
- S4 FECHAMENTO: convide a reserva com sinal reembolsável (até 48h antes), escassez real de
  carros grandes nas datas, SEM CAUÇÃO, Pix ou cartão em 12x. Pergunte "reservo?".
- S5 PAGAMENTO: use gerar_link_pagamento e envie o link oficial. NUNCA peça número de cartão.

INCLUÍDO NO PACOTE (sempre reforçar): você escolhe o modelo exato; sem caução; entrega e
retirada grátis no hotel (raio 30 milhas); cadeirinha instalada; carrinho de bebê; seguro
+ terceiros; condutor adicional grátis; pedágios ilimitados (SunPass); tanque cheio na
retirada; sem período mínimo; web check-in sem filas; atendimento 24h PT/EN/ES; Pix ou 12x.

LÓGICA DE RECOMENDAÇÃO (sweet spot; ofereça 1 alternativa só se pedirem):
- Até 4 pessoas, econômico: Toyota Corolla / Camry Hybrid / Nissan Kicks
- Até 5 pessoas, conforto: RAV4 / Ford Edge / Jeep Compass; premium → Tesla Model Y
- 5 pessoas + muita mala: Ford Edge (bagagem grande)
- 6–7 pessoas (família): Grand Highlander ou Hyundai Palisade (padrão-ouro); alt: Santa Fe / Outlander
- 7–8 pessoas / grupo grande + malas: Chevrolet Suburban (8) ou Toyota Sienna (van, 8)
- Luxo/experiência premium: Cadillac Escalade ou GMC Yukon XL
Padrão para família com crianças + malas de parques: Grand Highlander ou Palisade.

TABELA OFICIAL DE PREÇOS (única fonte de preço — NUNCA invente valor):
{_tabela_frota_texto()}

GUARDRAILS DE SEGURANÇA:
- Nunca coletar cartão/CVV/senha no chat → sempre o link oficial de pagamento.
- Nunca inventar preço → use a tabela acima.
- Nunca prometer disponibilidade sem checar → use checar_disponibilidade antes de garantir.
- Escalar para humano (use escalar_para_humano) em: reclamação, pedido fora do padrão, ou
  quando o cliente pedir para falar com uma pessoa.

FERRAMENTAS: use recomendar_veiculo para escolher o carro; checar_disponibilidade para
confirmar estoque real nas datas; calcular_comparacao_valor para a conta vs. aeroporto;
gerar_link_pagamento no fechamento; escalar_para_humano quando necessário. Mantenha as
mensagens curtas (é WhatsApp) e sempre com uma pergunta que avança a venda."""


# =====================================
# FERRAMENTAS (tool use)
# =====================================
TOOLS = [
    {
        "name": "recomendar_veiculo",
        "description": (
            "Recomenda UM modelo ideal com base no perfil da família. Use após a descoberta "
            "(pessoas, crianças, malas). Retorna o modelo recomendado, specs e diária da tabela oficial."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "adultos": {"type": "integer", "description": "Número de adultos"},
                "criancas": {"type": "integer", "description": "Número de crianças"},
                "malas": {"type": "integer", "description": "Número de malas grandes"},
                "preferencia": {
                    "type": "string",
                    "enum": ["economico", "conforto", "premium", "luxo"],
                    "description": "Preferência do cliente, se sinalizada",
                },
            },
            "required": ["adultos", "criancas", "malas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "checar_disponibilidade",
        "description": (
            "Confere na frota (API HQ) se há estoque para as datas. Use ANTES de prometer "
            "disponibilidade. Datas no formato yyyy-mm-dd."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modelo": {"type": "string", "description": "Modelo recomendado (opcional)"},
                "data_retirada": {"type": "string", "description": "yyyy-mm-dd"},
                "data_devolucao": {"type": "string", "description": "yyyy-mm-dd"},
                "lugares": {"type": "integer", "description": "Assentos desejados (opcional)"},
            },
            "required": ["data_retirada", "data_devolucao"],
            "additionalProperties": False,
        },
    },
    {
        "name": "calcular_comparacao_valor",
        "description": (
            "Calcula a comparação de custo total: Allycar (tudo incluído) vs. locadora de "
            "aeroporto (com extras somados). Use para apresentar o preço no S3."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "modelo": {"type": "string", "description": "Modelo recomendado (para pegar a diária da tabela)"},
                "num_dias": {"type": "integer", "description": "Número de diárias"},
                "num_criancas": {"type": "integer", "description": "Número de crianças (para cadeirinhas)"},
            },
            "required": ["modelo", "num_dias", "num_criancas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "gerar_link_pagamento",
        "description": (
            "Retorna o link oficial de pagamento (Pix/cartão 12x) para o cliente garantir a "
            "reserva. Use no S5, após o cliente aceitar. NUNCA colete cartão no chat."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "escalar_para_humano",
        "description": (
            "Sinaliza que um atendente humano deve assumir (reclamação, pedido fora do padrão, "
            "ou pedido explícito do cliente). Um consultor será notificado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {"type": "string", "description": "Motivo do encaminhamento"},
            },
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
        # Família com crianças + muita mala (cadeirinhas + bagagem de parque)
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
    return {
        "modelo": c["modelo"],
        "tipo": c["tipo"],
        "lugares": c["lugares"],
        "bagagem": c["bagagem"],
        "diaria_usd": c["diaria"],
    }


def _hq_disponibilidade(data_retirada, data_devolucao):
    """Chama a API HQ e devolve a contagem de classes com estoque > 0."""
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
            return None
        classes = r.json().get("data", {}).get("applicable_classes", [])
        return sum(
            1 for c in classes
            if c.get("availability", {}).get("selectable")
            and c.get("availability", {}).get("quantity", 0) > 0
        )
    except Exception as e:
        print(f"⚠️ HQ disponibilidade (agente): {e}")
        return None


def _checar_disponibilidade(data_retirada, data_devolucao, modelo=None, lugares=None):
    n = _hq_disponibilidade(data_retirada, data_devolucao)
    if n is None:
        return {"status": "indisponivel_checagem",
                "mensagem": "Não consegui checar o estoque agora; confirme com o consultor antes de garantir."}
    return {
        "status": "ok",
        "classes_com_estoque": n,
        "ha_disponibilidade": n > 0,
        "observacao": "Estoque real na frota para as datas; confirme o modelo específico no fechamento.",
    }


def _calcular_comparacao_valor(modelo, num_dias, num_criancas):
    c = FROTA_POR_MODELO.get(modelo)
    diaria = c["diaria"] if c else COMP["base_suv_dia"]
    dias = max(int(num_dias or 1), 1)
    criancas = max(int(num_criancas or 0), 0)

    allycar_total = diaria * dias

    base = (COMP["base_suv_dia"] + COMP["seguro_dia"]
            + COMP["condutor_add_dia"] + COMP["pedagio_dia"]) * dias
    cadeirinha = min(COMP["cadeirinha_dia"] * dias, COMP["cadeirinha_cap"]) * criancas
    subtotal = base + cadeirinha
    aeroporto_total = round(subtotal * (1 + COMP["taxa_percentual"]))

    return {
        "modelo": modelo,
        "num_dias": dias,
        "allycar_total_usd": allycar_total,
        "allycar_inclui": "tudo incluído, sem caução, entrega no hotel, Pix/12x",
        "aeroporto_total_estimado_usd": aeroporto_total,
        "aeroporto_extras": f"+ caução ({COMP['caucao_hold']}) + fila + atendimento em inglês",
        "economia_usd": max(aeroporto_total - allycar_total, 0),
    }


def _executar_ferramenta(nome, entrada, conversa):
    """Executa a ferramenta e retorna (resultado_json, efeitos_colaterais)."""
    if nome == "recomendar_veiculo":
        return _recomendar_veiculo(**entrada), {}
    if nome == "checar_disponibilidade":
        return _checar_disponibilidade(**entrada), {}
    if nome == "calcular_comparacao_valor":
        return _calcular_comparacao_valor(**entrada), {}
    if nome == "gerar_link_pagamento":
        conversa["reservou"] = True
        return {"link": PAYMENT_LINK_URL,
                "instrucao": "Envie este link ao cliente; ele paga por Pix ou cartão 12x. Nunca peça o cartão no chat."}, {"reservou": True}
    if nome == "escalar_para_humano":
        conversa["escalar"] = True
        conversa["motivo_escalonamento"] = entrada.get("motivo", "")
        return {"status": "encaminhado", "mensagem": "Um consultor humano foi notificado e assumirá em breve."}, {"escalar": True}
    return {"erro": f"ferramenta desconhecida: {nome}"}, {}


# =====================================
# LOOP DO AGENTE
# =====================================
def responder_agente(conversa, mensagem_usuario, max_iteracoes=5):
    """
    Roda um turno do agente. `conversa` é um dict com pelo menos:
      conversa['history'] = lista de mensagens no formato da API (persistida entre turnos)
    Retorna dict: {'texto': str, 'escalar': bool, 'reservou': bool}
    Muta conversa['history'] com o turno do usuário e a resposta do assistente.
    """
    history = conversa.setdefault("history", [])
    history.append({"role": "user", "content": mensagem_usuario})

    # Cópia de trabalho das mensagens para o loop de tool use
    mensagens = list(history)
    texto_final = ""

    for _ in range(max_iteracoes):
        resposta = client.messages.create(
            model=MODELO,
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # frozen → prompt caching
            }],
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},  # chat responsivo; ajustável
            tools=TOOLS,
            messages=mensagens,
        )

        # Guarda o turno do assistente (inclui thinking/tool_use) no histórico da conversa
        mensagens.append({"role": "assistant", "content": resposta.content})

        if resposta.stop_reason == "tool_use":
            resultados = []
            for bloco in resposta.content:
                if bloco.type == "tool_use":
                    resultado, _efeitos = _executar_ferramenta(bloco.name, bloco.input, conversa)
                    resultados.append({
                        "type": "tool_result",
                        "tool_use_id": bloco.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })
            mensagens.append({"role": "user", "content": resultados})
            continue

        # end_turn (ou outro) → extrai o texto final
        texto_final = "".join(b.text for b in resposta.content if b.type == "text").strip()
        break

    # Persiste o histórico completo (com tool use) para o próximo turno
    conversa["history"] = mensagens

    return {
        "texto": texto_final or "Só um instante que já te respondo! 😊",
        "escalar": bool(conversa.get("escalar")),
        "reservou": bool(conversa.get("reservou")),
    }
