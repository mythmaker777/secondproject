import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TELEGRAM_ID  = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
PAYNOW_NUMBER      = os.getenv("PAYNOW_NUMBER", "+65XXXXXXXX")
REVOLUT_REVTAG     = os.getenv("REVOLUT_REVTAG", "@yourrevtag")
PAYMENT_AMOUNT     = float(os.getenv("PAYMENT_AMOUNT", "2.99"))
DATABASE_PATH      = os.getenv("DATABASE_PATH", "bot_data.db")
