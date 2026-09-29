import os
import asyncio
import time
import zipfile
import re
from telethon import events, Button, TelegramClient, types
from telethon.errors import (
    MessageNotModifiedError, PhoneNumberInvalidError, PhoneNumberOccupiedError,
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    FreshChangePhoneForbiddenError, FloodWaitError
)
from telethon.tl.functions.account import SendChangePhoneCodeRequest, ChangePhoneRequest
from database import (
    cur, db, get_flag_by_country_name, get_bot_mode, get_panel_price, get_lzt_key,
    get_change_number_fee, get_more_account_filters_enabled,
)
from config import (
    PE_LOCATION, PE_GIFT, PE_LIGHTNING, PE_CHECK, P_MONEY, P_PKG, P_CARD, P_WARN,
    P_NO, P_YES, P_INR, P_TIME, P_FLAG, P_OTP, P_2FA, P_PHONE, AUTO_CANCEL_SECONDS,
    OTP_REGEX, bot, logger, API_ID, API_HASH
)
from utils.keyboards import style_btn
from utils.banners import send_bannered_message
from utils.states import active_orders, session_buy_state, get_user_lock
from utils.lzt import lzt_client, COUNTRY_TO_LZT
from utils.stock_filters import claim_stock_account, stock_filter_clause
from utils.telegram_sessions import materialize_session, persist_session

search_state = {}
change_number_state = {}

def get_active_order_card(order, phone, is_admin_user=False):
    fee = get_change_number_fee()
    fee_badge = f" (+₹{fee})" if fee > 0 else " (FREE)"
    msg = (f"<blockquote expandable>{PE_LIGHTNING} <b>𝐎ʀᴅᴇʀ 𝐀ᴄᴛɪᴠᴇ!</b>\n\n"
           f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>+{phone}</code>\n"
           f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
           f"🔐 <b>2𝐅𝐀 𝐏ᴀssᴡᴏʀᴅ:</b> <code>{order['twofa']}</code>\n\n"
           f"🔻 <b>𝐈ɴsᴛʀᴜᴄᴛɪᴏɴs:</b>\n"
           f"1. 𝐎ᴘᴇɴ 𝐓ᴇʟᴇɢʀᴀᴍ & 𝐀ᴅᴅ 𝐀ᴄᴄᴏᴜɴᴛ (<code>+{phone}</code>).\n"
           f"2. ⏳ <b>𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ!</b> 𝐓ʜᴇ ʙᴏᴛ ɪs ᴀᴄᴛɪᴠᴇʟʏ ʟɪsᴛᴇɴɪɴɢ ғᴏʀ ʏᴏᴜʀ 𝐎𝐓𝐏.\n\n"
           f"<i>💡 𝐘ᴏᴜ ᴄᴀɴ ᴀʟsᴏ ᴛᴀᴘ '🔄 𝐂ʜᴀɴɢᴇ ᴛᴏ 𝐌ʏ 𝐍ᴜᴍʙᴇʀ' ᴛᴏ ᴍɪɢʀᴀᴛᴇ ᴛʜɪs ᴀᴄᴄᴏᴜɴᴛ ᴅɪʀᴇᴄᴛʟʏ ᴛᴏ ʏᴏᴜʀ ᴘᴇʀsᴏɴᴀʟ ɴᴜᴍʙᴇʀ!</i></blockquote>")
    
    btns = [
        [style_btn(f"🔄 𝐂ʜᴀɴɢᴇ ᴛᴏ 𝐌ʏ 𝐍ᴜᴍʙᴇʀ{fee_badge}", f"chg_num|{phone}", "success", icon=5409320020058584473)],
        [
            style_btn("🔄 𝐆ᴇᴛ 𝐎𝐓𝐏 𝐀ɢᴀɪɴ", f"get_otp_again|{phone}", "primary", icon=5408995930416362034),
            style_btn("✅ 𝐅ɪɴɪsʜ 𝐎ʀᴅᴇʀ", f"finish_order|{phone}", "primary", icon=5409320020058584473)
        ]
    ]
    if is_admin_user:
        btns.append([style_btn("❌ [Admin] Cancel & Refund", f"cancel_order|{phone}", "danger", icon=6129888444245089008)])
    return msg, btns

async def get_local_stock_countries(mode="bulk", year=None):
    """Return local countries and counts matching one stock filter."""
    where, params = stock_filter_clause(mode, year=year)
    rows = cur.execute(
        f"SELECT country_name, COUNT(*) FROM stock WHERE {where} GROUP BY country_name",
        params,
    ).fetchall()
    return sorted(rows, key=lambda x: x[0])


async def get_countries_list(mode="bulk", year=None):
    """Retrieve available countries based on bot_mode and the selected filter."""
    bot_mode = get_bot_mode()

    # 1. Manual Mode: only local stock
    if bot_mode == 'manual':
        return await get_local_stock_countries(mode, year=year)

    # 2. Panel Mode: all supported countries from catalog
    if bot_mode == 'panel':
        all_c = set(COUNTRY_TO_LZT.keys())
        try:
            customs = cur.execute("SELECT name FROM custom_countries").fetchall()
            for (c,) in customs: all_c.add(c)
        except: pass
        return sorted([(c_name, '40+') for c_name in all_c], key=lambda x: x[0])

    # 3. Hybrid Mode: local stock with count + other catalog countries
    all_c = set(COUNTRY_TO_LZT.keys())
    try:
        customs = cur.execute("SELECT name FROM custom_countries").fetchall()
        for (c,) in customs: all_c.add(c)
    except: pass
    
    country_dict = {c_name: '40+' for c_name in all_c}
    local_rows = await get_local_stock_countries(mode, year=year)
    for c_name, count in local_rows:
        country_dict[c_name] = count

    return sorted(country_dict.items(), key=lambda x: x[0])

YEAR_BADGES = {
    2026: "✨ 2026 (Fresh)",
    2025: "🥉 2025 (1 Year Old)",
    2024: "🥈 2024 (2 Years Old)",
    2023: "🥇 2023 (3 Years Old)",
    2022: "💎 2022 (4 Years Old)",
    2021: "👑 2021 (5 Years Old)",
    2020: "🔥 2020 (6 Years Old)",
    2019: "⚡ 2019 & Older (Vintage)"
}

search_state = {}

async def search_countries_matching(query):
    import html
    from database import COUNTRY_CODES, get_flag_by_country_name
    from utils.lzt import get_lzt_code
    query_clean = query.strip().lower()
    dial_code = re.sub(r'[^\d]', '', query_clean)
    all_countries = await get_countries_list("bulk")
    
    matches = []
    for c_name, count in all_countries:
        if query_clean in c_name.lower():
            matches.append((c_name, count))
            continue
        if dial_code:
            for code, (name, _) in COUNTRY_CODES.items():
                if name == c_name and dial_code == code:
                    matches.append((c_name, count))
        lzt_code = get_lzt_code(c_name)
        if lzt_code and query_clean == lzt_code.lower():
            matches.append((c_name, count))
            continue
    return matches

FILTERS_LIST = [
    ("stars", "⭐ 𝐓ᴇʟᴇɢʀᴀᴍ 𝐒ᴛᴀʀs (𝐁ᴀʟᴀɴᴄᴇ)", 5409320020058584473),
    ("premium", "👑 𝐓ᴇʟᴇɢʀᴀᴍ 𝐏ʀᴇᴍɪᴜᴍ", 5408995930416362034),
    ("no_email", "🚫 𝐍ᴏ 𝐄ᴍᴀɪʟ 𝐁ᴏᴜɴᴅ (𝐃ɪʀᴇᴄᴛ 𝐎𝐓𝐏)", 5409320020058584473),
    ("with_email", "📧 𝐄ᴍᴀɪʟ 𝐁ᴏᴜɴᴅ (𝐖ɪᴛʜ 𝐌ᴀɪʟ)", 5408995930416362034),
    ("no_2fa", "🔓 𝐍ᴏ 2𝐅𝐀 (1-𝐂ʟɪᴄᴋ 𝐋ᴏɢɪɴ)", 5409320020058584473),
    ("with_2fa", "🔒 2𝐅𝐀 𝐄ɴᴀʙʟᴇᴅ (𝐏ᴀss 𝐈ɴᴄʟᴜᴅᴇᴅ)", 5408995930416362034),
    ("dc5", "🌐 𝐃𝐂 5 (𝐀sɪᴀ / 𝐈ɴᴅɪᴀ 𝐏ɪɴɢ)", 5409320020058584473),
    ("aged", "🏛️ 𝐀ɢᴇᴅ / 𝐎ʟᴅ 𝐀ᴄᴄᴏᴜɴᴛs", 5408995930416362034),
]

FILTER_BADGES = {
    "stars": "⭐ 𝐓ᴇʟᴇɢʀᴀᴍ 𝐒ᴛᴀʀs",
    "premium": "👑 𝐓ᴇʟᴇɢʀᴀᴍ 𝐏ʀᴇᴍɪᴜᴍ",
    "no_email": "🚫 𝐍ᴏ 𝐄ᴍᴀɪʟ 𝐁ᴏᴜɴᴅ (𝐃ɪʀᴇᴄᴛ 𝐎𝐓𝐏)",
    "with_email": "📧 𝐄ᴍᴀɪʟ 𝐁ᴏᴜɴᴅ (𝐖ɪᴛʜ 𝐌ᴀɪʟ)",
    "nonspam": "🟢 𝐍ᴏɴ-𝐒ᴘᴀᴍ (100% 𝐂ʟᴇᴀɴ)",
    "spam": "🟡 𝐒ᴘᴀᴍ / 𝐔sᴇᴅ (𝐂ʜᴇᴀᴘ)",
    "no_2fa": "🔓 𝐍ᴏ 2𝐅𝐀 (1-𝐂ʟɪᴄᴋ 𝐋ᴏɢɪɴ)",
    "with_2fa": "🔒 2𝐅𝐀 𝐄ɴᴀʙʟᴇᴅ (𝐏ᴀss 𝐈ɴᴄʟᴜᴅᴇᴅ)",
    "dc5": "🌐 𝐃𝐂 5 (𝐀sɪᴀ)",
    "aged": "🏛️ 𝐀ɢᴇᴅ / 𝐎ʟᴅ",
    "bulk": "🌍 𝐒ᴛᴀɴᴅᴀʀᴅ"
}

def buy_navigation_row(back_callback):
    return [
        style_btn("⬅️ 𝐁ᴀᴄᴋ", back_callback, "primary", icon=6129627894349045589),
        style_btn("🏠 𝐃ᴀsʜʙᴏᴀʀᴅ", b"dashboard_main", "danger", icon=6129812419028982717),
    ]

async def show_filters_catalog(event, page=1):
    limit = 4
    offset = (page - 1) * limit
    items = FILTERS_LIST[offset:offset+limit]
    total = len(FILTERS_LIST)
    total_pages = (total + limit - 1) // limit

    msg = (f"<blockquote>🎯 <b>𝐒ᴇʟᴇᴄᴛ ᴀɴ 𝐀ᴄᴄᴏᴜɴᴛ 𝐅ɪʟᴛᴇʀ:</b> (𝐏ᴀɢᴇ {page}/{total_pages})\n\n"
           f"<i>𝐂ʜᴏᴏsᴇ ᴀ sᴘᴇᴄɪғɪᴄ ᴀᴄᴄᴏᴜɴᴛ ᴛʏᴘᴇ ʙᴇʟᴏᴡ ᴛᴏ ʙʀᴏᴡsᴇ ᴄᴏᴜɴᴛʀɪᴇs:</i></blockquote>")
    
    btns = []
    for f_id, label, icon in items:
        if f_id == "aged":
            btns.append([style_btn(label, b"by_years_menu|filters", "primary", icon=icon)])
        else:
            btns.append([style_btn(label, f"pg_c|{f_id}|1|filters", "primary", icon=icon)])

    nav = []
    if page > 1: nav.append(style_btn("⬅️ 𝐏ʀᴇᴠ", f"pg_filters|{page-1}", "primary", icon=6129627894349045589))
    if offset + limit < total: nav.append(style_btn("𝐍ᴇxᴛ ➡️", f"pg_filters|{page+1}", "primary", icon=6129732880529628243))
    if nav: btns.append(nav)

    btns.append(buy_navigation_row(b"buy_back_main"))

    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else:
        await event.respond(msg, buttons=btns)

async def show_buy_menu(event):
    show_more_filters = (
        get_more_account_filters_enabled()
        if get_bot_mode() == "manual" else True
    )
    more_filters_line = (
        "🎯 <b>𝐌ᴏʀᴇ 𝐅ɪʟᴛᴇʀs:</b> 𝐒ᴛᴀʀs, 𝐄ᴍᴀɪʟ, 2𝐅𝐀, 𝐏ʀᴇᴍɪᴜᴍ, 𝐃𝐂...\n"
        if show_more_filters else ""
    )
    msg = (f"<blockquote>{PE_GIFT} <b>𝐒ᴇʟᴇᴄᴛ 𝐀ᴄᴄᴏᴜɴᴛ 𝐂ᴀᴛᴇɢᴏʀʏ:</b>\n\n"
           f"🔍 <b>𝐒ᴇᴀʀᴄʜ 𝐂ᴏᴜɴᴛʀʏ:</b> 𝐐ᴜɪᴄᴋ ʟᴏᴏᴋᴜᴘ ʙʏ ɴᴀᴍᴇ ᴏʀ ᴅɪᴀʟ ᴄᴏᴅᴇ (+91, +55...)\n"
           f"🟢 <b>𝐍ᴏɴ-𝐒ᴘᴀᴍ / 𝐂ʟᴇᴀɴ:</b> 100% 𝐒ᴘᴀᴍʙʟᴏᴄᴋ-𝐅ʀᴇᴇ (𝐃𝐌 & 𝐏ᴇʀsᴏɴᴀʟ 𝐔sᴇ).\n"
           f"🟡 <b>𝐒ᴘᴀᴍ / 𝐔sᴇᴅ (𝐂ʜᴇᴀᴘ):</b> 𝐁ᴜᴅɢᴇᴛ 𝐀ᴄᴄᴏᴜɴᴛs (𝐂ʜᴀɴɴᴇʟ 𝐉ᴏɪɴᴇʀs & 𝐌ᴇᴍʙᴇʀs).\n"
           f"{more_filters_line}"
           f"🌍 <b>𝐀ʟʟ 𝐂ᴏᴜɴᴛʀɪᴇs:</b> 𝐁ʀᴏᴡsᴇ 50+ ᴄᴏᴜɴᴛʀɪᴇs sᴛᴏᴄᴋ (𝐅ʀᴇsʜ & 𝐀ʟʟ).</blockquote>")
    btns = [
        [style_btn("🔍 𝐒ᴇᴀʀᴄʜ 𝐂ᴏᴜɴᴛʀʏ", b"search_country_btn", "primary", icon=5409098988156629257)],
        [
            style_btn("🟢 𝐍ᴏɴ-𝐒ᴘᴀᴍ / 𝐂ʟᴇᴀɴ", b"pg_c|nonspam|1|menu", "success", icon=5409320020058584473),
            style_btn("🟡 𝐒ᴘᴀᴍ / 𝐔sᴇᴅ (𝐂ʜᴇᴀᴘ)", b"pg_c|spam|1|menu", "primary", icon=5408995930416362034)
        ],
        [style_btn("🌍 𝐀ʟʟ 𝐂ᴏᴜɴᴛʀɪᴇs (𝐅ʀᴇsʜ & 𝐀ʟʟ)", b"pg_c|bulk|1|menu", "primary", icon=6154249597532248059)],
        [style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐃ᴀsʜʙᴏᴀʀᴅ", b"dashboard_main", "danger", icon=6129812419028982717)]
    ]
    if show_more_filters:
        btns.insert(3, [style_btn("🎯 𝐌ᴏʀᴇ 𝐀ᴄᴄᴏᴜɴᴛ 𝐅ɪʟᴛᴇʀs (𝐒ᴛᴀʀs/2𝐅𝐀...)", b"pg_filters|1", "success", icon=5409320020058584473)])
    if await send_bannered_message(bot, event, "buy", msg, btns):
        return
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else:
        await event.respond(msg, buttons=btns)

async def show_years_catalog(event, back_target="menu"):
    msg = (f"<blockquote>🏛️ <b>𝐒ᴇʟᴇᴄᴛ 𝐀ᴄᴄᴏᴜɴᴛ 𝐘ᴇᴀʀ (𝐀ɢᴇ):</b>\n\n"
           f"<i>𝐀ɢᴇᴅ ᴀᴄᴄᴏᴜɴᴛs ʜᴀᴠᴇ ʜɪɢʜᴇʀ ᴛʀᴜsᴛ, ʟᴏᴡᴇʀ ʙᴀɴ ʀᴀᴛᴇs, ᴀɴᴅ ʟᴏɴɢᴇʀ ʜɪsᴛᴏʀʏ!</i></blockquote>")
    bot_mode = get_bot_mode()
    years = [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019]
    if bot_mode == 'manual':
        rows = cur.execute(
            "SELECT DISTINCT account_year FROM stock WHERE available=1 "
            "AND account_year IS NOT NULL ORDER BY account_year DESC"
        ).fetchall()
        years = [row[0] for row in rows]
        if not years:
            return await event.respond(f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ᴀᴠᴀɪʟᴀʙʟᴇ ғᴏʀ ᴛʜɪs ғɪʟᴛᴇʀ.")

    btns = []
    for y in years:
        label = YEAR_BADGES.get(y, f"📅 {y}")
        btns.append([style_btn(label, f"c_by_yr|{y}|1|{back_target}", "primary", icon=5408995930416362034)])
    back_callback = b"pg_filters|1" if back_target == "filters" else b"buy_back_main"
    btns.append(buy_navigation_row(back_callback))
    
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else:
        await event.respond(msg, buttons=btns)

async def show_countries_for_year(event, year, page, back_target="menu"):
    limit = 10
    offset = (page - 1) * limit
    countries_all = await get_countries_list("bulk", year=year)
    total = len(countries_all)
    countries = countries_all[offset:offset+limit]
    
    if not countries:
        return await event.respond(f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ᴀᴠᴀɪʟᴀʙʟᴇ ғᴏʀ ᴛʜɪs ғɪʟᴛᴇʀ.")

    btns = []
    for c_name, count in countries:
        flag = get_flag_by_country_name(c_name)
        price = get_panel_price(c_name, year)
        btns.append(style_btn(f"{flag} {c_name} — {P_INR}{price}", f"by|bulk|{c_name}|{year}|{price}", "primary", icon=6154249597532248059))
        
    f_btns = [btns[i:i+2] for i in range(0, len(btns), 2)]
    
    nav = []
    if page > 1: nav.append(style_btn("⬅️ 𝐏ʀᴇᴠ", f"c_by_yr|{year}|{page-1}|{back_target}", "primary", icon=6129627894349045589))
    if offset + limit < total: nav.append(style_btn("𝐍ᴇxᴛ ➡️", f"c_by_yr|{year}|{page+1}|{back_target}", "primary", icon=6129732880529628243))
    if nav: f_btns.append(nav)
    
    year_back_callback = b"by_years_menu" if back_target == "menu" else f"by_years_menu|{back_target}"
    f_btns.append(buy_navigation_row(year_back_callback))
    
    total_pages = (total + limit - 1) // limit
    msg = f"<blockquote>🏛️ <b>𝐒ᴇʟᴇᴄᴛ 𝐂ᴏᴜɴᴛʀʏ ғᴏʀ {year} 𝐀ᴄᴄᴏᴜɴᴛs:</b> (𝐏ᴀɢᴇ {page}/{total_pages})</blockquote>"
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=f_btns)
        except MessageNotModifiedError: pass
    else: await event.respond(msg, buttons=f_btns)

async def show_countries(event, mode, page, back_target="menu"):
    limit = 12
    offset = (page - 1) * limit
    countries_all = await get_countries_list(mode)
    total = len(countries_all)
    countries = countries_all[offset:offset+limit]
    
    if not countries:
        return await event.respond(f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ᴀᴠᴀɪʟᴀʙʟᴇ ғᴏʀ ᴛʜɪs ғɪʟᴛᴇʀ.")

    btns = []
    for c_name, count in countries:
        flag = get_flag_by_country_name(c_name)
        cnt_str = f"({count})" if count else "(40+)"
        btns.append(style_btn(f"{flag} {c_name} {cnt_str}", f"bc|{mode}|{c_name}|{back_target}", "primary", icon=6154249597532248059))
        
    f_btns = [btns[i:i+2] for i in range(0, len(btns), 2)]
    
    nav = []
    if page > 1: nav.append(style_btn("⬅️ 𝐏ʀᴇᴠ", f"pg_c|{mode}|{page-1}|{back_target}", "primary", icon=6129627894349045589))
    nav.append(style_btn("🔍 𝐒ᴇᴀʀᴄʜ", b"search_country_btn", "primary", icon=5409098988156629257))
    if offset + limit < total: nav.append(style_btn("𝐍ᴇxᴛ ➡️", f"pg_c|{mode}|{page+1}|{back_target}", "primary", icon=6129732880529628243))
    if nav: f_btns.append(nav)
    
    back_callback = b"pg_filters|1" if back_target == "filters" else b"buy_back_main"
    f_btns.append(buy_navigation_row(back_callback))
    
    total_pages = (total + limit - 1) // limit
    if mode in FILTER_BADGES and mode != 'bulk':
        cat_header = f"🎯 <b>𝐒ᴇʟᴇᴄᴛ 𝐂ᴏᴜɴᴛʀʏ ({FILTER_BADGES[mode]}):</b>"
    else:
        cat_header = f"{PE_LOCATION} <b>𝐒ᴇʟᴇᴄᴛ ᴀ 𝐂ᴏᴜɴᴛʀʏ:</b>"

    msg = f"<blockquote>{cat_header} (𝐏ᴀɢᴇ {page}/{total_pages})</blockquote>"
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=f_btns)
        except MessageNotModifiedError: pass
    else: await event.respond(msg, buttons=f_btns)

async def show_years(event, mode, country, back_target="menu"):
    if "|" in country:
        parts = country.split("|")
        country = parts[-1].strip()
        if len(parts) > 1 and mode == 'bulk':
            mode = parts[0].strip()
            
    bot_mode = get_bot_mode()
    year_options = []
    
    # 1. Check local stock first
    if bot_mode in ('manual', 'hybrid'):
        where, params = stock_filter_clause(mode, country=country)
        rows = cur.execute(
            f"SELECT account_year, COUNT(*), price FROM stock WHERE {where} "
            "GROUP BY account_year, price",
            params,
        ).fetchall()
            
        for y, count, price in rows:
            year_options.append({
                'year': y, 'count': count, 'price': price, 'source': 'local'
            })

    # 2. If panel mode, or hybrid with no local stock
    if (bot_mode == 'panel' or (bot_mode == 'hybrid' and not year_options)) and get_lzt_key():
        try:
            items = await lzt_client.search_items(country, mode=mode)
            # Group by year
            years_grouped = {}
            for itm in items:
                y = itm['year']
                if y not in years_grouped:
                    price = get_panel_price(country, y, itm['price_rub'], mode=mode)
                    years_grouped[y] = {'count': 0, 'price': price}
                years_grouped[y]['count'] += 1
                
            for y, info in sorted(years_grouped.items(), key=lambda x: x[0], reverse=True):
                year_options.append({
                    'year': y, 'count': info['count'], 'price': info['price'], 'source': 'lzt'
                })
        except Exception as e:
            logger.error(f"Error fetching LZT years for {country}: {e}")

    # Manual mode must never manufacture panel years.
    if not year_options and bot_mode != 'manual':
        for y in [2026, 2025, 2024, 2023, 2022, 2021]:
            year_options.append({
                'year': y, 'count': '40+', 'price': get_panel_price(country, y, mode=mode), 'source': 'lzt'
            })

    if not year_options:
        return await event.edit(f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ᴀᴠᴀɪʟᴀʙʟᴇ ғᴏʀ ᴛʜɪs ғɪʟᴛᴇʀ.")
    
    flag = get_flag_by_country_name(country)
    btns = []
    for opt in year_options:
        y, count, price = opt['year'], opt['count'], opt['price']
        badge = YEAR_BADGES.get(int(y) if str(y).isdigit() else y, f"📅 {y}")
        cnt_text = f"({count} left)" if isinstance(count, int) else f"({count})"
        btns.append([style_btn(f"{badge} — {P_INR}{price} {cnt_text}", f"by|{mode}|{country}|{y}|{price}", "primary", icon=5408995930416362034)])
    
    if mode != 'bulk':
        btns.append(buy_navigation_row(f"pg_c|{mode}|1|{back_target}"))
    else:
        btns.append(buy_navigation_row(f"pg_c|bulk|1|{back_target}"))
    
    if mode in FILTER_BADGES and mode != 'bulk':
        cat_label = f" ({FILTER_BADGES[mode]})"
    else:
        cat_label = ""
    
    await event.edit(f"<blockquote>{flag} <b>𝐒ᴇʟᴇᴄᴛ 𝐘ᴇᴀʀ & 𝐏ʀɪᴄᴇ ғᴏʀ {country}{cat_label}:</b></blockquote>", buttons=btns)

async def confirm_purchase(event, mode, country, year, price):
    if "|" in country:
        parts = country.split("|")
        country = parts[-1].strip()
        if len(parts) > 1 and mode == 'bulk':
            mode = parts[0].strip()
            
    flag = get_flag_by_country_name(country)
    badge = YEAR_BADGES.get(int(year) if str(year).isdigit() else year, f"📅 {year}")
    
    if mode == 'nonspam': cat_badge = "🟢 𝐍ᴏɴ-𝐒ᴘᴀᴍ (100% 𝐂ʟᴇᴀɴ)"
    elif mode == 'spam': cat_badge = "🟡 𝐒ᴘᴀᴍ / 𝐔sᴇᴅ (𝐂ʜᴇᴀᴘ)"
    else: cat_badge = "🌍 𝐒ᴛᴀɴᴅᴀʀᴅ"

    msg = (f"<blockquote>{PE_GIFT} <b>𝐂ᴏɴғɪʀᴍ 𝐏ᴜʀᴄʜᴀsᴇ</b>\n\n"
           f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {flag} {country}\n"
           f"🏷️ <b>𝐂ᴀᴛᴇɢᴏʀʏ:</b> {cat_badge}\n"
           f"📆 <b>𝐘ᴇᴀʀ:</b> {badge}\n"
           f"{P_MONEY} <b>𝐏ʀɪᴄᴇ:</b> {P_INR}{price}\n\n"
           f"<b>𝐀ʀᴇ ʏᴏᴜ sᴜʀᴇ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ʙᴜʏ?</b></blockquote>")
    btns = [
        [style_btn("✅ 𝐂ᴏɴғɪʀᴍ 𝐁ᴜʏ", f"buy_cf|{mode}|{country}|{year}|{price}", "success", icon=5409320020058584473)],
        [style_btn("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action", "danger", icon=6129888444245089008)]
    ]
    await event.edit(msg, buttons=btns)

async def process_purchase(event, mode, country, year, price_str):
    if "|" in country:
        parts = country.split("|")
        country = parts[-1].strip()
        if len(parts) > 1 and mode == 'bulk':
            mode = parts[0].strip()
            
    uid, price = event.sender_id, int(price_str)
    bot_mode = get_bot_mode()
    
    async with get_user_lock(uid):
        disc_row = cur.execute("SELECT discount, balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not disc_row: return await event.answer("❌ User not found!", alert=True)
        discount, balance = disc_row[0], disc_row[1]
        final_price = price if discount == 0 else int(price * (100 - discount) / 100)
        
        if balance < final_price:
            return await event.answer("❌ Insufficient Balance!", alert=True)

        local_row = None
        is_local = bot_mode in ('manual', 'hybrid')
        if is_local:
            local_row = claim_stock_account(db, mode, country=country, year=year)
            is_local = local_row is not None
        
        if not is_local and bot_mode == 'manual':
            return await event.answer("❌ Out of stock!", alert=True)

        if is_local:
            phone = local_row.get('phone')
            twofa_pass = local_row.get('twofa') or "None"
            cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?", (final_price, uid, final_price))
            if cur.rowcount == 0:
                db.rollback()
                return await event.answer("❌ Insufficient Balance!", alert=True)
            db.commit()
        else:
            # Panel / LZT order: reserve balance first
            cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?", (final_price, uid, final_price))
            if cur.rowcount == 0: return await event.answer("❌ Insufficient Balance!", alert=True)
            db.commit()

    c_icon = get_flag_by_country_name(country)
    actual_year = int(year)

    if is_local:
        # Local session processing
        await event.edit(f"{PE_LIGHTNING} <b>𝐏ʀᴏᴄᴇssɪɴɢ ʏᴏᴜʀ ᴏʀᴅᴇʀ...</b>\n𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ ᴡʜɪʟᴇ ᴡᴇ ɪɴɪᴛɪᴀʟɪᴢᴇ ᴛʜᴇ sᴇssɪᴏɴ.")
        
        session_id = local_row.get('session_id')
        try:
            if not session_id:
                raise ValueError("Stock account has no session_id")
            sess = materialize_session(db.repository, session_id, account_key=phone)
        except (OSError, ValueError, TimeoutError) as exc:
            logger.warning("Session restore failed: error_type=%s", type(exc).__name__)
            async with get_user_lock(uid):
                cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (final_price, uid))
                db.repository.release_stock_account(local_row)
                db.commit()
            return await event.edit(f"{P_NO} <b>Error initializing account. (Session unavailable)</b> Money refunded.")

        try:
            client = TelegramClient(sess, API_ID, API_HASH, connection_retries=None, retry_delay=3, auto_reconnect=True)
            await client.connect()
            if not await client.is_user_authorized():
                raise Exception("Session expired or not authorized")
        except Exception as e:
            logger.error(f"Client init error: {e}")
            try: await client.disconnect()
            except: pass
            async with get_user_lock(uid):
                cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (final_price, uid))
                db.repository.release_stock_account(local_row)
                db.commit()
            return await event.edit(f"{P_NO} <b>Error initializing account. (Session Dead)</b> Money refunded.")

        from database import is_admin
        temp_order = {'c_icon': c_icon, 'country': country, 'twofa': twofa_pass}
        msg, active_btns = get_active_order_card(temp_order, phone, is_admin(uid))
        sent_msg = await event.edit(msg, buttons=active_btns)
        
        active_orders[phone] = {
            'uid': uid, 'client': client, 'sess': sess, 'start_time': time.time(), 
            'session_id': session_id,
            'paid': False, 'price': final_price, 'country': country, 'year': actual_year, 
            'c_icon': c_icon, 'twofa': twofa_pass, 'msg_id': sent_msg.id, 'is_lzt': False
        }
        asyncio.create_task(auto_otp_task(phone))
    else:
        # LZT Panel Purchase Flow
        await event.edit(f"{PE_LIGHTNING} <b>𝐏ʀᴏᴄᴇssɪɴɢ ʏᴏᴜʀ ᴏʀᴅᴇʀ...</b>\n𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ ᴡʜɪʟᴇ ᴡᴇ ɪɴɪᴛɪᴀʟɪᴢᴇ ᴛʜᴇ sᴇssɪᴏɴ.")
        try:
            items = await lzt_client.search_items(country, actual_year, mode=mode)
            if not items:
                items = await lzt_client.search_items(country, mode=mode)
            
            if not items:
                async with get_user_lock(uid):
                    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (final_price, uid))
                    db.commit()
                return await event.edit(f"<blockquote>{P_NO} <b>❌ 𝐎ᴜᴛ ᴏғ 𝐒ᴛᴏᴄᴋ!</b>\n\n𝐍ᴏ ᴀᴄᴄᴏᴜɴᴛs ᴀʀᴇ ᴄᴜʀʀᴇɴᴛʟʏ ᴀᴠᴀɪʟᴀʙʟᴇ ғᴏʀ <b>{c_icon} {country}</b>.\n𝐘ᴏᴜʀ ᴍᴏɴᴇʏ (<b>{P_INR}{final_price}</b>) ʜᴀs ʙᴇᴇɴ <b>ɪɴsᴛᴀɴᴛʟʏ ʀᴇғᴜɴᴅᴇᴅ</b>.</blockquote>", buttons=[[style_btn("🛒 𝐁ᴜʏ 𝐀ɴᴏᴛʜᴇʀ 𝐂ᴏᴜɴᴛʀʏ", "buy_back_main", "primary", icon=5408995930416362034)]])

            buy_success = False
            bought_info = None
            client = None
            phone = None
            last_err = ""

            for itm in items[:5]:
                item_id = itm['item_id']
                price_str = itm.get('price_str', str(itm.get('price_usd', '0.1')))
                balance_id = itm.get('balance_id')
                ok, buy_result = await lzt_client.fast_buy(item_id, price_str, balance_id)
                if ok:
                    # Post-purchase spam safety check
                    bought_data = buy_result.get("item_data") or {}
                    post_sb = bought_data.get("telegram_spam_block")
                    if mode == 'nonspam' and post_sb is not None and post_sb != -1:
                        logger.warning(f"Bought item {item_id} has spamblock {post_sb}, rejecting for nonspam mode...")
                        continue
                    if mode == 'spam' and post_sb == -1:
                        logger.warning(f"Bought item {item_id} in spam mode is clean, rejecting for spam mode...")
                        continue

                    str_sess = buy_result.get("string_session")
                    if str_sess:
                        try:
                            from telethon.sessions import StringSession
                            test_client = TelegramClient(StringSession(str_sess), 2040, 'b18441a1ff607e10a989891a5462e627', connection_retries=None, retry_delay=3, auto_reconnect=True)
                            await test_client.connect()
                            if await test_client.is_user_authorized():
                                me = await test_client.get_me()
                                p = getattr(me, 'phone', None) or buy_result.get("phone") or f"Item-{item_id}"
                                phone = str(p).lstrip('+')
                                client = test_client
                                bought_info = buy_result
                                buy_success = True
                                break
                            else:
                                logger.warning(f"Session for item {item_id} expired on Telegram, trying next...")
                                try: await test_client.disconnect()
                                except: pass
                        except Exception as conn_err:
                            logger.warning(f"Connection error for item {item_id}: {conn_err}, trying next...")
                else:
                    last_err = str(buy_result)
                    logger.warning(f"Fast-buy attempt failed for {item_id}: {last_err}, trying next...")

            if not buy_success or not client:
                async with get_user_lock(uid):
                    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (final_price, uid))
                    db.commit()
                return await event.edit(f"{P_NO} <b>Error initializing account.</b> {last_err or 'Session unavailable.'}\nYour money has been refunded.")

            item_id = bought_info['item_id']
            twofa_pass = bought_info.get("twofa") or "None"
            str_sess = bought_info.get("string_session")

            from database import is_admin
            temp_order = {'c_icon': c_icon, 'country': country, 'twofa': twofa_pass}
            msg, active_btns = get_active_order_card(temp_order, phone, is_admin(uid))
            sent_msg = await event.edit(msg, buttons=active_btns)

            active_orders[phone] = {
                'uid': uid, 'client': client, 'sess': str_sess, 'item_id': item_id,
                'start_time': time.time(), 'paid': False, 'price': final_price,
                'country': country, 'year': actual_year, 'c_icon': c_icon,
                'twofa': twofa_pass, 'msg_id': sent_msg.id, 'is_lzt': True
            }
            asyncio.create_task(auto_otp_task(phone))
        except Exception as e:
            logger.error(f"Purchase flow error: {e}")
            async with get_user_lock(uid):
                cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (final_price, uid))
                db.commit()
            return await event.edit(f"{P_NO} <b>Error initializing account.</b> Money refunded.")

def extract_otp_from_text(text):
    if not text:
        return None
    patterns = [
        r"(?i)(?:login\s*code|web\s*login\s*code|código\s*de\s*inicio\s*de\s*sesión|код\s*подтверждения)[\s:]*([0-9]{5,6})",
        r"(?i)(?:code|kod|kód|código|код)[\s:]*([0-9]{5,6})",
        r"\b([0-9]{5})\b",
        r"\b([0-9]{6})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            code = m.group(1) if m.groups() else m.group(0)
            if code not in {"2024", "2025", "2026", "2027"}:
                return code
    return None

async def fetch_order_otp(order):
    client = order.get('client')
    start_time = order.get('start_time', 0)
    
    # 1. Fetch from Telegram session if client is available
    if client:
        try:
            if not client.is_connected():
                try:
                    await client.connect()
                except Exception as ce:
                    logger.warning(f"Reconnecting client failed: {ce}")
            
            if client.is_connected():
                msgs = []
                try:
                    msgs = await client.get_messages(777000, limit=5)
                except Exception:
                    try:
                        # Preload dialogs to ensure 777000 entity has valid access hash
                        await client.get_dialogs(limit=10)
                        msgs = await client.get_messages(777000, limit=5)
                    except Exception as e:
                        logger.debug(f"Failed to fetch 777000 messages directly: {e}")
                        try:
                            dialogs = await client.get_dialogs(limit=5)
                            for d in dialogs:
                                if getattr(d.entity, 'id', None) == 777000 or getattr(d, 'name', '') == 'Telegram':
                                    msgs = await client.get_messages(d.entity, limit=5)
                                    break
                        except Exception:
                            pass
                
                for m in msgs:
                    if hasattr(m, 'date') and m.date.timestamp() > start_time - 15:
                        if m.message:
                            code = extract_otp_from_text(m.message)
                            if code:
                                return code
        except Exception as e:
            logger.error(f"Error checking Telegram messages: {e}")

    # 2. Check LZT API if this is an LZT order
    if order.get('is_lzt') and order.get('item_id'):
        try:
            lzt_code = await lzt_client.get_otp_code(order['item_id'])
            if lzt_code:
                code = extract_otp_from_text(str(lzt_code)) or str(lzt_code).strip()
                if code and re.match(r"^\d{4,8}$", code):
                    return code
        except Exception as lzt_err:
            logger.debug(f"LZT get_otp_code error: {lzt_err}")

    return None

async def auto_otp_task(phone):
    if phone not in active_orders: return
    
    order = active_orders[phone]
    start_time = order['start_time']
    uid = order['uid']
    msg_id = order['msg_id']
    
    while time.time() - start_time < AUTO_CANCEL_SECONDS:
        if phone not in active_orders: return 
        try:
            code = await fetch_order_otp(order)
            
            if code:
                if not order['paid']:
                    async with get_user_lock(uid):
                        cur.execute("INSERT INTO orders (user_id, country, year, price, phone, otp) VALUES (?,?,?,?,?,?)", (uid, order['country'], order['year'], order['price'], phone, code))
                        if not order.get('is_lzt'):
                            cur.execute("DELETE FROM stock WHERE phone=?", (phone,))
                        db.commit()
                        order['paid'] = True
                        
                        from database import get_log_channels_db
                        from config import P_YES
                        for log_ch in get_log_channels_db():
                            try:
                                await bot.send_message(log_ch, f"{P_YES} <b>ACCOUNT SOLD</b>\n\n👤 <b>User:</b> <code>{uid}</code>\n📱 <b>Phone:</b> <code>+{phone}</code>\n💰 <b>Price:</b> ₹{order['price']}\n🌍 <b>Country:</b> {order['country']}")
                            except Exception as log_ex:
                                logger.error(f"Failed to log sale to {log_ch}: {log_ex}")
                
                twofa_text = f"{P_2FA} <b>2FA:</b> <code>{order['twofa']}</code>" if order['twofa'] != "None" else f"🔓 <b>2FA:</b> <code>Disabled (No Password)</code>"
                msg_text = (f"<blockquote>{PE_CHECK} <b>𝐋ᴀᴛᴇsᴛ 𝐎𝐓𝐏 𝐅ᴇᴛᴄʜᴇᴅ!</b>\n\n"
                            f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>+{phone}</code>\n"
                            f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                            f"{P_OTP} <b>𝐎𝐓𝐏:</b> <code><tg-spoiler>{code}</tg-spoiler></code>\n"
                            f"{twofa_text}</blockquote>")
                
                otp_btns = [
                    [style_btn("🔄 𝐆ᴇᴛ 𝐎𝐓𝐏 𝐀ɢᴀɪɴ", f"get_otp_again|{phone}", "primary", icon=5408995930416362034)],
                    [style_btn("✅ 𝐅ɪɴɪsʜ 𝐎ʀᴅᴇʀ", f"finish_order|{phone}", "success", icon=5409320020058584473)]
                ]
                try: await bot.edit_message(uid, msg_id, msg_text, buttons=otp_btns)
                except MessageNotModifiedError: pass
                return 
        except Exception as ex:
            logger.error(f"OTP fetch error for {phone}: {ex}")
        await asyncio.sleep(4) 
        
    if phone in active_orders and not active_orders[phone]['paid']:
        order = active_orders.pop(phone)
        try: await order['client'].disconnect()
        except Exception as exc: logger.warning("Order client disconnect failed after timeout: error_type=%s", type(exc).__name__)
        if not order.get('is_lzt'):
            try: persist_session(db.repository, order['session_id'], order['sess'], account_key=phone)
            except (OSError, ValueError, TimeoutError) as exc: logger.warning("Session save failed after timeout: error_type=%s", type(exc).__name__)
        
        # Local manual stock -> refund & restore stock
        if not order.get('is_lzt'):
            async with get_user_lock(uid):
                cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (order['price'], uid))
                cur.execute("UPDATE stock SET available=1 WHERE phone=?", (phone,))
                db.commit()
            try: await bot.edit_message(uid, msg_id, f"<blockquote>{P_TIME} <b>𝐎ʀᴅᴇʀ 𝐄xᴘɪʀᴇᴅ!</b>\n\n𝐓ʜᴇ 10-ᴍɪɴᴜᴛᴇ ʟɪᴍɪᴛ ғᴏʀ <code>+{phone}</code> ʀᴀɴ ᴏᴜᴛ. 𝐘ᴏᴜʀ ᴍᴏɴᴇʏ ({P_INR}{order['price']}) ʜᴀs ʙᴇᴇɴ ʀᴇғᴜɴᴅᴇᴅ.</blockquote>")
            except Exception as exc: logger.warning("Expired order notification failed: error_type=%s", type(exc).__name__)
        else:
            # LZT Panel purchased accounts -> do not auto-refund to prevent wallet drainage exploits
            try: await bot.edit_message(uid, msg_id, f"<blockquote>{P_TIME} <b>𝐒ᴇssɪᴏɴ 𝐓ɪᴍᴇᴏᴜᴛ</b>\n\n𝐓ʜᴇ 10-ᴍɪɴᴜᴛᴇ ʟɪsᴛᴇɴᴇʀ ғᴏʀ <code>+{phone}</code> ʜᴀs ᴇɴᴅᴇᴅ.\n𝐈ғ ʏᴏᴜ ɴᴇᴇᴅ ᴀssɪsᴛᴀɴᴄᴇ, ᴘʟᴇᴀsᴇ ᴄᴏɴᴛᴀᴄᴛ 𝐒ᴜᴘᴘᴏʀᴛ.</blockquote>")
            except Exception as exc: logger.warning("LZT timeout notification failed: error_type=%s", type(exc).__name__)

def register_buy(bot):
    @bot.on(events.NewMessage(pattern=r"(?i)^(🛒 𝐁ᴜʏ 𝐀ᴄᴄᴏᴜɴᴛ|🛒 Buy Account|📁 Buy Sessions)$"))
    async def msg_buy(e):
        await show_buy_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^(open_buy_categories|open_buy_menu|buy_categories)$"))
    async def cb_open_buy_categories(e):
        await show_buy_menu(e)

    @bot.on(events.CallbackQuery(pattern=b"^buy_back_main$"))
    async def cb_buy_back_main(e):
        await show_buy_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^by_years_menu(?:\|([^|]+))?$"))
    async def cb_by_years_menu(e):
        back_target = e.pattern_match.group(1)
        await show_years_catalog(e, back_target.decode() if back_target else "menu")

    @bot.on(events.CallbackQuery(pattern=r"^pg_filters\|(\d+)$"))
    async def cb_pg_filters(e):
        p = e.pattern_match
        page = int(p.group(1).decode())
        await show_filters_catalog(e, page)

    @bot.on(events.CallbackQuery(pattern=r"^c_by_yr\|(\d+)\|(\d+)(?:\|([^|]+))?$"))
    async def cb_c_by_yr(e):
        p = e.pattern_match
        year = int(p.group(1).decode())
        page = int(p.group(2).decode())
        back_target = p.group(3).decode() if p.group(3) else "menu"
        await show_countries_for_year(e, year, page, back_target)

    @bot.on(events.CallbackQuery(pattern=r"^bc\|([^|]+)\|([^|]+)(?:\|([^|]+))?$"))
    async def cb_bc(e):
        p = e.pattern_match
        back_target = p.group(3).decode() if p.group(3) else "menu"
        await show_years(e, p.group(1).decode(), p.group(2).decode(), back_target)

    @bot.on(events.CallbackQuery(pattern=r"^pg_c\|([^|]+)\|(\d+)(?:\|([^|]+))?$"))
    async def cb_pg_c(e):
        p = e.pattern_match
        back_target = p.group(3).decode() if p.group(3) else "menu"
        await show_countries(e, p.group(1).decode(), int(p.group(2).decode()), back_target)

    @bot.on(events.CallbackQuery(pattern=r"^by\|([^|]+)\|([^|]+)\|(\d+)\|(\d+)$"))
    async def cb_by_single(e):
        p = e.pattern_match
        await confirm_purchase(e, p.group(1).decode(), p.group(2).decode(), p.group(3).decode(), p.group(4).decode())
        
    @bot.on(events.CallbackQuery(pattern=r"^buy_cf\|([^|]+)\|([^|]+)\|(\d+)\|(\d+)$"))
    async def cb_buy_cf_4(e):
        p = e.pattern_match
        await process_purchase(e, p.group(1).decode(), p.group(2).decode(), p.group(3).decode(), p.group(4).decode())

    @bot.on(events.CallbackQuery(pattern=r"^cancel_order\|([^|]+)$"))
    async def cb_cancel_order(e):
        phone = e.pattern_match.group(1).decode()
        uid = e.sender_id
        from database import is_admin
        if not is_admin(uid):
            return await e.answer("🚫 Cancellation is disabled for members. Please complete your login or contact Support.", alert=True)
            
        if phone not in active_orders:
            return await e.answer("⚠️ Order already completed or expired.", alert=True)
            
        order = active_orders[phone]
        if order.get('paid'):
            return await e.answer("⚠️ OTP was already sent! Order is completed.", alert=True)
            
        # Admin cancel & refund
        active_orders.pop(phone)
        refund_amt = order['price']
        
        try:
            if 'client' in order and order['client']:
                await order['client'].disconnect()
        except: pass
        if not order.get('is_lzt'):
            try: persist_session(db.repository, order['session_id'], order['sess'], account_key=phone)
            except (OSError, ValueError, TimeoutError) as exc: logger.warning("Session save failed after cancel: error_type=%s", type(exc).__name__)
            
        async with get_user_lock(order['uid']):
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (refund_amt, order['uid']))
            if not order.get('is_lzt'):
                cur.execute("UPDATE stock SET available=1 WHERE phone=?", (phone,))
            db.commit()
            
        msg = f"<blockquote>{P_NO} <b>❌ 𝐎ʀᴅᴇʀ 𝐂ᴀɴᴄᴇʟʟᴇᴅ!</b>\n\n𝐘ᴏᴜʀ ᴍᴏɴᴇʏ (<b>{P_INR}{refund_amt}</b>) ʜᴀs ʙᴇᴇɴ <b>ɪɴsᴛᴀɴᴛʟʏ ʀᴇғᴜɴᴅᴇᴅ</b> ᴛᴏ ʏᴏᴜʀ ʙᴀʟᴀɴᴄᴇ.</blockquote>"
        await e.edit(msg, buttons=[[style_btn("🛒 𝐁ᴜʏ 𝐀ɢᴀɪɴ", "buy_back_main", "primary", icon=5408995930416362034)]])
        await e.answer("✅ Order Cancelled & Refunded!", alert=True)

    @bot.on(events.CallbackQuery(pattern=r"^get_otp_again\|([^|]+)$"))
    async def cb_get_otp_again(e):
        phone = e.pattern_match.group(1).decode()
        if phone not in active_orders: return await e.answer("⚠️ Session expired.", alert=True)
        await e.answer("🔄 Fetching latest OTP...")
        order = active_orders[phone]
        uid = order['uid']
        msg_id = order['msg_id']
        client = order.get('client')

        if not client and not order.get('is_lzt'):
            return await e.answer("⚠️ Client disconnected.", alert=True)

        try:
            code = await fetch_order_otp(order)
            if code:
                if not order['paid']:
                    order['paid'] = True
                    async with get_user_lock(uid):
                        cur.execute("INSERT INTO orders (user_id, country, year, price, phone, otp) VALUES (?,?,?,?,?,?)", (uid, order['country'], order['year'], order['price'], phone, code))
                        if not order.get('is_lzt'):
                            cur.execute("DELETE FROM stock WHERE phone=?", (phone,))
                        db.commit()
                twofa_text = f"{P_2FA} <b>2FA:</b> <code>{order['twofa']}</code>" if order['twofa'] != "None" else f"🔓 <b>2FA:</b> <code>Disabled (No Password)</code>"
                msg_text = (f"<blockquote>{PE_CHECK} <b>𝐋ᴀᴛᴇsᴛ 𝐎𝐓𝐏 𝐅ᴇᴛᴄʜᴇᴅ!</b>\n\n"
                            f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>+{phone}</code>\n"
                            f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                            f"{P_OTP} <b>𝐎𝐓𝐏:</b> <code><tg-spoiler>{code}</tg-spoiler></code>\n"
                            f"{twofa_text}</blockquote>")
                otp_btns = [
                    [style_btn("🔄 𝐆ᴇᴛ 𝐎𝐓𝐏 𝐀ɢᴀɪɴ", f"get_otp_again|{phone}", "primary", icon=5408995930416362034)],
                    [style_btn("✅ 𝐅ɪɴɪsʜ 𝐎ʀᴅᴇʀ", f"finish_order|{phone}", "success", icon=5409320020058584473)]
                ]
                try: await bot.edit_message(uid, msg_id, msg_text, buttons=otp_btns)
                except MessageNotModifiedError: pass
            else:
                await e.answer("⚠️ No new OTP found yet. Make sure you tapped Send Code in Telegram!", alert=True)
        except Exception as ex:
            logger.error(f"OTP fetch error for {phone}: {ex}")
            await e.answer("❌ Error fetching OTP.", alert=True)
        
    @bot.on(events.CallbackQuery(pattern=r"^(?:finish_order|logout_bot)\|([^|]+)$"))
    async def cb_finish_order(e):
        phone = e.pattern_match.group(1).decode()
        if phone in active_orders:
            order = active_orders.pop(phone)
            if 'client' in order and order['client']:
                try: await order['client'].disconnect()
                except: pass
            if not order.get('is_lzt') and 'sess' in order:
                try: persist_session(db.repository, order['session_id'], order['sess'], account_key=phone)
                except (OSError, ValueError, TimeoutError) as exc: logger.warning("Session save failed after order: error_type=%s", type(exc).__name__)
                for ext in ['.session', '.session-wal', '.session-shm', '.session-journal']:
                    if os.path.exists(order['sess'] + ext): os.remove(order['sess'] + ext)
            msg = f"<blockquote>{PE_CHECK} <b>🎉 𝐎ʀᴅᴇʀ 𝐂ᴏᴍᴘʟᴇᴛᴇᴅ!</b>\n\n𝐓ʜᴀɴᴋ ʏᴏᴜ ғᴏʀ ʏᴏᴜʀ ᴘᴜʀᴄʜᴀsᴇ. 𝐘ᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ɪs ʀᴇᴀᴅʏ ᴛᴏ ᴜsᴇ!</blockquote>"
            await e.edit(msg, buttons=[[style_btn("🛒 𝐁ᴜʏ 𝐀ɴᴏᴛʜᴇʀ 𝐀ᴄᴄᴏᴜɴᴛ", "buy_back_main", "primary", icon=5408995930416362034)]])
        else:
            await e.answer("✅ Order already completed.", alert=True)

    @bot.on(events.CallbackQuery(pattern=b"^search_country_btn$"))
    async def cb_search_country_btn(e):
        uid = e.sender_id
        search_state[uid] = True
        msg = (f"<blockquote>🔍 <b>𝐒ᴇᴀʀᴄʜ 𝐂ᴏᴜɴᴛʀʏ:</b>\n\n"
               f"𝐏ʟᴇᴀsᴇ ᴛʏᴘᴇ ᴛʜᴇ <b>𝐂ᴏᴜɴᴛʀʏ 𝐍ᴀᴍᴇ</b> (ᴇ.ɢ. <i>India, Brazil, Russia, France</i>) ᴏʀ <b>𝐃ɪᴀʟɪɴɢ 𝐂ᴏᴅᴇ</b> (ᴇ.ɢ. <i>+91, +55, +7</i>) ʙᴇʟᴏᴡ:</blockquote>")
        btns = [[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐌ᴇɴᴜ", b"buy_back_main", "danger", icon=6129812419028982717)]]
        try: await e.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in search_state and not e.text.startswith('/')))
    async def msg_search_country(e):
        import html
        uid = e.sender_id
        search_state.pop(uid, None)
        query = (e.text or "").strip()
        if not query: return
        
        matches = await search_countries_matching(query)
        if matches:
            btns = []
            for c_name, count in matches[:12]:
                flag = get_flag_by_country_name(c_name)
                cnt_str = f"({count})" if count else "(40+)"
                btns.append(style_btn(f"{flag} {c_name} {cnt_str}", f"bc|bulk|{c_name}", "primary", icon=6154249597532248059))
            
            f_btns = [btns[i:i+2] for i in range(0, len(btns), 2)]
            f_btns.append([
                style_btn("🔍 𝐒ᴇᴀʀᴄʜ 𝐀ɢᴀɪɴ", b"search_country_btn", "primary", icon=5409098988156629257),
                style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐌ᴇɴᴜ", b"buy_back_main", "danger", icon=6129812419028982717)
            ])
            msg = (f"<blockquote>🔍 <b>𝐒ᴇᴀʀᴄʜ 𝐑ᴇsᴜʟᴛs ғᴏʀ:</b> <code>{html.escape(query)}</code>\n\n"
                   f"𝐅ᴏᴜɴᴅ <b>{len(matches)}</b> ᴍᴀᴛᴄʜɪɴɢ ᴄᴏᴜɴᴛʀɪᴇs:</blockquote>")
            await e.reply(msg, buttons=f_btns)
        else:
            f_btns = [
                [style_btn("🔍 𝐒ᴇᴀʀᴄʜ 𝐀ɢᴀɪɴ", b"search_country_btn", "primary", icon=5409098988156629257)],
                [style_btn("🌍 𝐀ʟʟ 𝐂ᴏᴜɴᴛʀɪᴇs", b"pg_c|bulk|1", "primary", icon=6154249597532248059)],
                [style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐌ᴇɴᴜ", b"buy_back_main", "danger", icon=6129812419028982717)]
            ]
            msg = (f"<blockquote>❌ <b>𝐍ᴏ ᴄᴏᴜɴᴛʀɪᴇs ғᴏᴜɴᴅ ᴍᴀᴛᴄʜɪɴɢ:</b> <code>{html.escape(query)}</code>\n\n"
                   f"𝐏ʟᴇᴀsᴇ ᴄʜᴇᴄᴋ ᴛʜᴇ sᴘᴇʟʟɪɴɢ ᴏʀ ʙʀᴏᴡsᴇ <b>𝐀ʟʟ 𝐂ᴏᴜɴᴛʀɪᴇs</b>.</blockquote>")
            await e.reply(msg, buttons=f_btns)

    @bot.on(events.CallbackQuery(pattern=r"^chg_num\|([^|]+)$"))
    async def cb_chg_num(e):
        phone = e.pattern_match.group(1).decode()
        uid = e.sender_id
        if phone not in active_orders:
            return await e.answer("⚠️ Order expired or completed.", alert=True)
        order = active_orders[phone]
        if order['uid'] != uid:
            return await e.answer("🚫 Not your order!", alert=True)
        
        fee = get_change_number_fee()
        u_bal_row = cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        u_bal = u_bal_row[0] if u_bal_row else 0
        if fee > 0 and u_bal < fee:
            return await e.answer(f"❌ Insufficient Balance! Service Fee for Number Change is ₹{fee}. Your balance: ₹{u_bal}.", alert=True)
        
        change_number_state[uid] = {'stage': 'await_new_phone', 'phone': phone}
        fee_info = f"💰 <b>𝐒ᴇʀᴠɪᴄᴇ 𝐅ᴇᴇ:</b> <code>₹{fee}</code> <i>(Deducted upon successful migration)</i>\n\n" if fee > 0 else "🆓 <b>𝐒ᴇʀᴠɪᴄᴇ 𝐅ᴇᴇ:</b> <code>FREE</code>\n\n"
        msg = (f"<blockquote>🔄 <b>𝐂ʜᴀɴɢᴇ 𝐀ᴄᴄᴏᴜɴᴛ 𝐏ʜᴏɴᴇ 𝐍ᴜᴍʙᴇʀ</b>\n\n"
               f"📱 <b>𝐂ᴜʀʀᴇɴᴛ 𝐍ᴜᴍʙᴇʀ:</b> <code>+{phone}</code>\n"
               f"{fee_info}"
               f"🔻 <b>𝐈ɴsᴛʀᴜᴄᴛɪᴏɴs:</b>\n"
               f"𝐏ʟᴇᴀsᴇ sᴇɴᴅ ʏᴏᴜʀ <b>𝐍ᴇᴡ 𝐏ʜᴏɴᴇ 𝐍ᴜᴍʙᴇʀ</b> ɪɴ ɪɴᴛᴇʀɴᴀᴛɪᴏɴᴀʟ ғᴏʀᴍᴀᴛ ʙᴇʟᴏᴡ:\n"
               f"<i>𝐄xᴀᴍᴘʟᴇ: <code>+919876543210</code> ᴏʀ <code>+14155552671</code></i>\n\n"
               f"⚠️ <i>𝐍ᴏᴛᴇ: 𝐌ᴀᴋᴇ sᴜʀᴇ ʏᴏᴜʀ ɴᴇᴡ ɴᴜᴍʙᴇʀ ᴅᴏᴇs ɴᴏᴛ ᴀʟʀᴇᴀᴅʏ ʜᴀᴠᴇ ᴀ 𝐓ᴇʟᴇɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛ!</i></blockquote>")
        btns = [[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{phone}", "danger", icon=6129812419028982717)]]
        try: await e.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass

    @bot.on(events.CallbackQuery(pattern=r"^back_to_order\|([^|]+)$"))
    async def cb_back_to_order(e):
        phone = e.pattern_match.group(1).decode()
        uid = e.sender_id
        change_number_state.pop(uid, None)
        if phone not in active_orders:
            return await e.answer("⚠️ Order expired or completed.", alert=True)
        order = active_orders[phone]
        from database import is_admin
        msg, btns = get_active_order_card(order, phone, is_admin(uid))
        try: await e.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in change_number_state and not e.text.startswith('/')))
    async def msg_change_number(e):
        uid = e.sender_id
        state = change_number_state.get(uid)
        if not state: return
        
        stage = state.get('stage')
        
        if stage == 'await_new_phone':
            orig_phone = state['phone']
            if orig_phone not in active_orders:
                change_number_state.pop(uid, None)
                return await e.reply("⚠️ Order expired or completed.")
                
            new_phone = re.sub(r"[^\d+]", "", (e.text or "").strip())
            if not new_phone.startswith('+'):
                new_phone = '+' + new_phone
                
            if len(new_phone) < 8 or len(new_phone) > 17:
                return await e.reply(f"{P_NO} <b>Invalid phone number format!</b>\nPlease send in international format, e.g. <code>+919876543210</code>")
                
            order = active_orders[orig_phone]
            client = order['client']
            loading = await e.reply("⏳ <i>Sending verification code to your new number...</i>")
            
            try:
                sent_res = await client(SendChangePhoneCodeRequest(phone_number=new_phone))
                change_number_state[uid] = {
                    'stage': 'await_otp',
                    'orig_phone': orig_phone,
                    'new_phone': new_phone,
                    'phone_code_hash': sent_res.phone_code_hash
                }
                msg = (f"<blockquote>📩 <b>𝐕ᴇʀɪғɪᴄᴀᴛɪᴏɴ 𝐂ᴏᴅᴇ 𝐒ᴇɴᴛ!</b>\n\n"
                       f"📱 <b>𝐍ᴇᴡ 𝐍ᴜᴍʙᴇʀ:</b> <code>{new_phone}</code>\n\n"
                       f"<i>𝐀 ᴠᴇʀɪғɪᴄᴀᴛɪᴏɴ ᴄᴏᴅᴇ (𝐎𝐓𝐏) ʜᴀs ʙᴇᴇɴ sᴇɴᴛ ᴛᴏ ʏᴏᴜʀ ɴᴇᴡ ɴᴜᴍʙᴇʀ ᴠɪᴀ 𝐒𝐌𝐒 / 𝐓ᴇʟᴇɢʀᴀᴍ.</i>\n\n"
                       f"👉 <b>𝐏ʟᴇᴀsᴇ ᴛʏᴘᴇ ᴀɴᴅ sᴇɴᴅ ᴛʜᴇ 𝐎𝐓𝐏 ᴄᴏᴅᴇ ʜᴇʀᴇ ɴᴏᴡ:</b></blockquote>")
                btns = [[style_btn("❌ 𝐂ᴀɴᴄᴇʟ 𝐍ᴜᴍʙᴇʀ 𝐂ʜᴀɴɢᴇ", f"back_to_order|{orig_phone}", "danger", icon=6129888444245089008)]]
                await loading.edit(msg, buttons=btns)
            except PhoneNumberOccupiedError:
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐍ᴜᴍʙᴇʀ 𝐀ʟʀᴇᴀᴅʏ 𝐔sᴇᴅ!</b>\n\n𝐓ʜᴇ ɴᴜᴍʙᴇʀ <code>{new_phone}</code> ɪs ᴀʟʀᴇᴀᴅʏ ʀᴇɢɪsᴛᴇʀᴇᴅ ᴏɴ 𝐓ᴇʟᴇɢʀᴀᴍ.\n𝐏ʟᴇᴀsᴇ ᴜsᴇ ᴀ ɴᴜᴍʙᴇʀ ᴛʜᴀᴛ ᴅᴏᴇs ɴᴏᴛ ʜᴀᴠᴇ ᴀ 𝐓ᴇʟᴇɢʀᴀᴍ ᴀᴄᴄᴏᴜɴᴛ, ᴏʀ ᴅᴇʟᴇᴛᴇ ᴛʜᴀᴛ ᴀᴄᴄᴏᴜɴᴛ ғɪʀsᴛ.</blockquote>", buttons=[[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{orig_phone}", "primary", icon=6129812419028982717)]])
            except FreshChangePhoneForbiddenError:
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐂ᴀɴɴᴏᴛ 𝐂ʜᴀɴɢᴇ 𝐍ᴜᴍʙᴇʀ 𝐘ᴇᴛ</b>\n\n𝐓ᴇʟᴇɢʀᴀᴍ sᴇᴄᴜʀɪᴛʏ ʀᴇǫᴜɪʀᴇs ɴᴇᴡ 𝐬𝐞𝐬𝐬𝐢𝐨𝐧𝐬 ᴛᴏ ᴡᴀɪᴛ ʙᴇғᴏʀᴇ ᴄʜᴀɴɢɪɴɢ ɴᴜᴍʙᴇʀs.\n𝐏ʟᴇᴀsᴇ ʟᴏɢɪɴ ᴠɪᴀ ᴛʜᴇ ɴᴏʀᴍᴀʟ 𝐎𝐓𝐏 ғɪʀsᴛ.</blockquote>", buttons=[[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{orig_phone}", "primary", icon=6129812419028982717)]])
            except Exception as ex:
                logger.error(f"SendChangePhoneCode error: {ex}")
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐄ʀʀᴏʀ:</b> {str(ex)}</blockquote>", buttons=[[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{orig_phone}", "primary", icon=6129812419028982717)]])

        elif stage == 'await_otp':
            orig_phone = state['orig_phone']
            new_phone = state['new_phone']
            phone_code_hash = state['phone_code_hash']
            
            if orig_phone not in active_orders:
                change_number_state.pop(uid, None)
                return await e.reply("⚠️ Order expired or completed.")
                
            otp_code = re.sub(r"\D", "", (e.text or "").strip())
            if not otp_code or len(otp_code) < 4:
                return await e.reply(f"{P_NO} <b>Invalid OTP!</b> Please send the numeric code received on your phone.")
                
            order = active_orders[orig_phone]
            client = order['client']
            loading = await e.reply("⏳ <i>Verifying code and changing phone number...</i>")
            
            try:
                await client(ChangePhoneRequest(
                    phone_number=new_phone,
                    phone_code_hash=phone_code_hash,
                    phone_code=otp_code
                ))
                
                change_number_state.pop(uid, None)
                order['paid'] = True
                fee = get_change_number_fee()
                
                async with get_user_lock(uid):
                    if fee > 0:
                        cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=? AND balance >= ?", (fee, uid, fee))
                    cur.execute("INSERT INTO orders (user_id, country, year, price, phone, otp) VALUES (?,?,?,?,?,?)", 
                                (uid, order['country'], order['year'], order['price'] + fee, new_phone, f"Migrated from {orig_phone} (Fee: ₹{fee})"))
                    cur.execute("DELETE FROM stock WHERE phone=?", (orig_phone,))
                    db.commit()
                    
                    from database import get_log_channels_db
                    for ch in get_log_channels_db():
                        try:
                            admin_log = (f"<blockquote><b>🛍️ 𝐍ᴇᴡ 𝐏ᴜʀᴄʜᴀsᴇ (𝐍ᴜᴍʙᴇʀ 𝐌ɪɢʀᴀᴛᴇᴅ)</b>\n\n"
                                         f"👤 <b>𝐔sᴇᴅ 𝐁ʏ:</b> <code>{uid}</code>\n"
                                         f"📱 <b>𝐎ʀɪɢɪɴᴀʟ:</b> <code>+{orig_phone}</code>\n"
                                         f"🔄 <b>𝐍ᴇᴡ 𝐍ᴜᴍʙᴇʀ:</b> <code>{new_phone}</code>\n"
                                         f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                                         f"💰 <b>𝐂ʜᴀɴɢᴇ 𝐅ᴇᴇ:</b> {P_INR}{fee}\n"
                                         f"{P_MONEY} <b>𝐓ᴏᴛᴀʟ 𝐏ʀɪᴄᴇ:</b> {P_INR}{order['price'] + fee}</blockquote>")
                            await bot.send_message(ch, admin_log)
                        except: pass
                
                try: await client.disconnect()
                except: pass
                if not order.get('is_lzt'):
                    try: persist_session(db.repository, order['session_id'], order['sess'], account_key=orig_phone)
                    except (OSError, ValueError, TimeoutError) as exc: logger.warning("Session save failed after number change: error_type=%s", type(exc).__name__)
                active_orders.pop(orig_phone, None)
                
                fee_note = f"\n💰 <b>𝐒ᴇʀᴠɪᴄᴇ 𝐅ᴇᴇ 𝐂ʜᴀʀɢᴇᴅ:</b> <code>{P_INR}{fee}</code>" if fee > 0 else ""
                success_msg = (
                    f"<blockquote>{PE_GIFT} <b>🎉 𝐏ʜᴏɴᴇ 𝐍ᴜᴍʙᴇʀ 𝐂ʜᴀɴɢᴇᴅ 𝐒ᴜᴄᴄᴇssғᴜʟʟʏ!</b>\n\n"
                    f"📱 <b>𝐍ᴇᴡ 𝐏ʜᴏɴᴇ:</b> <code>{new_phone}</code>\n"
                    f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                    f"📆 <b>𝐀ᴄᴄᴏᴜɴᴛ 𝐘ᴇᴀʀ:</b> <b>{order['year']}</b>\n"
                    f"🔐 <b>2𝐅𝐀 𝐏ᴀssᴡᴏʀᴅ:</b> <code>{order['twofa']}</code>{fee_note}\n\n"
                    f"✅ <b>𝐓ʜᴇ ᴀᴄᴄᴏᴜɴᴛ ɪs ɴᴏᴡ 100% ᴍɪɢʀᴀᴛᴇᴅ ᴛᴏ ʏᴏᴜʀ ɴᴇᴡ ɴᴜᴍʙᴇʀ!</b>\n"
                    f"𝐘ᴏᴜ ᴄᴀɴ ɴᴏᴡ ʟᴏɢɪɴ ᴅɪʀᴇᴄᴛʟʏ ᴜsɪɴɢ ʏᴏᴜʀ ᴏᴡɴ ɴᴜᴍʙᴇʀ (<code>{new_phone}</code>).</blockquote>"
                )
                await loading.edit(success_msg, buttons=[[style_btn("🛒 𝐁ᴜʏ 𝐀ɴᴏᴛʜᴇʀ 𝐀ᴄᴄᴏᴜɴᴛ", "buy_back_main", "primary", icon=5408995930416362034)]])
            except PhoneCodeInvalidError:
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐈ɴᴠᴀʟɪᴅ 𝐎𝐓𝐏 𝐂ᴏᴅᴇ!</b>\n𝐏ʟᴇᴀsᴇ ᴄʜᴇᴄᴋ ᴛʜᴇ ᴄᴏᴅᴇ ᴀɴᴅ sᴇɴᴅ ɪᴛ ᴀɢᴀɪɴ.</blockquote>")
            except PhoneCodeExpiredError:
                change_number_state.pop(uid, None)
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐎𝐓𝐏 𝐄xᴘɪʀᴇᴅ!</b>\n𝐏ʟᴇᴀsᴇ ʀᴇᴛʀʏ ᴄʜᴀɴɢɪɴɢ ɴᴜᴍʙᴇʀ.</blockquote>", buttons=[[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{orig_phone}", "primary", icon=6129812419028982717)]])
            except Exception as ex:
                logger.error(f"ChangePhone error: {ex}")
                await loading.edit(f"<blockquote>{P_NO} <b>❌ 𝐄ʀʀᴏʀ:</b> {str(ex)}</blockquote>", buttons=[[style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐎ʀᴅᴇʀ", f"back_to_order|{orig_phone}", "primary", icon=6129812419028982717)]])
