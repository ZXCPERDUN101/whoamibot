import asyncio
import os
import random
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from thefuzz import fuzz

# Загружаем переменные окружения (токен из файла .env или панели хостинга)
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")

if not API_TOKEN:
  raise ValueError(
      "❌ Ошибка: Переменная окружения BOT_TOKEN не найдена! Проверь файл .env"
      " или настройки хостинга."
  )

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class GameStates(StatesGroup):
  registration = State()
  waiting_for_words = State()
  in_game = State()


def generate_random_assignments(players):
  """Генерирует случайные пары (кто кому загадывает) без самоповторов."""
  targets = players.copy()
  while True:
    random.shuffle(targets)
    if all(
        giver["id"] != target["id"] for giver, target in zip(players, targets)
    ):
      break
  assignments = {}
  for giver, target in zip(players, targets):
    assignments[giver["id"]] = target
  return assignments


def get_turn_keyboard():
  """Клавиатура действий для игрока в его ход."""
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🎯 Дать ответ", callback_data="try_guess"
              ),
              InlineKeyboardButton(
                  text="🏳️ Сдаться", callback_data="surrender"
              ),
          ]
      ]
  )


async def check_bot_permissions(message: Message) -> bool:
  """Проверяет права бота и просит выдать недостающие разрешения."""
  try:
    bot_member = await message.chat.get_member(bot.id)

    if bot_member.status not in ["administrator", "creator"]:
      await message.answer(
          "⚠️ **Внимание: требуются права администратора!**\n\n"
          "Чтобы я мог удалять чужие сообщения не в свою очередь и следить за"
          " порядком в игре, сделай меня **администратором** этой группы с правом"
          " **«Удаление сообщений»**.\n\n"
          "🛠 *Как это сделать:* Нажми на название группы ➡️ Изменить ➡️"
          " Администраторы ➡️ Добавить администратора ➡️ Выбери меня и включи"
          " право удаления сообщений."
      )
      return False

    if (
        bot_member.status == "administrator"
        and not bot_member.can_delete_messages
    ):
      await message.answer(
          "⚠️ **Недостаточно прав!**\n\n"
          "У меня есть статус администратора, но выключено право **«Удаление"
          " сообщений»**. Без него я не смогу удалять лишние сообщения во"
          " время игры.\n\n"
          "🛠 *Пожалуйста, зайди в настройки администраторов группы и включи"
          " эту галочку!*"
      )
      return False

    return True
  except Exception:
    await message.answer(
        "❌ Не удалось проверить мои права в чате. Убедитесь, что я добавлен в"
        " группу и назначен администратором!"
    )
    return False


# Таймер хода на 60 секунд
async def turn_timeout(
    bot: Bot, chat_id: int, state: FSMContext, expected_version: int
):
  await asyncio.sleep(60)
  data = await state.get_data()
  current_state = await state.get_state()

  if (
      current_state == GameStates.in_game
      and data.get("turn_version") == expected_version
  ):
    players = data.get("players", [])
    winners = data.get("winners", [])
    winner_ids = [w["id"] for w in winners]

    active_players = [p for p in players if p["id"] not in winner_ids]
    if len(active_players) <= 1:
      return

    current_turn_index = data.get("current_turn_index", 0)
    current_player = players[current_turn_index]

    guessing_user_ids = data.get("guessing_user_ids", [])
    if current_player["id"] in guessing_user_ids:
      guessing_user_ids.remove(current_player["id"])
      await state.update_data(guessing_user_ids=guessing_user_ids)

    await bot.send_message(
        chat_id,
        f"⏰ **Время вышло!** Игрок **{current_player['name']}** не успел сделать"
        " ход за 1 минуту и пропускает его.",
    )
    await advance_turn(bot, chat_id, state)


# Переход к следующему ходу
async def advance_turn(bot: Bot, chat_id: int, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  winners = data.get("winners", [])
  winner_ids = [w["id"] for w in winners]

  active_players = [p for p in players if p["id"] not in winner_ids]
  if len(active_players) <= 1:
    await finish_turn_game(bot, chat_id, state)
    return

  current_turn_index = data.get("current_turn_index", 0)
  while True:
    current_turn_index = (current_turn_index + 1) % len(players)
    candidate = players[current_turn_index]
    if candidate["id"] not in winner_ids:
      break

  turn_version = data.get("turn_version", 0) + 1
  await state.update_data(
      current_turn_index=current_turn_index, turn_version=turn_version
  )
  next_player = players[current_turn_index]

  await bot.send_message(
      chat_id,
      f"💬 Ход игрока **{next_player['name']}** ⏱ *(на ход дается 1 минута)*.\n"
      "Напиши свой вопрос **одним сообщением** или выбери действие:",
      reply_markup=get_turn_keyboard(),
  )

  asyncio.create_task(turn_timeout(bot, chat_id, state, turn_version))


# Завершение игры
async def finish_turn_game(bot: Bot, chat_id: int, state: FSMContext):
  data = await state.get_data()
  winners = data.get("winners", [])
  players = data.get("players", [])
  target_words = data.get("target_words", {})

  winner_ids = [w["id"] for w in winners]
  for p in players:
    if p["id"] not in winner_ids:
      winners.append(
          {
              "id": p["id"],
              "name": p["name"],
              "word": f"{target_words.get(p['id'])} (Не отгадал)",
          }
      )

  result_text = (
      "🏆 **ИГРА ОКОНЧЕНА! ИТОГОВАЯ ТУРНИРНАЯ ТАБЛИЦА:**\n\nКто быстрее всех"
      " нашел своего персонажа:\n"
  )
  for i, w in enumerate(winners, 1):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal = medals.get(i, f"▫️ {i}.")
    result_text += f"{medal} **{w['name']}** — слово: *{w['word']}*\n"

  await bot.send_message(chat_id, result_text)
  await state.clear()


# 1. Запуск регистрации
@router.message(Command("whoami"))
async def cmd_whoami(message: Message, state: FSMContext):
  if message.chat.type not in ["group", "supergroup"]:
    await message.answer("Эту игру можно запускать только в групповом чате!")
    return

  has_permissions = await check_bot_permissions(message)
  if not has_permissions:
    return

  await state.update_data(players=[], group_chat_id=message.chat.id)
  await state.set_state(GameStates.registration)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🎮 Участвовать", callback_data="join_game"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🚀 Начать игру (мин. 4)", callback_data="start_game"
              )
          ],
      ]
  )

  await message.answer(
      "🎯 **Игра «Кто я?» объявляется открытой!**\n\nПрава бота проверены"
      " ✅\nНажимайте кнопку **«Участвовать»**. Как только наберется **минимум"
      " 4 человека**, нажимайте **«Начать игру»**!",
      reply_markup=keyboard,
  )


# 2. Участие
@router.callback_query(GameStates.registration, F.data == "join_game")
async def process_join(callback: CallbackQuery, state: FSMContext):
  user = callback.from_user
  data = await state.get_data()
  players = data.get("players", [])

  if any(p["id"] == user.id for p in players):
    await callback.answer("Ты уже в игре!", show_alert=True)
    return

  players.append({"id": user.id, "name": user.full_name})
  await state.update_data(players=players)

  names_list = "\n".join([f"▫️ {p['name']}" for p in players])
  text = (
      f"🎯 **Игра «Кто я?»**\n\n**Участники ({len(players)}):**\n{names_list}\n\nЖдем"
      " остальных или нажимайте «Начать игру»!"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=f"🎮 Участвовать ({len(players)})",
                  callback_data="join_game",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🚀 Начать игру (мин. 4)", callback_data="start_game"
              )
          ],
      ]
  )

  try:
    await callback.message.edit_text(text, reply_markup=keyboard)
  except Exception:
    pass

  await callback.answer(f"Ты в игре, {user.first_name}!")


# 3. Старт игры
@router.callback_query(GameStates.registration, F.data == "start_game")
async def process_start(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])

  if len(players) < 4:
    await callback.answer(
        f"❌ Недостаточно игроков! Собралось {len(players)} из 4 необходимых.",
        show_alert=True,
    )
    return

  assignments = generate_random_assignments(players)

  await state.update_data(
      assignments=assignments,
      target_words={},
      winners=[],
      guessing_user_ids=[],
  )
  await state.set_state(GameStates.waiting_for_words)

  await callback.message.edit_text(
      "🎲 **Регистрация завершена!**\n\nЯ отправил каждому в ЛС имя игрока,"
      " которому нужно загадать слово. Жду ваши варианты в ЛС!"
  )

  for giver_id, target in assignments.items():
    try:
      await bot.send_message(
          giver_id,
          f"🤫 **Твоё секретное задание:**\n\nТебе нужно придумать и написать"
          f" мне слово для игрока: **{target['name']}**!\n\nПросто отправь"
          " слово текстом в ответ на это сообщение.",
      )
    except Exception:
      pass


# 4. Прием слов в ЛС
@router.message(GameStates.waiting_for_words, F.chat.type == "private")
async def receive_word(message: Message, state: FSMContext):
  user_id = message.from_user.id
  word = message.text.strip()
  data = await state.get_data()

  assignments = data.get("assignments", {})
  target_words = data.get("target_words", {})
  players = data.get("players", [])
  group_chat_id = data.get("group_chat_id")

  if user_id not in assignments:
    return

  target = assignments[user_id]
  target_words[target["id"]] = word
  await state.update_data(target_words=target_words)

  await message.answer(
      f"✅ Отлично! Ты загадал **«{word}»** для игрока"
      f" **{target['name']}**.\nОжидаем остальных участников..."
  )

  if len(target_words) == len(players):
    turn_version = 1
    await state.update_data(
        target_words=target_words, current_turn_index=0, turn_version=turn_version
    )
    await state.set_state(GameStates.in_game)

    for current_player in players:
      current_id = current_player["id"]
      personal_list = (
          "🎮 **Все слова загаданы! Вот список всех участников и их ролей:**\n\n"
      )

      for p in players:
        p_id = p["id"]
        p_word = target_words.get(p_id)
        if p_id == current_id:
          personal_list += f"👤 **{p['name']}** — ❓ *[Это ты, угадывай!]*\n"
        else:
          personal_list += f"👤 **{p['name']}** — {p_word}\n"

      personal_list += "\n💬 Возвращайтесь в групповой чат! Игра началась."
      try:
        await bot.send_message(current_id, personal_list)
      except Exception:
        pass

    if group_chat_id:
      first_player = players[0]
      await bot.send_message(
          group_chat_id,
          "🔥 **Все слова успешно загаданы! Каждому в ЛС отправлены роли.**\n\n"
          f"🎯 **Игра началась!**\n💬 Ход игрока **{first_player['name']}** ⏱"
          " *(на ход дается 1 минута)*.\nНапиши свой вопрос **одним"
          " сообщением** или выбери действие:",
          reply_markup=get_turn_keyboard(),
      )
      asyncio.create_task(
          turn_timeout(bot, group_chat_id, state, turn_version)
      )


# 5. Обработка вопросов и ответов в группе
@router.message(GameStates.in_game, F.chat.type.in_(["group", "supergroup"]))
async def handle_game_messages(message: Message, state: FSMContext):
  if message.text and message.text.startswith("/"):
    return

  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  guessing_user_ids = data.get("guessing_user_ids", [])
  winners = data.get("winners", [])
  winner_ids = [w["id"] for w in winners]
  group_chat_id = data.get("group_chat_id")

  user_id = message.from_user.id
  player_ids = [p["id"] for p in players]

  if user_id not in player_ids:
    try:
      await message.delete()
    except Exception:
      pass
    return

  if user_id in winner_ids:
    try:
      await message.delete()
      warning = await message.answer(
          f"🤫 {message.from_user.first_name}, ты уже отгадал своего персонажа!"
          " Не подсказывай остальным. 🤐"
      )
      await asyncio.sleep(4)
      await warning.delete()
    except Exception:
      pass
    return

  current_player = players[current_turn_index]

  if user_id != current_player["id"]:
    try:
      await message.delete()
      warning = await message.answer(
          f"🤫 {message.from_user.first_name}, сейчас не твой ход! Ходит"
          f" **{current_player['name']}**."
      )
      await asyncio.sleep(4)
      await warning.delete()
    except Exception:
      pass
    return

  # Процесс угадывания
  if user_id in guessing_user_ids:
    raw_guess = message.text.strip()
    target_words = data.get("target_words", {})
    real_word = target_words.get(user_id, "").strip()

    try:
      await message.delete()
    except Exception:
      pass

    guessing_user_ids.remove(user_id)
    await state.update_data(guessing_user_ids=guessing_user_ids)

    similarity = fuzz.ratio(raw_guess.lower(), real_word.lower())

    if similarity >= 80 or raw_guess.lower() == real_word.lower():
      winners.append(
          {"id": user_id, "name": current_player["name"], "word": real_word}
      )
      await state.update_data(winners=winners)

      place_num = len(winners)
      await message.answer(
          f"🎉 **Браво, {current_player['name']}!** Ты верно отгадал своего"
          f" персонажа: **{real_word}**!\n🏆 Ты занимаешь **{place_num}-е"
          " место** в рейтинге! *Теперь ты наблюдаешь со стороны.* 🤐"
      )

      active_players = [
          p for p in players if p["id"] not in [w["id"] for w in winners]
      ]
      if len(active_players) <= 1:
        await finish_turn_game(bot, group_chat_id, state)
        return
    else:
      await message.answer(
          f"❌ Увы, **{current_player['name']}**, «{raw_guess}» — это неверный"
          " ответ! Игра продолжается."
      )

    await advance_turn(bot, group_chat_id, state)
    return

  # Обычный вопрос
  question_text = message.text
  if not question_text:
    return

  try:
    await message.delete()
  except Exception:
    pass

  keyboard_vote = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(text="✅ Да", callback_data="vote_yes"),
              InlineKeyboardButton(text="❌ Нет", callback_data="vote_no"),
              InlineKeyboardButton(text="🤷‍♂️ Не знаю", callback_data="vote_idk"),
          ]
      ]
  )

  await message.answer(
      f"❓ **Вопрос от {current_player['name']}:**\n\n«{question_text}»\n\n"
      "👇 *Голосуют только участники игры:*",
      reply_markup=keyboard_vote,
  )

  await advance_turn(bot, group_chat_id, state)


# 6. Кнопка «Дать ответ»
@router.callback_query(GameStates.in_game, F.data == "try_guess")
async def process_try_guess(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  current_player = players[current_turn_index]

  if callback.from_user.id != current_player["id"]:
    await callback.answer("Сейчас не твой ход!", show_alert=True)
    return

  guessing_user_ids = data.get("guessing_user_ids", [])
  if callback.from_user.id not in guessing_user_ids:
    guessing_user_ids.append(callback.from_user.id)
    await state.update_data(guessing_user_ids=guessing_user_ids)

  keyboard_back = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к вопросу", callback_data="cancel_guess"
              )
          ]
      ]
  )

  await callback.message.edit_text(
      f"🎯 **{current_player['name']}** решил назвать отгадку!\n\nНапиши своего"
      " персонажа/слово **одним сообщением** в этот чат или передумал?",
      reply_markup=keyboard_back,
  )
  await callback.answer()


@router.callback_query(GameStates.in_game, F.data == "cancel_guess")
async def process_cancel_guess(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  current_player = players[current_turn_index]

  if callback.from_user.id != current_player["id"]:
    await callback.answer("Сейчас не твой ход!", show_alert=True)
    return

  guessing_user_ids = data.get("guessing_user_ids", [])
  if callback.from_user.id in guessing_user_ids:
    guessing_user_ids.remove(callback.from_user.id)
    await state.update_data(guessing_user_ids=guessing_user_ids)

  await callback.message.edit_text(
      f"💬 Ход игрока **{current_player['name']}**. Напиши свой вопрос **одним"
      " сообщением** или выбери действие:",
      reply_markup=get_turn_keyboard(),
  )
  await callback.answer("Возвращаемся к вопросу!")


# 7. Кнопка «Сдаться»
@router.callback_query(GameStates.in_game, F.data == "surrender")
async def process_surrender_request(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  current_player = players[current_turn_index]

  if callback.from_user.id != current_player["id"]:
    await callback.answer("Сейчас не твой ход!", show_alert=True)
    return

  keyboard_confirm = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Да, сдаться", callback_data="confirm_surrender"
              ),
              InlineKeyboardButton(
                  text="❌ Нет, продолжить", callback_data="cancel_surrender"
              ),
          ]
      ]
  )

  await callback.message.edit_text(
      f"⚠️ **{current_player['name']}**, ты уверен, что хочешь сдаться? Твое"
      " слово будет раскрыто!",
      reply_markup=keyboard_confirm,
  )
  await callback.answer()


@router.callback_query(GameStates.in_game, F.data == "confirm_surrender")
async def process_confirm_surrender(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  current_player = players[current_turn_index]
  group_chat_id = data.get("group_chat_id")

  if callback.from_user.id != current_player["id"]:
    await callback.answer("Сейчас не твой ход!", show_alert=True)
    return

  target_words = data.get("target_words", {})
  real_word = target_words.get(current_player["id"], "Неизвестно")
  winners = data.get("winners", [])

  winners.append(
      {
          "id": current_player["id"],
          "name": current_player["name"],
          "word": f"{real_word} (🏳️ Сдался)",
      }
  )
  await state.update_data(winners=winners)

  await callback.message.edit_text(
      f"🏳️ Игрок **{current_player['name']}** сдался!\n"
      f"Его загаданное слово было: **{real_word}**.\n"
      " *Теперь он наблюдает со стороны.* 🤐"
  )
  await callback.answer()

  active_players = [
      p for p in players if p["id"] not in [w["id"] for w in winners]
  ]
  if len(active_players) <= 1:
    await finish_turn_game(bot, group_chat_id, state)
    return

  await advance_turn(bot, group_chat_id, state)


@router.callback_query(GameStates.in_game, F.data == "cancel_surrender")
async def process_cancel_surrender(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  players = data.get("players", [])
  current_turn_index = data.get("current_turn_index", 0)
  current_player = players[current_turn_index]

  if callback.from_user.id != current_player["id"]:
    await callback.answer("Сейчас не твой ход!", show_alert=True)
    return

  await callback.message.edit_text(
      f"💬 Ход игрока **{current_player['name']}**. Напиши свой вопрос **одним"
      " сообщением** или выбери действие:",
      reply_markup=get_turn_keyboard(),
  )
  await callback.answer("Продолжаем борьбу!")


# 8. Голосование
@router.callback_query(
    GameStates.in_game, F.data.in_(["vote_yes", "vote_no", "vote_idk"])
)
async def process_vote(callback: CallbackQuery):
  vote_map = {
      "vote_yes": "✅ Да",
      "vote_no": "❌ Нет",
      "vote_idk": "🤷‍♂️ Не знаю",
  }
  choice = vote_pop = vote_map.get(callback.data)
  await callback.answer(f"Твой голос учтен: {choice}")


async def main():
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
