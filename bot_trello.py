#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_trello.py
Versão completa final (multiusuário) — integra Trello <-> Telegram:
- Multi-usuário (credenciais salvas em usuarios.json)
- /start, /config
- /buscar, /agenda
- checklist: /verchk, /addchk (modo guiado) com separador "--", /delchk, /movchk, /verchk paginado
- marcar/desmarcar: /marcar, /desmarcar (modo guiado)
- edição de cartão (modo guiado): /nvnome, /nvdesc, /nvdata (dd/mm/aaaa)
- membros: /addmembro, /rmmembro
- comentários: /coment (modo guiado)
- anexos: /anexo (entra em modo aguardando arquivos), enviar arquivos e depois /fim para subir
- /veranexos para listar anexos com links
- mensagens em Português
- polling (local)
- NOVO: Sistema de criação de cartões a partir de PDFs com prévia e edição
- NOVO: Sistema de checklist com separador "--"
- NOVO: Sistema de busca com interface de edição
- NOVO: Cancelamento global com /cancelar
- NOVO: Adição de anexos durante criação/edição de pedidos
Notes:
- Substitua TELEGRAM_TOKEN por seu token real (apenas nessa linha).
- Instalar dependências:
    pip install python-telegram-bot==20.5 requests pdfplumber
"""

import os
import json
import logging
import unicodedata
import shutil
from typing import Dict, Any, List, Optional
from datetime import datetime
import requests
import pdfplumber
import re

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    File as TgFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# -------------------- CONFIG --------------------

# Debug: Ver todas as variáveis de ambiente
print("=== DEBUG VARIÁVEIS DE AMBIENTE ===")
for key, value in os.environ.items():
    if 'TELEGRAM' in key.upper() or 'TOKEN' in key.upper():
        print(f"{key}: {value}")

print("=== TODAS AS VARIÁVEIS ===")
print(dict(os.environ))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")
print(f"TELEGRAM_TOKEN value: {TELEGRAM_TOKEN}")

if not TELEGRAM_TOKEN:
    # Tentar outros nomes comuns
    TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN") or os.environ.get("TG_TOKEN")
    print(f"Tentando nomes alternativos: {TELEGRAM_TOKEN}")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN não encontrado!")


USERS_FILE = "usuarios.json"
API_BASE = "https://api.trello.com/1"
DOWNLOAD_DIR = "downloads"
RASCUNHOS_DIR = "rascunhos_cartoes"
MAX_MSG_CHARS = 3800
ITEMS_PER_PAGE = 15

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(RASCUNHOS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Log para verificar se o bot iniciou
logger.info("🤖 Bot iniciando...")
logger.info(f"📁 Diretório atual: {os.getcwd()}")
logger.info(f"📁 Conteúdo do diretório: {os.listdir('.')}")

# in-memory per-user state
user_states: Dict[int, Dict[str, Any]] = {}


# -------------------- Helpers --------------------


def load_users() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception("Erro lendo usuarios.json: %s", e)
        return {}


def save_users(data: Dict[str, Dict[str, str]]):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Erro salvando usuarios.json: %s", e)


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s_nfkd = unicodedata.normalize("NFKD", s)
    only_ascii = "".join([c for c in s_nfkd if not unicodedata.combining(c)])
    return only_ascii.lower().strip()


def user_data_or_raise(user_id: int) -> Dict[str, str]:
    users = load_users()
    u = users.get(str(user_id))
    if not u:
        raise ValueError("Credenciais não encontradas. Use /start para configurar.")
    return u


def trello_request_for_user(user_id: int, method: str, path: str, params=None, json_payload=None, files=None,
                            timeout=30):
    u = user_data_or_raise(user_id)
    if params is None:
        params = {}
    params.update({"key": u["api_key"], "token": u["token"]})
    url = API_BASE + path
    resp = requests.request(method, url, params=params, json=json_payload, files=files, timeout=timeout)
    if not resp.ok:
        logger.error("Trello API error %s %s -> %s", method, url, resp.status_code)
        logger.error(resp.text)
    resp.raise_for_status()
    # for delete it might be empty body -> return text/json safely
    try:
        return resp.json()
    except Exception:
        return resp.text


# convenience wrappers
def get_board_lists(user_id: int, board_id: str):
    return trello_request_for_user(user_id, "GET", f"/boards/{board_id}/lists")


def get_board_cards(user_id: int, board_id: str):
    return trello_request_for_user(user_id, "GET", f"/boards/{board_id}/cards")


def get_card_by_id(user_id: int, card_id: str):
    return trello_request_for_user(user_id, "GET", f"/cards/{card_id}")


def get_card_checklists(user_id: int, card_id: str):
    return trello_request_for_user(user_id, "GET", f"/cards/{card_id}/checklists")


def get_card_comments(user_id: int, card_id: str):
    return trello_request_for_user(user_id, "GET", f"/cards/{card_id}/actions", params={"filter": "commentCard"})


def get_card_attachments(user_id: int, card_id: str):
    return trello_request_for_user(user_id, "GET", f"/cards/{card_id}/attachments")


def create_checklist(user_id: int, card_id: str, name: str):
    return trello_request_for_user(user_id, "POST", f"/cards/{card_id}/checklists", params={"name": name})


def add_checkitem(user_id: int, checklist_id: str, name: str):
    return trello_request_for_user(user_id, "POST", f"/checklists/{checklist_id}/checkItems", params={"name": name})


def delete_checklist(user_id: int, checklist_id: str):
    return trello_request_for_user(user_id, "DELETE", f"/checklists/{checklist_id}")


def mark_checkitem(user_id: int, card_id: str, id_checkitem: str, state: str):
    return trello_request_for_user(user_id, "PUT", f"/cards/{card_id}/checkItem/{id_checkitem}",
                                   params={"state": state})


def delete_checkitem(user_id: int, card_id: str, id_checkitem: str):
    return trello_request_for_user(user_id, "DELETE", f"/cards/{card_id}/checkItem/{id_checkitem}")


def move_card(user_id: int, card_id: str, list_name: str):
    card = get_card_by_id(user_id, card_id)
    board_id = card.get("idBoard")
    lists = get_board_lists(user_id, board_id)
    for l in lists:
        if normalize_text(l.get("name")) == normalize_text(list_name):
            return trello_request_for_user(user_id, "PUT", f"/cards/{card_id}", params={"idList": l.get("id")})
    return None


def add_comment(user_id: int, card_id: str, text: str):
    return trello_request_for_user(user_id, "POST", f"/cards/{card_id}/actions/comments", params={"text": text})


def update_card_field(user_id: int, card_id: str, fields: Dict[str, Any]):
    return trello_request_for_user(user_id, "PUT", f"/cards/{card_id}", params=fields)


def upload_file_to_card(user_id: int, card_id: str, local_path: str, filename: Optional[str] = None):
    u = user_data_or_raise(user_id)
    url = API_BASE + f"/cards/{card_id}/attachments"
    params = {"key": u["api_key"], "token": u["token"]}
    with open(local_path, "rb") as f:
        files = {"file": (filename or os.path.basename(local_path), f)}
        resp = requests.post(url, params=params, files=files, timeout=120)
    if not resp.ok:
        logger.error("Erro upload arquivo: %s", resp.text)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return resp.text


# -------------------- Sistema de Rascunhos para PDFs --------------------

def salvar_rascunho(user_id: int, dados_cartao: Dict[str, Any]):
    """Salva um rascunho de cartão em arquivo temporário"""
    user_dir = os.path.join(RASCUNHOS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)

    arquivo_rascunho = os.path.join(user_dir, f"rascunho_{len(os.listdir(user_dir))}.json")

    with open(arquivo_rascunho, 'w', encoding='utf-8') as f:
        json.dump(dados_cartao, f, ensure_ascii=False, indent=2)

    return arquivo_rascunho


def carregar_rascunhos(user_id: int) -> List[Dict[str, Any]]:
    """Carrega todos os rascunhos de um usuário"""
    user_dir = os.path.join(RASCUNHOS_DIR, str(user_id))
    if not os.path.exists(user_dir):
        return []

    rascunhos = []
    for arquivo in os.listdir(user_dir):
        if arquivo.startswith('rascunho_') and arquivo.endswith('.json'):
            try:
                with open(os.path.join(user_dir, arquivo), 'r', encoding='utf-8') as f:
                    rascunhos.append(json.load(f))
            except Exception as e:
                logger.warning(f"Erro ao carregar rascunho {arquivo}: {e}")

    return rascunhos


def atualizar_rascunho(user_id: int, index: int, dados_atualizados: Dict[str, Any]):
    """Atualiza um rascunho específico"""
    user_dir = os.path.join(RASCUNHOS_DIR, str(user_id))
    if not os.path.exists(user_dir):
        return False

    arquivos = [f for f in os.listdir(user_dir) if f.startswith('rascunho_') and f.endswith('.json')]
    arquivos.sort()

    if 0 <= index < len(arquivos):
        arquivo_rascunho = os.path.join(user_dir, arquivos[index])
        with open(arquivo_rascunho, 'w', encoding='utf-8') as f:
            json.dump(dados_atualizados, f, ensure_ascii=False, indent=2)
        return True

    return False


def limpar_rascunhos(user_id: int):
    """Limpa todos os rascunhos de um usuário"""
    user_dir = os.path.join(RASCUNHOS_DIR, str(user_id))
    if os.path.exists(user_dir):
        shutil.rmtree(user_dir)


# -------------------- Extração de PDF --------------------

def extract_info_from_pdf(pdf_path):
    """Extrai informações do PDF no formato específico para o Trello"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            text = page.extract_text()

            order_number_match = re.search(r'Nº\.?:\s*(\d{5})', text)
            client_name_match = re.search(r'Cliente:\s*([^\n\r]+?)(?:\s*-\s*\d{5}|\n|\r)', text)
            retirada_date_match = re.search(r'Retirada:\s*(\d{2}/\d{2}/\d{4})', text)

            extracted_data = {
                'order_number': order_number_match.group(1).strip() if order_number_match else 'N/A',
                'client_name': client_name_match.group(1).strip() if client_name_match else 'N/A',
                'products': [],
                'observations': 'N/A',
                'retirada_date': retirada_date_match.group(1).strip() if retirada_date_match else 'N/A'
            }

            # Extract observations
            obs_start_index = -1
            obs_end_index = -1

            obs_label_match = re.search(r'Observações:\s*', text)
            if obs_label_match:
                obs_start_index = obs_label_match.end()

            product_table_start_match = re.search(r'Código:\s*Referência:\s*Descrição:', text)
            if product_table_start_match:
                obs_end_index = product_table_start_match.start()
            else:
                obs_end_index = len(text)

            if obs_start_index != -1 and obs_end_index != -1:
                obs_text_raw = text[obs_start_index:obs_end_index].strip()
                cleaned_obs_lines = []
                for line in obs_text_raw.split('\n'):
                    line = line.strip()
                    line = re.sub(r'Retirada:\s*\d{2}/\d{2}/\d{4}', '', line).strip()
                    if line:
                        cleaned_obs_lines.append(line)

                if cleaned_obs_lines:
                    extracted_data['observations'] = '\n'.join(cleaned_obs_lines)

            # Extract products and quantities
            products_section_match = re.search(r'Descrição:\s*Quantidade:.*?Preço Total:\s*([\s\S]*?)Total Volumes:',
                                               text)
            if products_section_match:
                products_raw_text = products_section_match.group(1)
                for line in products_raw_text.split('\n'):
                    line = line.strip()
                    if line:
                        product_match = re.search(r'(?:\d+\s+)?(?:\d+\s+)?([^\d].*?)\s+(\d+\s*UND)', line)
                        if product_match:
                            description = product_match.group(1).strip()
                            quantity = product_match.group(2).strip()
                            extracted_data['products'].append(f"{description} - {quantity}")

            # Formata EXATAMENTE como no exemplo
            titulo = f"{extracted_data['order_number']} | {extracted_data['client_name']}"

            # Descrição no formato específico
            descricao = ""
            if extracted_data["products"]:
                descricao += "\n".join(extracted_data["products"]) + "\n\n"
            if extracted_data["observations"] and extracted_data["observations"] != "N/A":
                descricao += extracted_data["observations"] + "\n\n"

            # Data formatada
            data_formatada = f"📅 Data entrega: {extracted_data['retirada_date']}"

            return {
                "titulo": titulo.strip(),
                "descricao": descricao.strip(),
                "data_entrega": extracted_data["retirada_date"],
                "data_formatada": data_formatada,
                "produtos": extracted_data["products"],
                "observacoes": extracted_data["observations"],
                "checklists": [],
                "comentarios": "",
                "membros": [],
                "membros_ids": [],
                "arquivo_pdf_original": pdf_path,
                "editado": False,
                "anexos": []  # Nova lista para armazenar anexos
            }
    except Exception as e:
        logger.exception(f"Erro ao extrair PDF: {e}")
        return None


# -------------------- Presentation utils --------------------


def chunk_text(s: str, max_len: int = MAX_MSG_CHARS) -> List[str]:
    if len(s) <= max_len:
        return [s]
    parts = []
    lines = s.splitlines(True)
    cur = ""
    for ln in lines:
        if len(cur) + len(ln) > max_len:
            parts.append(cur)
            cur = ln
        else:
            cur += ln
    if cur:
        parts.append(cur)
    return parts


def parse_date_ddmmaa(s: str) -> Optional[str]:
    """Converte data dd/mm/aaaa para formato ISO do Trello com horário 16:00"""
    try:
        s2 = s.replace("-", "/").strip()
        d = datetime.strptime(s2, "%d/%m/%Y")
        # Formato ISO com horário 16:00 (4 PM) e timezone UTC
        return d.strftime("%Y-%m-%dT16:00:00.000Z")
    except Exception:
        return None


# -------------------- Telegram Handlers --------------------

HELP_TEXT = (
    "Comandos:\n"
    "/start - configurar credenciais (API Key, Token, Board ID)\n"
    "/config - reconfigurar credenciais\n"
    "/pedido - criar cartões a partir de PDFs (com prévia)\n"
    "/buscar <texto> - busca cartões pelo NOME\n"    
    "/addchk - inicia modo guiado para criar/adicionar checklist com itens separados por --\n"    
    "/cancelar - cancela qualquer operação em andamento\n"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users = load_users()
    if str(user_id) in users:
        await update.message.reply_text(
            "Você já tem credenciais salvas. Use /config para reconfigurar ou use os comandos.\n" + HELP_TEXT)
        return
    user_states[user_id] = {"flow": "register_api_key", "buffer": {}}
    await update.message.reply_text("Olá! Vamos configurar seu Trello. Envie sua *Trello API Key* (cole aqui):",
                                    parse_mode="Markdown")


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_states[user_id] = {"flow": "register_api_key", "buffer": {}}
    await update.message.reply_text("Reconfiguração iniciada. Envie sua Trello API Key:")


# -------------------- NOVO: Sistema de Checklist com Separador -- --------------------

async def addchk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia o modo de criação de checklist com separador --"""
    user_id = update.effective_user.id

    try:
        users = load_users()
        if not users.get(str(user_id)):
            await update.message.reply_text("Configure suas credenciais primeiro com /start.")
            return

        # Verifica se há um cartão selecionado
        state = user_states.get(user_id, {})
        if not state.get("selected_card"):
            await update.message.reply_text(
                "❌ Nenhum cartão selecionado.\n\n"
                "Primeiro use /buscar para encontrar e selecionar um cartão, "
                "ou use /agenda para abrir um cartão da lista '📕 AGENDA'."
            )
            return

        # Inicia o modo de criação de checklist
        state["mode"] = "add_checklist"
        state["checklist_stage"] = "aguardando_nome"
        user_states[user_id] = state

        await update.message.reply_text(
            "📝 **Modo de Criação de Checklist**\n\n"
            "Por favor, envie o *nome da checklist*:",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.exception(f"Erro no comando /addchk: {e}")
        await update.message.reply_text(f"❌ Erro ao iniciar modo de checklist: {str(e)}")


async def add_checklist_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE, index_cartao: int):
    """Inicia modo de adição de checklist para um cartão específico"""
    user_id = update.effective_user.id if update.message else update.callback_query.from_user.id

    state = user_states.get(user_id, {})
    state.update({
        "mode": "add_checklist_pdf",  # Modo específico para PDFs
        "checklist_stage": "aguardando_nome",
        "index_cartao": index_cartao,
        "buffer": []
    })
    user_states[user_id] = state

    mensagem = "📋 *Modo de adição de checklist*\n\nEnvie o nome da checklist:"

    if update.callback_query:
        await update.callback_query.message.reply_text(mensagem, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensagem, parse_mode="Markdown")


async def handle_checklist_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    """Manipula a criação de checklist em etapas"""
    state = user_states.get(user_id, {})

    # Verifica se está no modo de checklist normal OU no modo de checklist para PDFs
    if state.get("mode") not in ["add_checklist", "add_checklist_pdf"]:
        return False

    stage = state.get("checklist_stage")
    is_pdf_mode = state.get("mode") == "add_checklist_pdf"

    if stage == "aguardando_nome":
        # Primeira etapa: recebe o nome da checklist
        if not text.strip():
            await update.message.reply_text("❌ O nome da checklist não pode estar vazio. Tente novamente:")
            return True

        state["checklist_name"] = text.strip()
        state["checklist_stage"] = "aguardando_itens"
        user_states[user_id] = state

        await update.message.reply_text(
            f"✅ Nome da checklist definido como: *{text.strip()}*\n\n"
            "📋 **Agora envie os itens da checklist:**\n\n"
            "Separe os itens usando **--**\n"
            "Exemplo: `item 1--item 2--item 3`\n\n"
            "Ou envie /cancelar para cancelar a operação.",
            parse_mode="Markdown"
        )
        return True

    elif stage == "aguardando_itens":
        # Segunda etapa: recebe os itens separados por --
        if not text.strip():
            await update.message.reply_text("❌ Os itens não podem estar vazios. Tente novamente:")
            return True

        # Processa os itens separados por --
        items = [item.strip() for item in text.split('--') if item.strip()]

        if not items:
            await update.message.reply_text("❌ Nenhum item válido encontrado. Tente novamente:")
            return True

        if is_pdf_mode:
            # Modo PDF: adiciona ao rascunho
            try:
                index_cartao = state.get("index_cartao")
                checklist_name = state.get("checklist_name")

                rascunhos = carregar_rascunhos(user_id)
                if rascunhos and 0 <= index_cartao < len(rascunhos):
                    rascunho = rascunhos[index_cartao]
                    if "checklists" not in rascunho:
                        rascunho["checklists"] = []

                    # Adiciona a checklist com itens ao rascunho
                    checklist_completa = {
                        "nome": checklist_name,
                        "itens": items
                    }
                    rascunho["checklists"].append(checklist_completa)
                    rascunho["editado"] = True
                    atualizar_rascunho(user_id, index_cartao, rascunho)

                    # Limpa o estado
                    state["mode"] = None
                    state["checklist_stage"] = None
                    state["checklist_name"] = None
                    state["index_cartao"] = None
                    user_states[user_id] = state

                    # Mensagem de sucesso
                    items_list = "\n".join([f"• {item}" for item in items])
                    await update.message.reply_text(
                        f"🎉 **Checklist '{checklist_name}' adicionada com sucesso!**\n\n"
                        f"📋 **Itens adicionados ({len(items)}):**\n"
                        f"{items_list}",
                        parse_mode="Markdown"
                    )

                    # Volta para as opções de edição
                    fake_query = type('Obj', (object,), {
                        'from_user': update.effective_user,
                        'edit_message_text': update.message.reply_text,
                        'message': update.message
                    })
                    await mostrar_opcoes_edicao(fake_query, context, index_cartao)
                else:
                    await update.message.reply_text("❌ Cartão não encontrado.")
                    # Limpa o estado em caso de erro
                    state["mode"] = None
                    state["checklist_stage"] = None
                    state["checklist_name"] = None
                    user_states[user_id] = state

            except Exception as e:
                logger.exception(f"Erro ao adicionar checklist ao PDF: {e}")
                await update.message.reply_text(f"❌ Erro ao adicionar checklist: {str(e)}")
                # Limpa o estado em caso de erro
                state["mode"] = None
                state["checklist_stage"] = None
                state["checklist_name"] = None
                user_states[user_id] = state

        else:
            # Modo normal: cria no Trello
            try:
                card_id = state.get("selected_card")
                checklist_name = state.get("checklist_name")

                # Cria a checklist
                checklist = create_checklist(user_id, card_id, checklist_name)

                # Adiciona os itens
                for item in items:
                    add_checkitem(user_id, checklist["id"], item)

                # Limpa o estado
                state["mode"] = None
                state["checklist_stage"] = None
                state["checklist_name"] = None
                user_states[user_id] = state

                # Mensagem de sucesso
                items_list = "\n".join([f"• {item}" for item in items])
                await update.message.reply_text(
                    f"🎉 **Checklist '{checklist_name}' criada com sucesso!**\n\n"
                    f"📋 **Itens adicionados ({len(items)}):**\n"
                    f"{items_list}",
                    parse_mode="Markdown"
                )

            except Exception as e:
                logger.exception(f"Erro ao criar checklist: {e}")
                await update.message.reply_text(f"❌ Erro ao criar checklist: {str(e)}")
                # Limpa o estado em caso de erro
                state["mode"] = None
                state["checklist_stage"] = None
                state["checklist_name"] = None
                user_states[user_id] = state

        return True

    return False


async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela qualquer operação em andamento"""
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})

    if state.get("mode"):
        modo_anterior = state.get("mode")
        # Limpa todos os estados ativos
        user_states[user_id] = {"mode": None}
        await update.message.reply_text(f"❌ Operação '{modo_anterior}' cancelada.")
    else:
        await update.message.reply_text("Nenhuma operação ativa para cancelar.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return

    st = user_states.get(user_id)
    if st and st.get("flow"):
        flow = st["flow"]
        if flow == "register_api_key":
            st["buffer"]["api_key"] = text
            st["flow"] = "register_token"
            user_states[user_id] = st
            await update.message.reply_text("OK. Agora envie seu *Trello Token*:", parse_mode="Markdown")
            return
        elif flow == "register_token":
            st["buffer"]["token"] = text
            st["flow"] = "register_board"
            user_states[user_id] = st
            await update.message.reply_text("Agora envie o *Board ID* (ID do quadro):", parse_mode="Markdown")
            return
        elif flow == "register_board":
            st["buffer"]["board_id"] = text
            buf = st["buffer"]
            users = load_users()
            users[str(user_id)] = {"api_key": buf.get("api_key"), "token": buf.get("token"),
                                   "board_id": buf.get("board_id")}
            save_users(users)
            user_states[user_id] = {"mode": None}
            await update.message.reply_text("Configuração salva ✅\n" + HELP_TEXT)
            return

    # Primeiro verifica se está no modo de criação de checklist
    if await handle_checklist_creation(update, context, user_id, text):
        return

    # Restante do código existente para outros modos...
    state = st or {}
    mode = state.get("mode")

    # Modos de edição de cartões PDF
    if mode in ["editando_data_cartao", "adicionando_comentario_cartao"]:
        index_cartao = state.get("index_cartao")
        buffer = state.get("buffer", [])
        buffer.append(text)
        state["buffer"] = buffer
        user_states[user_id] = state

        if mode == "editando_data_cartao":
            # Processa imediatamente a data
            nova_data = text.strip()
            if parse_date_ddmmaa(nova_data):
                rascunhos = carregar_rascunhos(user_id)
                if rascunhos and 0 <= index_cartao < len(rascunhos):
                    rascunho = rascunhos[index_cartao]
                    rascunho["data_entrega"] = nova_data
                    rascunho["data_formatada"] = f"📅 Data entrega: {nova_data}"
                    rascunho["editado"] = True
                    atualizar_rascunho(user_id, index_cartao, rascunho)

                    await update.message.reply_text(f"✅ Data alterada para: {nova_data}")
                    # Volta para as opções de edição
                    fake_query = type('Obj', (object,), {
                        'from_user': update.effective_user,
                        'edit_message_text': update.message.reply_text,
                        'message': update.message
                    })
                    await mostrar_opcoes_edicao(fake_query, context, index_cartao)
                else:
                    await update.message.reply_text("Cartão não encontrado.")
            else:
                await update.message.reply_text("❌ Formato de data inválido. Use dd/mm/aaaa")

            # Reseta o estado
            state["mode"] = None
            state["buffer"] = []
            user_states[user_id] = state

        elif mode == "adicionando_comentario_cartao":
            # Processa imediatamente o comentário
            comentario = text.strip()
            if comentario:
                rascunhos = carregar_rascunhos(user_id)
                if rascunhos and 0 <= index_cartao < len(rascunhos):
                    rascunho = rascunhos[index_cartao]
                    rascunho["comentarios"] = comentario
                    rascunho["editado"] = True
                    atualizar_rascunho(user_id, index_cartao, rascunho)

                    await update.message.reply_text("✅ Comentário adicionado")
                    # Volta para as opções de edição
                    fake_query = type('Obj', (object,), {
                        'from_user': update.effective_user,
                        'edit_message_text': update.message.reply_text,
                        'message': update.message
                    })
                    await mostrar_opcoes_edicao(fake_query, context, index_cartao)
                else:
                    await update.message.reply_text("Cartão não encontrado.")

            # Reseta o estado
            state["mode"] = None
            state["buffer"] = []
            user_states[user_id] = state

        return

    # Verifica se está no modo direto de checklist
    if state.get("mode") == "add_checklist_direto":
        if text.startswith("/cancelar_checklist"):
            await cancelar_cmd(update, context)
            return

        # Processa os itens da checklist
        index_cartao = state.get("index_cartao")
        buffer = state.get("checklist_buffer", [])
        buffer.append(text)
        state["checklist_buffer"] = buffer

        # Se tiver pelo menos 1 item, pergunta se quer finalizar
        if len(buffer) >= 1:
            itens = parse_items_from_buffer_lines(buffer)
            mensagem = f"📋 *Itens da Checklist ({len(itens)}):*\n" + "\n".join(f"• {item}" for item in itens)
            mensagem += "\n\nEnvie mais itens ou /finalizar_checklist para confirmar"

            keyboard = [[InlineKeyboardButton("✅ Finalizar Checklist", callback_data="finalizar_checklist_agora")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(mensagem, parse_mode="Markdown", reply_markup=reply_markup)

        user_states[user_id] = state
        return

    # default fallback
    await update.message.reply_text("Não entendi. Use /start para configurar ou use os comandos do bot.\n" + HELP_TEXT)


# -------------------- Sistema de Busca Aprimorado --------------------

async def buscar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca cartões por nome e mostra interface de edição"""
    user_id = update.effective_user.id
    users = load_users()

    if not users.get(str(user_id)):
        await update.message.reply_text("Configure suas credenciais primeiro com /start.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /buscar <termo de busca>")
        return

    termo_busca = " ".join(context.args).strip()
    if not termo_busca:
        await update.message.reply_text("Por favor, forneça um termo para buscar.")
        return

    try:
        # Busca cartões no quadro
        board_id = users[str(user_id)]["board_id"]
        cards = get_board_cards(user_id, board_id)

        # Filtra cartões pelo termo de busca (case insensitive)
        cartoes_encontrados = []
        for card in cards:
            if termo_busca.lower() in card.get("name", "").lower():
                cartoes_encontrados.append(card)

        if not cartoes_encontrados:
            await update.message.reply_text(f"❌ Nenhum cartão encontrado com o termo '{termo_busca}'.")
            return

        # Limpa qualquer estado anterior e salva os resultados
        user_states[user_id] = {
            "mode": "busca_cartoes",
            "cartoes_encontrados": cartoes_encontrados,
            "termo_busca": termo_busca  # SALVA O TERMO DE BUSCA
        }

        # Mostra resultados com interface de edição
        await mostrar_resultados_busca(update, context, cartoes_encontrados, termo_busca)

    except Exception as e:
        logger.exception(f"Erro no comando /buscar: {e}")
        await update.message.reply_text(f"❌ Erro ao buscar cartões: {str(e)}")


async def mostrar_resultados_busca(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   cartoes_encontrados: List[Dict], termo_busca: str):
    """Mostra resultados da busca com interface de edição"""
    user_id = update.effective_user.id

    texto_resultado = f"🔍 *Resultados da busca por '{termo_busca}':*\n\n"

    for i, card in enumerate(cartoes_encontrados):
        lista_nome = card.get("list_name", "Lista desconhecida")
        texto_resultado += f"*{i + 1}. {card['name']}*\n"
        texto_resultado += f"📋 Lista: {lista_nome}\n"

        # Informações adicionais
        if card.get("due"):
            data_entrega = datetime.fromisoformat(card["due"].replace('Z', '+00:00')).strftime("%d/%m/%Y")
            texto_resultado += f"📅 Data: {data_entrega}\n"

        if card.get("desc"):
            desc_curta = card["desc"][:100] + "..." if len(card["desc"]) > 100 else card["desc"]
            texto_resultado += f"📝 Descrição: {desc_curta}\n"

        texto_resultado += "\n"

    texto_resultado += f"📊 *Total encontrado: {len(cartoes_encontrados)} cartão(s)*\n\n"
    texto_resultado += "Clique nos botões abaixo para editar cada cartão:"

    # Cria botões para cada cartão encontrado
    keyboard = []
    for i, card in enumerate(cartoes_encontrados):
        nome_curto = card['name']
        if len(nome_curto) > 30:
            nome_curto = nome_curto[:27] + "..."

        keyboard.append([InlineKeyboardButton(f"📝 {i + 1}. {nome_curto}",
                                              callback_data=f"editar_cartao_busca|{i}")])

    # Botão para nova busca
    keyboard.append([InlineKeyboardButton("🔍 Nova Busca", callback_data="nova_busca")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(texto_resultado, parse_mode="Markdown", reply_markup=reply_markup)


async def mostrar_resultados_busca_from_callback(query, context, cartoes_encontrados: List[Dict], termo_busca: str):
    """Versão do mostrar_resultados_busca para ser chamada via callback"""
    user_id = query.from_user.id

    texto_resultado = f"🔍 *Resultados da busca por '{termo_busca}':*\n\n"

    for i, card in enumerate(cartoes_encontrados):
        lista_nome = card.get("list_name", "Lista desconhecida")
        texto_resultado += f"*{i + 1}. {card['name']}*\n"
        texto_resultado += f"📋 Lista: {lista_nome}\n"

        # Informações adicionais
        if card.get("due"):
            data_entrega = datetime.fromisoformat(card["due"].replace('Z', '+00:00')).strftime("%d/%m/%Y")
            texto_resultado += f"📅 Data: {data_entrega}\n"

        if card.get("desc"):
            desc_curta = card["desc"][:100] + "..." if len(card["desc"]) > 100 else card["desc"]
            texto_resultado += f"📝 Descrição: {desc_curta}\n"

        texto_resultado += "\n"

    texto_resultado += f"📊 *Total encontrado: {len(cartoes_encontrados)} cartão(s)*\n\n"
    texto_resultado += "Clique nos botões abaixo para editar cada cartão:"

    # Cria botões para cada cartão encontrado
    keyboard = []
    for i, card in enumerate(cartoes_encontrados):
        nome_curto = card['name']
        if len(nome_curto) > 30:
            nome_curto = nome_curto[:27] + "..."

        keyboard.append([InlineKeyboardButton(f"📝 {i + 1}. {nome_curto}",
                                              callback_data=f"editar_cartao_busca|{i}")])

    # Botão para nova busca
    keyboard.append([InlineKeyboardButton("🔍 Nova Busca", callback_data="nova_busca")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(texto_resultado, parse_mode="Markdown", reply_markup=reply_markup)


async def mostrar_opcoes_edicao_cartao_existente(query, context, index_cartao: int):
    """Mostra opções de edição para um cartão existente (da busca)"""
    user_id = query.from_user.id
    state = user_states.get(user_id, {})
    cartoes_encontrados = state.get("cartoes_encontrados", [])

    if not cartoes_encontrados or index_cartao >= len(cartoes_encontrados):
        await query.answer("Cartão não encontrado")
        return

    card = cartoes_encontrados[index_cartao]
    card_id = card["id"]

    try:
        # Busca informações atualizadas do cartão
        card_detalhes = get_card_by_id(user_id, card_id)
        checklists = get_card_checklists(user_id, card_id)
        comentarios = get_card_comments(user_id, card_id)
        anexos = get_card_attachments(user_id, card_id)

        # Detalhes do cartão
        detalhes_text = f"*EDITANDO CARTÃO:*\n\n"
        detalhes_text += f"*{card_detalhes['name']}*\n\n"

        # Descrição
        if card_detalhes.get('desc'):
            detalhes_text += f"{card_detalhes['desc']}\n\n"

        # Data
        if card_detalhes.get('due'):
            data_entrega = datetime.fromisoformat(card_detalhes['due'].replace('Z', '+00:00')).strftime("%d/%m/%Y")
            detalhes_text += f"📅 Data entrega: {data_entrega}\n\n"

        # Checklists
        if checklists:
            detalhes_text += f"📋 *Checklists:*\n"
            for checklist in checklists:
                detalhes_text += f"• {checklist['name']} ({len(checklist.get('checkItems', []))} itens)\n"
                for item in checklist.get('checkItems', []):
                    status = "✅" if item.get('state') == 'complete' else "☐"
                    detalhes_text += f"  {status} {item['name']}\n"
            detalhes_text += "\n"

        # Comentários
        if comentarios:
            detalhes_text += f"💬 *Comentários ({len(comentarios)}):*\n"
            for comentario in comentarios[:3]:  # Mostra apenas os 3 primeiros
                texto_comentario = comentario['data']['text']
                if len(texto_comentario) > 50:
                    texto_comentario = texto_comentario[:47] + "..."
                detalhes_text += f"• {texto_comentario}\n"
            detalhes_text += "\n"

        # Anexos
        if anexos:
            detalhes_text += f"📎 *Anexos ({len(anexos)}):*\n"
            for anexo in anexos[:3]:  # Mostra apenas os 3 primeiros
                nome_anexo = anexo.get('name', 'Arquivo')
                if len(nome_anexo) > 30:
                    nome_anexo = nome_anexo[:27] + "..."
                detalhes_text += f"• {nome_anexo}\n"

        # Botões de edição
        keyboard = [
            [InlineKeyboardButton("📝 Editar Nome", callback_data=f"editar_nome_existente|{index_cartao}")],
            [InlineKeyboardButton("📄 Editar Descrição", callback_data=f"editar_desc_existente|{index_cartao}")],
            [InlineKeyboardButton("📅 Editar Data", callback_data=f"editar_data_existente|{index_cartao}")],
            [InlineKeyboardButton("📋 Gerenciar Checklists", callback_data=f"gerenciar_checklists|{index_cartao}")],
            [InlineKeyboardButton("💬 Adicionar Comentário", callback_data=f"add_comentario_existente|{index_cartao}")],
            [InlineKeyboardButton("📎 Adicionar Anexo", callback_data=f"add_anexo_existente|{index_cartao}")],
            [InlineKeyboardButton("👥 Gerenciar Membros", callback_data=f"gerenciar_membros_existente|{index_cartao}")],
            [
                InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_busca"),
                InlineKeyboardButton("🚀 Mover Cartão", callback_data=f"mover_cartao|{index_cartao}")
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(detalhes_text, parse_mode="Markdown", reply_markup=reply_markup)

    except Exception as e:
        logger.exception(f"Erro ao carregar detalhes do cartão: {e}")
        await query.edit_message_text(f"❌ Erro ao carregar detalhes do cartão: {str(e)}")


# -------------------- Sistema de PDFs com Prévia e Edição --------------------

async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para coletar PDFs e gerar prévia antes de criar cartões"""
    user_id = update.effective_user.id
    users = load_users()

    if not users.get(str(user_id)):
        await update.message.reply_text("Configure suas credenciais primeiro com /start.")
        return

    # Limpa rascunhos anteriores
    limpar_rascunhos(user_id)

    # Entra em modo de coleta de PDFs
    state = user_states.get(user_id, {})
    state.update({
        "mode": "coletando_pdfs",
        "arquivos_temp": []  # Apenas para armazenar caminhos dos PDFs originais
    })
    user_states[user_id] = state

    await update.message.reply_text(
        "📄 *Modo de criação de cartões por PDF ativado!*\n\n"
        "Envie os arquivos PDF um por um. \n"
        "Após enviar todos os PDFs, use:\n"
        "• `/ok` para ver a prévia dos cartões com opções de edição\n"
        "• `/criar` para criar todos os cartões na lista '🚨 PEDIDOS SEM ARTE'\n"
        "• `/cancelar` para cancelar a operação",
        parse_mode="Markdown"
    )


async def ok_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra prévia dos cartões no formato específico com botões de edição"""
    user_id = update.effective_user.id

    # Carrega rascunhos
    rascunhos = carregar_rascunhos(user_id)

    if not rascunhos:
        await update.message.reply_text("Nenhum PDF foi processado ainda. Envie os arquivos PDF primeiro.")
        return

    # Mensagem de prévia
    preview_text = "📋 *PRÉVIA DOS CARTÕES - CLIQUE PARA EDITAR:*\n\n"

    for i, rascunho in enumerate(rascunhos):
        status_editado = " ✏️" if rascunho.get("editado", False) else ""
        preview_text += f"*Cartão {i + 1}:*{status_editado}\n"
        preview_text += f"*{rascunho['titulo']}*\n"

        # Mostra primeiros produtos (máximo 2)
        produtos_preview = rascunho.get('produtos', [])[:2]
        if produtos_preview:
            preview_text += "\n".join(produtos_preview)
            if len(rascunho.get('produtos', [])) > 2:
                preview_text += f"\n... +{len(rascunho.get('produtos', [])) - 2} produtos"

        preview_text += f"\n\n{rascunho['data_formatada']}"

        # Informações adicionais se houver
        if rascunho.get('checklists'):
            preview_text += f"\n📋 {len(rascunho['checklists'])} checklist(s)"
        if rascunho.get('comentarios'):
            preview_text += f"\n💬 Comentário adicionado"
        if rascunho.get('membros'):
            preview_text += f"\n👥 {len(rascunho['membros'])} membro(s)"
        if rascunho.get('anexos'):
            preview_text += f"\n📎 {len(rascunho['anexos'])} anexo(s)"

        preview_text += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    preview_text += f"📊 *Total: {len(rascunhos)} cartões*\n\n"
    preview_text += "Clique nos botões abaixo para editar cada cartão individualmente."

    # Cria botões para cada cartão
    keyboard = []
    for i in range(len(rascunhos)):
        rascunho = rascunhos[i]
        # Nome curto: "38379 | IGREJA BATISTA..."
        titulo_curto = rascunho['titulo']
        if len(titulo_curto) > 30:
            titulo_curto = titulo_curto[:27] + "..."

        status_editado = " ✏️" if rascunho.get("editado", False) else ""
        keyboard.append([InlineKeyboardButton(f"📝 Cartão {i + 1}: {titulo_curto}{status_editado}",
                                              callback_data=f"editar_cartao|{i}")])

    # Botões de ação global
    keyboard.append([
        InlineKeyboardButton("🔄 Atualizar Prévia", callback_data="atualizar_previa"),
        InlineKeyboardButton("🚀 Criar Todos", callback_data="criar_todos_cartoes")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(preview_text, parse_mode="Markdown", reply_markup=reply_markup)


async def ok_cmd_from_callback(query, context):
    """Versão do ok_cmd para ser chamada via callback"""
    user_id = query.from_user.id

    # Carrega rascunhos
    rascunhos = carregar_rascunhos(user_id)

    if not rascunhos:
        await query.edit_message_text("Nenhum PDF foi processado ainda. Envie os arquivos PDF primeiro.")
        return

    # Mensagem de prévia
    preview_text = "📋 *PRÉVIA DOS CARTÕES - CLIQUE PARA EDITAR:*\n\n"

    for i, rascunho in enumerate(rascunhos):
        status_editado = " ✏️" if rascunho.get("editado", False) else ""
        preview_text += f"*Cartão {i + 1}:*{status_editado}\n"
        preview_text += f"*{rascunho['titulo']}*\n"

        # Mostra primeiros produtos (máximo 2)
        produtos_preview = rascunho.get('produtos', [])[:2]
        if produtos_preview:
            preview_text += "\n".join(produtos_preview)
            if len(rascunho.get('produtos', [])) > 2:
                preview_text += f"\n... +{len(rascunho.get('produtos', [])) - 2} produtos"

        preview_text += f"\n\n{rascunho['data_formatada']}"

        # Informações adicionais se houver
        if rascunho.get('checklists'):
            preview_text += f"\n📋 {len(rascunho['checklists'])} checklist(s)"
        if rascunho.get('comentarios'):
            preview_text += f"\n💬 Comentário adicionado"
        if rascunho.get('membros'):
            preview_text += f"\n👥 {len(rascunho['membros'])} membro(s)"
        if rascunho.get('anexos'):
            preview_text += f"\n📎 {len(rascunho['anexos'])} anexo(s)"

        preview_text += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    preview_text += f"📊 *Total: {len(rascunhos)} cartões*\n\n"
    preview_text += "Clique nos botões abaixo para editar cada cartão individualmente."

    # Cria botões para cada cartão
    keyboard = []
    for i in range(len(rascunhos)):
        rascunho = rascunhos[i]
        # Nome curto: "38379 | IGREJA BATISTA..."
        titulo_curto = rascunho['titulo']
        if len(titulo_curto) > 30:
            titulo_curto = titulo_curto[:27] + "..."

        status_editado = " ✏️" if rascunho.get("editado", False) else ""
        keyboard.append([InlineKeyboardButton(f"📝 Cartão {i + 1}: {titulo_curto}{status_editado}",
                                              callback_data=f"editar_cartao|{i}")])

    # Botões de ação global
    keyboard.append([
        InlineKeyboardButton("🔄 Atualizar Prévia", callback_data="atualizar_previa"),
        InlineKeyboardButton("🚀 Criar Todos", callback_data="criar_todos_cartoes")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(preview_text, parse_mode="Markdown", reply_markup=reply_markup)


async def mostrar_opcoes_edicao(query, context, index_cartao: int):
    """Mostra opções de edição para um cartão específico no formato correto"""
    user_id = query.from_user.id
    rascunhos = carregar_rascunhos(user_id)

    if not rascunhos or index_cartao >= len(rascunhos):
        await query.answer("Cartão não encontrado")
        return

    rascunho = rascunhos[index_cartao]

    # Detalhes do cartão NO FORMATO ESPECÍFICO
    detalhes_text = f"*EDITANDO CARTÃO {index_cartao + 1}:*\n\n"
    detalhes_text += f"*{rascunho['titulo']}*\n\n"

    # Produtos
    if rascunho.get('produtos'):
        detalhes_text += "\n".join(rascunho['produtos']) + "\n\n"

    # Observações
    if rascunho.get('observacoes') and rascunho['observacoes'] != "N/A":
        detalhes_text += f"{rascunho['observacoes']}\n\n"

    # Data
    detalhes_text += f"{rascunho['data_formatada']}\n\n"

    # Informações adicionais
    if rascunho.get('checklists'):
        detalhes_text += f"📋 *Checklists Adicionadas:*\n"
        for checklist in rascunho['checklists']:
            if isinstance(checklist, dict):
                # Nova estrutura com itens
                detalhes_text += f"• {checklist['nome']} ({len(checklist.get('itens', []))} itens)\n"
                for item in checklist.get('itens', []):
                    detalhes_text += f"  ◦ {item}\n"
            else:
                # Estrutura antiga (apenas nome)
                detalhes_text += f"• {checklist}\n"
        detalhes_text += "\n"

    if rascunho.get('comentarios'):
        detalhes_text += f"💬 *Comentários:*\n{rascunho['comentarios']}\n\n"
    if rascunho.get('membros'):
        detalhes_text += f"👥 *Membros:* {', '.join(rascunho['membros'])}\n\n"
    if rascunho.get('anexos'):
        detalhes_text += f"📎 *Anexos ({len(rascunho['anexos'])}):*\n"
        for anexo in rascunho['anexos']:
            nome_arquivo = os.path.basename(anexo)
            detalhes_text += f"• {nome_arquivo}\n"
        detalhes_text += "\n"

    # Botões de edição
    keyboard = [
        [InlineKeyboardButton("📅 Editar Data", callback_data=f"editar_data|{index_cartao}")],
        [InlineKeyboardButton("💬 Adicionar Comentário", callback_data=f"add_comentario|{index_cartao}")],
        [InlineKeyboardButton("📋 Adicionar Checklist", callback_data=f"add_checklist|{index_cartao}")],
        [InlineKeyboardButton("👥 Adicionar Membro", callback_data=f"add_membro|{index_cartao}")],
        [InlineKeyboardButton("📎 Adicionar Anexo", callback_data=f"add_anexo|{index_cartao}")],
        [
            InlineKeyboardButton("⬅️ Voltar", callback_data="voltar_previa"),
            InlineKeyboardButton("🗑️ Excluir", callback_data=f"excluir_cartao|{index_cartao}")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(detalhes_text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        await query.message.reply_text(detalhes_text, parse_mode="Markdown", reply_markup=reply_markup)


async def editar_data_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE, index_cartao: int):
    """Inicia modo de edição de data para um cartão específico"""
    user_id = update.effective_user.id if update.message else update.callback_query.from_user.id

    state = user_states.get(user_id, {})
    state.update({
        "mode": "editando_data_cartao",
        "index_cartao": index_cartao,
        "buffer": []
    })
    user_states[user_id] = state

    mensagem = "📅 *Modo de edição de data*\n\nEnvie a nova data no formato dd/mm/aaaa:"

    if update.callback_query:
        await update.callback_query.message.reply_text(mensagem, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensagem, parse_mode="Markdown")


async def add_comentario_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE, index_cartao: int):
    """Inicia modo de adição de comentário para um cartão específico"""
    user_id = update.effective_user.id if update.message else update.callback_query.from_user.id

    state = user_states.get(user_id, {})
    state.update({
        "mode": "adicionando_comentario_cartao",
        "index_cartao": index_cartao,
        "buffer": []
    })
    user_states[user_id] = state

    mensagem = "💬 *Modo de adição de comentário*\n\nEnvie o comentário:"

    if update.callback_query:
        await update.callback_query.message.reply_text(mensagem, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensagem, parse_mode="Markdown")


async def add_anexo_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE, index_cartao: int):
    """Inicia modo de adição de anexo para um cartão específico"""
    user_id = update.effective_user.id if update.message else update.callback_query.from_user.id

    state = user_states.get(user_id, {})
    state.update({
        "mode": "adicionando_anexo_cartao",
        "index_cartao": index_cartao,
        "anexos": []
    })
    user_states[user_id] = state

    mensagem = (
        "📎 *Modo de adição de anexos*\n\n"
        "Agora envie os arquivos que deseja anexar.\n"
        "Após enviar todos os arquivos, use:\n"
        "• `/ok` para finalizar e voltar à edição\n"
        "• `/cancelar` para cancelar a operação"
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(mensagem, parse_mode="Markdown")
    else:
        await update.message.reply_text(mensagem, parse_mode="Markdown")


async def add_membro_cartao(update: Update, context: ContextTypes.DEFAULT_TYPE, index_cartao: int):
    """Inicia modo de adição de membro para um cartão específico com lista de múltipla escolha"""
    user_id = update.effective_user.id if update.message else update.callback_query.from_user.id

    try:
        users = load_users()
        ud = users.get(str(user_id))
        if not ud:
            await update.message.reply_text("Configuração não encontrada.")
            return

        board_id = ud["board_id"]
        # Busca membros do quadro
        membros = trello_request_for_user(user_id, "GET", f"/boards/{board_id}/members")

        if not membros:
            mensagem = "Nenhum membro encontrado no quadro."
            if update.callback_query:
                await update.callback_query.message.reply_text(mensagem)
            else:
                await update.message.reply_text(mensagem)
            return

        # Salva membros no contexto para seleção
        context.user_data["membros_disponiveis"] = membros
        context.user_data["index_cartao_editando"] = index_cartao

        # Carrega membros já selecionados
        rascunhos = carregar_rascunhos(user_id)
        membros_selecionados = []
        if rascunhos and index_cartao < len(rascunhos):
            membros_selecionados = rascunhos[index_cartao].get('membros_ids', [])

        # Cria teclado com membros (múltipla seleção)
        keyboard = []
        for i, membro in enumerate(membros):
            nome = membro.get('fullName') or membro.get('username', 'Sem nome')
            selecionado = "✅ " if membro['id'] in membros_selecionados else "☐ "
            keyboard.append([InlineKeyboardButton(f"{selecionado}{nome}", callback_data=f"selecionar_membro|{i}")])

        keyboard.append([InlineKeyboardButton("✅ Finalizar Seleção", callback_data="finalizar_selecao_membros")])
        keyboard.append([InlineKeyboardButton("⬅️ Voltar", callback_data=f"editar_cartao|{index_cartao}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        mensagem = "👥 *Selecionar Membros*\n\nClique nos membros para adicionar/remover (seleção múltipla):"

        if update.callback_query:
            await update.callback_query.edit_message_text(mensagem, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(mensagem, parse_mode="Markdown", reply_markup=reply_markup)

    except Exception as e:
        logger.exception(f"Erro ao carregar membros: {e}")
        mensagem = "Erro ao carregar membros do quadro."
        if update.callback_query:
            await update.callback_query.message.reply_text(mensagem)
        else:
            await update.message.reply_text(mensagem)


async def selecionar_membro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula seleção/deseleção de membros"""
    query = update.callback_query
    data = query.data

    index_membro = int(data.split("|")[1])
    membros_disponiveis = context.user_data.get("membros_disponiveis", [])
    index_cartao = context.user_data.get("index_cartao_editando")

    if not membros_disponiveis or index_membro >= len(membros_disponiveis):
        await query.answer("Membro não encontrado")
        return

    # Carrega rascunho atual
    user_id = query.from_user.id
    rascunhos = carregar_rascunhos(user_id)
    if not rascunhos or index_cartao >= len(rascunhos):
        await query.answer("Cartão não encontrado")
        return

    rascunho = rascunhos[index_cartao]
    membros_selecionados = rascunho.get('membros_ids', [])

    membro = membros_disponiveis[index_membro]
    membro_id = membro['id']

    # Alterna seleção
    if membro_id in membros_selecionados:
        membros_selecionados.remove(membro_id)
        status = "❌ Removido"
    else:
        membros_selecionados.append(membro_id)
        status = "✅ Adicionado"

    # Atualiza rascunho
    rascunho['membros_ids'] = membros_selecionados
    rascunho['membros'] = [
        membro['fullName'] or membro['username']
        for membro in membros_disponiveis
        if membro['id'] in membros_selecionados
    ]
    rascunho['editado'] = True
    atualizar_rascunho(user_id, index_cartao, rascunho)

    # Atualiza a mensagem com os novos estados
    await add_membro_cartao(update, context, index_cartao)
    await query.answer(f"{status}: {membro['fullName'] or membro['username']}")


async def finalizar_selecao_membros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza seleção de membros e volta para edição do cartão"""
    query = update.callback_query
    index_cartao = context.user_data.get("index_cartao_editando")

    await query.answer("Seleção de membros finalizada")
    await mostrar_opcoes_edicao(query, context, index_cartao)


async def criar_cartoes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cria todos os cartões a partir dos rascunhos"""
    user_id = update.effective_user.id
    users = load_users()

    if not users.get(str(user_id)):
        await update.message.reply_text("Configure suas credenciais primeiro com /start.")
        return

    rascunhos = carregar_rascunhos(user_id)

    if not rascunhos:
        await update.message.reply_text("Nenhum cartão para criar. Use /pedido para processar PDFs primeiro.")
        return

    try:
        # Busca a lista "🚨 PEDIDOS SEM ARTE"
        board_id = users[str(user_id)]["board_id"]
        lists = get_board_lists(user_id, board_id)

        lista_destino = None
        for lst in lists:
            if normalize_text(lst.get("name")) == normalize_text("🚨 PEDIDOS SEM ARTE"):
                lista_destino = lst
                break

        if not lista_destino:
            await update.message.reply_text("❌ Lista '🚨 PEDIDOS SEM ARTE' não encontrada no quadro.")
            return

        cartoes_criados = []
        erros = []

        for i, rascunho in enumerate(rascunhos, 1):
            try:
                # Prepara os dados do cartão
                card_data = {
                    "name": rascunho["titulo"],
                    "desc": rascunho["descricao"],
                    "idList": lista_destino["id"]
                }

                # Adiciona data se for válida
                if rascunho["data_entrega"] and rascunho["data_entrega"] != "N/A":
                    data_iso = parse_date_ddmmaa(rascunho["data_entrega"])
                    if data_iso:
                        card_data["due"] = data_iso
                    else:
                        logger.warning(f"Data inválida no cartão {i}: {rascunho['data_entrega']}")

                # Cria o cartão
                card = trello_request_for_user(user_id, "POST", "/cards", json_payload=card_data)

                # Adiciona checklists
                for checklist_data in rascunho.get("checklists", []):
                    try:
                        if isinstance(checklist_data, dict):
                            # Nova estrutura com itens
                            checklist_name = checklist_data['nome']
                            checklist_items = checklist_data.get('itens', [])
                        else:
                            # Estrutura antiga (apenas nome)
                            checklist_name = checklist_data
                            checklist_items = []

                        checklist = create_checklist(user_id, card["id"], checklist_name)

                        # Adiciona os itens se houver
                        for item in checklist_items:
                            add_checkitem(user_id, checklist["id"], item)

                    except Exception as e:
                        logger.warning(f"Erro ao criar checklist {checklist_name}: {e}")

                # Adiciona comentários
                if rascunho.get("comentarios"):
                    try:
                        add_comment(user_id, card["id"], rascunho["comentarios"])
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar comentário: {e}")

                # Adiciona membros
                for membro_id in rascunho.get("membros_ids", []):
                    try:
                        trello_request_for_user(user_id, "POST", f"/cards/{card['id']}/idMembers",
                                                params={"value": membro_id})
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar membro {membro_id}: {e}")

                # Adiciona anexos
                for anexo_path in rascunho.get("anexos", []):
                    try:
                        if os.path.exists(anexo_path):
                            upload_file_to_card(user_id, card["id"], anexo_path)
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar anexo {anexo_path}: {e}")

                cartoes_criados.append(card["name"])
                await update.message.reply_text(f"✅ Cartão {i} criado: {card['name']}")

            except Exception as e:
                erro_msg = f"Cartão {i} ({rascunho['titulo']}): {str(e)}"
                erros.append(erro_msg)
                logger.error(f"Erro ao criar cartão {i}: {e}")
                await update.message.reply_text(f"❌ Erro ao criar cartão {i}: {str(e)}")

        # Limpa rascunhos após criação
        limpar_rascunhos(user_id)

        # Resumo final
        if erros:
            resumo = (
                    f"📊 *Resumo da criação:*\n\n"
                    f"✅ *Criados com sucesso:* {len(cartoes_criados)}\n"
                    f"❌ *Com erro:* {len(erros)}\n\n"
                    f"*Erros:*\n" + "\n".join(f"• {erro}" for erro in erros)
            )
        else:
            resumo = f"🎉 *Todos os {len(cartoes_criados)} cartões foram criados com sucesso!*"

        await update.message.reply_text(resumo, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Erro geral ao criar cartões: {e}")
        await update.message.reply_text(f"❌ Erro ao criar cartões: {str(e)}")


async def criar_cartoes_cmd_from_callback(query, context):
    """Versão do criar_cartoes_cmd para ser chamada via callback"""
    user_id = query.from_user.id
    users = load_users()

    if not users.get(str(user_id)):
        await query.edit_message_text("Configure suas credenciais primeiro com /start.")
        return

    rascunhos = carregar_rascunhos(user_id)

    if not rascunhos:
        await query.edit_message_text("Nenhum cartão para criar. Use /pedido para processar PDFs primeiro.")
        return

    try:
        # Busca a lista "🚨 PEDIDOS SEM ARTE"
        board_id = users[str(user_id)]["board_id"]
        lists = get_board_lists(user_id, board_id)

        lista_destino = None
        for lst in lists:
            if normalize_text(lst.get("name")) == normalize_text("🚨 PEDIDOS SEM ARTE"):
                lista_destino = lst
                break

        if not lista_destino:
            await query.edit_message_text("❌ Lista '🚨 PEDIDOS SEM ARTE' não encontrada no quadro.")
            return

        cartoes_criados = []
        erros = []

        for i, rascunho in enumerate(rascunhos, 1):
            try:
                # Prepara os dados do cartão
                card_data = {
                    "name": rascunho["titulo"],
                    "desc": rascunho["descricao"],
                    "idList": lista_destino["id"]
                }

                # Adiciona data se for válida
                if rascunho["data_entrega"] and rascunho["data_entrega"] != "N/A":
                    data_iso = parse_date_ddmmaa(rascunho["data_entrega"])
                    if data_iso:
                        card_data["due"] = data_iso
                    else:
                        logger.warning(f"Data inválida no cartão {i}: {rascunho['data_entrega']}")

                # Cria o cartão
                card = trello_request_for_user(user_id, "POST", "/cards", json_payload=card_data)

                # Adiciona checklists
                for checklist_data in rascunho.get("checklists", []):
                    try:
                        if isinstance(checklist_data, dict):
                            # Nova estrutura com itens
                            checklist_name = checklist_data['nome']
                            checklist_items = checklist_data.get('itens', [])
                        else:
                            # Estrutura antiga (apenas nome)
                            checklist_name = checklist_data
                            checklist_items = []

                        checklist = create_checklist(user_id, card["id"], checklist_name)

                        # Adiciona os itens se houver
                        for item in checklist_items:
                            add_checkitem(user_id, checklist["id"], item)

                    except Exception as e:
                        logger.warning(f"Erro ao criar checklist {checklist_name}: {e}")

                # Adiciona comentários
                if rascunho.get("comentarios"):
                    try:
                        add_comment(user_id, card["id"], rascunho["comentarios"])
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar comentário: {e}")

                # Adiciona membros
                for membro_id in rascunho.get("membros_ids", []):
                    try:
                        trello_request_for_user(user_id, "POST", f"/cards/{card['id']}/idMembers",
                                                params={"value": membro_id})
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar membro {membro_id}: {e}")

                # Adiciona anexos
                for anexo_path in rascunho.get("anexos", []):
                    try:
                        if os.path.exists(anexo_path):
                            upload_file_to_card(user_id, card["id"], anexo_path)
                    except Exception as e:
                        logger.warning(f"Erro ao adicionar anexo {anexo_path}: {e}")

                cartoes_criados.append(card["name"])

            except Exception as e:
                erro_msg = f"Cartão {i} ({rascunho['titulo']}): {str(e)}"
                erros.append(erro_msg)
                logger.error(f"Erro ao criar cartão {i}: {e}")

        # Limpa rascunhos após criação
        limpar_rascunhos(user_id)

        # Resumo final
        if erros:
            resumo = (
                    f"📊 *Resumo da criação:*\n\n"
                    f"✅ *Criados com sucesso:* {len(cartoes_criados)}\n"
                    f"❌ *Com erro:* {len(erros)}\n\n"
                    f"*Erros:*\n" + "\n".join(f"• {erro}" for erro in erros)
            )
        else:
            resumo = f"🎉 *Todos os {len(cartoes_criados)} cartões foram criados com sucesso!*"

        await query.edit_message_text(resumo, parse_mode="Markdown")

    except Exception as e:
        logger.exception(f"Erro geral ao criar cartões: {e}")
        await query.edit_message_text(f"❌ Erro ao criar cartões: {str(e)}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula documentos (PDFs) enviados"""
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})

    if state.get("mode") == "coletando_pdfs":
        # Modo de coleta de PDFs para criação de cartões
        document = update.message.document
        if not document.mime_type or "pdf" not in document.mime_type.lower():
            await update.message.reply_text("❌ Por favor, envie apenas arquivos PDF.")
            return

        try:
            # Baixa o arquivo
            file = await context.bot.get_file(document.file_id)
            file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{document.file_name}")
            await file.download_to_drive(file_path)

            # Extrai informações do PDF
            dados_cartao = extract_info_from_pdf(file_path)

            if not dados_cartao:
                await update.message.reply_text("❌ Não foi possível extrair informações do PDF.")
                os.remove(file_path)
                return

            # Salva como rascunho
            salvar_rascunho(user_id, dados_cartao)

            # Remove o arquivo PDF temporário
            os.remove(file_path)

            # Conta quantos rascunhos existem
            rascunhos = carregar_rascunhos(user_id)
            total_rascunhos = len(rascunhos)

            await update.message.reply_text(
                f"✅ PDF processado com sucesso! ({total_rascunhos} cartão(s) aguardando)\n\n"
                f"Use `/ok` para ver a prévia ou continue enviando mais PDFs.",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.exception(f"Erro ao processar PDF: {e}")
            await update.message.reply_text(f"❌ Erro ao processar PDF: {str(e)}")

    elif state.get("mode") == "adicionando_anexo_cartao":
        # Modo de adição de anexos para cartões existentes ou em criação
        document = update.message.document
        try:
            # Baixa o arquivo
            file = await context.bot.get_file(document.file_id)
            file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{document.file_name}")
            await file.download_to_drive(file_path)

            # Adiciona ao estado
            anexos = state.get("anexos", [])
            anexos.append(file_path)
            state["anexos"] = anexos
            user_states[user_id] = state

            await update.message.reply_text(
                f"✅ Arquivo '{document.file_name}' recebido. \n"
                f"Total de anexos: {len(anexos)}\n\n"
                f"Envie mais arquivos ou use `/ok` para finalizar."
            )

        except Exception as e:
            logger.exception(f"Erro ao processar anexo: {e}")
            await update.message.reply_text(f"❌ Erro ao processar arquivo: {str(e)}")

    else:
        # Modo anexo normal
        await handle_anexo_document(update, context)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula callbacks dos botões inline"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "atualizar_previa":
        await ok_cmd_from_callback(query, context)

    elif data == "criar_todos_cartoes":
        await criar_cartoes_cmd_from_callback(query, context)

    elif data.startswith("editar_cartao|"):
        index_cartao = int(data.split("|")[1])
        await mostrar_opcoes_edicao(query, context, index_cartao)

    elif data == "voltar_previa":
        await ok_cmd_from_callback(query, context)

    elif data.startswith("excluir_cartao|"):
        index_cartao = int(data.split("|")[1])
        await query.edit_message_text(f"❌ Funcionalidade de exclusão ainda não implementada.")

    elif data.startswith("editar_data|"):
        index_cartao = int(data.split("|")[1])
        await editar_data_cartao(update, context, index_cartao)

    elif data.startswith("add_comentario|"):
        index_cartao = int(data.split("|")[1])
        await add_comentario_cartao(update, context, index_cartao)

    elif data.startswith("add_checklist|"):
        index_cartao = int(data.split("|")[1])
        await add_checklist_cartao(update, context, index_cartao)

    elif data.startswith("add_membro|"):
        index_cartao = int(data.split("|")[1])
        await add_membro_cartao(update, context, index_cartao)

    elif data.startswith("add_anexo|"):
        index_cartao = int(data.split("|")[1])
        await add_anexo_cartao(update, context, index_cartao)

    # Novos handlers para seleção de membros
    elif data.startswith("selecionar_membro|"):
        await selecionar_membro_handler(update, context)

    elif data == "finalizar_selecao_membros":
        await finalizar_selecao_membros(update, context)

    # Handlers para busca de cartões
    elif data.startswith("editar_cartao_busca|"):
        index_cartao = int(data.split("|")[1])
        await mostrar_opcoes_edicao_cartao_existente(query, context, index_cartao)

    elif data == "nova_busca":
        await query.edit_message_text("🔍 Digite /buscar <termo> para realizar uma nova busca.")

    elif data == "voltar_busca":
        state = user_states.get(user_id, {})
        cartoes_encontrados = state.get("cartoes_encontrados", [])
        termo_busca = state.get("termo_busca", "")
        if cartoes_encontrados:
            # CORREÇÃO: Usar a função específica para callbacks
            await mostrar_resultados_busca_from_callback(query, context, cartoes_encontrados, termo_busca)
        else:
            await query.edit_message_text("❌ Nenhum resultado de busca encontrado. Use /buscar para uma nova busca.")


# -------------------- Funções Auxiliares para Modos Guiados --------------------

def parse_items_from_buffer_lines(lines: List[str]) -> List[str]:
    """Parseia itens de checklist a partir de linhas do buffer"""
    items = []
    for line in lines:
        # Remove espaços extras e ignora linhas vazias
        line = line.strip()
        if line:
            items.append(line)
    return items


async def fim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza o modo guiado atual"""
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    mode = state.get("mode")
    if not mode:
        await update.message.reply_text("Nenhum modo ativo.")
        return

    # Limpa o modo
    state["mode"] = None
    buffer = state.get("buffer", [])
    state["buffer"] = []
    user_states[user_id] = state

    await update.message.reply_text(f"Modo {mode} finalizado. Buffer: {len(buffer)} itens.")


async def handle_anexo_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manipula documentos enviados no modo anexo normal"""
    user_id = update.effective_user.id
    state = user_states.get(user_id, {})
    if state.get("mode") != "anexo":
        return

    document = update.message.document
    file = await context.bot.get_file(document.file_id)
    file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{document.file_name}")
    await file.download_to_drive(file_path)

    # Armazena o caminho do arquivo
    anexos = state.get("anexos", [])
    anexos.append(file_path)
    state["anexos"] = anexos
    user_states[user_id] = state

    await update.message.reply_text(f"Arquivo '{document.file_name}' recebido. Envie mais ou /fim para subir.")


# -------------------- Main --------------------

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()

    # Comandos básicos
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("config", config_cmd))
    app.add_handler(CommandHandler("fim", fim_cmd))
    app.add_handler(CommandHandler("cancelar", cancelar_cmd))

    # NOVO: Sistema de checklist com separador --
    app.add_handler(CommandHandler("addchk", addchk_cmd))

    # NOVO: Sistema de busca aprimorado
    app.add_handler(CommandHandler("buscar", buscar_cmd))

    # Sistema de PDFs
    app.add_handler(CommandHandler("pedido", pdf_cmd))
    app.add_handler(CommandHandler("ok", ok_cmd))
    app.add_handler(CommandHandler("criar", criar_cartoes_cmd))

    # Handlers de callbacks (importante: deve vir antes do handler de documentos)
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Handlers de documentos
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Handler de texto (deve ser o último)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot iniciado...")
    app.run_polling()


if __name__ == "__main__":

    main()




