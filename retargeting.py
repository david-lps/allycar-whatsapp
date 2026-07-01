"""
Campanha de retargeting (módulo isolado).

Lê a guia "Retargeting" da mesma planilha do Google Sheets e dispara um
template de marketing (WhatsApp via Twilio) para os leads pendentes:
parcelamento em até 12x + 5% de desconto (e PIX/IOF no template em PT).

Este módulo NÃO altera o fluxo principal (main.py / webhook.py). Ele apenas
reaproveita funções utilitárias já existentes (conexão com a planilha,
formatação de telefone e verificação de horário comercial).
"""

import json
from datetime import datetime

from twilio.rest import Client

from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    CAMPAIGN_TEMPLATE_SID_BR,
    CAMPAIGN_TEMPLATE_SID_ES,
)
from main import (
    conectar_google_sheets,
    formatar_telefone,
    esta_no_horario_comercial,
)

RETARGETING_SHEET = "Retargeting"


def _template_e_idioma(pais):
    """Retorna (template_sid, idioma) conforme o país do lead."""
    if (pais or "").strip().lower() in ("brazil", "brasil"):
        return CAMPAIGN_TEMPLATE_SID_BR, "pt"
    return CAMPAIGN_TEMPLATE_SID_ES, "es"


def enviar_campanha_retargeting(registrar_conversa=None):
    """
    Dispara a campanha para os leads pendentes da guia "Retargeting".

    registrar_conversa: callback opcional (phone, name, language) usado para
    registrar a conversa no webhook, de forma que a resposta do cliente seja
    tratada e registrada em Leads_Qualificados como já acontece hoje.

    Retorna um resumo: {"enviados", "erros", "pulados"}.
    """
    print("🚀 Iniciando campanha de retargeting...")

    base = conectar_google_sheets()
    ws = base.spreadsheet.worksheet(RETARGETING_SHEET)
    leads = ws.get_all_records()
    headers = ws.row_values(1)
    col_status = headers.index("STATUS") + 1

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    enviados = 0
    erros = 0
    pulados = 0

    for idx, lead in enumerate(leads, start=2):  # linha 1 = cabeçalho
        nome = str(lead.get("NOME", "")).strip()
        telefone = str(lead.get("TELEFONE", "")).strip()
        pais = str(lead.get("PAIS", "")).strip()
        status = str(lead.get("STATUS", "")).strip()

        # Pula quem já foi processado (STATUS preenchido)
        if status:
            pulados += 1
            continue

        # Valida dados mínimos
        if not nome or not telefone:
            ws.update_cell(idx, col_status, "Erro - dados incompletos")
            erros += 1
            continue

        # Respeita horário comercial do país
        if not esta_no_horario_comercial(pais):
            print(f"⏰ Fora do horário comercial de {pais} — pulando {nome}")
            pulados += 1
            continue

        template_sid, idioma = _template_e_idioma(pais)
        telefone_formatado = formatar_telefone(telefone)

        try:
            msg = client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=telefone_formatado,
                content_sid=template_sid,
                content_variables=json.dumps({"1": nome}),
            )
            ws.update_cell(idx, col_status, f"Enviado {datetime.now():%Y-%m-%d %H:%M}")
            enviados += 1
            print(f"✅ Retargeting enviado para {nome} ({telefone_formatado}) [{idioma}] SID {msg.sid}")

            # Registra a conversa para tratar a resposta do cliente
            if registrar_conversa:
                try:
                    registrar_conversa(telefone_formatado, nome, idioma)
                except Exception as e:
                    print(f"⚠️ Falha ao registrar conversa de {nome}: {e}")

        except Exception as e:
            ws.update_cell(idx, col_status, f"Erro: {str(e)[:120]}")
            erros += 1
            print(f"❌ Erro ao enviar retargeting para {nome}: {e}")

    resumo = {"enviados": enviados, "erros": erros, "pulados": pulados}
    print(f"🏁 Campanha de retargeting concluída: {resumo}")
    return resumo
