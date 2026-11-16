# Как владельцу опубликовать rebuild в main

Работайте в PowerShell 7 из VS Code. Предусмотрены **четыре коммита и три push** в `main`. Первый отправляет правила Git, второй — совместимую версию сервиса и тестов двумя коммитами, третий — документацию. Каждый commit и push выполняет владелец. Выполняйте команды по одной и останавливайтесь при первой ошибке.

Кратко о составе обновления: [что добавить и что заменить](publication/what-to-update.md), [все новые файлы](publication/added-files.txt), [все заменяемые файлы](publication/updated-files.txt).

## Состав поставки

| Блок | Что попадёт в GitHub | Точные пути для git add |
| --- | --- | --- |
| 1 | Правила исключений и сохранения байтов тестовых наборов | [01-git.txt](publication/01-git.txt) |
| 2 | AML, UI, PostgreSQL, Swagger, SQL-упражнения, Kafka, DBeaver, промпты, синтетические исходные данные, контракты, версии зависимостей, скрипты установки | [02-service.txt](publication/02-service.txt) |
| 3 | QA, Playwright, тесты моделей, Dark Factory, каталог требований, анализ влияния, единый запуск, квалификация оценщиков и сценарии окружения, CI, инструмент копирования. Удаляется унаследованный `tests/pytest.ini`, настройки тестов теперь в корне | [03-qa.txt](publication/03-qa.txt) |
| 3, исключения | Пять синтетических входов Judge, используемых тестами с проверкой хэшей | [03-judge-fixtures.txt](publication/03-judge-fixtures.txt) |
| 4 | README, справочные материалы, инструкция оператора, этот порядок отправки и списки файлов | [04-docs.txt](publication/04-docs.txt) |

Сопоставление с базовым `main`, обновлённое 16 ноября 2026 года, даёт следующий состав. «Без изменения» означает, что файл есть в проверенном списке, но Git не создаст для него новый diff.

| Список | Новых файлов | Заменяемых файлов | Без изменения |
| --- | ---: | ---: | ---: |
| `01-git` | 1 | 1 | 0 |
| `02-service` | 129 | 13 | 37 |
| `03-qa` | 376 | 4 | 5 |
| `03-judge-fixtures` | 5 | 0 | 0 |
| `04-docs` | 45 | 17 | 0 |
| **Всего в списках: 633** | **556** | **35** | **42** |

В сервисном блоке заменяются конфигурация, зависимости, фасад агента, UI, загрузчик базы знаний и прежние синтетические данные. Новыми добавляются runtime/workflow, контракты v1 и исходники SQL/API/Kafka/DBeaver. В QA-блоке обновляются workflow GitHub Actions, конфигурация/проверки прежнего оценщика и добавляется новый QA-контур. В документации обновляются README и 16 исходных спецификаций/диаграмм; их исторический статус теперь обозначен явно.

Отдельно удаляется только `tests/pytest.ini`. Унаследованный `reports/iteration_log.md` сохраняется без изменения и не добавляется заново. Итоговое дерево Git содержит 634 файлов. Точный перечень изменений каждого блока виден в `git diff --cached --name-status` перед соответствующим коммитом.

**Остаются у владельца**: весь `var/` (базы PostgreSQL/SQLite, рабочее пространство DBeaver, роли и ключи, модели и индексы, логи, результаты упражнений), Docker volumes и сообщения Kafka, окружения Python, `.env`, ярлыки, `qa/progress.json`, исходный аудит, архивы и результаты прогонов из `verification/` кроме указанных пяти файлов. Из `.rebuild/` отправляются только `origin.json` и `release-version.json`. Исходники стенда нужны в GitHub, чтобы другой инженер смог развернуть собственную базу и брокер. Уже существующие в истории GitHub материалы этим порядком не удаляются, за исключением указанного файла pytest.

## Подготовить отдельный клон

Пути ниже относятся к машине владельца. Оставьте исходный checkout и рабочий стенд в их каталогах. Для отправки нужен новый каталог. Сохраните переменные в одном терминале до окончания публикации.

```powershell
$src = 'D:\work\aml-ai-agent-system-rebuild'
$pub = 'D:\work\aml-ai-agent-system-publish'
$lists = Join-Path $src 'docs\publication'
if (Test-Path -LiteralPath $pub) { throw 'Для публикации требуется новый каталог. Проверьте предыдущую копию.' }
git clone --branch main --single-branch https://github.com/slim2115/aml-ai-agent-system-design.git $pub
Set-Location -LiteralPath $pub
git status --short --branch
git remote -v
git log -1 --oneline
& "$src\.venv\Scripts\python.exe" -I -B "$src\scripts\release\publication_files.py" copy-to $pub
if ($LASTEXITCODE -ne 0) { throw 'Перенос не выполнен. Проверьте сообщение перед продолжением.' }
```

Помощник проверяет чистый клон, имя каталога, ветку, origin и базовый коммит `535940fd645c2b41d1ec167e8a98d58597adadf0`. Если удалённая ветка уже продвинулась, сначала сопоставьте новые изменения с rebuild и пересмотрите списки. При прерванном копировании сохраните проблемный каталог для разбора и начните с чистого клона. Помощник только копирует перечисленные файлы и сверяет их байты. Из-за ограничений защиты веток GitHub при отказе в push потребуется решение владельца репозитория.

## Коммит 1 и push 1. Правила Git

В этом push меняются только правила Git. Новый workflow появится после push 2; первый push не является проверкой нового CI.

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\01-git.txt"
git diff --cached --name-status
git diff --cached
git commit -m "chore(git): исключить локальный стенд и сохранить байты тестовых наборов"
git push origin main
```

## Коммит 2. Сервис и исходники учебного стенда

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\02-service.txt"
git diff --cached --name-status
git diff --cached --stat
git commit -m "feat(aml): добавить сервис расследований и стенд PostgreSQL, Swagger и Kafka"
```

Здесь сделайте только коммит. Следом добавьте проверки именно этой версии сервиса.

## Коммит 3 и push 2. Тесты и CI

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\03-qa.txt"
git --literal-pathspecs add -f --pathspec-from-file="$lists\03-judge-fixtures.txt"
git rm -- tests/pytest.ini
git diff --cached --name-status
git diff --cached --stat
git diff --cached --name-only -- verification
git commit -m "test(qa): добавить Playwright, проверки моделей, Dark Factory и Windows CI"
git push origin main
```

В выводе по `verification` ожидаются ровно пять путей из списка. Проверяйте каждый файл перед отправкой. В GitHub откройте вкладку **Actions** и дождитесь результата заданий `hermetic-windows` и `browser-smoke-windows` для верхнего коммита.

При падении задания сохраните URL запуска и разберите причину до следующего push. Успешная локальная репетиция не заменяет этот результат. Автоматический deployment не настроен.

## Коммит 4 и push 3. Инструкции

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\04-docs.txt"
git diff --cached --name-status
git diff --cached --stat
git commit -m "docs: описать развёртывание, работу оператора и публикацию по блокам"
git push origin main
git fetch origin main
git rev-parse HEAD origin/main
git status --short --branch
git log -4 --oneline
```

Два хэша в выводе `rev-parse` должны совпасть, рабочая копия должна быть чистой. Откройте README на GitHub, проверьте ссылку на [сквозное руководство](operator/service-and-lab.md) и результат Actions для последнего коммита. Если push отклонён из-за новых удалённых изменений, остановитесь и сравните истории перед повтором.
