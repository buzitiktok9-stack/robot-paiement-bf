import os, re, base64, json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# IDs déjà utilisés (en prod, utiliser une vraie base de données)
used_ids = set()

OPERATEURS = {
    "orange": "Orange Money",
    "moov":   "Moov Money",
    "wave":   "Wave",
    "any":    "Mobile Money"
}

OP_KEYWORDS = {
    "orange": r"orange\s*money",
    "moov":   r"moov",
    "wave":   r"wave",
    "any":    r".*"
}

# Formats de numéros par opérateur
NUM_PATTERNS = {
    "orange_national":      r"^(226)?0?[0567]\d{7}$",
    "orange_international": r"^\+?226[0567]\d{7}$|^\+\d{10,15}$",
    "moov_national":        r"^(226)?[67]\d{7}$",
    "moov_international":   r"^\+?226[67]\d{7}$|^\+\d{10,15}$",
    "wave_national":        r"^\+?226\d{8}$|^\d{8}$",
    "wave_international":   r"^\+?\d{10,15}$",
    "any_national":         r"^\+?226\d{8}$|^\d{8}$",
    "any_international":    r"^\+?\d{10,15}$",
}


def extract_text_from_image(image_b64: str, media_type: str = "image/jpeg") -> str:
    """Utilise Claude Vision pour extraire le texte d'une image de paiement."""
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": (
                        "Extrais UNIQUEMENT le texte brut visible dans cette capture d'écran "
                        "de paiement mobile money (Orange Money, Moov Money ou Wave). "
                        "Retourne seulement le texte exact, sans commentaire ni formatage markdown."
                    )
                }
            ]
        }]
    )
    return message.content[0].text.strip()


def parse_payment_text(txt: str) -> dict:
    """Extrait les données de paiement depuis le texte SMS/OCR."""
    data = {
        "amount": None, "tx_id": None, "number": None,
        "date": None, "fees": None, "balance": None
    }

    # Montant
    amt_patterns = [
        r"transfere\s+([\d\s,\.]+)\s*f?cfa",
        r"transfert\s+d.argent\s+(?:international\s+de\s+|de\s+)([\d\s,\.]+)\s*f?cfa",
        r"de\s+([\d\s,\.]+)\s*f?cfa\s+re[cç]u",
        r"montant\s*:?\s*([\d\s,\.]+)\s*f?cfa",
        r"([\d\s,\.]+)\s*f\s*cfa",
        r"([\d\s,\.]+)\s*f?cfa",
    ]
    for p in amt_patterns:
        m = re.search(p, txt, re.I)
        if m:
            val = float(m.group(1).replace(" ", "").replace(",", "."))
            if not any(c.isalpha() for c in m.group(1)) and val > 0:
                data["amount"] = val
                break

    # ID Transaction
    id_patterns = [
        r"ID\s*Trans\s*:?\s*([A-Za-z0-9_\.\-]+)",
        r"TID\s*:?\s*([A-Za-z0-9_\.\-]+)",
        r"ID\s*:?\s*([A-Za-z0-9_\.\-]{4,})",
        r"Ref(?:erence)?\s*:?\s*([A-Za-z0-9_\.\-]+)",
    ]
    for p in id_patterns:
        m = re.search(p, txt, re.I)
        if m and len(m.group(1)) > 3:
            data["tx_id"] = m.group(1)
            break

    # Numéro de téléphone
    num_patterns = [
        r"(\+226[\s\d]{8,12})",
        r"(226[\s\d]{8,12})",
        r"(\+221[\s\d]{9,10})",
        r"(\+229[\s\d]{8,10})",
        r"(?:vers\s+le|au\s+num[ée]ro|de)\s*(\+?[\d\s]{8,15})",
        r"(\d{8})",
    ]
    for p in num_patterns:
        m = re.search(p, txt, re.I)
        if m:
            data["number"] = (m.group(1) or m.group(0)).replace(" ", "")
            break

    # Date
    date_patterns = [
        r"(\d{4}[\/\-]\d{2}[\/\-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?)",
        r"(\d{2}[\/\-]\d{2}[\/\-]\d{4}\s+\d{2}:\d{2}(?::\d{2})?)",
        r"Date\s*:?\s*([\d\/\-\s:]+\d{2}:\d{2})",
    ]
    for p in date_patterns:
        m = re.search(p, txt, re.I)
        if m:
            data["date"] = (m.group(1) or m.group(0)).strip()
            break

    # Frais
    m = re.search(r"frais\s*=?\s*([\d\.]+)\s*f?cfa", txt, re.I)
    if m:
        data["fees"] = float(m.group(1))

    # Solde
    m = re.search(r"(?:nouveau\s+)?solde\s*(?:est\s+de)?\s*:?\s*([\d\s,\.]+)\s*f?cfa", txt, re.I)
    if m:
        data["balance"] = float(m.group(1).replace(" ", "").replace(",", "."))

    return data


def parse_date_str(s: str):
    """Convertit une chaîne de date en objet datetime."""
    if not s:
        return None
    formats = [
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def verify_payment(txt: str, operateur: str, pay_type: str,
                   expected_amount: float, max_delay_min: int, tolerance: float) -> dict:
    """Effectue toutes les vérifications de paiement."""
    parsed = parse_payment_text(txt)
    checks = []
    verdict = "ok"

    # 1. Vérif opérateur
    if operateur != "any":
        kw = OP_KEYWORDS[operateur]
        if re.search(kw, txt, re.I):
            checks.append({"status": "ok", "msg": f"Opérateur {OPERATEURS[operateur]} confirmé"})
        else:
            checks.append({"status": "ko", "msg": f"Opérateur {OPERATEURS[operateur]} non trouvé dans le texte"})
            verdict = "ko"

    # 2. Vérif ID transaction
    if not parsed["tx_id"]:
        checks.append({"status": "ko", "msg": "Aucun ID de transaction trouvé"})
        verdict = "ko"
    elif parsed["tx_id"] in used_ids:
        checks.append({"status": "ko", "msg": f"ID \"{parsed['tx_id']}\" déjà utilisé — fraude détectée !"})
        verdict = "ko"
    else:
        checks.append({"status": "ok", "msg": f"ID unique confirmé : {parsed['tx_id']}"})

    # 3. Vérif montant
    if parsed["amount"] is None:
        checks.append({"status": "ko", "msg": "Montant introuvable dans le texte"})
        verdict = "ko"
    elif expected_amount and expected_amount > 0:
        diff = abs(parsed["amount"] - expected_amount)
        if diff <= tolerance:
            checks.append({"status": "ok", "msg": f"Montant correct : {parsed['amount']:,.0f} FCFA"})
        else:
            checks.append({
                "status": "ko",
                "msg": f"Montant incorrect : {parsed['amount']:,.0f} FCFA reçu, {expected_amount:,.0f} FCFA attendu"
            })
            verdict = "ko"
    else:
        checks.append({
            "status": "warn",
            "msg": f"Montant détecté : {parsed['amount']:,.0f} FCFA (aucun montant attendu défini)"
            if parsed["amount"] else "Montant non trouvé"
        })
        if verdict == "ok":
            verdict = "warn"

    # 4. Vérif numéro
    if parsed["number"]:
        num_clean = re.sub(r"\D", "", parsed["number"])
        key = f"{operateur}_{pay_type}"
        pattern = NUM_PATTERNS.get(key, r"^\d{8,15}$")
        if re.match(pattern, num_clean) or re.match(pattern, parsed["number"]):
            checks.append({"status": "ok", "msg": f"Numéro valide : {parsed['number']}"})
        else:
            checks.append({"status": "warn", "msg": f"Numéro format inhabituel : {parsed['number']}"})
            if verdict == "ok":
                verdict = "warn"
    else:
        checks.append({"status": "warn", "msg": "Aucun numéro de téléphone détecté"})
        if verdict == "ok":
            verdict = "warn"

    # 5. Vérif heure
    if parsed["date"]:
        tx_dt = parse_date_str(parsed["date"])
        if tx_dt:
            diff_min = (datetime.now() - tx_dt).total_seconds() / 60
            if diff_min < 0:
                checks.append({"status": "ko", "msg": "Date dans le futur — SMS falsifié !"})
                verdict = "ko"
            elif diff_min > max_delay_min:
                h = int(diff_min // 60)
                mn = int(diff_min % 60)
                checks.append({
                    "status": "ko",
                    "msg": f"Paiement trop ancien : {h}h{mn}min (max autorisé : {max_delay_min}min). Contacter le support !"
                })
                verdict = "ko"
            else:
                checks.append({"status": "ok", "msg": f"Heure valide : paiement fait il y a {int(diff_min)}min"})
        else:
            checks.append({"status": "warn", "msg": f"Date détectée mais non interprétée : {parsed['date']}"})
            if verdict == "ok":
                verdict = "warn"
    else:
        checks.append({"status": "warn", "msg": "Aucune date/heure trouvée dans le texte"})
        if verdict == "ok":
            verdict = "warn"

    # Enregistrer l'ID si valide
    if verdict == "ok" and parsed["tx_id"]:
        used_ids.add(parsed["tx_id"])

    return {
        "verdict": verdict,
        "checks": checks,
        "parsed": parsed,
        "raw_text": txt
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/verify", methods=["POST"])
def verify():
    data = request.json
    txt = data.get("text", "").strip()

    # Si image fournie, extraire le texte via Claude Vision
    if data.get("image_b64"):
        media_type = data.get("media_type", "image/jpeg")
        try:
            txt = extract_text_from_image(data["image_b64"], media_type)
        except Exception as e:
            return jsonify({"error": f"Erreur extraction image : {str(e)}"}), 500

    if not txt:
        return jsonify({"error": "Aucun texte à analyser"}), 400

    result = verify_payment(
        txt=txt,
        operateur=data.get("operateur", "any"),
        pay_type=data.get("pay_type", "national"),
        expected_amount=float(data.get("expected_amount") or 0),
        max_delay_min=int(data.get("max_delay_min") or 60),
        tolerance=float(data.get("tolerance") or 5),
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
