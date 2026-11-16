# AML Copilot — расследования и лаборатория AI QA

Локальный учебный сервис для разбора синтетических AML-инцидентов. По идентификатору инцидента он собирает факты, находит фрагменты учебных правил и формирует проверяемый черновик. Оператор проходит пять шагов: подготовка → выбор инцидента → проверка источников → решение → сохранение и локальный экспорт.

Проект объединяет практику системного анализа, SQL, HTTP API, Kafka и проверки AI. Он позволяет проследить запрос от JSON-контракта до записи в БД, задания, отчёта и решения, а затем проверить эту цепочку тестами.

**Статус:** учебная реализация на синтетических данных. Банковские интеграции, производственный IAM, независимая AML-приёмка и доказанный бизнес-эффект отсутствуют. Эта версия подготовлена в rebuild; команды клонирования ниже становятся применимы после её публикации владельцем.

## Возможности

| Задача | Реализация |
| --- | --- |
| Проверить инцидент | Streamlit с пошаговой формой, историей заданий и раскрытием источников фактов |
| Получить воспроизводимый черновик | `template`: поиск MiniLM на CPU, текст и суммы формирует код |
| Изучить локальную LLM | `local-model`: Ollama упорядочивает разрешённые факты и правила; результат проходит строгую проверку |
| Практиковать SQL и API | PostgreSQL, DBeaver, FastAPI, локальный Swagger UI, заметки с контролем ревизии |
| Изучить событийную обработку | Kafka, сохранение входящих событий в PostgreSQL, повторная доставка, идемпотентность и DLQ |
| Проверить качество | Изолированные тесты, Playwright, реальные retrieval/LLM-прогоны, консультативный Judge и Dark Factory |
| Связать требования с проверками | Версионированный каталог рисков, положительные и отрицательные сценарии, запуск связанных тестов и отчёт о неподтверждённых требованиях |
| Определить влияние изменений | Объявленные пути и смены моделей связываются с компонентами, требованиями и профилями проверок; причины выбора сохраняются в плане |
| Проверить сами оценщики | Фиксированные контрольные примеры и намеренные ошибки наблюдений проверяют обнаружение дефектов, полноту измерения и сохранение FAIL |
| Разобрать сбои и восстановление | Пошаговые модели окружения проверяют реальные intake/scheduler, HTTP-клиент и SQLite при потере ACK, повторах и ошибках зависимостей |

**[Полная инструкция](docs/operator/service-and-lab.md)** содержит установку с нуля, ежедневный запуск, упражнения, диагностику, QA и команды публикации. Для первого занятия используйте `INC-000123`.

## Архитектура и данные

```text
Swagger / HTTP → Kafka → AML worker → PostgreSQL: входящие события и статусы
                               └──→ AML API → обработчик → SQLite: задания и отчёты
                                       ↑                       ↓
                               пошаговый UI ← проверка → решение → локальный экспорт

DBeaver / SQL ────────────────────────→ PostgreSQL
AML-обработчик → фиксированный JSON-снимок + MiniLM → template / Ollama
```

API владеет обработчиком в интегрированном режиме. Worker сначала сохраняет событие, затем обращается к API с устойчивым ключом идемпотентности. HTTP `202` подтверждает приём события брокером; готовность отчёта проверяется отдельно по `event_id` и `job_id`. Доставка допускает повторы. Универсальной гарантии exactly-once проект не заявляет.

PostgreSQL содержит эталон `reference`, таблицы для упражнений `practice` и состояние Kafka `kafka_lab`. Источником AML-черновика служит зафиксированный синтетический снимок. Изменение `practice` не меняет этот снимок. SQLite хранит задания, версии отчётов, решения и квитанции. В автономном UI и интегрированном API разные базы истории.

Текущую оркестрацию реализуют `src/runtime` и `src/workflow`. Документы исходного PoC о LangGraph/ReAct и старые показатели оценки сохранены с отметкой об историческом статусе.

## Требования

Поддерживаемая и проверенная конфигурация — **Windows x64, Python 3.12 x64, PowerShell 7, Git**. Linux и macOS отдельно не квалифицированы. Откройте корень проекта в VS Code и выберите терминал PowerShell 7. Команды выполняйте по одной; при ошибке остановитесь.

| Компонент | Когда нужен | Локальный адрес |
| --- | --- | --- |
| MiniLM, около 480 МБ файлов | Любое AML-расследование, включая `template` | Файлы в `var/models` |
| Streamlit | Работа оператора | `127.0.0.1:8501` |
| PostgreSQL 17.11 и FastAPI | SQL, Swagger, интегрированный режим | `127.0.0.1:55432`, `127.0.0.1:8765` |
| DBeaver | SQL через графический клиент | Подключение к локальному PostgreSQL |
| Docker Desktop, WSL 2, Linux containers | Kafka | `127.0.0.1:9092` |
| Ollama с `qwen2.5:14b` | Режим `local-model` и LLM Judge | `127.0.0.1:11434` |

Первичная установка требует доступа к источникам пакетов и моделей либо заранее подготовленных проверенных файлов. В обычной работе используются локальные сервисы. Держите одну активную копию стенда на машине. Требования к памяти и времени для Qwen зависят от оборудования; SLA и универсальный минимум RAM не установлены.

## Быстрый запуск: первый черновик

```powershell
git clone --branch main https://github.com/slim2115/aml-ai-agent-system-design.git aml-learning
Set-Location aml-learning
Get-Item .rebuild/origin.json, scripts/rebuild/Prepare-Embedding.ps1
py -3.12 -m venv .venv
.\scripts\rebuild\Enter-LocalEnvironment.ps1
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/windows-py312.lock
.\.venv\Scripts\python.exe -m pip check
.\scripts\rebuild\Prepare-Embedding.ps1 -Download
.\.venv\Scripts\python.exe -I -B -m streamlit run src/ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

Если `Prepare-Embedding.ps1` отсутствует, в клоне ещё старая версия: проверьте опубликованную ветку. Скрипт модели сверяет десять файлов фиксированной ревизии с SHA256. Установка из локального снимка: `.\scripts\rebuild\Prepare-Embedding.ps1 -SourceDirectory 'C:\models\MiniLM-snapshot'`. Без аргументов он проверяет уже установленные файлы.

Откройте [UI оператора](http://127.0.0.1:8501), нажмите **Подготовить поиск** и дождитесь готовности. Подтвердите учебный режим, выберите `INC-000123` и **Проверяемый шаблон (без LLM)**, создайте задание. Ожидаемый результат — черновик с фактами и источниками. Сверьте его, выберите решение и сохраните. Одобрение открывает экспорт в локальный симулятор. Завершите UI через **Ctrl+C** в терминале.

## Подключить SQL, Swagger и Kafka

Для SQL/API модель не требуется. В терминале из корня проекта:

```powershell
.\scripts\lab\Install-LabDependencies.ps1
.\scripts\lab\Initialize-Lab.ps1
.\scripts\lab\Start-Lab.ps1
Invoke-RestMethod http://127.0.0.1:8765/health -NoProxy
$access = Get-Content var/postgres/lab-connection.json -Raw -Encoding UTF8 | ConvertFrom-Json
$access.api_token | Set-Clipboard
.\scripts\lab\Setup-DBeaver.ps1 -Download
.\scripts\lab\Start-SqlDesktop.ps1 -ConnectRole reader
```

В [Swagger](http://127.0.0.1:8765/docs) нажмите **Authorize**, вставьте ключ из буфера и выполните `GET /lab/status`. Ожидается `ready`. В DBeaver выберите **AML — просмотр** и выполните `SELECT current_database(), current_user;`. Ожидаются `aml_lab` и `aml_reader`. Установку DBeaver выполняйте при закрытом клиенте.

Для полной цепочки запустите Docker Desktop, остановите автономный UI и выполните:

```powershell
.\scripts\kafka\Install-KafkaDependencies.ps1 -UsePackageIndex
.\scripts\kafka\Initialize-KafkaLab.ps1
.\scripts\kafka\Initialize-AmlIntegration.ps1
.\scripts\kafka\Broker.ps1 -Action Start -Flow AML
```

В отдельных терминалах запустите `.\scripts\kafka\Run-AmlWorker.ps1` и `.\scripts\rebuild\Start-IntegratedUI.ps1`. В UI подготовьте поиск. JSON события, его отправка через Swagger, SQL-проверка обработки и получение отчёта приведены в полной инструкции. После занятия остановите worker и UI через Ctrl+C, выполните `.\scripts\kafka\Broker.ps1 -Action Stop`, затем `.\scripts\lab\Stop-Lab.ps1`.

## Проверки и границы доказательств

Для быстрой проверки кода создайте отдельное окружение:

```powershell
py -3.12 -m venv .venv-ci
.\.venv-ci\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/ci-windows-py312.lock
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$checkDir = 'var/checks/local-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/check_ci_inputs.py --output-dir $checkDir
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/run_checks.py --output-dir $checkDir
```

Исполняемый каталог связывает 12 требований с 24 сценариями и 41 селектором тестов. Для отдельного прогона из того же окружения:

```powershell
$requirementsRun = 'var/qa/requirements/local-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_requirements.py run --output-dir $requirementsRun
Get-Content "$requirementsRun/report.md" -Encoding UTF8
```

Команда проверяет допуск задания, отмену, источники и факты, решения и экспорт, порядок сохранения Kafka-события, идемпотентность и границы AI-оценки. Она запускает связанные программные проверки с контролируемыми зависимостями. `PASS` подтверждает их выполнение в заявленной области; живые модели и полный стенд измеряются отдельно. Режим `validate` проверяет только описание и ссылки: итог требований остаётся `INCOMPLETE`. [Порядок работы и чтение статусов](docs/operator/service-and-lab.md#executable-requirements), [формат каталога](qa/requirements/README.md).

Локальный прогон каталога 12 ноября 2026 года подтвердил все 12 требований в этой области: выполнены 103 собранных тестовых случая. Продуктовая приёмка осталась `INCOMPLETE`, допуск кандидата — `NOT_EVALUATED`.

Для планирования проверок после изменения кода или модели:

```powershell
$impactRun = 'var/qa/impact/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/plan_changes.py --changed src/runtime/transport.py --model-changed llm --output-dir $impactRun
Get-Content "$impactRun/plan.md" -Encoding UTF8
```

Это декларация изменений для анализа: команда не редактирует указанный файл и не заменяет модель. Карта 20 компонентов объясняет цепочки влияния и выбор проверок. `READY` означает готовый план; выполнение остаётся `NOT_RUN`, полнота входов — `NOT_VERIFIED`. Неизвестные пути дают `INCOMPLETE` и полный резервный набор. Полная регрессия обязательна при любом плане. [Рабочие примеры](docs/operator/service-and-lab.md#change-impact), [контракт карты и результата](qa/impact/README.md).

Этап 3 добавляет единый запуск четырёх детерминированных профилей и привязку свидетельств к текущим исходникам и окружению:

```powershell
$executionRun = 'var/qa/execution/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_plan.py --plan-dir $impactRun --output-dir $executionRun
Get-Content "$executionRun/report.md" -Encoding UTF8
```

Полная регрессия, требования, UI/CLI и входы CI выполняются заново. Для приведённого выше изменения LLM измерения модели остаются `NOT_RUN`: даже при `deterministic_status=PASS` весь план остаётся `INCOMPLETE`, код `3`. Изменение исходников, старые результаты, тайм-аут или неподтверждённая очистка процессов не дают успешного подтверждения версии. [Команды и диагностика](docs/operator/service-and-lab.md#owned-qa-execution), [границы свидетельств](qa/execution/README.md).

Этап 4 добавляет отдельную программную квалификацию оценщиков. Каталог задаёт ожидаемые ответы на корректные, ошибочные и неполные наблюдения; намеренные подмены проверяют чувствительность контрольного набора:

```powershell
$qualificationRun = 'var/qa/evaluator-qualification/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/qualify_evaluators.py --output-dir $qualificationRun
Get-Content "$qualificationRun/report.md" -Encoding UTF8
```

Это проверка программных измерителей на синтетических контролях, без вызовов моделей. PASS не доказывает пригодность настоящего LLM Judge или качество AML-решений. [Порядок работы и учебный разбор](docs/operator/service-and-lab.md#evaluator-qualification), [паспорт квалификации](qa/qualification/README.md).

Этап 5 добавляет воспроизводимые последовательности сбоев и восстановления:

```powershell
$environmentRun = 'var/qa/environments/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_environments.py --output-dir $environmentRun
Get-Content "$environmentRun/report.md" -Encoding UTF8
```

Выполняются настоящий планировщик, HTTP-клиент и SQLite; Kafka, очередь PostgreSQL, сеть и время моделируются явно. Отчёт сохраняет промежуточные состояния и порядок операций. Это не проверка живой инфраструктуры или SLA. [Порядок работы](docs/operator/service-and-lab.md#environment-scenarios), [границы модели](qa/environments/README.md).

GitHub Actions содержит два задания: `hermetic-windows` и `browser-smoke-windows`. Они проверяют детерминированные сценарии и браузер с подменённым вычислителем. Автоматический deployment и GitLab CI не настроены; Markdown-документы и относительные ссылки пригодны для GitHub и GitLab.

Проверки второго этапа развития Dark Factory 11–12 ноября 2026 года: **2372 теста и 645 подслучаев без ошибок и пропусков**, 29 проверок UI/CLI и сверка обоих контрактов. В общий набор вошёл 81 новый тест планировщика и CLI. Локальное окружение соответствует закреплённым зависимостям CI. Последний Playwright smoke относится к репетиции поставки 5 ноября. Это результаты конкретных версий и окружений; успешный удалённый CI после публикации проверяется отдельно. Живые MiniLM, Ollama и Kafka не подменяются этой статистикой.

В Dark Factory раздельно учитываются качество измерения, качество кандидата и обнаружение внесённого дефекта. Учебный Judge показал ложные одобрения и остаётся консультативным. Независимая калибровка, скрытая контрольная выборка и продуктовая приёмка не завершены. Проект не принимает AML-решения автоматически и не подтверждает соответствие банковским требованиям.

## Структура проекта

| Путь | Содержимое |
| --- | --- |
| `src/runtime`, `src/workflow`, `src/ui` | Вычисление, задания, хранение, решения, интерфейс |
| `src/lab`, `lab`, `scripts/lab`, `scripts/kafka` | HTTP API, PostgreSQL, DBeaver, Kafka и упражнения |
| `contracts/v1`, `config`, `data/demo-v1` | Контракты, учебная политика, фиксированные источники |
| `tests`, `qa`, `.github/workflows` | Проверки кода, браузера, моделей и CI |
| `docs` | Эксплуатация, архитектурные решения, история и публикация |
| `var` | Локальные базы, модели, ключи, логи и результаты; в Git не отправляется |

Исходники учебного стенда публикуются, чтобы каждый инженер создавал свою среду. Личные БД, ключи, DBeaver workspace, сообщения Kafka и результаты занятий остаются локальными. Владелец выполняет [четыре коммита и три push](docs/publication.md) самостоятельно.

## Помощь и участие

Владелец проекта — [slim2115](https://github.com/slim2115). Ошибку можно описать в [Issues](https://github.com/slim2115/aml-ai-agent-system-design/issues): версия/коммит, режим запуска, шаги, ожидаемый и фактический результат, обезличенный фрагмент лога. Пароли, API-ключи, базы и исходные данные в обращение не включайте. Перед предложением изменения проверьте контракты и детерминированные тесты; изменение политики или модели требует отдельной проверки качества.

В исходном README проекта заявлена MIT; отдельного файла `LICENSE` с полным текстом и атрибуцией в текущей поставке пока нет. Сторонние компоненты и модели сохраняют собственные лицензии.
