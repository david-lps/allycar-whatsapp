import os
import json

# Credenciais Twilio
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
COMMERCIAL_WHATSAPP = os.getenv('COMMERCIAL_WHATSAPP')
TWILIO_WHATSAPP_TEMPLATE_SID_BR = os.getenv('TWILIO_WHATSAPP_TEMPLATE_SID_BR')
TWILIO_WHATSAPP_TEMPLATE_SID_LATAM = os.getenv('TWILIO_WHATSAPP_TEMPLATE_SID_LATAM')
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")

# Google Sheets
SPREADSHEET_NAME = os.getenv('SPREADSHEET_NAME', 'Leads_Allycar')
GOOGLE_CREDENTIALS = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))

# Horários comerciais
BUSINESS_HOURS = {
    'Brazil': {'start': 9, 'end': 24, 'timezone': 'America/Sao_Paulo'},
    'Portugal': {'start': 9, 'end': 18, 'timezone': 'Europe/Lisbon'},
    'USA': {'start': 9, 'end': 18, 'timezone': 'America/New_York'},
    'Argentina': {'start': 9, 'end': 18, 'timezone': 'America/Argentina/Buenos_Aires'},
    'UK': {'start': 9, 'end': 18, 'timezone': 'Europe/London'},
    'Spain': {'start': 9, 'end': 18, 'timezone': 'Europe/Madrid'}
}
