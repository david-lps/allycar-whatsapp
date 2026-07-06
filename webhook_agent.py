"""
webhook_agent.py — Camada de I/O do agente consultivo (Flask + Twilio).

ISOLADO da produção (webhook.py/main.py intactos). Rota nova /webhook/agent.
Estado próprio (conversations_agent). Reaproveita só leitura do main.py.

Fluxo assíncrono (o agente com Opus pode passar do timeout de ~15s do Twilio):
  1. Recebe a mensagem do cliente e responde 200 vazio na hora (ack).
  2. Em thread de fundo: roda o agente, envia a resposta via API REST do Twilio
     (dentro da janela de 24h, pois o cliente acabou de escrever), registra o
     lead na planilha e, se o agente escalar, notifica a equipe por email.
"""

import os
import threading
from datetime import datetime

from flask import Flask, request
from dotenv import load_dotenv
import requests
from twilio.rest import Client

from main import conectar_google_sheets
import main_agent

load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # "whatsapp:+1..."

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Estado próprio do agente (em memória — persistência é fase futura)
conversations_agent = {}


# ------- normalização de telefone (mesma lógica da produção, isolada) -------
def _variantes_telefone(from_number):
    num = (from_number or "").replace("whatsapp:", "")
    variantes = set()

    def add(n):
        variantes.add(n)
        variantes.add(f"whatsapp:{n}")

    add(num)
    if num.startswith("+55") and len(num) >= 5:
        ddd, local = num[3:5], num[5:]
        if len(local) == 9 and local.startswith("9"):
            add("+55" + ddd + local[1:])
        elif len(local) == 8:
            add("+55" + ddd + "9" + local)
    if num.startswith("+521"):
        add("+52" + num[4:])
    elif num.startswith("+52"):
        add("+521" + num[3:])
    if num.startswith("+549"):
        add("+54" + num[4:])
    elif num.startswith("+54"):
        add("+549" + num[3:])
    return variantes


def _encontrar_conversa_key(from_number):
    for k in _variantes_telefone(from_number):
        if k in conversations_agent:
            return k
    return None


# ------- efeitos: registro na planilha e alerta de escalonamento -------
def _registrar_planilha(lead_info):
    try:
        sheet = conectar_google_sheets()
        ws = sheet.spreadsheet.worksheet("Leads_Qualificados")
        ws.append_row([
            lead_info["timestamp"], lead_info["name"], lead_info["phone"],
            lead_info["category"], lead_info["message"],
        ])
    except Exception as e:
        print(f"⚠️ Falha ao registrar lead (agente): {e}")


def _alertar_escalonamento(lead_info, motivo):
    try:
        conteudo = f"""Atendimento do agente escalado para humano (WhatsApp)

Data/Hora: {lead_info['timestamp']}
Telefone: {lead_info['phone']}
Motivo: {motivo}

Última mensagem do cliente:
{lead_info['message']}
"""
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                     "Content-Type": "application/json"},
            json={
                "from": "Allycar <booking@allycar.com>",
                "to": ["booking@allycar.com", "david@allycar.com", "bruno@allycar.com"],
                "subject": "🧑‍💼 Agente Allycar: cliente pede atendimento humano",
                "text": conteudo,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️ Falha ao alertar escalonamento (agente): {e}")


# ------- processamento assíncrono (thread de fundo) -------
def _processar(from_number, body):
    try:
        key = _encontrar_conversa_key(from_number) or from_number
        conversa = conversations_agent.setdefault(
            key, {"name": "Cliente", "phone": from_number.replace("whatsapp:", ""), "history": []}
        )

        resultado = main_agent.responder_agente(conversa, body)
        texto = resultado["texto"]

        # Envia a resposta do agente ao cliente (janela de 24h — cliente acabou de escrever)
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER, to=from_number, body=texto
        )
        print(f"🤖 Agente respondeu {from_number}: {texto[:80]}")

        lead_info = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": conversa.get("name", "Cliente"),
            "phone": from_number.replace("whatsapp:", ""),
            "category": "Reservou" if resultado["reservou"] else ("Escalar humano" if resultado["escalar"] else "Agente - em conversa"),
            "message": body,
        }
        _registrar_planilha(lead_info)

        if resultado["escalar"]:
            _alertar_escalonamento(lead_info, conversa.get("motivo_escalonamento", ""))

    except Exception as e:
        print(f"❌ Erro no processamento do agente: {e}")


# ------- rotas -------
@app.route("/webhook/agent", methods=["POST"])
def webhook_agent():
    """Recebe mensagens do cliente e responde de forma assíncrona via agente."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    button_payload = request.form.get("ButtonPayload")
    if button_payload:
        body = button_payload

    print(f"📥 [agente] mensagem de {from_number}: {body}")

    if from_number and body:
        threading.Thread(target=_processar, args=(from_number, body), daemon=True).start()

    # Ack imediato (a resposta real vai pela API REST, na thread de fundo)
    return ("", 204)


@app.route("/agent/health", methods=["GET"])
def health():
    return {"status": "ok", "conversas": len(conversations_agent), "modelo": main_agent.MODELO}, 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print("🤖 webhook_agent (consultor) iniciado.")
    app.run(host="0.0.0.0", port=port)
