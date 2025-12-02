import os
import threading
from flask import Flask, request, jsonify
import webhook   # seu webhook.py
import main      # seu main.py

app = Flask(__name__)


# ==========================================
# ROTA PRINCIPAL DO TWILIO WEBHOOK
# ==========================================
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    """Recebe mensagens do Twilio e delega ao handler dentro do webhook.py"""
    try:
        return webhook.app.full_dispatch_request()
    except Exception as e:
        print("Erro no webhook:", e)
        return "Erro interno", 500


# ==========================================
# ROTA PARA DISPARAR OS LEADS DO MAIN.PY
# ==========================================
@app.route("/run-job", methods=["POST"])
def run_job():
    """Dispara o processamento de leads manualmente ou via API"""
    try:
        threading.Thread(target=main.processar_leads).start()
        return jsonify({"ok": True, "message": "Job iniciado"}), 200
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ==========================================
# INÍCIO DO SERVIDOR
# ==========================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("🚀 Servidor Flask iniciado. Webhook ativo!")
    app.run(host="0.0.0.0", port=port)
