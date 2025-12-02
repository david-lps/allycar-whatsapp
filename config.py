import os
import json
from dotenv import load_dotenv

# Carregar .env (para desenvolvimento local)
load_dotenv()

# Credenciais Twilio
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
COMMERCIAL_WHATSAPP = os.getenv('COMMERCIAL_WHATSAPP')

# Google Sheets
SPREADSHEET_NAME = os.getenv('SPREADSHEET_NAME', 'Leads_Allycar')

# Google Credentials
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')

if GOOGLE_CREDENTIALS_JSON:
    GOOGLE_CREDENTIALS = json.loads(GOOGLE_CREDENTIALS_JSON)
else:
    # Fallback para desenvolvimento local
    try:
        with open('credentials.json', 'r') as f:
            GOOGLE_CREDENTIALS = json.load(f)
    except FileNotFoundError:
        print("⚠️  AVISO: credentials.json não encontrado e GOOGLE_CREDENTIALS_JSON não definida")
        GOOGLE_CREDENTIALS = {}

# Horários comerciais por país
BUSINESS_HOURS = {
    'Brazil': {'start': 9, 'end': 18, 'timezone': 'America/Sao_Paulo'},
    'Portugal': {'start': 9, 'end': 18, 'timezone': 'Europe/Lisbon'},
    'USA': {'start': 9, 'end': 22, 'timezone': 'America/New_York'},
    'Argentina': {'start': 9, 'end': 18, 'timezone': 'America/Argentina/Buenos_Aires'},
    'UK': {'start': 9, 'end': 18, 'timezone': 'Europe/London'},
    'Spain': {'start': 9, 'end': 18, 'timezone': 'Europe/Madrid'}
}
