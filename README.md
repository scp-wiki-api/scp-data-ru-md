# SCP API — Markdown / Русский

Автоматически сгенерированный датасет материалов [SCP Wiki](http://www.scp-wiki.net/)
в формате Markdown с переводом на русский язык через [argostranslate](https://github.com/argosopentech/argostranslate).

**Исходные данные:** [scp-data/scp-api](https://github.com/scp-data/scp-api) — обновляется ежедневно.

## Быстрый старт

```bash
pip install -r requirements.txt
python workflow.py --section items --limit 50
```

## Опции

| Флаг | Описание |
|------|----------|
| `--section` | Один из: `items`, `tales`, `hubs`, `goi` (по умолчанию — все) |
| `--limit N` | Ограничить количество статей на секцию |
| `--no-translate` | Пропустить перевод (только конвертация JSON→Markdown) |
| `--github-token` | GitHub PAT для повышения лимита API (или `GITHUB_TOKEN` env) |
| `--workers N` | Потоки для параллельной записи (по умолчанию: 4) |
| `--verbose` | Подробный вывод |

## Структура репозитория

```
.
├── workflow.py        # Главный скрипт
├── requirements.txt
├── LICENSE            # CC BY-SA 3.0
├── README.md
├── cache/             # Кэш загруженных JSON-файлов (git-ignored)
└── output/            # Результат (git-ignored)
    ├── items/         # Объекты СЦП (SCP-001 … SCP-9999+)
    ├── tales/         # Рассказы
    ├── hubs/          # Хабы
    └── goi/           # Группы Интересов
```

## Как работает workflow

```
GitHub API (scp-data/scp-api)
        │
        ▼
  cache/<section>/index.json          ← метаданные всех статей
  cache/items/content_<series>.json   ← HTML-контент по сериям (items only)
        │
        ▼
  Парсинг JSON (ijson / стриминг для больших файлов)
        │
        ▼
  _clean_html() → убирает теги, сохраняет параграфы
        │
        ▼
  argostranslate en→ru (офлайн-модель)
        │
        ▼
  output/<section>/<slug>.md
```

Файлы content_series-*.json могут весить 20–40 МБ. Скрипт кэширует их
в `cache/` — повторный запуск загружает только изменившиеся.

## Лицензия

Весь контент SCP Wiki распространяется по лицензии
**[Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)](https://creativecommons.org/licenses/by-sa/3.0/)**
в соответствии с [политикой лицензирования SCP Wiki](http://www.scp-wiki.wikidot.com/licensing-guide).

Данный проект является производным произведением и распространяется на тех же условиях.
Подробности — в файле [LICENSE](./LICENSE).
