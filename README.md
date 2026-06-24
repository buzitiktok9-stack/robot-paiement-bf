# Robot Vérificateur — Mobile Money BF

Vérification automatique des paiements Orange Money, Moov Money et Wave
pour le Burkina Faso. Utilise Claude Vision pour lire les captures d'écran.

## Fonctionnalités

- Scan d'image par IA (Claude Vision) — lit les captures d'écran directement
- Vérification opérateur (Orange / Moov / Wave)
- Vérification ID de transaction unique (anti-fraude double paiement)
- Vérification du montant avec tolérance configurable
- Validation format numéro de téléphone (national/international BF)
- Vérification de l'heure — refuse les paiements trop anciens
- Historique de session
- Compatible bot Telegram via l'API REST

---

## Installation locale

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python app.py
```
Ouvrir http://localhost:5000

---

## Déploiement gratuit sur Render.com

1. Créer un compte sur https://render.com
2. "New Web Service" → connecter ton dépôt GitHub
3. Ajouter la variable d'environnement :
   - `ANTHROPIC_API_KEY` = ta clé API Anthropic
4. Start command : `gunicorn app:app`
5. C'est tout — URL publique fournie automatiquement

---

## Déploiement sur Railway

```bash
npm install -g @railway/cli
railway login
railway new
railway up
railway variables set ANTHROPIC_API_KEY=sk-ant-...
```

---

## API REST (pour intégration bot Telegram)

### Vérifier via SMS texte

```
POST /verify
Content-Type: application/json

{
  "text": "Cher client, vous avez transféré 5000 FCFA...",
  "operateur": "orange",
  "pay_type": "national",
  "expected_amount": 5000,
  "max_delay_min": 60,
  "tolerance": 5
}
```

### Vérifier via image (base64)

```
POST /verify
Content-Type: application/json

{
  "image_b64": "iVBORw0KGgoAAAANS...",
  "media_type": "image/jpeg",
  "operateur": "orange",
  "pay_type": "national",
  "expected_amount": 5000,
  "max_delay_min": 60,
  "tolerance": 5
}
```

### Réponse

```json
{
  "verdict": "ok",
  "checks": [
    {"status": "ok", "msg": "Opérateur Orange Money confirmé"},
    {"status": "ok", "msg": "ID unique confirmé : PP260621.1449.79348199"},
    {"status": "ok", "msg": "Montant correct : 5 000 FCFA"},
    {"status": "ok", "msg": "Numéro valide : +22665669292"},
    {"status": "ok", "msg": "Heure valide : il y a 12min"}
  ],
  "parsed": {
    "amount": 5000.0,
    "tx_id": "PP260621.1449.79348199",
    "number": "+22665669292",
    "date": "2026/06/23 14:30:00",
    "fees": 39.0,
    "balance": 897.52
  },
  "raw_text": "Cher client, vous avez transféré..."
}
```

verdict = "ok" | "ko" | "warn"

---

## Intégration bot Telegram (Python)

```python
import requests
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

VERIFY_URL = "https://ton-app.onrender.com/verify"
MONTANT_COMMANDE = 5000  # FCFA

async def handle_photo(update, context):
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    import base64, httpx
    img_bytes = await file.download_as_bytearray()
    b64 = base64.b64encode(img_bytes).decode()

    result = requests.post(VERIFY_URL, json={
        "image_b64": b64,
        "media_type": "image/jpeg",
        "operateur": "orange",
        "pay_type": "national",
        "expected_amount": MONTANT_COMMANDE,
        "max_delay_min": 60,
        "tolerance": 5,
    }).json()

    if result["verdict"] == "ok":
        await update.message.reply_text("✅ Paiement confirmé ! Votre commande est validée.")
    elif result["verdict"] == "ko":
        reasons = "\n".join(f"• {c['msg']}" for c in result["checks"] if c["status"] == "ko")
        await update.message.reply_text(f"❌ Paiement rejeté :\n{reasons}")
    else:
        await update.message.reply_text("⚠️ Paiement suspect — vérification manuelle requise.")

app = Application.builder().token("TON_TOKEN_TELEGRAM").build()
app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
app.run_polling()
```

---

## Variables d'environnement requises

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic (https://console.anthropic.com) |
