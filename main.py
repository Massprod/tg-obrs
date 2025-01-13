import os
import json
import base64
import asyncio
from loguru import logger
from dotenv import load_dotenv
from redis.asyncio import Redis
from aiohttp import ClientSession
from datetime import datetime, timedelta
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# region About
about_message: str = "Бот используется для отображения `Обращение` из 1С.\n" \
                     "Сообщения в группе, привязаны к `Обращение` из базы 1С.\n" \
                     "Добавляемые комментарии и изменение статуса вносят изменения" \
                     " в привязанное `Обращение` в системе 1С.\n" \
                     "Все поступаемые в группу сообщения должны быть либо инициированы" \
                     " нажатием кнопки отправки в 1С, либо запрошены из личного сообщения поиска (/search).\n" \
                     "/search <- для использования необходимо взаимодействовать с ботом (отправить сообщение)\n" \
                     "Команда создаёт личное сообщение для ициатора, с возможностью поиска" \
                     " всех `Обращение` представленных в системе 1С. " \
                     "Это сообщение позволяет видеть данные обращений и запросить создание сообщения `Обращение` в группу\n" \
                     "/cancel_search <- личное сообщение созданое /search имеет привязку к пользователю с одной копией" \
                     " поэтому для его отвязки, можно либо удалить сообщение стандартными средствами ТГ, либо использовать эту команду"
# endregion About

# region Logs
log_dir: str = os.getenv('LOGS_FLD', './logs')
os.makedirs(log_dir, exist_ok=True)
logger.add(
    os.path.join(log_dir, 'log.log'),
    rotation='50 MB',
    retention='14 days',
    compression='zip',
    backtrace=True,
    diagnose=True,
)
# endregion Logs

# region Env
logger.info(
    'Loading environment variables'
)
load_dotenv('.env')
# TG-bot
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNERS = set([int(owner_id) for owner_id in os.getenv("BOT_OWNERS").split(';') if owner_id])
logger.info(
    f'Currently set BotOwners: {BOT_OWNERS}'
)
API_URL = os.getenv("API_URL")
TASK_ENDPOINT = os.getenv("E_TASK")
TASKS_ENDPOINT = os.getenv("E_TASKS")
GROUP_ID = os.getenv("MAIN_GROUP")
# 1C API
S_USERNAME = os.getenv("API_USERNAME")
S_PWD = os.getenv("API_PWD")
# Deletes
COMMENT_MSG_DEL_DELAY = int(os.getenv("DELETE_DELAY"))
NOTIF_DELETE_DELAY = int(os.getenv("NOTIF_DELETE_DELAY"))
# Redis
REDIS_HOST: str = os.getenv("REDIS_HOST")
REDIS_PORT: str = os.getenv("REDIS_PORT")
REDIS_N_DB: str = os.getenv("REDIS_N_DB")
REDIS_PWD: str = os.getenv("REDIS_PWD")
REDIS_W_INPUT_NAME: str = os.getenv("REDIS_W_INPUT_NAME")
REDIS_USER_TAG: str = os.getenv("REDIS_USER_TAG")
# endregion Env

# region Redis
REDIS: Redis | None = None


def init_redis() -> Redis:
    global REDIS
    con_url: str = f'redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_N_DB}'
    logger.info(
        f'Connecting to Redis: {con_url}'
    )
    REDIS = Redis.from_url(
        url=con_url,
        # password=REDIS_PWD,  don't have it set for now
        decode_responses=True
    )
    logger.info(
        f'Connected to Redis: {con_url}'
    )


def close_redis() -> None:
    global REDIS
    try:
        REDIS.close()
        logger.info("Redis connection closed.")
    except Exception as e:
        logger.warning(f"Failed to close Redis connection: {e}")



async def initial_psm_state() -> dict:
    init_state: dict = {
        'page': 1,              # <- current data page
        'perPage': 5,           # <- records per page
        'sortBy': 'Номер',      # <- records sorted by this element 
        'ascending': True,      # <- records sorted in this direction
        'searchBy': {
            'number': {         # <- records filtered by `Номер`
                'value': '',
                'listening': False
            },
            'status': {         # <- records filtered by `Состояние`
                'value': '',
                'listening': False
            },
            'periodStart': {    # <- records filtered by dates => `year.month.day`
                'value': '',    # <- `S` == separator
                'listening': False
            },  
            'periodEnd': {      # ^same
                'value': '',
                'listening': False
            }
        },
        'searchListen': '',  # <- listeting for user input for `searchBy`
        'openSearch': False,
    }
    return init_state
# endregion Redis


sort_map: dict[str, str] = {
    'date': 'Дата',
    'status': 'Состояние',
    'number': 'Номер'
}

rev_sort_map: dict[str, str] = {
    ru: eng for ru, eng in sort_map.items()
}

search_by_trans: dict[str, str] = {
    'number': 'Номер',
}

# region Utility
# region RedisRel
async def cr_user_w_string(
    user_id: int
) -> str:
    return f'{REDIS_W_INPUT_NAME}:{user_id}'

async def cr_user_k_string(
    user_id: int
) -> str:
    return f'{REDIS_USER_TAG}:{user_id}'


async def add_to_waiting_input(
    redis_client: Redis,
    user_id: int,
    task_id: int,
) -> None:
    await redis_client.set(f'{REDIS_W_INPUT_NAME}:{user_id}', task_id)


async def delete_from_waiting_input(
    redis_client: Redis,
    user_id: int,
) -> None:
    await redis_client.delete(f'{REDIS_W_INPUT_NAME}:{user_id}')


async def get_awaited_user(
    redis_client: Redis,
    user_id: int,
) -> str:
    return await redis_client.get(f'{REDIS_W_INPUT_NAME}:{user_id}')
# endregion RedisRel

async def lstrip_min_length(inp_str: str, min_length: int, symbol: str = '0') -> str:
    slice_index: int = 0
    while (symbol == inp_str[slice_index]
            and min_length < (len(inp_str) - slice_index)):
        slice_index += 1
    return inp_str[slice_index:]


async def get_fio(user: dict) -> str:
    name: str
    surname: str
    parenty: str
    if user['Имя'] and user['Отчество'] and user['Фамилия']:
        name = user['Имя']
        surname = user['Фамилия']
        parenty = user['Отчество']
    else:
        string_name: str = user['Наименование']
        full_name: list[str] = string_name.split(' ')
        if len(full_name) != 3:
            return 'Не указано'
        name, surname, parenty = full_name[1], full_name[0], full_name[2]
    return f'{parenty} {name[0].capitalize()}.{surname[0].capitalize()}'


async def escape_special(inp_str: str) -> str:
    special_chars: set[str] = set('_*[]()~`>#+-=|{}.!')
    escaped_chars: list[str] = []
    for char in inp_str:
        escaped_chars.append(
            char if char not in special_chars else f'\\{char}'
        )
    return ''.join(escaped_chars)


async def delete_msg(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        delay: int,
) -> None:
    await asyncio.sleep(delay)
    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
# endregion

# region AIOHttp
session = None


async def make_patch_request(
    url: str,
    payload: str,
    handle_errors: bool = True,
    close_connection: bool = True,    
):
    global session
    if not session or session.closed:
        session = ClientSession()
    # We're only using this request to our set API, so leaving it like this is fine.
    auth_str = f'{S_USERNAME}:{S_PWD}'
    auth_base: str = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f"Basic {auth_base}"
    }
    if close_connection:
        async with session.patch(url, json=payload, headers=headers) as response:
            if not handle_errors:
                return response
            if response.status == 200:
                return response
            else:
                return {"error": f"Failed with status {response.status}"}
    else:
        response = await session.patch(url, json=payload, headers=headers)
        if not handle_errors:
            return response
        if response.status == 200:
            return response
        else:
            return {"error": f"Failed with status {response.status}"}


async def make_post_request(
    url: str,
    payload: str,
    handle_errors: bool = True,
    close_connection: bool = True,
):
    global session
    if not session or session.closed:
        session = ClientSession()
    # We're only using this request to our set API, so leaving it like this is fine.
    auth_str = f'{S_USERNAME}:{S_PWD}'
    auth_base: str = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f"Basic {auth_base}"
    }
    if close_connection:
        async with session.post(url, json=payload, headers=headers) as response:
            if not handle_errors:
                return response
            if response.status == 200:
                return response
            else:
                return {"error": f"Failed with status {response.status}"}
    else:
        response = await session.post(url, json=payload, headers=headers)
        if not handle_errors:
            return response
        if response.status == 200:
            return response
        else:
            return {"error": f"Failed with status {response.status}"}


async def make_put_request(
    url: str,
    payload: str,
    handle_errors: bool = True,
    close_connection: bool = True,
):
    global session
    if not session or session.closed:
        session = ClientSession()
    # We're only using this request to our set API, so leaving it like this is fine.
    auth_str = f'{S_USERNAME}:{S_PWD}'
    auth_base: str = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f"Basic {auth_base}"
    }
    if close_connection:
        async with session.put(url, json=payload, headers=headers) as response:
            if not handle_errors:
                return response
            if response.status == 200:
                return response
            else:
                return {"error": f"Failed with status {response.status}"}
    else:
        response = await session.put(url, json=payload, headers=headers)
        if not handle_errors:
            return response
        if response.status == 200:
            return response
        else:
            return {"error": f"Failed with status {response.status}"}


async def make_get_request(
    url: str,
    handle_errors: bool = True,
    close_connection: bool = True
):
    global session
    if not session or session.closed:
        session = ClientSession()
    # We're only using this request to our set API, so leaving it like this is fine.
    auth_str = f'{S_USERNAME}:{S_PWD}'
    auth_base: str = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f"Basic {auth_base}"
    }
    if close_connection:
        async with session.get(url, headers=headers) as response:
            if not handle_errors:
                return response
            if response.status == 200:
                return response
            else:
                return {"error": f"Failed with status {response.status}"}
    else:
        response = await session.get(url, headers=headers)
        if not handle_errors:
            return response
        if response.status == 200:
            return response
        else:
            return {"error": f"Failed with status {response.status}"}
        
# endregion AIOHttp

# region GroupHandlers
async def handle_empty_comment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    cur_bot = context.bot
    callback_query = update.callback_query
    
    clicked_by = callback_query.from_user
    author = clicked_by.username
    button_data = callback_query.data
    task_id = button_data.split('_')[1]
    await callback_query.answer(
        f'Отправка комментария к заявке: {task_id}'
    )

    req_url = f'{API_URL}/{TASK_ENDPOINT}'
    empt_msg = 'Нет картриджа'
    payload = {
        'author': author,
        'taskId': task_id,
        'comment': empt_msg,
        'botToken': BOT_TOKEN,
        'chatId': GROUP_ID,
    }
    response = await make_patch_request(
        req_url, payload, False, False
    )
    if 200 == response.status:
        return
    chat_id = callback_query.message.chat.id
    await cur_bot.send_message(
        chat_id=chat_id,
        text=f'Ошибка запроса:\n {response.text}'
    )

    response.close()


async def handle_std_comment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    callback_query = update.callback_query
    clicked_by = callback_query.from_user
    user_id: int = clicked_by.id

    task_id = callback_query.data.split("_")[1]
    _a_tasks = []
    _a_tasks.append(
        callback_query.answer(
            'Ваше следующее сообщение будет добавлено как комментарий'
        )
    )
    _a_tasks.append(
        add_to_waiting_input(
            REDIS, user_id, task_id
        )
    )
    asyncio.gather(*_a_tasks)


async def handle_awaited_comment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.message.from_user
    user_id: int = user.id
    
    task_id: str = await get_awaited_user(REDIS, user_id)
    if not task_id:
        return
    comment = update.message.text
    req_url = f'{API_URL}/{TASK_ENDPOINT}'
    payload = {
        'author': user.username,
        'taskId': task_id,
        'comment': comment,
        'botToken': BOT_TOKEN,
        'chatId': GROUP_ID,
    }
    response = await make_patch_request(
        req_url, payload, False, False
    )
    resp_status = response.status
    response.close()
    chat_id = update.message.chat_id
    message_id = update.message.id
    if 200 != resp_status:
        warn_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f'*{user.username}* - ошибка при добавлении комментария\\nПовторите ввод.',
            parse_mode='MarkdownV2',
        )
        asyncio.create_task(
            delete_msg(context, chat_id, warn_msg, COMMENT_MSG_DEL_DELAY)
        )
        return
    chat_id = update.message.chat_id
    message_id = update.message.id
    asyncio.create_task(
        delete_msg(context, chat_id, message_id, COMMENT_MSG_DEL_DELAY)
    )
    await delete_from_waiting_input(REDIS, user_id)


async def handle_close_req(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    callback_query = update.callback_query
    user = callback_query.from_user
    task_id = callback_query.data.split('_')[1]
    author = user.username

    req_url = f'{API_URL}/{TASK_ENDPOINT}'
    payload = {
        'author': author,
        'taskId': task_id,
        'botToken': BOT_TOKEN,
        'chatId': GROUP_ID,
    }
    response = await make_post_request(
        req_url, payload, False, True
    )
# endregion GroupHandlers

# region PrivateSearchMessageHandlers
async def delete_psm_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    callback = update.callback_query
    user_data = update.message.from_user
    user_id = user_data.id
    
    user_state_json: str = await REDIS.get(f'{REDIS_USER_TAG}:{user_id}')
    user_state: dict = json.loads(user_state_json)
    if not user_state:
        return
    psm_message_id = user_state.get('psm_message_id')
    if not psm_message_id:
        callback.answer('Нет привязанного сообщения поиска')
        return
    asyncio.create_task(
        delete_msg(context,user_id, psm_message_id, 0)
    )
    asyncio.create_task(
        delete_msg(context, update.message.chat_id, update.message.message_id, 0)
    )
    user_state['psm_message_id'] = None
    await REDIS.set(f'{REDIS_USER_TAG}:{user_id}', json.dumps(user_state))


async def send_or_edit_psm_message(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_state: dict,
) -> None:
    # region QueryArgs
    page: int = user_state['page']
    per_page: int = user_state['perPage']
    sort_by: str = user_state['sortBy']
    ascending: int = 1 if user_state['ascending'] else 0
    search_by: dict[str, str] = user_state['searchBy']
    # endregion QueryArgs
    get_url: str = f'{API_URL}/{TASKS_ENDPOINT}?'
    query_args: str = f'page={page}&perPage={per_page}&sortBy={sort_by}' \
                      f'&ascending={ascending}'
    for arg, data in search_by.items():
        if data['value']:
            query_args += f'&{arg}={data['value']}'
    get_url = get_url + query_args
    response = await make_get_request(
        get_url, True, False
    )
    data: dict = await response.json()
    resp_status = response.status
    resp_text = response.text
    response.close()
    if 200 != resp_status:
        error_msg: str = f'Error while getting `psm` data:\n {resp_text}'
        logger.error(error_msg)
        err_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=error_msg
        )
        asyncio.create_task(
            delete_msg(context, chat_id, err_msg.message_id, 10)
        )
        return
    total_records: int = int(data['total_records'])
    total_pages: int = (total_records + per_page - 1) // per_page
    page = max(min(total_pages, page), 1)
    user_state['page'] = page
    tasks_data: list[dict] = data['data']
    # Task button -> get mesage with an extra info on task
    task_buttons = [
        [
            InlineKeyboardButton(
                text=f'# {(await lstrip_min_length(task['Номер'], 5)).rstrip()} | '\
                     f'{task['Дата']} | {task['Статус'] or 'НетСтатуса'}',
                callback_data=f'psm_einfo_{task['Номер']}'
            )
        ]
        for task in tasks_data
    ]
    if not task_buttons:
        task_buttons.append(
            [
                InlineKeyboardButton(
                    text=f'Нет данных.', callback_data='none'
                )
            ]
        )
    # region Navigation
    navigation_buttons= [[]]
    if 1 < page:
        navigation_buttons[0].append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"psm_page_prev_{total_pages}")
        )
    if page < total_pages:
        navigation_buttons[0].append(
            InlineKeyboardButton("Next ➡️", callback_data=f"psm_page_next_{total_pages}")
        )
    # endregion Navigation
    settings = []
    # region sortBy
    # Cringe with sort checks -> rebuild late
    sorting_row = [
        InlineKeyboardButton(
            text=f'Номер {'⬆️' if user_state['sortBy'] == 'Номер' and ascending else ('⬇️' if user_state['sortBy'] == 'Номер' else '')}',
            callback_data='psm_sortby_number',
        ),
        InlineKeyboardButton(
            text=f'Дата {'⬆️' if user_state['sortBy'] == 'Дата' and ascending else ('⬇️' if user_state['sortBy'] == 'Дата' else '')}',
            callback_data='psm_sortby_date',
        ),
        InlineKeyboardButton(
            text=f'Статус {'⬆️' if user_state['sortBy'] == 'Состояние' and ascending else ('⬇️' if user_state['sortBy'] == 'Состояние' else '')}',
            callback_data='psm_sortby_status',
        )
    ]
    settings.append(sorting_row)
    # endregion sortBy
    # region searchBy
    # TODO: add option to show and hide `searchBy` buttons.
    #  We need to show/hide them + we need to add some separators.
    if not user_state['openSearch']:
        search_by_show_but = InlineKeyboardButton(
            '📋 Показать данные отбора', callback_data='psm_search_show'
        )
        settings.append([search_by_show_but])
    else:
        search_by_opener = InlineKeyboardButton(
            '📋 Спрятать данные отбора', callback_data='psm_search_show'
        )
        settings.append([search_by_opener])
        # region `number`
        search_by_number = search_by['number']
        if not search_by_number['value']:
            search_but_number = InlineKeyboardButton(
                '🔍 Установить отбор по `Номер`', callback_data='psm_search_number',
            )
        else:
            search_but_number = InlineKeyboardButton(
                f'🔄 Убрать отбор `Номер`: {search_by_number['value']}', callback_data='psm_reset_number'
            )
        settings.append([search_but_number])
        # endregion `number`
        # region `status`
        search_by_status = search_by['status']
        if not search_by_status['listening']:
            search_but_status =InlineKeyboardButton(
                '🔍 Установить отбор по `Состояние`', callback_data='psm_search_status',
            )
            settings.append([search_but_status])
        else:
            search_but_status = [[]]
            complete_str: str = 'Исполнено' if 'complete' != search_by_status['value'] else '✔️ Исполнено'
            complete_but = InlineKeyboardButton(
                complete_str, callback_data='psm_search_set_status_complete',
            )
            search_but_status[0].append(complete_but)

            active_str: str = 'Активна' if 'active' != search_by_status['value'] else '✔️ Активна'
            active_but = InlineKeyboardButton(
                active_str, callback_data='psm_search_set_status_active',
            )
            search_but_status[0].append(active_but)

            canceled_str: str = 'Отменена' if 'canceled' != search_by_status['value'] else '✔️ Отменена'
            canceled_but = InlineKeyboardButton(
                canceled_str, callback_data='psm_search_set_status_canceled',
            )
            search_but_status[0].append(canceled_but)
            settings.append(search_but_status[0])

            cancel_status_search_but = InlineKeyboardButton(
                '❌ Отменить отбор по `Состояние`', callback_data='psm_search_unset_status'
            )
            settings.append([cancel_status_search_but])
        # endregion `status`
        # region `periodStart`
        search_by_period_start = search_by['periodStart']
        if not search_by_period_start['listening']:
            period_start_init_but = InlineKeyboardButton(
                '🔍 Установить `Начало периода`', callback_data='psm_search_periodStart'
            )
            settings.append([period_start_init_but])
        else:
            period_start: str = user_state['searchBy']['periodStart']['value']
            if not period_start:
                # TODO: .env variable for starting shift?
                cur_date = datetime.now() - timedelta(days=7)
                cur_period = f'{cur_date.year}S{cur_date.month}S{cur_date.day}'
                user_state['searchBy']['periodStart']['value'] = cur_period
            start_year, start_month, start_day = user_state['searchBy']['periodStart']['value'].split('S')
            period_start_row = []
            period_start_header = InlineKeyboardButton(
                '🕒 Начало периода', callback_data='none'
            )
            period_start_row.append([period_start_header])

            year_row = []
            period_start_year_show = InlineKeyboardButton(
                f'Год: {start_year}', callback_data='none',
            )
            year_row.append(period_start_year_show)
            period_start_year_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodStart_increase_year',
            )
            year_row.append(period_start_year_increase)
            period_start_year_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodStart_decrease_year',
            )
            year_row.append(period_start_year_decrease)
            period_start_row.append(year_row)
            
            month_row = []
            period_start_month_show = InlineKeyboardButton(
                f'Месяц: {start_month}', callback_data='none',
            )
            month_row.append(period_start_month_show)
            period_start_month_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodStart_increase_month',
            )
            month_row.append(period_start_month_increase)
            period_start_month_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodStart_decrease_month',                
            )
            month_row.append(period_start_month_decrease)
            period_start_row.append(month_row)

            day_row = []
            period_start_day_show = InlineKeyboardButton(
                f'День: {start_day}', callback_data='none',
            )
            day_row.append(period_start_day_show)
            period_start_day_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodStart_increase_day',
            )
            day_row.append(period_start_day_increase)
            period_start_day_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodStart_decrease_day',                
            )
            day_row.append(period_start_day_decrease)
            period_start_row.append(day_row)
            period_start_unset = InlineKeyboardButton(
                f'❌ Отменить отбор по `Начало периода`',
                callback_data='psm_search_unset_periodStart',
            )
            period_start_row.append([period_start_unset])
            for _ in period_start_row:
                settings.append(_)
        # endregion `periodStart`
        # region `periodEnd`
        search_by_period_start = search_by['periodEnd']
        if not search_by_period_start['listening']:
            period_start_init_but = InlineKeyboardButton(
                '🔍 Установить `Конец периода`', callback_data='psm_search_periodEnd'
            )
            settings.append([period_start_init_but])
        else:
            period_start: str = user_state['searchBy']['periodEnd']['value']
            if not period_start:
                # TODO: .env variable for starting shift?
                cur_date = datetime.now()
                cur_period = f'{cur_date.year}S{cur_date.month}S{cur_date.day}'
                user_state['searchBy']['periodEnd']['value'] = cur_period
            start_year, start_month, start_day = user_state['searchBy']['periodEnd']['value'].split('S')
            period_start_row = []
            period_start_header = InlineKeyboardButton(
                '🕒 Конец периода', callback_data='none'
            )
            period_start_row.append([period_start_header])

            year_row = []
            period_start_year_show = InlineKeyboardButton(
                f'Год: {start_year}', callback_data='none',
            )
            year_row.append(period_start_year_show)
            period_start_year_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodEnd_increase_year',
            )
            year_row.append(period_start_year_increase)
            period_start_year_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodEnd_decrease_year',
            )
            year_row.append(period_start_year_decrease)
            period_start_row.append(year_row)
            
            month_row = []
            period_start_month_show = InlineKeyboardButton(
                f'Месяц: {start_month}', callback_data='none',
            )
            month_row.append(period_start_month_show)
            period_start_month_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodEnd_increase_month',
            )
            month_row.append(period_start_month_increase)
            period_start_month_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodEnd_decrease_month',                
            )
            month_row.append(period_start_month_decrease)
            period_start_row.append(month_row)

            day_row = []
            period_start_day_show = InlineKeyboardButton(
                f'День: {start_day}', callback_data='none',
            )
            day_row.append(period_start_day_show)
            period_start_day_increase = InlineKeyboardButton(
                '⬆️', callback_data='psm_search_periodEnd_increase_day',
            )
            day_row.append(period_start_day_increase)
            period_start_day_decrease = InlineKeyboardButton(
                '⬇️', callback_data='psm_search_periodEnd_decrease_day',                
            )
            day_row.append(period_start_day_decrease)
            period_start_row.append(day_row)
            period_start_unset = InlineKeyboardButton(
                f'❌ Отменить отбор по `Конец периода`',
                callback_data='psm_search_unset_periodEnd',
            )
            period_start_row.append([period_start_unset])
            for _ in period_start_row:
                settings.append(_)
        # endregion `periodEnd`
        search_by_closer = InlineKeyboardButton(
            '--- --- ---', callback_data='none'
        )
        settings.append([search_by_closer])
    # endregion search
    # region pagination
    pagination_row = []
    five_per_page = InlineKeyboardButton(
        'Кол-во: 5', callback_data='psm_items_5',
    )
    ten_per_page = InlineKeyboardButton(
        'Кол-во: 10', callback_data='psm_items_10',
    )
    twenty_per_page = InlineKeyboardButton(
        'Кол-во: 20', callback_data='psm_items_20',
    )
    pagination_row.append(five_per_page)
    pagination_row.append(ten_per_page)
    pagination_row.append(twenty_per_page)
    settings.append(pagination_row)
    # endregion pagination
    full_msg_keyboard = task_buttons + navigation_buttons + settings
    psm_text: str = f'Страница: {page}/{max(1, total_pages)} - {sort_by} - ' \
                    f'{'Возрастание' if ascending else 'Убывание'}'
    psm_id: int = user_state.get('psm_message_id', None)
    if psm_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=psm_id,
                text=psm_text,
                reply_markup=InlineKeyboardMarkup(full_msg_keyboard),
            )
        except Exception as error:
            error_msg: str = str(error)
            logger.error(
                f'Failed to edit message: {error_msg}'
            )
            # TG returns 400 for `NotFound` xdd
            cringe_skip: str = "Message to edit not found"
            if error_msg.strip() == cringe_skip:
                user_state['psm_message_id'] = None
                psm_id = None
    if not psm_id:
        new_psm = await context.bot.send_message(
            chat_id=chat_id,
            text=psm_text,
            reply_markup=(InlineKeyboardMarkup(full_msg_keyboard)),
        )
        user_state['psm_message_id'] = new_psm.message_id
    # private => `chat_id` == `user_id`
    await REDIS.set(f'{REDIS_USER_TAG}:{chat_id}', json.dumps(user_state))


async def show_einfo_messsage(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    user_state: dict, task_number: str,
) -> None:
    user_data = update.callback_query.from_user
    # All messages are in private => chat_id == user_id
    user_id: int = user_data.id
    psm_message_id: int = user_state['psm_message_id']
    einfo_url = f'{API_URL}/{TASK_ENDPOINT}?'
    # 1C db stores `Номер` with leading and trailing spaces...
    # And we can't parse them without spaces == we wont find it.
    # So, we need to use other char to identify and replace later.
    saved_spaces: list[str] = []
    for char in task_number:
        if ' ' == char:
            saved_spaces.append('_')
        else:
            saved_spaces.append(char)
    cor_task_number: str = ''.join(saved_spaces)
    
    einfo_args = f'taskNumber={cor_task_number}'
    einfo_url = einfo_url + einfo_args
    response = await make_get_request(
        einfo_url, False, False
    )
    if 200 != response.status:
        error_msg: str = f'Error while getting data:\n{response.text}'
        await context.bot.send_message(
            chat_id=user_id, 
            text=error_msg,
        )
        return
    task_data: dict = await response.json()
    response.close()
    blank_string: str = 'Не указано'

    text_field: str = ''
    for line in task_data['Текст'].split('\n'):
        escaped_row: str = await escape_special(line)
        if escaped_row:
            text_field += f'>{escaped_row}\n'
    text_field = text_field.removesuffix('\n') + '||' if text_field else text_field
    
    company: dict = task_data['Организация']
    if company:
        company_field: str = await escape_special(company.get('Наименование', ''))
    else:
        company_field = ''

    initiator: str = task_data['Заявитель']
    initiator_field: str = ''
    if not initiator:
        initiator_field = blank_string
    else:
        initiator_field = await get_fio(initiator)

    workers_string = ''
    for worker in task_data['Исполнители']:
        workers_string += f'\n  {await get_fio(worker)}'
    t0 = task_data['ВидЗаявки']
    t1 = task_data['Состояние']
    t2 = task_data['Срочность'][3:]
    t3 = task_data['Комментарий']
    text_tasks = [
        escape_special(t0),
        escape_special(t1),
        escape_special(t2),
        escape_special(initiator_field),
        escape_special(workers_string),
        escape_special(t3),
    ]

    text_results = await asyncio.gather(*text_tasks)
    task_id: str = task_data.get('Номер')
    einfo_text: str = (
    f'*Номер*: {task_id or blank_string}'
    f'\n*Организация*:\n {company_field or blank_string}'
    f'\n*Вид заявки:*\n {text_results[0] or blank_string}'
    f'\n\-\-\- \-\-\- \-\-\-'
    f'\n*Состояние:* {text_results[1] or blank_string}'
    f'\n*Срочность:* {text_results[2] or blank_string}'
    f'\n*Заявитель:* {text_results[3]}'
    f'\n*Исполнители:* {text_results[4]}'
    f'\n\-\-\- \-\-\- \-\-\-'
    f'\n*Текст:* \n{text_field}'
    f'\n*Комментарий:*\n{text_results[5] or blank_string}'
    )
    einfo_buttons = [
        [InlineKeyboardButton(
            '🔙 Вернуть меню', callback_data='psm_restore_menu'
        )],
        [InlineKeyboardButton(
            '➕ Добавить в группу', callback_data=f'psm_create_task_{task_id}'
        )]
    ]
    
    einfo_type: str = 'MarkdownV2'
    await update.callback_query.edit_message_text(
        text=einfo_text,
        parse_mode=einfo_type,
        reply_markup=InlineKeyboardMarkup(einfo_buttons)
    )


async def send_psm_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username: str = update.effective_user.username
    user_id: int = update.effective_user.id
    chat_id: int = user_id
    chat_type: str = update.message.chat.type
    tasks = []

    async def check_private() -> None:
        nonlocal update, context
        if 'private' != chat_type:
            notify_msg = await update.message.reply_text(
                f'{username} <- отправлено личное сообщение для поиска'
            )
            asyncio.create_task(
                delete_msg(
                    context, update.message.chat_id,
                    notify_msg.message_id, NOTIF_DELETE_DELAY
                )
            )
            asyncio.create_task(
                delete_msg(
                    context, update.message.chat_id,
                    update.message.message_id, NOTIF_DELETE_DELAY
                )
            )

    async def send_message() -> None:
        nonlocal update, context, user_id
        user_data: str = await REDIS.get(f'{REDIS_USER_TAG}:{user_id}')
        user_state: dict | None = None
        if not user_data:
            user_state = await initial_psm_state()
            await REDIS.set(f'{REDIS_USER_TAG}:{user_id}', json.dumps(user_state))
        else:
            user_state = json.loads(user_data)
        await send_or_edit_psm_message(
            context, chat_id, user_state
        )

    tasks.append(check_private())
    tasks.append(send_message())
    asyncio.gather(*tasks)


# region Callbacks

# psm_einfo         <- extra info on clicked task
# psm_restore_menu  <- restore message to menuView
# psm_page_prev_{total_pages} <- previous page
# psm_page_next_{total_pages} <- next page
# psm_sortby_number <- sorting by taskNumber
# psm_sortby_date   <- sorting by taskDate
# psm_sortby_status <- sorting by taskStatus
# psm_search_number <- listening for the next message of a user to search by it
# psm_reset         <- reset psm to default setting
# psm_items_5       <- change pagination to 5 records per page
# psm_items_10      <- change pagination to 10 records per page
# psm_items_20      <- change pagination to 20 records per page

# endregion Callbacks
async def handle_psm_callbacks(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user_data = query.from_user
    user_id: int = user_data.id

    user_record: str = await REDIS.get(f'{REDIS_USER_TAG}:{user_id}')
    user_state: dict = json.loads(user_record)
    if not user_state:
        await query.answer(
            'Нет данных о привязанном сообщении. Используйте /search'
        )
        return
    
    call_data = query.data.split('_')
    call_type = call_data[0]
    if 'none' == call_type:
        await query.answer('Не кликай!')
        return
    if 'psm' != call_type:
        return
    call_command = call_data[1]
    if 'page' == call_command:
        page_shift = call_data[2]
        total_pages = int(call_data[3])
        if 'prev' == page_shift:
            user_state['page'] = max(1, user_state['page'] - 1)
        if 'next' == page_shift:
            user_state['page'] = min(total_pages, user_state['page'] + 1)
    elif 'sortby' == call_command:
        user_state['ascending'] = not user_state['ascending']        
        user_state['sortBy'] = sort_map[call_data[2]]
    elif 'reset' == call_command:
        reset_value = call_data[2]
        user_state['searchBy'][reset_value]['value'] = ''
        user_state['searchBy'][reset_value]['listening'] = False
        user_state['page'] = 1
    elif 'items' == call_command:
        per_page: int = int(call_data[2])
        user_state['perPage'] = per_page
        user_state['page'] = 1
    elif 'search' == call_command:
        search_value = call_data[2]
        if 'show' == search_value:
            user_state['openSearch'] = not user_state['openSearch']
        # region `set`
        # We only set value with buttons == preseted values
        elif 'set' == search_value:
            set_type = call_data[3]
            set_value = call_data[4]
            user_state['searchBy'][set_type]['value'] = set_value
        elif 'unset' == search_value:
            set_type = call_data[3]
            user_state['searchBy'][set_type]['value'] = ''
            user_state['searchBy'][set_type]['listening'] = False
        # endregion `set`
        # region `number`
        elif 'number' == search_value:
            user_state['searchListen'] = True
            user_state['searchBy']['number']['listening'] = True
        # endregion `number`
        # TODO: think about more adequate solution
        elif 'status' == search_value:
            user_state['searchBy'][search_value]['listening'] = True
        elif 'periodStart' == search_value or 'periodEnd' == search_value:
            today = datetime.now()
            user_state['searchBy'][search_value]['listening'] = True
            if len(call_data) > 3:
                value_state = user_state['searchBy'][search_value]
                change_type = call_data[3]
                change_value = call_data[4]
                cur_year, cur_month, cur_day = [
                    int(val) for val in value_state['value'].split('S')
                ]
                cur_data = {
                    'year': cur_year,
                    'month': cur_month,
                    'day': cur_day,
                }
                if 'year' == change_value:
                    start_year, end_year = 1990, today.year
                    if change_type == 'increase':
                        cur_data[change_value] = start_year if cur_data[change_value] == end_year else cur_data[change_value] + 1
                    elif change_type == 'decrease':
                        cur_data[change_value] = end_year if cur_data[change_value] == start_year else cur_data[change_value] - 1
                elif 'month' == change_value:
                    if change_type == 'increase':
                        cur_data[change_value] = (cur_data[change_value] % 12) + 1
                    elif change_type == 'decrease':
                        cur_data[change_value] = (cur_data[change_value] - 2) % 12 + 1
                elif 'day' == change_value:
                    min_day, max_day = 1, 31
                    if change_type == 'increase':
                        cur_data[change_value] = min_day if cur_data[change_value] == max_day else cur_data[change_value] + 1
                    elif change_type == 'decrease':
                        cur_data[change_value] = max_day if cur_data[change_value] == min_day else cur_data[change_value] - 1
                user_state['searchBy'][search_value]['value'] = f'{cur_data['year']}S{cur_data['month']}S{cur_data['day']}'
    elif 'restore' == call_command:
        restore_type = call_data[2]
        if 'menu' == restore_type:
            await query.answer(
                'Восстанавливаю вид меню...'
            )
    elif 'einfo' == call_command:
        task_number: str = call_data[2]
        _a_tasks = []
        _a_tasks.append(
            query.answer('Загружаю выбранные данные...')
        )
        _a_tasks.append(
            show_einfo_messsage(update, context, user_state, task_number)
        )
        asyncio.gather(*_a_tasks)
        return
    elif 'create' == call_command:
        create_type = call_data[2]
        if 'task' == create_type:
            task_id = call_data[3]
            _a_tasks = []
            _a_tasks.append(
                query.answer('Создаю сообщение...')
            )
            url = f'{API_URL}/{TASK_ENDPOINT}?'
            payload = {
                'botToken': BOT_TOKEN,
                'chatId': GROUP_ID,
                'author': user_data.username,
                'taskId': task_id, 
            }
            _a_tasks.append(
                make_put_request(url, payload, False, False)
            )
            _a_results = await asyncio.gather(*_a_tasks)
            response = _a_results[1]
            if 200 != response.status:
                error_msg: str = f'Error while getting data:\n{response.text}'
                err_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=error_msg,
                )
                response.close()
                return
            resp_body = await response.json()
            response.close()
            result = resp_body['result']
            res_chat_id = result['chat']['id']
            res_chat_id_stripped = str(res_chat_id).replace("-100", "")
            res_message_id = result['message_id']
            notif_msg = await context.bot.send_message(
                chat_id=user_id,
                text=f'Сообщение создано https://t.me/c/{res_chat_id_stripped}/{res_message_id}',
            )
            asyncio.create_task(
                delete_msg(
                    context, notif_msg.chat_id,
                    notif_msg.message_id, NOTIF_DELETE_DELAY
                )
            )
        return
    final_tasks = []
    # TODO: Currently only using msg to get `number`.
    # So, it's hardcoded version to get `number`.
    listen_search_by: str = user_state['searchListen']
    if listen_search_by:
        final_tasks.append(
            query.answer(
                f'Ожидаю `Номер` для поиска'
            )
        )
    else:
        final_tasks.append(
            query.answer('Загружаю данные...')
        )
    final_tasks.append(
        REDIS.set(f'{REDIS_USER_TAG}:{user_id}', json.dumps(user_state))
    )
    final_tasks.append(
        send_or_edit_psm_message(context, user_id, user_state)
    )
    asyncio.gather(*final_tasks)


async def handle_search_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_type = update.message.chat.type
    chat_id = update.message.chat.id
    user_data = update.message.from_user
    user_id = user_data.id
    waiting = await get_awaited_user(REDIS, user_id)
    if 'private' != chat_type and waiting:
        await handle_awaited_comment(update, context)
        return
    user_k_string: str = await cr_user_k_string(user_id)
    user_record: str = await REDIS.get(user_k_string)
    if not user_record:
        return
    user_state: dict = json.loads(user_record)
    search_value: str = user_state['searchListen']
    if not search_value:
        del_ms = await context.bot.send_message(
            chat_id=chat_id,
            text='Сообщения кроме поиска и комманд будут проигнорированы и удалены',
        )
        asyncio.create_task(
            delete_msg(context, chat_id, del_ms.id, NOTIF_DELETE_DELAY)
        )
        asyncio.create_task(
            delete_msg(context, chat_id, update.message.id, 0)
        )
        return
    search_value_input: str = update.message.text
    # TODO: hardcoded version for `number` everything else is set through buttons
    user_state['searchBy']['number']['value'] = f'{search_value_input}'
    user_state['searchBy']['number']['listening'] = False
    user_state['searchListen'] = ''
    user_state['page'] = 1
    await REDIS.set(user_k_string, json.dumps(user_k_string))
    asyncio.create_task(
        delete_msg(context, user_id, update.message.id, 1)
    )
    await send_or_edit_psm_message(
        context, user_id, user_state
    )

# endregion PrivateSearchMessageHandlers


# region BasicCommands
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(
        f'Attemp to reset bot commands'
    )
    if update.message.from_user.id not in BOT_OWNERS:
        logger.warning(
            f'Not Admin user attempted to setup basic commands {update.message.from_user.username}'
        )
        return
    await context.bot.set_my_commands(
        []
    )
    chat_id: int = update.message.chat_id
    cmd_message_id: int = update.message.message_id
    reply_txt = "Commands cleared."
    await context.bot.send_message(
        chat_id=chat_id,
        text=reply_txt,
        reply_to_message_id=cmd_message_id,
    )


async def setup (update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(
        f'Attemp to setup standard Bot commands'
    )
    if update.message.from_user.id not in BOT_OWNERS:
        logger.warning(
            f'Not Admin user attempted to setup basic commands {update.message.from_user.username}'
        )
        return
    basic_commands: list[BotCommand] = [
        BotCommand("search", "Сообщение поиска обращений"),
        BotCommand("cancel_search", "Удаляет сообщение поиска обращений"),
        BotCommand("about", "Общие сведения. Используется только в личном чате"),
    ]
    await context.bot.set_my_commands(
        basic_commands
    )
    await update.message.reply_text(
        "Bot setup complete! Use the menu button to access commands."
    )
    logger.info(
        f'Bot command are set by {update.message.from_user.username}'
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    msg_id = update.message.id
    if 'private' != update.message.chat.type:
        repl_msg = await update.message.reply_text(
            f'Используется только в личном чате. Напишите МНЕ в ЛС.'
        )
        asyncio.create_task(
            delete_msg(context, chat_id, msg_id, 1)
        )
        asyncio.create_task(
            delete_msg(context, chat_id, repl_msg.id, NOTIF_DELETE_DELAY)
        )
        return
    await update.message.reply_text(about_message)
# endregion BasicCommands


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters=filters.TEXT & ~filters.COMMAND, callback=handle_search_message)
    )
    # region Commands
    app.add_handler(
        CommandHandler("reset", reset)
    )
    app.add_handler(
        CommandHandler("setup", setup)
    )
    app.add_handler(
        CommandHandler("search", send_psm_message)
    )
    app.add_handler(
        CommandHandler("cancel_search", delete_psm_message)
    )
    app.add_handler(
        CommandHandler("about", about)
    )
    # endregion Commands
    # region GroupButtons
    app.add_handler(
        CallbackQueryHandler(handle_empty_comment, pattern='emptycomment_')
    )
    app.add_handler(
        CallbackQueryHandler(handle_std_comment, pattern="waitcomment_")
    )
    app.add_handler(
        CallbackQueryHandler(handle_close_req, pattern="closereq_")
    )
    # endregion GroupButtons
    # region PrivateSearchMessageButton
    app.add_handler(
        CallbackQueryHandler(handle_psm_callbacks)
    )
    
    # endregion PrivateSearchMessageButton
    app.run_polling()


if __name__ == "__main__":
    init_redis()
    start_time: datetime = datetime.now().strftime("%d%m%Y | %H:%M:%S")
    logger.info(f"{start_time} <- Bot started")
    main()
