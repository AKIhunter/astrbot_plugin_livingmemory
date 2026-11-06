<p align="center">
  <a href="README.md">中文</a> · <a href="README_en.md">English</a> · <strong>Русский</strong>
</p>

> Поддерживаемый fork 2.10.0 сохраняет OneID и защиту приватности; долгосрочное хранение включается отдельно и по умолчанию выключено. Историческая семантическая точность — 89.8%, цель 90% не достигнута на один вопрос. [Данные теста](benchmarks/five-year-20261106/README.md).

![LivingMemory: долговременная память, связывающая предпочтения, людей, планы и контекст](docs/public/images/livingmemory-cover.svg)

<p align="center">
  <a href="https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en.md"><strong>Документация · EN</strong></a> ·
  <a href="https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/webui.md"><strong>Панель управления</strong></a> ·
  <a href="https://github.com/AKIhunter/astrbot_plugin_livingmemory/releases"><strong>Скачать</strong></a> ·
  <a href="https://github.com/AKIhunter/astrbot_plugin_livingmemory/issues"><strong>Обратная связь</strong></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-52675c?style=flat-square" alt="Python 3.10 или новее">
  <img src="https://img.shields.io/badge/AstrBot-4.24.2%2B-52675c?style=flat-square" alt="Для представлений плагина требуется AstrBot 4.24.2 или новее">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-52675c?style=flat-square" alt="AGPL-3.0"></a>
</p>

## Сохранить важное из разговора

LivingMemory хранит для AstrBot предпочтения, отношения, ход проектов и прежние договорённости. Плагин создаёт сводки диалогов, находит нужный контекст с помощью ключевых слов, векторов и графа, а также управляет памятью через архивирование, затухание и усиление при обращении.

> **Последняя стабильная версия: 2.10.0**<br>
> Исправлена загрузка 3D с авторизацией, улучшена диагностика SQLite и добавлены инструкции по обновлению AstrBot при ошибках времени.<br>
> [Скачать 2.10.0](https://github.com/AKIhunter/astrbot_plugin_livingmemory/releases/tag/2.10.0) · [Обновление · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/releases.md) · [Прежние версии](https://github.com/AKIhunter/astrbot_plugin_livingmemory/releases)

## От сохранения к воспоминанию

- **Автоматическая запись** — Сводки создаются после заданного числа реплик. Важные записи могут сохранять исходные сообщения для проверки и повторного обобщения.
- **Поиск с учётом контекста** — Документы и граф поддерживают поиск по ключевым словам и векторам, объединение рейтингов, фильтрацию области и проверку жизненного цикла.
- **Инструменты агента** — `recall_long_term_memory` и `memorize_long_term_memory` позволяют читать и записывать долговременную память по мере необходимости.
- **Управление через Views** — Просмотр графов 2D/3D, приближение к узлам и связанным воспоминаниям, редактирование памяти, тестирование поиска, настройка промптов и состояние системы.

## Одно пространство, четыре стиля

**Editorial** — зелёная сетка и чёткие линии. **Studio** — мягкие карточки. **Paper** — тёплые поверхности и заголовки с засечками. **Terminal** — моноширинный шрифт и приборная компоновка.

Каждый стиль поддерживает светлый, тёмный и автоматический режимы. Переключение доступно в настройках внешнего вида; выбор сохраняется в текущем браузере. При увеличении графа подписи остаются стабильными, а на телефоне доступны фильтр диалога и переход к записи.

Граф 3D использует отдельную сцену с тёплым светом, вращением, масштабированием, фокусировкой на узлах и связанными воспоминаниями. Узлы и связи отрисовываются пакетами на GPU.

[Темы и работа с панелью · EN →](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/webui.md)

## Начало работы

1. Установите плагин из каталога AstrBot или поместите выбранную версию в `data/plugins/astrbot_plugin_livingmemory`.
2. Сначала создайте и включите провайдер Embedding в разделе AstrBot «Model Providers → Embedding» (настройка бесплатной `BAAI/bge-m3` описана [здесь · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/guide/embedding-model.md)), затем перезагрузите AstrBot и укажите ID этого провайдера и LLM-провайдера в настройках LivingMemory. Пустое поле Embedding лишь выбирает первый доступный провайдер, что может привести к неверной модели или сбою инициализации индексов.
3. Откройте `Plugins → LivingMemory → Views → dashboard` (в старых версиях AstrBot раздел называется `Pages`). Для представлений плагина требуется **AstrBot 4.24.2 или новее**.

Для тестирования скачайте указанный выше тег и следуйте [инструкции по установке и откату · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/releases.md). Перед обновлением сохраните резервную копию данных и конфигурации плагина.

После нескольких реплик проверьте `/lmem status`, `/lmem summarize` и `/lmem search ключевые слова`.

## Бесплатная модель Embedding

Семантический поиск LivingMemory зависит от Embedding Provider. Рекомендуется бесплатная модель `BAAI/bge-m3` от SiliconFlow (1024 измерения). В разделе AstrBot «Model Providers → Embedding → Add Model Provider → OpenAI Embedding» укажите:

- Provider ID: оставьте значение, созданное AstrBot, например `openai_embedding`
- API Base URL: `https://api.siliconflow.cn/v1`
- API Key: ключ, созданный в консоли SiliconFlow
- Embedding Model: `BAAI/bge-m3`
- Embedding Dimensions: `1024`

Сохраните и протестируйте провайдер, затем укажите его Provider ID в `provider_settings.embedding_provider_id` настроек LivingMemory. ID модели и ID провайдера различаются; полная инструкция доступна [здесь · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/guide/embedding-model.md).

## Подробнее

[Быстрый старт · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/guide/getting-started.md) · [Настройки · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/configuration.md) · [Команды · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/commands.md) · [Архитектура · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/architecture.md) · [История изменений](CHANGELOG.md)

При обновлении с v1.4.0–v1.4.2 сначала прочитайте [инструкцию по миграции · EN](https://github.com/AKIhunter/astrbot_plugin_livingmemory/blob/main/docs/en/configuration.md#backup-migration-and-cleanup).

---

Сообщество: группа QQ 2: `1129994749` (новая) · [Группа QQ 1: 953245617](https://qm.qq.com/cgi-bin/qm/qr?k=WdyqoP-AOEXqGAN08lOFfVSguF2EmBeO&jump_from=webapi&authKey=tPyfv90TVYSGVhbAhsAZCcSBotJuTTLf03wnn7/lQZPUkWfoQ/J8e9nkAipkOzwh) (заполнена) · Пароль: `lxfight`<br>
Лицензия: [AGPL-3.0](LICENSE)
