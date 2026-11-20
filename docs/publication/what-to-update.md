# Что обновить для этапов 6–8 Dark Factory

**Сверка на 20 ноября 2026 года.** Основная реализация и этап 5 уже находятся в [main](https://github.com/slim2115/aml-ai-agent-system-design) на коммите `97d8e07c990186bd9e6b7621a9e06224316ad65d`. Этапы 6 и 7 ещё не опубликованы: перечни включают этапы 6–8 вместе. Старый порядок первой публикации и восемь замен этапа 5 повторять не нужно.

## Изменения относительно опубликованной версии

- **Добавить 34 файлов:** точный перечень в [added-files.txt](added-files.txt).
- **Обновить 11 файлов:** точный перечень в [updated-files.txt](updated-files.txt).
- **Удалений нет.** `tests/pytest.ini` уже отсутствует в опубликованном дереве.
- **622 файлов из полных списков совпадают с main.** Сохранённый `reports/iteration_log.md` также остаётся в дереве.

| Коммит | Состав | Назначение |
| --- | --- | --- |
| 1. QA, 37 файлов | Новые `qa/stability/`, `qa/autonomy/`, `qa/analyst/`, их CLI и тесты; изменённые файлы из `03-qa.txt` | Статистика, бюджет перед каждым вызовом, исторический пересчёт, ограниченное исправление JSON-кандидатов, локальная Ollama, журнал, управление worker, интерфейс аналитика и карта влияния |
| 2. Документация, 8 файлов | README сервиса, эксплуатация, публикация и её перечни; изменённые файлы из `04-docs.txt` | Запуск и интерпретация этапов 6–8, ограничения статистики и цикла, точные добавления/замены и два commit/push |

Все новые пути перечислены ниже; существующие файлы для замены даны по одному пути на строку в `updated-files.txt`.

```text
qa/analyst/README.md
qa/analyst/__init__.py
qa/analyst/app.py
qa/analyst/projection.py
qa/analyst/reader.py
qa/autonomy/README.md
qa/autonomy/__init__.py
qa/autonomy/candidates.py
qa/autonomy/cycle.py
qa/autonomy/demo.py
qa/autonomy/providers.py
qa/autonomy/runner.py
qa/autonomy/worker.py
qa/stability/README.md
qa/stability/__init__.py
qa/stability/budget.py
qa/stability/demo.py
qa/stability/history.py
qa/stability/runner.py
qa/stability/statistics.py
qa/tools/_autonomy_worker.py
qa/tools/check_analyst_browser.py
qa/tools/run_autonomy.py
qa/tools/run_stability.py
tests/dark_factory/test_analyst_reader.py
tests/dark_factory/test_analyst_ui.py
tests/dark_factory/test_autonomy_candidates.py
tests/dark_factory/test_autonomy_cycle.py
tests/dark_factory/test_autonomy_providers.py
tests/dark_factory/test_autonomy_runner.py
tests/dark_factory/test_stability_budget.py
tests/dark_factory/test_stability_history.py
tests/dark_factory/test_stability_runner.py
tests/dark_factory/test_stability_statistics.py
```

Пять полных списков `01-git` — `04-docs` содержат **667 файлов**. Они используются для копирования и проверки всей поставки; Git создаёт diff только для указанных добавлений и замен. С сохранённым `reports/iteration_log.md` итоговое дерево содержит **668 файлов**. Пять синтетических входов Judge сохраняются без изменений. Исходники runtime и учебной интеграционной цепочки уже опубликованы.

## Как отправить

Используйте [самодостаточную инструкцию с командами](../publication.md). Новый чистый клон создаётся в отдельном каталоге `publication-stage8/aml-ai-agent-system-publish` внутри work; помощник проверяет origin, main, чистоту каталога и оба базовых SHA. После копирования сверяются байты, точные перечни новых и изменённых файлов, затем точные staged-наборы A/M каждого коммита. Удаления не предусмотрены.

Сначала выполняется commit/push QA, затем commit/push документации. У каждого коммита есть понятное сообщение с двумя параметрами `-m`. Commit и push выполняет владелец; после каждого push нужно проверить GitHub Actions для нового SHA. Если main изменился, базовый guard остановит перенос: сначала нужна повторная сверка.

Статистика сохраняет ошибки и пропуски в полном плане наблюдений; условный интервал Wilson не подтверждает перенос на реальную популяцию. Исторический пересчёт не вызывает LLM заново и не подтверждает ранее не заданный бюджет. Новый автономный цикл исправляет только ограниченный JSON-кандидат ответа; он не изменяет исходники, не разрешает релиз и сохраняет первый FAIL даже после исправления. Для него есть отдельный режим настоящей локальной Ollama. Условные repair-попытки не объявляются независимыми статистическими повторами.

## Что остаётся локально

Публикуются исходники учебного стенда для развёртывания другим инженером. Ваши базы PostgreSQL/SQLite, сообщения Kafka и Docker volumes, DBeaver workspace, модели и индексы, окружения Python, `.env`, секреты, логи и результаты занятий остаются локально.

Новые результаты из `var/` и `verification/`, исходный аудит и `qa/progress.json` не добавляются. Пять ранее опубликованных входов Judge — единственное исключение для `verification/`. Из `.rebuild/` в поставке только `origin.json` и `release-version.json`. Личный `portfolio_rodin_sergey.md` автоматически не включается.

Внешние инструкции владельца в каталоге work не входят в 667 опубликованных путей. Раздел 11 руководства и `docs/publication.md` содержат те же исполняемые команды. Старый checkout и уже использованные клоны сохраняются.
