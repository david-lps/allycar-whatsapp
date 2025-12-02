import threading
import os
from flask import Flask, request
import webhook  # importa seu webhook.py
import main     # importa seu main.py

app = Flask(__name__)

# ==========================================
#  Rotas do webhook (Twilio → sua API)
# ==========================================
@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    """
    Esta rota recebe POST do Twilio e passa para o handler
    dentro do webhook.py
    """
    try:
        return webhook.app.full_dispatch_request()
    except Exception as e:
        print("Erro no webhook:", e)
        return "Erro interno", 500


# ==========================================
# Inicialização paralela
# ==========================================
def start_worker():
    """
    Roda o main.py continuamente.
    """
    try:
        main.__main__()  # chama sua função principal
    except AttributeError:
        # Caso main.py não tenha main(), roda como módulo
        import main as m


if __name__ == "__main__":

    # Iniciar o main.py em thread paralela
    worker_thread = threading.Thread(target=start_worker)
    worker_thread.daemon = True
    worker_thread.start()

    print("🚀 Worker iniciado (main.py). Iniciando servidor Flask...")

    # Servidor Flask
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
