# api/index.py
import os
import mercadopago
from fastapi import FastAPI, HTTPException, Request
from vercel_kv import kv
from pydantic import BaseModel
from dotenv import load_dotenv

# Carrega .env em teste local
load_dotenv()

# SDK Mercado Pago
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
if not MP_ACCESS_TOKEN:
    # Em produção, configure variável no Vercel
    print("AVISO: MERCADOPAGO_ACCESS_TOKEN não definido")

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

class PaymentRequest(BaseModel):
    amount: float
    description: str
    payer_email: str = "test_user@test.com"

app = FastAPI()

@app.get("/api/hello")
def hello_world():
    return {"message": "API de Pagamentos PIX está funcionando!"}

@app.post("/api/create_payment")
def create_payment(req: PaymentRequest):
    try:
        notification_url = os.getenv("MP_NOTIFICATION_URL") or "https://<SEU_DOMINIO_VERCEL>/api/webhook/mercadopago"
        payment_data = {
            "transaction_amount": float(req.amount),
            "payment_method_id": "pix",
            "description": req.description,
            "payer": {"email": req.payer_email},
            "notification_url": notification_url
        }

        result = sdk.payment().create(payment_data)
        payment_response = result.get("response")

        if not payment_response or "id" not in payment_response:
            raise HTTPException(status_code=500, detail="Erro ao criar pagamento no Mercado Pago")

        payment_id = str(payment_response["id"])
        qr_code = payment_response["point_of_interaction"]["transaction_data"]["qr_code"]

        # Salva com status pending; vercel-kv aceita bytes/strings
        kv.set(payment_id, "pending")

        return {
            "payment_id": payment_id,
            "status": "pending",
            "qr_code_copy_paste": qr_code
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/payment_status/{payment_id}")
def get_payment_status(payment_id: str):
    try:
        status = kv.get(payment_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Pagamento não encontrado")
        # vercel-kv retorna bytes; convert para string
        if isinstance(status, (bytes, bytearray)):
            status = status.decode('utf-8')
        return {"payment_id": payment_id, "status": status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        data = await request.json()
        # O Mercado Pago envia várias notificações; processamos tipo "payment"
        if data.get("type") == "payment":
            payment_id = str(data.get("data", {}).get("id"))
            if not payment_id:
                return {"status":"id não fornecido"}

            payment_info = sdk.payment().get(payment_id)
            status = payment_info.get("response", {}).get("status")

            if status == "approved":
                kv.set(payment_id, "approved")
                print(f"Pagamento {payment_id} aprovado!")
            elif status in ["cancelled", "rejected"]:
                kv.set(payment_id, status)
                print(f"Pagamento {payment_id} {status}")

        return {"status":"ok"}
    except Exception as e:
        print(f"Erro webhook: {e}")
        return {"status":"error", "detail": str(e)}
