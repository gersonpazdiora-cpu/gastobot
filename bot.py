import os
import re
import requests
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
SHEETS_WEBHOOK = os.environ.get("SHEETS_WEBHOOK", "")
SEU_ID         = int(os.environ.get("SEU_TELEGRAM_ID", "0"))

PALAVRAS_ENTRADA = {"recebi", "entrada", "recebimento", "honorário", "honorários", "receita"}

CATEGORIAS_AUTO = {
    "almoço": "Alimentação", "jantar": "Alimentação", "café": "Alimentação",
    "lanche": "Alimentação", "mercado": "Alimentação", "supermercado": "Alimentação",
    "uber": "Transporte", "gasolina": "Transporte", "combustível": "Transporte",
    "estacionamento": "Transporte", "ônibus": "Transporte",
    "google": "Marketing", "meta": "Marketing", "ads": "Marketing",
    "tráfego": "Marketing", "instagram": "Marketing",
    "contador": "Escritório", "cartório": "Escritório", "internet": "Escritório",
    "celular": "Escritório", "assinatura": "Escritório", "software": "Escritório",
    "hotel": "Viagem", "passagem": "Viagem", "airbnb": "Viagem",
    "honorário": "Receita", "honorários": "Receita", "cliente": "Receita",
    "processo": "Receita", "acordo": "Receita",
}

def detectar_tipo(texto: str) -> str:
    for p in texto.lower().split():
        if p in PALAVRAS_ENTRADA:
            return "Entrada"
    return "Saída"

def detectar_categoria(descricao: str, tipo: str) -> str:
    for palavra, categoria in CATEGORIAS_AUTO.items():
        if palavra in descricao.lower():
            return categoria
    return "Receita" if tipo == "Entrada" else "Outros"

def parse_lancamento(texto: str):
    texto = texto.strip()
    match = re.search(r'(\d+(?:[.,]\d{1,2})?)', texto)
    if not match:
        return None

    valor = float(match.group(1).replace(",", "."))
    descricao = texto[:match.start()].strip().rstrip("-").strip()
    resto = texto[match.end():].strip()
    tipo = detectar_tipo(texto)

    for kw in PALAVRAS_ENTRADA:
        descricao = re.sub(rf'\b{kw}\b', '', descricao, flags=re.IGNORECASE).strip()

    categoria = resto if resto else detectar_categoria(descricao, tipo)
    return descricao or "Lançamento", valor, tipo, categoria.capitalize()

def registrar_no_sheets(descricao, valor, tipo, categoria):
    agora = datetime.now()
    payload = {
        "data":      agora.strftime("%d/%m/%Y"),
        "hora":      agora.strftime("%H:%M"),
        "descricao": descricao,
        "valor":     valor,
        "tipo":      tipo,
        "categoria": categoria,
    }
    try:
        r = requests.post(SHEETS_WEBHOOK, json=payload, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def buscar_resumo():
    try:
        r = requests.get(SHEETS_WEBHOOK, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Olá! Registre gastos e receitas:\n\n"
        "*Gasto:* `almoço 45` ou `gasolina 150 transporte`\n"
        "*Entrada:* `recebi 5000 honorários`\n\n"
        "Comandos:\n"
        "  /resumo — saldo e resumo do mês\n"
        "  /ajuda — como usar\n"
        "  /categorias — lista de categorias",
        parse_mode="Markdown"
    )

async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Como registrar:*\n\n"
        "*Gasto:* `descrição valor` → `café 12`\n"
        "*Gasto c/ categoria:* `descrição valor categoria` → `uber 35 transporte`\n"
        "*Receita:* `recebi valor descrição` → `recebi 5000 honorários`\n\n"
        "Use /resumo pra ver saldo e resumo do mês.",
        parse_mode="Markdown"
    )

async def cmd_categorias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cats = sorted(set(CATEGORIAS_AUTO.values()))
    texto = "*Categorias:*\n" + "\n".join(f"  • {c}" for c in cats)
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_resumo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return

    dados = buscar_resumo()
    if not dados or "saldo" not in dados:
        await update.message.reply_text("Não consegui buscar o resumo. Tente novamente.")
        return

    saldo     = dados.get("saldo", 0)
    entradas  = dados.get("entradas_mes", 0)
    saidas    = dados.get("saidas_mes", 0)
    resultado = dados.get("resultado_mes", 0)
    top_cats  = dados.get("top_categorias", [])

    top_texto = ""
    if top_cats:
        top_texto = "\n\n*Maiores gastos:*\n"
        for cat, val in top_cats:
            top_texto += f"  • {cat}: R$ {val:,.2f}\n"

    emoji_res = "✅" if resultado >= 0 else "⚠️"
    texto = (
        f"💰 *Saldo atual: R$ {saldo:,.2f}*\n\n"
        f"*Este mês:*\n"
        f"  📈 Entradas: R$ {entradas:,.2f}\n"
        f"  📉 Saídas:   R$ {saidas:,.2f}\n"
        f"  {emoji_res} Resultado: R$ {resultado:,.2f}"
        f"{top_texto}"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")

async def handle_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        await update.message.reply_text("Acesso não autorizado.")
        return

    texto = update.message.text.strip()
    resultado = parse_lancamento(texto)

    if not resultado:
        await update.message.reply_text(
            "Não entendi. Exemplos:\n`almoço 45` ou `recebi 5000 honorários`",
            parse_mode="Markdown"
        )
        return

    descricao, valor, tipo, categoria = resultado
    ok = registrar_no_sheets(descricao, valor, tipo, categoria)

    if ok:
        emoji = "💰" if tipo == "Entrada" else "📌"
        cor   = "✅" if tipo == "Entrada" else "🔴"
        await update.message.reply_text(
            f"Anotado!\n\n"
            f"{emoji} *{descricao.title()}*\n"
            f"{cor} R$ {valor:.2f} — {tipo}\n"
            f"🏷 {categoria}\n"
            f"🗓 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Não consegui salvar na planilha.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("ajuda",      cmd_ajuda))
    app.add_handler(CommandHandler("categorias", cmd_categorias))
    app.add_handler(CommandHandler("resumo",     cmd_resumo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mensagem))
    print("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
