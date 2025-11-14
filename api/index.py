# api/index.py
import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# Tenta importar mercadopago; se faltar, a rota devolverá erro controlado
try:
    import mercadopago
except Exception as e:
    mercadopago = None
    mercadopago_import_error = str(e)
else:
    mercadopago_import_error = None

# Tenta importar vercel_kv; se não estiver configurado ou instalado, usa fallback in-memory
try:
    from vercel_kv import kv
    kv_available = True
except Exception:
    kv_available = False
    # fallback simples em memória (apenas para dev; reinício perde dados)
    class KVFallback(dict):
        def get(self, k, default=None):
            return super().get(k, default)
        def set(self, k, v):
            super().__setitem__(k, v)
    kv = KVFallback()

# Carrega .env localmente se presente (não obrigatório)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Lê token do ambiente
MP_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN")
MP_NOTIFICATION_URL = os.getenv("MP_NOTIFICATION_URL")

app = FastAPI(title="PIX Proxy API")

# Modelo de requisição
class PaymentRequest(BaseModel):
    amount: float
    description: str
    payer_email: str = "test_user@test.com"

@app.get("/api/hello")
def hello_world():
    return {"message": "API de Pagamentos PIX está funcionando!"}

@app.post("/api/create_payment")
async def create_payment(req: PaymentRequest):
    # Valida pré-condições com mensagens claras (não crash)
    if mercadopago is None:
        raise HTTPException(status_code=500, detail=f"SDK mercadopago não disponível: {mercadopago_import_error}")

    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MERCADOPAGO_ACCESS_TOKEN não configurado no ambiente")

    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

        notification_url = MP_NOTIFICATION_URL or f"https://{os.getenv('VERCEL_URL','<seu-dominio>')}/api/webhook/mercadopago"

        payment_data = {
            "transaction_amount": float(req.amount),
            "payment_method_id": "pix",
            "description": req.description,
            "payer": {"email": req.payer_email},
            "notification_url": notification_url
        }

        result = sdk.payment().create(payment_data)
        payment_response = result.get("response", {})

        payment_id = str(payment_response.get("id", ""))
        txi = payment_response.get("point_of_interaction", {}).get("transaction_data", {})
        qr_code = txi.get("qr_code")

        if not payment_id or not qr_code:
            # devolve erro legível com o corpo bruto do MP (se houver)
            raise HTTPException(status_code=500, detail={"mp_result": result})

        # salva status inicial
        try:
            kv.set(payment_id, "pending")
        except Exception:
            # fallback: se kv não suportar set, tente atribuir método .set (fizemos fallback acima)
            try:
                kv.set(payment_id, "pending")
            except Exception:
                pass

        return {"payment_id": payment_id, "status": "pending", "qr_code_copy_paste": qr_code}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/payment_status/{payment_id}")
def get_payment_status(payment_id: str):
    try:
        status = kv.get(payment_id)
        # se for bytes (alguns bindings retornam bytes), decode
        if isinstance(status, (bytes, bytearray)):
            status = status.decode("utf-8")
        if status is None:
            raise HTTPException(status_code=404, detail="Pagamento não encontrado")
        return {"payment_id": payment_id, "status": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    # recebe notificação do Mercado Pago e atualiza kv
    try:
        data = await request.json()
    except Exception as e:
        return {"status": "invalid_json", "detail": str(e)}

    # exemplo de payload: {"type":"payment","data":{"id":12345}}
    if data.get("type") == "payment":
        payment_id = str(data.get("data", {}).get("id", ""))
        if not payment_id:
            return {"status": "no_id"}

        # tenta obter status diretamente no MP (somente se SDK disponível)
        if mercadopago is not None and MP_ACCESS_TOKEN:
            try:
                sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
                mp_resp = sdk.payment().get(payment_id)
                status = mp_resp.get("response", {}).get("status")
            except Exception:
                status = "unknown"
        else:
            status = "unknown"

        try:
            kv.set(payment_id, status)
        except Exception:
            # fallback simples: se falhar, ignore — polling ainda funcionará ao consultar MP diretamente
            pass

        return {"status": "ok", "payment_id": payment_id, "new_status": status}

    return {"status": "ignored", "detail": "unsupported type"}
