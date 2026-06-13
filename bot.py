import os
import re
import sqlite3
import datetime
import requests
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
SHEETS_WEBHOOK    = os.environ.get("SHEETS_WEBHOOK", "")
SEU_ID            = int(os.environ.get("SEU_TELEGRAM_ID", "0"))
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DB_PATH = "/data/gastobot.db" if os.path.isdir("/data") else "./gastobot.db"
BRT = datetime.timezone(datetime.timedelta(hours=-3))

# ─── BANCO DE DADOS ───────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS metas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT, valor_alvo REAL, valor_atual REAL DEFAULT 0,
            prazo TEXT, criado_em TEXT DEFAULT CURRENT_DATE
        );
        CREATE TABLE IF NOT EXISTS habitos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT, emoji TEXT DEFAULT '✅',
            categoria TEXT DEFAULT 'geral', ativo INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habito_id INTEGER, data TEXT, UNIQUE(habito_id, data)
        );
        CREATE TABLE IF NOT EXISTS livros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT, total_paginas INTEGER,
            paginas_por_dia INTEGER DEFAULT 20,
            pagina_atual INTEGER DEFAULT 0,
            ativo INTEGER DEFAULT 1, criado_em TEXT DEFAULT CURRENT_DATE
        );
        CREATE TABLE IF NOT EXISTS leituras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            livro_id INTEGER, data TEXT, paginas INTEGER
        );
        CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
    """)
    # Hábitos padrão
    c.execute("SELECT COUNT(*) FROM habitos")
    if c.fetchone()[0] == 0:
        habitos_padrao = [
            ("Acordar no horário", "⏰", "saude"),
            ("Exercício físico 30min", "💪", "saude"),
            ("Leitura do dia", "📚", "estudo"),
            ("Meditar 10min", "🧘", "saude"),
            ("Atividade com filho", "👦", "familia"),
            ("Registrar gastos", "💰", "financeiro"),
            ("Beber 2L de água", "💧", "saude"),
            ("Dormir no horário", "😴", "saude"),
        ]
        c.executemany("INSERT INTO habitos (nome, emoji, categoria) VALUES (?,?,?)", habitos_padrao)
    # Config padrão
    configs = [
        ("hora_acordar", "06:00"),
        ("hora_dormir", "22:00"),
        ("meta_mensal_renda", "15000"),
        ("livro_atual_id", "0"),
    ]
    for chave, valor in configs:
        c.execute("INSERT OR IGNORE INTO config VALUES (?,?)", (chave, valor))
    # Meta padrão de 1 milhão
    c.execute("SELECT COUNT(*) FROM metas")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO metas (nome, valor_alvo, prazo) VALUES (?,?,?)",
            ("1 Milhão em 10 anos", 1000000.0, "2036-06-11")
        )
    conn.commit()
    conn.close()

def cfg_get(chave: str, padrao: str = "") -> str:
    conn = get_db()
    row = conn.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    conn.close()
    return row["valor"] if row else padrao

def cfg_set(chave: str, valor: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (chave, valor))
    conn.commit()
    conn.close()

def barra_progresso(atual, total, tamanho=15) -> str:
    if total <= 0:
        return "░" * tamanho + " 0%"
    pct = min(atual / total, 1.0)
    cheio = int(pct * tamanho)
    return "█" * cheio + "░" * (tamanho - cheio) + f" {pct*100:.0f}%"

# ─── SHEETS (financeiro existente) ───────────────────────────────────────────

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
    agora = datetime.datetime.now()
    payload = {
        "data": agora.strftime("%d/%m/%Y"), "hora": agora.strftime("%H:%M"),
        "descricao": descricao, "valor": valor, "tipo": tipo, "categoria": categoria,
    }
    try:
        r = requests.post(SHEETS_WEBHOOK, json=payload, timeout=15, allow_redirects=False)
        # Google Apps Script retorna 302 quando executa com sucesso
        if r.status_code in (200, 302):
            return True, None
        return False, f"HTTP {r.status_code}: {r.text[:80]}"
    except Exception as e:
        return False, str(e)[:100]

def buscar_resumo():
    try:
        r = requests.get(SHEETS_WEBHOOK, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

# ─── AI COACH ────────────────────────────────────────────────────────────────

def perguntar_coach(pergunta: str, contexto: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return "Configure ANTHROPIC_API_KEY para usar o coach de IA."
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = (
            "Você é um coach de vida pessoal e financeiro. Seu usuário tem 30-40 anos, "
            "um filho de 12 anos, e quer acumular R$ 1 milhão em 10 anos. "
            "Ele acorda às " + cfg_get("hora_acordar", "06:00") + " e dorme às " + cfg_get("hora_dormir", "22:00") + ". "
            "Meta de renda mensal: R$ " + cfg_get("meta_mensal_renda", "15000") + ". "
            "Seja direto, motivador e prático. Máximo 300 palavras. "
            "Foque em ações concretas para hoje. Fale em português brasileiro."
        )
        prompt = (contexto + "\n\n" + pergunta).strip() if contexto else pergunta
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text
        return "Sem resposta."
    except Exception as e:
        return f"Erro ao consultar coach: {str(e)[:100]}"

def gerar_briefing_diario() -> str:
    if not ANTHROPIC_API_KEY:
        return None
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    conn = get_db()
    # hábitos de hoje
    habitos = conn.execute("SELECT nome, emoji FROM habitos WHERE ativo=1").fetchall()
    checkins_hoje = conn.execute(
        "SELECT habito_id FROM checkins WHERE data=?", (hoje,)
    ).fetchall()
    ids_feitos = {r["habito_id"] for r in checkins_hoje}
    habitos_pendentes = [f"{h['emoji']} {h['nome']}" for h in habitos
                         if h["id"] not in ids_feitos] if False else []
    # recarregar com id
    habitos_full = conn.execute("SELECT id, nome, emoji FROM habitos WHERE ativo=1").fetchall()
    habitos_pendentes = [f"{h['emoji']} {h['nome']}" for h in habitos_full
                         if h["id"] not in ids_feitos]
    # livro ativo
    livro = conn.execute("SELECT * FROM livros WHERE ativo=1 ORDER BY id DESC LIMIT 1").fetchone()
    # metas
    metas = conn.execute("SELECT nome, valor_alvo, valor_atual FROM metas").fetchall()
    conn.close()

    resumo_fin = buscar_resumo()
    saldo = resumo_fin.get("saldo", 0) if resumo_fin else 0
    meta_mes = float(cfg_get("meta_mensal_renda", "15000"))

    partes = [
        f"Hoje é {datetime.datetime.now(BRT).strftime('%d/%m/%Y')}.",
        f"Saldo atual: R$ {saldo:,.2f}. Meta mensal de renda: R$ {meta_mes:,.2f}.",
    ]
    if livro:
        restante = livro["total_paginas"] - livro["pagina_atual"]
        dias_restantes = max(1, restante // max(livro["paginas_por_dia"], 1))
        partes.append(f"Livro: '{livro['titulo']}' — página {livro['pagina_atual']}/{livro['total_paginas']} "
                      f"({livro['paginas_por_dia']} pág/dia, ~{dias_restantes} dias para terminar).")
    if habitos_pendentes:
        partes.append("Hábitos pendentes hoje: " + ", ".join(habitos_pendentes[:5]))
    if metas:
        m = metas[0]
        partes.append(f"Meta principal: {m['nome']} — R$ {m['valor_atual']:,.2f} / R$ {m['valor_alvo']:,.2f}")

    contexto = " ".join(partes)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=700,
            messages=[{"role": "user", "content": (
                f"{contexto}\n\n"
                "Crie um briefing motivador de manhã para o usuário. Inclua:\n"
                "1. Frase motivadora do dia\n"
                "2. 3 prioridades para hoje (financeiro, saúde, família)\n"
                "3. Dica prática para se aproximar do R$1 milhão\n"
                "4. Atividade sugerida com o filho hoje\n"
                "Seja conciso, use emojis, máximo 250 palavras. Português brasileiro."
            )}]
        )
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text
    except Exception:
        pass
    return None

def gerar_resumo_noite() -> str:
    if not ANTHROPIC_API_KEY:
        return None
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    conn = get_db()
    habitos_full = conn.execute("SELECT id, nome, emoji FROM habitos WHERE ativo=1").fetchall()
    checkins_hoje = conn.execute("SELECT habito_id FROM checkins WHERE data=?", (hoje,)).fetchall()
    ids_feitos = {r["habito_id"] for r in checkins_hoje}
    feitos = [f"{h['emoji']} {h['nome']}" for h in habitos_full if h["id"] in ids_feitos]
    pendentes = [f"{h['emoji']} {h['nome']}" for h in habitos_full if h["id"] not in ids_feitos]
    conn.close()

    total = len(habitos_full)
    pct = int(len(feitos) / total * 100) if total else 0
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                f"Hoje o usuário completou {len(feitos)}/{total} hábitos ({pct}%).\n"
                f"Feitos: {', '.join(feitos) if feitos else 'nenhum'}.\n"
                f"Perdidos: {', '.join(pendentes) if pendentes else 'nenhum'}.\n\n"
                "Faça uma reflexão noturna curta: elogie o que foi feito, "
                "encoraje para amanhã, lembre do objetivo do R$1 milhão. "
                "Máximo 150 palavras. Use emojis. Português brasileiro."
            )}]
        )
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text
    except Exception:
        pass
    return None

# ─── JOBS AGENDADOS ──────────────────────────────────────────────────────────

async def job_manha(context):
    briefing = gerar_briefing_diario()
    if not briefing:
        briefing = rotina_texto()
    if SEU_ID:
        await context.bot.send_message(chat_id=SEU_ID, text=f"☀️ *Bom dia!*\n\n{briefing}", parse_mode="Markdown")

async def job_noite(context):
    resumo = gerar_resumo_noite()
    if not resumo:
        hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM habitos WHERE ativo=1").fetchone()[0]
        feitos = conn.execute("SELECT COUNT(*) FROM checkins WHERE data=?", (hoje,)).fetchone()[0]
        conn.close()
        resumo = f"Hoje você completou {feitos}/{total} hábitos. Continue assim!"
    if SEU_ID:
        await context.bot.send_message(chat_id=SEU_ID, text=f"🌙 *Boa noite!*\n\n{resumo}", parse_mode="Markdown")

def rotina_texto() -> str:
    hora_acorda = cfg_get("hora_acordar", "06:00")
    hora_dorme = cfg_get("hora_dormir", "22:00")
    meta_renda = cfg_get("meta_mensal_renda", "15000")
    livro_id = cfg_get("livro_atual_id", "0")
    livro_info = ""
    if livro_id and livro_id != "0":
        conn = get_db()
        livro = conn.execute("SELECT titulo, paginas_por_dia FROM livros WHERE id=?", (livro_id,)).fetchone()
        conn.close()
        if livro:
            livro_info = f"\n📚 Leitura: {livro['paginas_por_dia']} páginas de *{livro['titulo']}*"

    return (
        f"*Sua Rotina Diária de Sucesso*\n\n"
        f"⏰ Acordar: {hora_acorda}\n"
        f"💪 06:30 — Exercício 30min\n"
        f"🧘 07:00 — Meditação 10min\n"
        f"☕ 07:10 — Café + planejamento do dia\n"
        f"💼 08:00 — Trabalho (foco em gerar R$ {float(meta_renda):,.0f}/mês)\n"
        f"🍽️ 12:00 — Almoço\n"
        f"📚 13:00 — Leitura 20min{livro_info}\n"
        f"💼 13:30 — Trabalho / prospecção de clientes\n"
        f"👦 17:00 — Atividade com seu filho\n"
        f"🍽️ 19:00 — Jantar em família\n"
        f"📊 20:00 — Revisão financeira do dia\n"
        f"📚 20:30 — Estudo / desenvolvimento\n"
        f"😴 Dormir: {hora_dorme}\n\n"
        f"💡 *Meta:* R$ 1.000.000 em 10 anos\n"
        f"   → Invista R$ {5000:,.0f}/mês para chegar lá"
    )

# ─── COMANDOS ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *GastoBot — Seu Sistema de Vida Completo*\n\n"
        "*Finanças:*\n"
        "  /resumo — saldo do mês\n"
        "  /meta — metas financeiras\n"
        "  /progresso — dashboard completo\n\n"
        "*Rotina & Saúde:*\n"
        "  /rotina — sua rotina diária\n"
        "  /checkin — marcar hábitos do dia\n"
        "  /horario — configurar acordar/dormir\n\n"
        "*Leitura:*\n"
        "  /livro — adicionar/ver livro atual\n"
        "  /leitura — registrar páginas lidas\n\n"
        "*Coach IA:*\n"
        "  /dia — briefing do dia com IA\n"
        "  /coach — perguntar ao seu coach\n"
        "  /agenda — atividades com seu filho\n\n"
        "*Lançamento:* `almoço 45` ou `recebi 5000 honorários`",
        parse_mode="Markdown"
    )

async def cmd_ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Como registrar gastos:*\n\n"
        "Gasto: `descrição valor` → `café 12`\n"
        "Receita: `recebi valor` → `recebi 5000 honorários`\n\n"
        "Use /start para ver todos os comandos.",
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
        await update.message.reply_text("Não consegui buscar o resumo.")
        return
    saldo = dados.get("saldo", 0)
    entradas = dados.get("entradas_mes", 0)
    saidas = dados.get("saidas_mes", 0)
    resultado = dados.get("resultado_mes", 0)
    top_cats = dados.get("top_categorias", [])
    meta_mes = float(cfg_get("meta_mensal_renda", "15000"))
    barra_renda = barra_progresso(entradas, meta_mes)

    top_texto = ""
    if top_cats:
        top_texto = "\n\n*Maiores gastos:*\n"
        for cat, val in top_cats:
            top_texto += f"  • {cat}: R$ {val:,.2f}\n"

    emoji_res = "✅" if resultado >= 0 else "⚠️"
    texto = (
        f"💰 *Saldo: R$ {saldo:,.2f}*\n\n"
        f"*Este mês:*\n"
        f"  📈 Entradas: R$ {entradas:,.2f}\n"
        f"  📊 Meta renda: `{barra_renda}`\n"
        f"  📉 Saídas:   R$ {saidas:,.2f}\n"
        f"  {emoji_res} Resultado: R$ {resultado:,.2f}"
        f"{top_texto}"
    )
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    args = ctx.args
    conn = get_db()
    if args and args[0] == "add" and len(args) >= 3:
        nome = " ".join(args[1:-1])
        try:
            valor = float(args[-1].replace(",", "."))
            conn.execute("INSERT INTO metas (nome, valor_alvo) VALUES (?,?)", (nome, valor))
            conn.commit()
            conn.close()
            await update.message.reply_text(f"✅ Meta criada: *{nome}* — R$ {valor:,.2f}", parse_mode="Markdown")
        except ValueError:
            conn.close()
            await update.message.reply_text("Uso: /meta add Nome da Meta 50000")
        return
    if args and args[0] == "deposito" and len(args) >= 2:
        try:
            valor = float(args[1].replace(",", "."))
            metas = conn.execute("SELECT * FROM metas ORDER BY id DESC").fetchall()
            if metas:
                m = metas[0]
                novo = m["valor_atual"] + valor
                conn.execute("UPDATE metas SET valor_atual=? WHERE id=?", (novo, m["id"]))
                conn.commit()
                conn.close()
                pct = novo / m["valor_alvo"] * 100
                await update.message.reply_text(
                    f"💰 Depósito de R$ {valor:,.2f} registrado!\n"
                    f"*{m['nome']}:* R$ {novo:,.2f} / R$ {m['valor_alvo']:,.2f} ({pct:.1f}%)",
                    parse_mode="Markdown"
                )
            else:
                conn.close()
                await update.message.reply_text("Nenhuma meta encontrada.")
        except ValueError:
            conn.close()
            await update.message.reply_text("Uso: /meta deposito 1000")
        return

    metas = conn.execute("SELECT * FROM metas ORDER BY id").fetchall()
    conn.close()
    if not metas:
        await update.message.reply_text(
            "Nenhuma meta. Use:\n/meta add Nome 50000\n/meta deposito 1000"
        )
        return
    texto = "🎯 *Suas Metas*\n\n"
    for m in metas:
        barra = barra_progresso(m["valor_atual"], m["valor_alvo"])
        texto += f"*{m['nome']}*\n`{barra}`\nR$ {m['valor_atual']:,.2f} / R$ {m['valor_alvo']:,.2f}\n\n"
    texto += "Comandos: /meta deposito 1000 | /meta add Nome Valor"
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_rotina(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    await update.message.reply_text(rotina_texto(), parse_mode="Markdown")

async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    conn = get_db()
    habitos = conn.execute("SELECT id, nome, emoji FROM habitos WHERE ativo=1").fetchall()
    checkins = conn.execute("SELECT habito_id FROM checkins WHERE data=?", (hoje,)).fetchall()
    conn.close()
    ids_feitos = {r["habito_id"] for r in checkins}

    keyboard = []
    for h in habitos:
        feito = h["id"] in ids_feitos
        label = f"{'✅' if feito else '⬜'} {h['emoji']} {h['nome']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"checkin_{h['id']}")])

    total = len(habitos)
    feitos = len(ids_feitos)
    barra = barra_progresso(feitos, total)
    await update.message.reply_text(
        f"📋 *Check-in do Dia — {datetime.datetime.now(BRT).strftime('%d/%m')}*\n"
        f"`{barra}` {feitos}/{total}\n\n"
        "Toque para marcar/desmarcar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def callback_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not query.data.startswith("checkin_"):
        return
    habito_id = int(query.data.split("_")[1])
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    conn = get_db()
    existe = conn.execute(
        "SELECT id FROM checkins WHERE habito_id=? AND data=?", (habito_id, hoje)
    ).fetchone()
    if existe:
        conn.execute("DELETE FROM checkins WHERE habito_id=? AND data=?", (habito_id, hoje))
    else:
        conn.execute("INSERT OR IGNORE INTO checkins (habito_id, data) VALUES (?,?)", (habito_id, hoje))
    conn.commit()
    habitos = conn.execute("SELECT id, nome, emoji FROM habitos WHERE ativo=1").fetchall()
    checkins = conn.execute("SELECT habito_id FROM checkins WHERE data=?", (hoje,)).fetchall()
    conn.close()
    ids_feitos = {r["habito_id"] for r in checkins}

    keyboard = []
    for h in habitos:
        feito = h["id"] in ids_feitos
        label = f"{'✅' if feito else '⬜'} {h['emoji']} {h['nome']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"checkin_{h['id']}")])

    total = len(habitos)
    feitos_count = len(ids_feitos)
    barra = barra_progresso(feitos_count, total)
    await query.edit_message_text(
        f"📋 *Check-in do Dia — {datetime.datetime.now(BRT).strftime('%d/%m')}*\n"
        f"`{barra}` {feitos_count}/{total}\n\n"
        "Toque para marcar/desmarcar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def cmd_livro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    args = ctx.args
    conn = get_db()
    if args and len(args) >= 2:
        try:
            paginas_totais = int(args[-1])
            titulo = " ".join(args[:-1])
            conn.execute("UPDATE livros SET ativo=0")
            conn.execute(
                "INSERT INTO livros (titulo, total_paginas, paginas_por_dia, ativo) VALUES (?,?,20,1)",
                (titulo, paginas_totais)
            )
            conn.commit()
            livro_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            cfg_set("livro_atual_id", str(livro_id))
            conn.close()
            dias = paginas_totais // 20
            await update.message.reply_text(
                f"📚 Livro adicionado!\n\n*{titulo}*\n"
                f"{paginas_totais} páginas — 20 pág/dia\n"
                f"Previsão: ~{dias} dias para terminar\n\n"
                f"Use /leitura 20 para registrar a leitura de hoje.",
                parse_mode="Markdown"
            )
        except (ValueError, IndexError):
            conn.close()
            await update.message.reply_text("Uso: /livro Título do Livro 300")
        return

    livro = conn.execute("SELECT * FROM livros WHERE ativo=1 ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not livro:
        await update.message.reply_text(
            "Nenhum livro ativo.\nUse: /livro Título do Livro 300\n\n"
            "💡 Sugestão: *Pai Rico Pai Pobre* (336 páginas)",
            parse_mode="Markdown"
        )
        return
    restante = livro["total_paginas"] - livro["pagina_atual"]
    dias_rest = max(1, restante // max(livro["paginas_por_dia"], 1))
    barra = barra_progresso(livro["pagina_atual"], livro["total_paginas"])
    await update.message.reply_text(
        f"📚 *{livro['titulo']}*\n\n"
        f"`{barra}`\n"
        f"Página {livro['pagina_atual']} / {livro['total_paginas']}\n"
        f"{livro['paginas_por_dia']} páginas/dia — ~{dias_rest} dias restantes\n\n"
        f"Use /leitura {livro['paginas_por_dia']} para registrar hoje.",
        parse_mode="Markdown"
    )

async def cmd_leitura(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Uso: /leitura 20 (páginas lidas hoje)")
        return
    try:
        paginas = int(args[0])
    except ValueError:
        await update.message.reply_text("Uso: /leitura 20")
        return
    conn = get_db()
    livro = conn.execute("SELECT * FROM livros WHERE ativo=1 ORDER BY id DESC LIMIT 1").fetchone()
    if not livro:
        conn.close()
        await update.message.reply_text("Nenhum livro ativo. Use /livro primeiro.")
        return
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO leituras (livro_id, data, paginas) VALUES (?,?,?)",
                 (livro["id"], hoje, paginas))
    nova_pagina = min(livro["pagina_atual"] + paginas, livro["total_paginas"])
    conn.execute("UPDATE livros SET pagina_atual=? WHERE id=?", (nova_pagina, livro["id"]))
    conn.commit()
    conn.close()
    barra = barra_progresso(nova_pagina, livro["total_paginas"])
    concluido = nova_pagina >= livro["total_paginas"]
    msg = (f"📖 +{paginas} páginas lidas!\n\n"
           f"*{livro['titulo']}*\n`{barra}`\n"
           f"Página {nova_pagina}/{livro['total_paginas']}")
    if concluido:
        msg += "\n\n🎉 *Livro concluído! Parabéns!*"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_horario(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    args = ctx.args
    if len(args) >= 2:
        chave = args[0].lower()
        valor = args[1]
        if chave in ("acordar", "acordar:"):
            cfg_set("hora_acordar", valor)
            await update.message.reply_text(f"⏰ Horário de acordar: {valor}")
        elif chave in ("dormir", "dormir:"):
            cfg_set("hora_dormir", valor)
            await update.message.reply_text(f"😴 Horário de dormir: {valor}")
        elif chave in ("renda", "meta"):
            cfg_set("meta_mensal_renda", valor.replace("R$", "").replace(".", "").replace(",", ""))
            await update.message.reply_text(f"💰 Meta mensal: R$ {valor}")
        else:
            await update.message.reply_text("Use: /horario acordar 06:00 | /horario dormir 22:00 | /horario renda 15000")
        return
    hora_acorda = cfg_get("hora_acordar", "06:00")
    hora_dorme = cfg_get("hora_dormir", "22:00")
    meta_renda = cfg_get("meta_mensal_renda", "15000")
    await update.message.reply_text(
        f"⚙️ *Configurações*\n\n"
        f"⏰ Acordar: {hora_acorda}\n"
        f"😴 Dormir: {hora_dorme}\n"
        f"💰 Meta mensal: R$ {float(meta_renda):,.0f}\n\n"
        f"Para alterar:\n"
        f"/horario acordar 06:00\n"
        f"/horario dormir 22:00\n"
        f"/horario renda 15000",
        parse_mode="Markdown"
    )

async def cmd_coach(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Pergunte ao seu coach:\n`/coach Como aumentar minha renda este mês?`",
            parse_mode="Markdown"
        )
        return
    pergunta = " ".join(ctx.args)
    await update.message.reply_text("🤔 Consultando coach...")
    resposta = perguntar_coach(pergunta)
    await update.message.reply_text(f"🎯 *Coach:*\n\n{resposta}", parse_mode="Markdown")

async def cmd_dia(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    await update.message.reply_text("☀️ Gerando seu briefing do dia...")
    try:
        briefing = gerar_briefing_diario()
    except Exception as e:
        briefing = None
    if not briefing:
        briefing = rotina_texto()
    await update.message.reply_text(f"☀️ *Seu Dia*\n\n{briefing}", parse_mode="Markdown")

async def cmd_agenda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    dia_semana = datetime.datetime.now(BRT).strftime("%A")
    dias_pt = {
        "Monday": "Segunda", "Tuesday": "Terça", "Wednesday": "Quarta",
        "Thursday": "Quinta", "Friday": "Sexta", "Saturday": "Sábado", "Sunday": "Domingo"
    }
    dia_pt = dias_pt.get(dia_semana, dia_semana)
    atividades = {
        "Segunda":  "⚽ Futebol no parque — 30min de jogo juntos",
        "Terça":    "🎮 Hora do jogo — 1h de video game com ele",
        "Quarta":   "📚 Estudar junto — ajude nas tarefas da escola",
        "Quinta":   "🚴 Ciclismo — pedalada no bairro",
        "Sexta":    "🎬 Pipoca e filme — escolha um filme juntos",
        "Sábado":   "🌳 Aventura ao ar livre — trilha, parque, ou passeio",
        "Domingo":  "👨‍🍳 Cozinhar juntos — preparem o almoço em família",
    }
    atividade = atividades.get(dia_pt, "🎯 Atividade livre com seu filho")
    if ANTHROPIC_API_KEY:
        await update.message.reply_text("👦 Gerando sugestão personalizada...")
        sugestao = perguntar_coach(
            f"Sugira uma atividade para hoje ({dia_pt}) com meu filho de 12 anos. "
            "Deve ser educativa, divertida e fortalecer nosso vínculo. "
            "Máximo 100 palavras com instruções práticas."
        )
        texto = f"👦 *Atividade com seu filho — {dia_pt}*\n\n{sugestao}"
    else:
        texto = (
            f"👦 *Atividade com seu filho — {dia_pt}*\n\n"
            f"{atividade}\n\n"
            f"💡 Dica: 1h de qualidade por dia = 365h/ano de memórias juntos.\n"
            f"Essas memórias valem mais que qualquer dinheiro."
        )
    await update.message.reply_text(texto, parse_mode="Markdown")

async def cmd_progresso(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if SEU_ID and update.effective_user.id != SEU_ID:
        return
    hoje = datetime.datetime.now(BRT).strftime("%Y-%m-%d")
    mes_atual = datetime.datetime.now(BRT).strftime("%Y-%m")
    conn = get_db()

    # hábitos do dia
    total_hab = conn.execute("SELECT COUNT(*) FROM habitos WHERE ativo=1").fetchone()[0]
    feitos_hoje = conn.execute("SELECT COUNT(*) FROM checkins WHERE data=?", (hoje,)).fetchone()[0]

    # streak de hábitos (dias consecutivos com pelo menos 1 check-in)
    streak = 0
    check_date = datetime.datetime.now(BRT).date()
    for _ in range(365):
        dt_str = check_date.strftime("%Y-%m-%d")
        cnt = conn.execute("SELECT COUNT(*) FROM checkins WHERE data=?", (dt_str,)).fetchone()[0]
        if cnt > 0:
            streak += 1
            check_date -= datetime.timedelta(days=1)
        else:
            break

    # livro
    livro = conn.execute("SELECT * FROM livros WHERE ativo=1 ORDER BY id DESC LIMIT 1").fetchone()

    # metas
    metas = conn.execute("SELECT * FROM metas ORDER BY id").fetchall()

    # leituras do mês
    leituras_mes = conn.execute(
        "SELECT SUM(paginas) FROM leituras WHERE data LIKE ?", (mes_atual + "%",)
    ).fetchone()[0] or 0

    conn.close()

    dados_fin = buscar_resumo()
    saldo = dados_fin.get("saldo", 0) if dados_fin else 0
    entradas = dados_fin.get("entradas_mes", 0) if dados_fin else 0
    meta_renda = float(cfg_get("meta_mensal_renda", "15000"))

    texto = f"📊 *Dashboard — {datetime.datetime.now(BRT).strftime('%d/%m/%Y')}*\n\n"

    texto += f"💰 *Financeiro*\n"
    texto += f"Saldo: R$ {saldo:,.2f}\n"
    texto += f"Renda do mês: `{barra_progresso(entradas, meta_renda)}`\n"
    texto += f"R$ {entradas:,.2f} / R$ {meta_renda:,.0f}\n\n"

    if metas:
        texto += "🎯 *Metas*\n"
        for m in metas:
            barra = barra_progresso(m["valor_atual"], m["valor_alvo"])
            texto += f"{m['nome']}\n`{barra}`\n"
        texto += "\n"

    texto += f"💪 *Hábitos Hoje*\n"
    texto += f"`{barra_progresso(feitos_hoje, total_hab)}` {feitos_hoje}/{total_hab}\n"
    texto += f"🔥 Sequência: {streak} dia{'s' if streak != 1 else ''}\n\n"

    if livro:
        texto += f"📚 *Leitura*\n"
        barra_lv = barra_progresso(livro["pagina_atual"], livro["total_paginas"])
        texto += f"{livro['titulo']}\n`{barra_lv}`\n"
        texto += f"Pág. {livro['pagina_atual']}/{livro['total_paginas']} | {leituras_mes} pág este mês\n\n"

    # meta 1 milhão em 10 anos
    anos_restantes = 10
    hoje_date = datetime.datetime.now(BRT).date()
    investimento_mensal_necessario = 5000
    texto += (
        f"🏆 *Caminho para R$ 1 Milhão*\n"
        f"Invista R$ {investimento_mensal_necessario:,.0f}/mês\n"
        f"Com rendimento de 10%/ano = R$ 1M em {anos_restantes} anos\n"
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
            "Não entendi. Exemplos:\n`almoço 45` ou `recebi 5000 honorários`\n\nUse /start para ver todos os comandos.",
            parse_mode="Markdown"
        )
        return
    descricao, valor, tipo, categoria = resultado
    ok, erro = registrar_no_sheets(descricao, valor, tipo, categoria)
    if ok:
        emoji = "💰" if tipo == "Entrada" else "📌"
        cor = "✅" if tipo == "Entrada" else "🔴"
        await update.message.reply_text(
            f"Anotado!\n\n{emoji} *{descricao.title()}*\n"
            f"{cor} R$ {valor:.2f} — {tipo}\n"
            f"🏷 {categoria}\n"
            f"🗓 {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Não consegui salvar.\nErro: {erro}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("ajuda",      cmd_ajuda))
    app.add_handler(CommandHandler("categorias", cmd_categorias))
    app.add_handler(CommandHandler("resumo",     cmd_resumo))
    app.add_handler(CommandHandler("meta",       cmd_meta))
    app.add_handler(CommandHandler("rotina",     cmd_rotina))
    app.add_handler(CommandHandler("checkin",    cmd_checkin))
    app.add_handler(CommandHandler("livro",      cmd_livro))
    app.add_handler(CommandHandler("leitura",    cmd_leitura))
    app.add_handler(CommandHandler("horario",    cmd_horario))
    app.add_handler(CommandHandler("coach",      cmd_coach))
    app.add_handler(CommandHandler("dia",        cmd_dia))
    app.add_handler(CommandHandler("agenda",     cmd_agenda))
    app.add_handler(CommandHandler("progresso",  cmd_progresso))
    app.add_handler(CallbackQueryHandler(callback_checkin, pattern="^checkin_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_mensagem))

    # Mensagens agendadas
    if SEU_ID and app.job_queue:
        app.job_queue.run_daily(
            job_manha,
            time=datetime.time(6, 30, tzinfo=BRT),
            name="briefing_manha"
        )
        app.job_queue.run_daily(
            job_noite,
            time=datetime.time(21, 0, tzinfo=BRT),
            name="resumo_noite"
        )

    print("GastoBot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
