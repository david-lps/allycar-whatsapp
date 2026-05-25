import gspread
from oauth2client.service_account import ServiceAccountCredentials
from twilio.rest import Client
from datetime import datetime, timedelta
import pytz
import time
import os
import http.client
import json
import smtplib
import requests
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import base64
from email.message import EmailMessage
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

import urllib.parse
import unicodedata
import re

# Importar configurações do arquivo config.py
from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_WHATSAPP_NUMBER,
    TWILIO_PHONE_NUMBER,
    SPREADSHEET_NAME,
    GOOGLE_CREDENTIALS,
    BUSINESS_HOURS,
    TWILIO_WHATSAPP_TEMPLATE_SID_BR,
    TWILIO_WHATSAPP_TEMPLATE_SID_LATAM,
    EMAIL_USER, 
    EMAIL_PASSWORD, 
    EMAIL_TO
)

# =====================================
# FUNÇÕES
# =====================================

def conectar_google_sheets():
    """Conecta ao Google Sheets"""
        
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        
        creds = ServiceAccountCredentials.from_json_keyfile_dict(GOOGLE_CREDENTIALS, scope)

        client = gspread.authorize(creds)
        
        sheet = client.open(SPREADSHEET_NAME).sheet1
        
        print("✅ Conectado ao Google Sheets com sucesso!")
        return sheet
        
    except FileNotFoundError:
        print(f"❌ ERRO: Arquivo '{CREDENTIALS_FILE}' não encontrado!")
        print("   Verifique se o arquivo credentials.json está na pasta correta")
        raise
        
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ ERRO: Planilha '{SPREADSHEET_NAME}' não encontrada!")
        print("   Verifique:")
        print("   1. Se o nome da planilha está correto no .env")
        print("   2. Se você compartilhou a planilha com o email da Service Account")
        raise
        
    except gspread.exceptions.APIError as e:
        print(f"❌ ERRO na API do Google: {e}")
        print("   Verifique se você ativou as APIs no Google Cloud Console")
        raise
        
    except Exception as e:
        print(f"❌ ERRO inesperado: {type(e).__name__}")
        print(f"   Detalhes: {str(e)}")
        raise

def esta_no_horario_comercial(pais):
    """Verifica se está no horário comercial do país"""
    if pais not in BUSINESS_HOURS:
        pais = 'Brazil'  # Default
    
    config = BUSINESS_HOURS[pais]
    tz = pytz.timezone(config['timezone'])
    hora_atual = datetime.now(tz).hour
    
    return config['start'] <= hora_atual < config['end']

def formatar_telefone(telefone):
    """Formata telefone para padrão WhatsApp"""
    # Remove espaços e caracteres especiais
    telefone = ''.join(filter(str.isdigit, str(telefone)))
    
    # Adiciona + se não tiver
    if not telefone.startswith('+'):
        telefone = '+' + telefone
    
    return f'whatsapp:{telefone}'

def enviar_mensagem_inicial_com_opcoes(telefone, nome, pais, email_cliente=None):
    try:
        
        pais_norm = unicodedata.normalize("NFKD", pais or "")
        pais_norm = "".join(c for c in pais_norm if not unicodedata.combining(c))
        pais_norm = pais_norm.lower()

        # ===============================
        # 🇺🇸 USA → EMAIL
        # ===============================
        if pais_norm in ["usa", "united states", "estados unidos", "eua"]:
            if not email_cliente and not telefone_cliente:
                print(f"⚠️ USA sem email e sem telefone para {nome}, pulando envio")
                return False, "USA sem email e sem telefone"
        
            email_ok = False
            sms_ok = False
            erros = []
        
            # =========================
            # 1) ENVIO DE EMAIL
            # =========================
            if email_cliente:
                try:
                    response = requests.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "from": "Allycar <booking@allycar.com>",
                            "reply_to": "david@allycar.com",
                            "to": [email_cliente],
                            "subject": "Allycar | Your Vehicle Rental in Orlando",
                            "html": f"""
                            <div style="font-family: Arial, sans-serif; color: #333; max-width: 600px;">
                                <p>Hello {nome},</p>
                                
                                <p>This is <strong>Allycar</strong> — Premium Car Rental in Orlando.</p>
                                
                                <p>We noticed your interest in renting a vehicle with us and would be happy to help you complete your reservation.</p>
                                
                                <p>To assist you as quickly and accurately as possible, please reply to this email with the following information:</p>
                                
                                <ul>
                                    <li>Preferred vehicle type (number of seats or model, if any)</li>
                                    <li>Rental start and end dates</li>
                                    <li>Number of passengers</li>
                                    <li>Pickup and drop-off location (airport or other)</li>
                                    <li>Any special requests or additional details</li>
                                </ul>
                                
                                <p>Once we receive your response, one of our specialists will review your needs and get back to you promptly with the best options available.</p>
                                
                                <p>We look forward to helping you secure the perfect vehicle for your trip to Orlando.</p>
                                
                                <br>
                                <div style="background-color: #006354; padding: 20px; text-align: center; border-radius: 8px;">
                                    <img src="https://allycar.com/assets/allycar.png" alt="Allycar Logo" style="max-width: 180px; display: block; margin: 0 auto 15px;">
                                    <p style="margin: 5px 0; color: #ffffff; font-size: 16px; font-weight: bold;">Allycar Team</p>
                                    <p style="margin: 5px 0; color: #ffffff; font-size: 14px;">Premium Car Rental | Orlando, FL</p>
                                    <p style="margin: 5px 0; color: #ffffff; font-size: 13px;">📞 +1 (407) 712-0270 | 📧 booking@allycar.com</p>
                                    <p style="margin: 5px 0;"><a href="https://www.allycar.com" style="color: #ffffff; font-size: 13px; text-decoration: none;">🌐 www.allycar.com</a></p>
                                </div>
                            </div>
                            """
                        },
                        timeout=10
                    )
        
                    if response.status_code in [200, 201]:
                        email_ok = True
                        print(f"✅ Email enviado para {nome} ({email_cliente})")
                    else:
                        erro_email = f"Erro Resend: {response.text}"
                        erros.append(erro_email)
                        print(f"❌ {erro_email}")
        
                except Exception as e:
                    erro_email = f"Falha ao enviar email: {e}"
                    erros.append(erro_email)
                    print(f"⚠️ {erro_email}")
            else:
                print(f"ℹ️ Sem email para {nome}, pulando envio de email")
        
            # =========================
            # 2) ENVIO DE SMS
            # =========================
            if telefone:
                try:
                    twilio_client = Client(
                        os.getenv("TWILIO_ACCOUNT_SID"),
                        os.getenv("TWILIO_AUTH_TOKEN")
                    )
        
                    sms_body = (
                        f"Hello {nome}, this is Allycar in Orlando. "
                        f"We noticed your interest in renting a vehicle with us. "
                        f"Please reply with: vehicle type/model, rental dates, number of passengers, "
                        f"pickup/drop-off location, and any special requests. "
                        f"More info: https://allycar.com"
                    )
        
                    sms = twilio_client.messages.create(
                        body=sms_body,
                        from_=os.getenv("TWILIO_PHONE_NUMBER"),
                        to=telefone.replace("whatsapp:", "").strip()
                    )
        
                    sms_ok = True
                    print(f"✅ SMS enviado para {nome} ({telefone}) | SID: {sms.sid}")
        
                except Exception as e:
                    erro_sms = f"Falha ao enviar SMS: {e}"
                    erros.append(erro_sms)
                    print(f"⚠️ {erro_sms}")
            else:
                print(f"ℹ️ Sem telefone para {nome}, pulando envio de SMS")
        
            # =========================
            # 3) RETORNO FINAL
            # =========================
            if email_ok and sms_ok:
                return True, "email+sms"
            elif email_ok:
                return True, "email"
            elif sms_ok:
                return True, "sms"
            else:
                return False, " | ".join(erros) if erros else "nenhum envio realizado"
        
        # ===============================
        # 🇧🇷 BRASIL → TEMPLATE BR
        # ===============================
        if pais_norm in ["brazil", "brasil"]:
            template_sid = TWILIO_WHATSAPP_TEMPLATE_SID_BR

        # ===============================
        # 🇦🇷 🇨🇴 AR / CO → TEMPLATE LATAM
        # ===============================
        elif pais_norm in [
            "argentina",
            "colombia",
            "colômbia",
            "mexico",
            "méxico",
            "chile",
            "peru",
            "uruguai",
            "uruguay",
            "equador",
            "ecuador",
            "paraguai",
            "paraguay",
            "guatemala",
            "bolivia",
            "bolívia",
            "venezuela"
        ]:
            template_sid = TWILIO_WHATSAPP_TEMPLATE_SID_LATAM

        # ===============================
        # OUTROS → NÃO ENVIA
        # ===============================
        else:
            print(f"⏭️ País não tratado ({pais}) para {nome}")
            return False, "pais_nao_tratado"

        # ===============================
        # ENVIO WHATSAPP
        # ===============================
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        message = client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=telefone,
            content_sid=template_sid,
            content_variables=json.dumps({
                "1": nome
            })
        )

        print(f"✅ WhatsApp enviado para {nome} ({pais}): {message.sid}")

        language = "es" if pais_norm in [
            "argentina",
            "colombia",
            "colômbia",
            "mexico",
            "méxico",
            "chile",
            "peru",
            "uruguai",
            "uruguay",
            "equador",
            "ecuador",
            "paraguai",
            "paraguay",
            "guatemala",
            "bolivia",
            "bolívia",
            "venezuela"
        ] else "pt"
        
        # registra conversa no webhook (não bloqueante)
        try:
            webhook_url = os.getenv(
                "WEBHOOK_URL",
                "https://allycar-whatsapp-production.up.railway.app"
            )
            requests.post(
                f"{webhook_url}/register_conversation",
                json={
                    "phone": telefone,
                    "name": nome,
                    "language": language
                },
                timeout=2
            )
        except Exception as e:
            print(f"⚠️ Falha ao registrar conversa: {e}")

        return True, message.sid

    except Exception as e:
        print(f"❌ Erro geral no envio para {nome}: {e}")
        return False, str(e)


# Cache em memória
cache_reservas_ativas = {
    "data": [],
    "timestamp": None,
    "validade_minutos": 15
}

def buscar_reservas_ativas_com_cache():
    """Busca apenas reservas ATIVAS (open + rental) com cache"""
    
    # Verificar cache
    if cache_reservas_ativas["timestamp"]:
        tempo_decorrido = datetime.now() - cache_reservas_ativas["timestamp"]
        if tempo_decorrido < timedelta(minutes=cache_reservas_ativas["validade_minutos"]):
            print(f"✅ Usando cache ({len(cache_reservas_ativas['data'])} reservas ativas)")
            return cache_reservas_ativas["data"]
    
    print("🔄 Buscando reservas ativas da API...")
    
    conn = http.client.HTTPSConnection("api-america-miami.caagcrm.com")
    headers = {
        'Authorization': 'Basic YzQzMlR2elRSbFdxMGlJNldUeEFGM1lvUjBqcjVkV2dxRWJ0NGs2TlFTZzhZbmd0RWg6NXVhQjZTWEdGNU1zTk40RExrd29wVTBuZ2RURVpGeHBNb0l4RnZZRHBveGRjaUgxZnA='
    }
    
    todas_reservas = []
    
    try:
        # Buscar cada status separadamente (open, rental)
        for status in ["open", "rental"]:
            
            pagina = 1
            
            while pagina <= 20:  # Limite de segurança
                # Filtro correto conforme documentação
                filtros = [{"type":"string","column":"status","operator":"equals","value":status}]
                filtros_json = json.dumps(filtros)
                filtros_encoded = urllib.parse.quote(filtros_json)
                
                endpoint = f"/api-america-miami/car-rental/reservations?page={pagina}&filters={filtros_encoded}"
                
                conn.request("GET", endpoint, headers=headers)
                res = conn.getresponse()
                data = res.read().decode("utf-8")
                
                if res.status != 200:
                    print(f"      ⚠️ Erro HTTP {res.status}")
                    break
                
                resposta = json.loads(data)
                reservas = resposta.get("data", [])
                
                if not reservas:
                    print(f"      ✓ Fim das páginas para {status}")
                    break
                
                todas_reservas.extend(reservas)
                print(f"      Página {pagina}: +{len(reservas)} reservas")
                
                pagina += 1
        
        # Atualizar cache
        cache_reservas_ativas["data"] = todas_reservas
        cache_reservas_ativas["timestamp"] = datetime.now()
        
        print(f"✅ Total no cache: {len(todas_reservas)} reservas ativas")
        return todas_reservas
        
    except Exception as e:
        print(f"❌ Erro ao buscar reservas: {e}")
        import traceback
        traceback.print_exc()
        return cache_reservas_ativas["data"]  # Retorna cache antigo se der erro

def cliente_ja_tem_reserva(telefone):
    """
    Verifica se telefone tem reserva ativa (open ou rental)
    Retorna True se encontrar, False se não
    """
    
    # Limpar telefone para comparação
    telefone_limpo = telefone.replace("whatsapp:", "").replace("+", "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    
    print(f"\n🔍 Verificando: {telefone}")
    
    # Buscar reservas ativas (com cache)
    reservas_ativas = buscar_reservas_ativas_com_cache()
    
    # Filtrar por telefone
    for reserva in reservas_ativas:
        cliente = reserva.get("customer", {})
        telefone_reserva = cliente.get("phone_number", "")

        if not telefone_reserva:
            continue  # Pula se não tem telefone
        
        # Limpar telefone da reserva
        tel_res_limpo = telefone_reserva.replace("+", "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        
        # Comparar (aceita match parcial)
        if telefone_limpo and tel_res_limpo and (telefone_limpo in tel_res_limpo or tel_res_limpo in telefone_limpo):
            print(f"⛔ RESERVA ATIVA ENCONTRADA!")
            print(f"   ID: {reserva.get('id')}")
            print(f"   Cliente: {cliente.get('label')}")
            print(f"   Status: {reserva.get('status')}")
            print(f"   Telefone: {telefone_reserva}")
            print(f"   Veículo: {reserva.get('vehicle_class', {}).get('name')}")
            print(f"   Pick-up: {reserva.get('pick_up_date')}")
            return True
    
    print(f"✅ Nenhuma reserva ativa encontrada para {telefone}")
    return False

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _pick_lang(country: str) -> str:
    c = _norm(country)

    # PT
    if c in ["brazil", "brasil"]:
        return "pt"

    # ES
    es_countries = {
        "mexico", "méxico",
        "colombia", "colômbia",
        "spain", "espanha", "españa",
        "argentina",
        "uruguay", "uruguai",
        "venezuela",
        "guatemala",
        "chile",
        "peru", "perú",
        "equador", "ecuador",
        "paraguai", "paraguay",
        "bolivia", "bolívia"
    }
    if c in es_countries:
        return "es"

    # EN (default)
    return "en"

def _email_templates(nome: str, lang: str):
    nome = nome or "there"

    if lang == "pt":
        subject = "Allycar | Parceria premium para clientes em Orlando"
        html = f"""
        <div style="font-family: Arial, sans-serif; color:#333; max-width:640px; line-height:1.55;">
          <p>Olá {nome},</p>

          <p>
            Meu nome é <strong>Ofli Guimarães</strong>, fundador da <strong>Allycar</strong>, uma locadora premium em Orlando criada para atender famílias com crianças que buscam uma experiência melhor, mais confortável e sem surpresas.
          </p>

          <p>
            Sabemos que, para uma agência de viagem ou parceiro de turismo, recomendar um serviço não é apenas indicar um fornecedor, é colocar a própria reputação em jogo.
          </p>

          <p>
            Por isso, a Allycar foi construída para resolver uma das maiores dores de quem chega a Orlando com sua família: aluguel de carro com filas, taxas ocultas, seguros confusos, bloqueios no cartão, falta de suporte e, muitas vezes, a frustração de não receber o carro esperado.
          </p>

          <p>
            Nosso modelo é diferente: entregamos uma experiência completa, premium e transparente:
          </p>

          <ul>
            <li><strong>Veículos novos e premium</strong></li>
            <li><strong>Escolha do carro exato</strong>, não apenas da categoria</li>
            <li><strong>Seguro, pedágios e condutor adicional incluídos</strong></li>
            <li><strong>Cadeirinha inclusa</strong> para famílias</li>
            <li><strong>Entrega gratuita em Orlando</strong></li>
            <li><strong>Sem filas, sem depósito caução e sem taxas escondidas</strong></li>
            <li><strong>Atendimento 24/7 em português, inglês e espanhol</strong></li>
          </ul>
          
          <p>
            Estou enviando em anexo uma breve apresentação sobre a Allycar e nosso programa de parceria. 
            Gostaria muito de agendar uma conversa rápida para entender se faz sentido construirmos uma 
            parceria com vocês.
          </p>

          <p>
            Você teria disponibilidade para uma conversa de 15 minutos nos próximos dias?
          </p>

          <p>
            Atenciosamente,<br>
            <strong>Ofli Guimarães</strong><br>
            Founder, Allycar
          </p>

          <br>

          <div style="background:#006354;padding:18px;border-radius:10px;text-align:center;">
            <img src="https://allycar.com/assets/allycar.png" style="max-width:160px;display:block;margin:0 auto 10px;" alt="Allycar">
            <p style="margin:0;color:#fff;font-weight:bold;">Allycar</p>
            <p style="margin:6px 0 0;color:#fff;font-size:13px;">Premium Car Rental Experience in Orlando</p>
            <p style="margin:6px 0 0;color:#fff;font-size:13px;">booking@allycar.com • Orlando, FL</p>
          </div>
        </div>
        """
        return subject, html

    if lang == "es":
        subject = "Allycar | Alianza premium para clientes en Orlando"
        html = f"""
        <div style="font-family: Arial, sans-serif; color:#333; max-width:640px; line-height:1.55;">
          <p>Hola {nome},</p>

            <p>
              Mi nombre es <strong>Ofli Guimarães</strong>, fundador de <strong>Allycar</strong>, una rentadora premium en Orlando creada para familias con niños que buscan una experiencia mejor, más cómoda y sin sorpresas.
            </p>
            
            <p>
              Sabemos que, para una agencia de viajes o socio de turismo, recomendar un servicio no es solo indicar un proveedor — significa poner en juego su propia reputación.
            </p>
            
            <p>
              Por eso, Allycar fue creada para resolver uno de los mayores dolores de las familias que llegan a Orlando: la renta de autos con filas largas, cargos ocultos, seguros confusos, bloqueos en la tarjeta, falta de soporte y, muchas veces, la frustración de no recibir el vehículo esperado.
            </p>
            
            <p>
              Nuestro modelo es diferente: entregamos una experiencia completa, premium y transparente:
            </p>

          <ul>
            <li><strong>Vehículos nuevos y premium</strong></li>
            <li><strong>Elección del auto exacto</strong>, no solo de la categoría</li>
            <li><strong>Seguro, peajes y conductor adicional incluidos</strong></li>
            <li><strong>Silla para niños incluida</strong> para familias</li>
            <li><strong>Entrega gratuita en Orlando</strong></li>
            <li><strong>Sin filas, sin depósito de seguridad y sin cargos ocultos</strong></li>
            <li><strong>Atención 24/7 en español, portugués e inglés</strong></li>
          </ul>

          <p>
            Adjunto una breve presentación sobre Allycar y nuestro programa de alianzas. 
            Me gustaría agendar una conversación rápida para entender si tiene sentido construir 
            una posible alianza con ustedes.
          </p>

          <p>
            ¿Tendrías disponibilidad para una llamada de 15 minutos en los próximos días?
          </p>

          <p>
            Saludos cordiales,<br>
            <strong>Ofli Guimarães</strong><br>
            Founder, Allycar
          </p>

          <br>

          <div style="background:#006354;padding:18px;border-radius:10px;text-align:center;">
            <img src="https://allycar.com/assets/allycar.png" style="max-width:160px;display:block;margin:0 auto 10px;" alt="Allycar">
            <p style="margin:0;color:#fff;font-weight:bold;">Allycar</p>
            <p style="margin:6px 0 0;color:#fff;font-size:13px;">Premium Car Rental Experience in Orlando</p>
            <p style="margin:6px 0 0;color:#fff;font-size:13px;">booking@allycar.com • Orlando, FL</p>
          </div>
        </div>
        """
        return subject, html

    # EN
    subject = "Allycar | Premium partnership for Orlando travelers"
    html = f"""
    <div style="font-family: Arial, sans-serif; color:#333; max-width:640px; line-height:1.55;">
      <p>Hello {nome},</p>

        <p>
          My name is <strong>Ofli Guimarães</strong>, founder of <strong>Allycar</strong>, a premium car rental company in Orlando created for families with children who are looking for a better, more comfortable, and surprise-free experience.
        </p>
        
        <p>
          We know that, for a travel agency or tourism partner, recommending a service is not just about referring a supplier — it means putting your own reputation on the line.
        </p>
        
        <p>
          That is why Allycar was built to solve one of the biggest pain points for families arriving in Orlando: car rental experiences with long lines, hidden fees, confusing insurance options, credit card holds, lack of support, and, often, the frustration of not receiving the car they expected.
        </p>
        
        <p>
          Our model is different: we deliver a complete, premium, and transparent experience:
        </p>

      <ul>
        <li><strong>New and premium vehicles</strong></li>
        <li><strong>Choose the exact car</strong>, not just the category</li>
        <li><strong>Insurance, tolls and additional driver included</strong></li>
        <li><strong>Child seat included</strong> for families</li>
        <li><strong>Free delivery in Orlando</strong></li>
        <li><strong>No lines, no security deposit and no hidden fees</strong></li>
        <li><strong>24/7 support in English, Portuguese and Spanish</strong></li>
      </ul>

      <p>
        I am attaching a short presentation about Allycar and our partner program. 
        I would love to schedule a quick conversation to understand whether there is an opportunity 
        to build a partnership with you.
      </p>

      <p>
        Would you be available for a 15-minute call in the next few days?
      </p>

      <p>
        Best regards,<br>
        <strong>Ofli Guimarães</strong><br>
        Founder, Allycar
      </p>

      <br>

      <div style="background:#006354;padding:18px;border-radius:10px;text-align:center;">
        <img src="https://allycar.com/assets/allycar.png" style="max-width:160px;display:block;margin:0 auto 10px;" alt="Allycar">
        <p style="margin:0;color:#fff;font-weight:bold;">Allycar</p>
        <p style="margin:6px 0 0;color:#fff;font-size:13px;">Premium Car Rental Experience in Orlando</p>
        <p style="margin:6px 0 0;color:#fff;font-size:13px;">booking@allycar.com • Orlando, FL</p>
      </div>
    </div>
    """
    return subject, html

def _resend_send_email(
    to_email,
    subject,
    html,
    attachment_path=None
):
    
    """Envia email via Resend. Retorna (True, 'ok') ou (False, 'erro')."""
    try:
        payload = {
            "from": "Allycar <booking@allycar.com>",
            "reply_to": "david@allycar.com",
            "to": [to_email],
            "subject": subject,
            "html": html
        }

        if attachment_path:
            file_path = Path(attachment_path)

            if not file_path.exists():
                return False, f"Arquivo não encontrado: {attachment_path}"

            with open(file_path, "rb") as f:
                encoded_file = base64.b64encode(f.read()).decode("utf-8")

            payload["attachments"] = [
                {
                    "filename": file_path.name,
                    "content": encoded_file
                }
            ]

        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )

        if resp.status_code in (200, 201):
            return True, "ok"

        return False, resp.text

    except Exception as e:
        return False, str(e)

def _find_col_idx(headers, candidates):
    """Acha coluna pelo nome do header (case-insensitive). Retorna índice 1-based."""
    headers_norm = [_norm(h) for h in headers]
    for c in candidates:
        c_norm = _norm(c)
        if c_norm in headers_norm:
            return headers_norm.index(c_norm) + 1
    return None

def descobrir_pais_por_telefone(telefone):
    numero = re.sub(r"\D", "", str(telefone or ""))

    ddi_to_country = {
        "93": "Afghanistan",
        "355": "Albania",
        "213": "Algeria",
        "376": "Andorra",
        "244": "Angola",
        "1": "USA",
        "1268": "Antigua and Barbuda",
        "54": "Argentina",
        "374": "Armenia",
        "61": "Australia",
        "43": "Austria",
        "994": "Azerbaijan",
    
        "1242": "Bahamas",
        "973": "Bahrain",
        "880": "Bangladesh",
        "1246": "Barbados",
        "375": "Belarus",
        "32": "Belgium",
        "501": "Belize",
        "229": "Benin",
        "975": "Bhutan",
        "591": "Bolivia",
        "387": "Bosnia and Herzegovina",
        "267": "Botswana",
        "55": "Brazil",
        "673": "Brunei",
        "359": "Bulgaria",
        "226": "Burkina Faso",
        "257": "Burundi",
    
        "855": "Cambodia",
        "237": "Cameroon",
        "238": "Cape Verde",
        "236": "Central African Republic",
        "235": "Chad",
        "56": "Chile",
        "86": "China",
        "57": "Colombia",
        "269": "Comoros",
        "242": "Republic of the Congo",
        "243": "Democratic Republic of the Congo",
        "506": "Costa Rica",
        "225": "Cote d'Ivoire",
        "385": "Croatia",
        "53": "Cuba",
        "357": "Cyprus",
        "420": "Czech Republic",
    
        "45": "Denmark",
        "253": "Djibouti",
        "1767": "Dominica",
        "1809": "Dominican Republic",
        "1829": "Dominican Republic",
        "1849": "Dominican Republic",
    
        "593": "Ecuador",
        "20": "Egypt",
        "503": "El Salvador",
        "240": "Equatorial Guinea",
        "291": "Eritrea",
        "372": "Estonia",
        "268": "Eswatini",
        "251": "Ethiopia",
    
        "679": "Fiji",
        "358": "Finland",
        "33": "France",
    
        "241": "Gabon",
        "220": "Gambia",
        "995": "Georgia",
        "49": "Germany",
        "233": "Ghana",
        "30": "Greece",
        "1473": "Grenada",
        "502": "Guatemala",
        "224": "Guinea",
        "245": "Guinea-Bissau",
        "592": "Guyana",
    
        "509": "Haiti",
        "504": "Honduras",
        "36": "Hungary",
    
        "354": "Iceland",
        "91": "India",
        "62": "Indonesia",
        "98": "Iran",
        "964": "Iraq",
        "353": "Ireland",
        "972": "Israel",
        "39": "Italy",
    
        "1876": "Jamaica",
        "81": "Japan",
        "962": "Jordan",
    
        "7": "Russia",
        "254": "Kenya",
        "686": "Kiribati",
        "850": "North Korea",
        "82": "South Korea",
        "965": "Kuwait",
        "996": "Kyrgyzstan",
    
        "856": "Laos",
        "371": "Latvia",
        "961": "Lebanon",
        "266": "Lesotho",
        "231": "Liberia",
        "218": "Libya",
        "423": "Liechtenstein",
        "370": "Lithuania",
        "352": "Luxembourg",
    
        "261": "Madagascar",
        "265": "Malawi",
        "60": "Malaysia",
        "960": "Maldives",
        "223": "Mali",
        "356": "Malta",
        "692": "Marshall Islands",
        "222": "Mauritania",
        "230": "Mauritius",
        "52": "Mexico",
        "691": "Micronesia",
        "373": "Moldova",
        "377": "Monaco",
        "976": "Mongolia",
        "382": "Montenegro",
        "212": "Morocco",
        "258": "Mozambique",
        "95": "Myanmar",
    
        "264": "Namibia",
        "674": "Nauru",
        "977": "Nepal",
        "31": "Netherlands",
        "64": "New Zealand",
        "505": "Nicaragua",
        "227": "Niger",
        "234": "Nigeria",
        "389": "North Macedonia",
        "47": "Norway",
    
        "968": "Oman",
    
        "92": "Pakistan",
        "680": "Palau",
        "970": "Palestine",
        "507": "Panama",
        "675": "Papua New Guinea",
        "595": "Paraguay",
        "51": "Peru",
        "63": "Philippines",
        "48": "Poland",
        "351": "Portugal",
    
        "974": "Qatar",
    
        "40": "Romania",
        "250": "Rwanda",
    
        "1869": "Saint Kitts and Nevis",
        "1758": "Saint Lucia",
        "1784": "Saint Vincent and the Grenadines",
        "685": "Samoa",
        "378": "San Marino",
        "239": "Sao Tome and Principe",
        "966": "Saudi Arabia",
        "221": "Senegal",
        "381": "Serbia",
        "248": "Seychelles",
        "232": "Sierra Leone",
        "65": "Singapore",
        "421": "Slovakia",
        "386": "Slovenia",
        "677": "Solomon Islands",
        "252": "Somalia",
        "27": "South Africa",
        "211": "South Sudan",
        "34": "Spain",
        "94": "Sri Lanka",
        "249": "Sudan",
        "597": "Suriname",
        "46": "Sweden",
        "41": "Switzerland",
        "963": "Syria",
    
        "886": "Taiwan",
        "992": "Tajikistan",
        "255": "Tanzania",
        "66": "Thailand",
        "670": "Timor-Leste",
        "228": "Togo",
        "676": "Tonga",
        "1868": "Trinidad and Tobago",
        "216": "Tunisia",
        "90": "Turkey",
        "993": "Turkmenistan",
        "688": "Tuvalu",
    
        "256": "Uganda",
        "380": "Ukraine",
        "971": "United Arab Emirates",
        "44": "United Kingdom",
        "598": "Uruguay",
        "998": "Uzbekistan",
    
        "678": "Vanuatu",
        "58": "Venezuela",
        "84": "Vietnam",
    
        "967": "Yemen",
    
        "260": "Zambia",
        "263": "Zimbabwe"
    }

    for ddi in sorted(ddi_to_country.keys(), key=len, reverse=True):
        if numero.startswith(ddi):
            return ddi_to_country[ddi]

    return "Brazil"

def processar_leads():
    """Processa leads da planilha e envia mensagens com opções interativas"""
    print("🚀 Iniciando processamento de leads...\n")
    
    # Conecta à planilha
    sheet = conectar_google_sheets()
    leads = sheet.get_all_records()
    headers = sheet.row_values(1)
    
    enviados = 0
    erros = 0
    pulados = 0

    # =====================================
    # PRIMEIRO FOR: PROCESSAR LEADS B2C 
    # =====================================
    
    for idx, lead in enumerate(leads, start=2):  # Começa em 2 (linha 1 é cabeçalho)
        nome = lead.get('NOME', '')
        telefone = lead.get('TELEFONE', '')
        status = lead.get('STATUS', '')
        email_cliente = lead.get('EMAIL', '')
        pais = descobrir_pais_por_telefone(telefone)
        
        # Debug
        print(f"\n📋 Processando linha {idx}: {nome}")

        # Pula se já foi enviado
        if status == 'Sent':
            print(f"⏭️  Pulando {nome} - já enviado")
            pulados += 1
            continue

        pais_normalizado = pais.strip().lower()
        
        # Verifica horário comercial
        if not esta_no_horario_comercial(pais):
            print(f"⏰ Pulando {nome} - fora do horário comercial de {pais}")
            pulados += 1
            continue
        
        # Valida dados
        if not nome or not telefone:
            print(f"⚠️  Pulando linha {idx} - dados incompletos")
            sheet.update_cell(idx, headers.index("STATUS") + 1, 'Error - Incomplete data')
            erros += 1
            continue

        # Formata telefone
        telefone_formatado = formatar_telefone(telefone)
        
        # Verificar se já existe reserva no HQ
        if cliente_ja_tem_reserva(telefone_formatado):
            print(f"⏭️ Pulando {nome} - cliente já possui reserva")
            sheet.update_cell(idx, headers.index("STATUS") + 1, 'Skipped - Already has reservation')
            pulados += 1
            continue
        
        # Envia mensagem INICIAL com OPÇÕES
        print(f"📤 Enviando mensagem para {nome} ({telefone_formatado})...")
        sucesso, resultado = enviar_mensagem_inicial_com_opcoes(
            telefone_formatado, 
            nome,
            pais_normalizado,
            email_cliente
        )
        
        if sucesso:
            # Atualiza planilha
            sheet.update_cell(idx, headers.index("STATUS") + 1, 'Sent')
            sheet.update_cell(idx, headers.index("NOTIFICAÇÃO") + 1, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            enviados += 1
            print(f"✅ Sucesso! O cliente vai receber opções interativas")
            print(f"   As respostas serão processadas pelo webhook")
        else:
            sheet.update_cell(idx, headers.index("STATUS") + 1, f'Error: {resultado[:50]}')

            erros += 1
        
        # Delay entre mensagens (respeitar limites Twilio)
        time.sleep(2)


    # =====================================
    # SEGUNDO FOR: PROCESSAR LEADS B2B (EMAIL)
    # =====================================
    try:
        ws_b2b = sheet.spreadsheet.worksheet("Leads_B2B")
        headers = ws_b2b.row_values(1)

        col_status = _find_col_idx(headers, ["STATUS", "Status"])
        col_email  = _find_col_idx(headers, ["E-mail", "Email", "EMAIL"])
        col_country = _find_col_idx(headers, ["Country", "PAIS", "Pais", "País"])
        col_nome = _find_col_idx(headers, ["NOME", "Nome", "NAME", "Name"])
        col_ts = _find_col_idx(headers, ["TIMESTAMP", "Timestamp", "DATA_ENVIO", "Data Envio"])

        if not col_status or not col_email or not col_country:
            print("❌ Leads_B2B: faltam colunas obrigatórias (STATUS, E-mail/EMAIL, Country/PAIS).")
        else:
            b2b_rows = ws_b2b.get_all_values()  # inclui header
            enviados_b2b = 0
            pulados_b2b = 0
            erros_b2b = 0

            for r_idx in range(2, len(b2b_rows) + 1):  # linhas 2..N
                row = b2b_rows[r_idx - 1]

                status_val = row[col_status - 1] if len(row) >= col_status else ""
                if _norm(status_val) != "":
                    pulados_b2b += 1
                    continue

                email_cliente = row[col_email - 1] if len(row) >= col_email else ""
                country = row[col_country - 1] if len(row) >= col_country else ""
                nome_b2b = row[col_nome - 1] if (col_nome and len(row) >= col_nome) else "there"

                if _norm(email_cliente) == "":
                    print(f"⚠️ Leads_B2B linha {r_idx}: sem email, pulando")
                    ws_b2b.update_cell(r_idx, col_status, "Error - Missing email")
                    erros_b2b += 1
                    continue

                lang = _pick_lang(country)
                subject, html = _email_templates(nome_b2b, lang)

                pdf_pt = "Allycar_Premium_Partner_Program_PT.pdf"
                pdf_en = "Allycar_Premium_Partner_Program_EN.pdf"

                attachment_path = pdf_pt if lang == "pt" else pdf_en
                
                print(f"📧 (B2B) Enviando email para {nome_b2b} <{email_cliente}> | {country} | {lang} ...")
                ok, info = _resend_send_email(email_cliente, subject, html, attachment_path)

                if ok:
                    ws_b2b.update_cell(r_idx, col_status, "Sent")
                    if col_ts:
                        ws_b2b.update_cell(r_idx, col_ts, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    enviados_b2b += 1
                    print("✅ (B2B) Email enviado")
                else:
                    ws_b2b.update_cell(r_idx, col_status, f"Error: {info[:120]}")
                    erros_b2b += 1
                    print(f"❌ (B2B) Falha: {info}")

                time.sleep(1)

            print("\n" + "-"*60)
            print("📊 RELATÓRIO B2B")
            print("-"*60)
            print(f"✅ Emails enviados: {enviados_b2b}")
            print(f"❌ Erros: {erros_b2b}")
            print(f"⏭️ Pulados (já tinham status): {pulados_b2b}")
            print("-"*60)

    except Exception as e:
        print(f"❌ Erro ao processar Leads_B2B: {e}")
    
    print("\n" + "="*60)
    print("📊 RELATÓRIO B2C")
    print("="*60)
    print(f"✅ Mensagens enviadas: {enviados}")
    print(f"❌ Erros: {erros}")
    print(f"⏭️  Pulados: {pulados}")
    print(f"📝 Total processado: {len(leads)}")
    print("="*60)
    print("\n💡 PRÓXIMOS PASSOS:")
    print("   1. Os clientes vão responder")
    print("   2. O webhook vai capturar as respostas automaticamente")
    print("   3. Leads interessados vão gerar notificação para o WhatsApp comercial")
    print("   4. Acompanhe os logs do webhook em tempo real!")
    print("="*60)

# =====================================
# EXECUÇÃO
# =====================================

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║              🚗 SISTEMA ALLYCAR - WHATSAPP BOT 🚗             ║
║                                                              ║
║  Sistema de envio automatizado de mensagens com opções       ║
║  interativas para leads de locação de veículos               ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    try:
        processar_leads()
    except KeyboardInterrupt:
        print("\n\n⚠️  Processo interrompido pelo usuário")
    except Exception as e:
        print(f"\n\n❌ Erro fatal: {str(e)}")
        import traceback
        print("\n🔍 Detalhes do erro:")
        traceback.print_exc()
