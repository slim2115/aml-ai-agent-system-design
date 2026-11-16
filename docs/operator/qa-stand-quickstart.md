# Изолированный QA-стенд: запуск и проверка

Основная установка и полный рабочий цикл собраны в [единой инструкции](service-and-lab.md). Команды этого справочника выполняются из корня вашей копии. Упоминания сохранённых прогонов относятся к указанной дате; их локальные архивы не входят в обычный клон.

Инструкция для Windows и терминала **PowerShell 7** в VS Code, актуальна на 08.10.2026. Стенд шага 2 нужен для проверок API, SQL и Kafka с отдельными данными. Обычные команды учебного сервиса сохраняют свои настройки.

Нужны уже подготовленные `.venv`, PostgreSQL из `var/tools/postgresql-17.11` и работающий Docker Desktop с Linux engine и локальным образом Kafka из `lab/kafka/compose.yaml`. Команды ниже ничего не устанавливают и не загружают модели или Docker-образы. Одновременно запускается один QA-стенд.

## 1. Подготовить новый запуск

```powershell
# Терминал открыт в корне вашей копии проекта.
$qaDescription = & .\.venv\Scripts\python.exe -I -B .\qa\tools\stand.py prepare
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить QA-стенд' }
$qaStand = $qaDescription | ConvertFrom-Json
$qaRunId = $qaStand.run_id
$qaBase = $qaStand.data_directory
$qaRunId
```

Сохраните показанный `run_id` в заметках. `prepare` создаёт новый каталог данных, выделяет каталог для отдельной истории SQLite, копирует синтетические источники и политику, создаёт собственные учётные данные. Файл истории SQLite появится при первом обращении к API. Службы пока не запускаются. Не меняйте копии источников во время проверки: для повторяемого результата нужен тот же снимок данных.

В новом терминале восстановите идентификатор, подставив **свой** сохранённый `run_id`:

```powershell
# Терминал открыт в корне вашей копии проекта.
$qaRunId = 'ВСТАВЬТЕ_СОХРАНЁННЫЙ_RUN_ID'
$qaBase = Join-Path (Get-Location) ('var\qa\runs\' + $qaRunId)
```

Не выполняйте `prepare` повторно для продолжения существующего запуска: используйте его `run_id`.

## 2. Запустить и проверить состояние

```powershell
& .\.venv\Scripts\python.exe -I -B .\qa\tools\stand.py start --run-id $qaRunId
if ($LASTEXITCODE -ne 0) { throw 'Запуск прерван; проверьте status и логи QA' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\stand.py status --run-id $qaRunId
```

Ожидаемые признаки готовности служб: `status: ready` в ответе `start`; в `status` — `postgres_running: true`, `broker.status: ready`, значения `api`, `ui`, `worker`, `review-worker` в `services` равны `true`. Это готовность инфраструктуры; готовность AML-поиска проверяется отдельно.

| Компонент | Адрес QA |
|---|---|
| Пошаговый интерфейс | [http://127.0.0.1:18501](http://127.0.0.1:18501) |
| Swagger | [http://127.0.0.1:18765/docs](http://127.0.0.1:18765/docs) |
| PostgreSQL | `127.0.0.1:56432`, имя БД из `postgres.database` ответа `prepare` |
| Kafka | `127.0.0.1:29092`; топики и группы из ответа `prepare` |

QA использует собственные роли PostgreSQL, отдельный кластер и БД, Kafka Compose project, volume, топики и группы. Файлы находятся в `var/qa/runs/<run_id>`, история — в `var/workflow/qa/<run_id>/api-workflow.sqlite3`. Порты фиксированы; заменять их в файлах вручную не нужно.

### Доступ к Swagger и SQL

Учётные данные лежат в **приватном** файле `<qaBase>/lab-connection.json`. Не публикуйте этот файл и не добавляйте ключи или пароли в отчёты.

```powershell
$qaAccess = Get-Content -LiteralPath (Join-Path $qaBase 'lab-connection.json') -Raw | ConvertFrom-Json
$qaAccess.api_token | Set-Clipboard
```

В QA Swagger нажмите **Authorize**, вставьте ключ без `Bearer`, подтвердите. Ключ используется в заголовке `X-Lab-Key`. После вставки можно очистить буфер: `Set-Clipboard -Value ''`.

Для SQL-клиента используйте host, port и database из `$qaAccess`, пользователя и пароль из `roles.student`. Роль `reader` предназначена для чтения; `admin` не нужна для обычных упражнений. Подключайтесь именно к QA-порту `56432`.

## 3. Запустить отдельную автоматическую проверку

Оставьте модель неподготовленной: на этом шаге не нажимайте «Подготовить локальную модель» и не вызывайте подготовку через API. Ожидаемый `/v1/readiness` — `ready: false`; это не ошибка инфраструктуры. Проверка фиксирует `models_started: false` и не подтверждает качество AML-отчётов.

```powershell
$qaEvidence = 'verification/qa-step-02-user-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_checks.py --run-id $qaRunId --output-dir $qaEvidence
if ($LASTEXITCODE -ne 0) { throw ('Проверка не пройдена; результаты: ' + $qaEvidence) }
Get-Content -LiteralPath (Join-Path $qaEvidence 'result.json') -Raw
```

Каталог результатов должен быть **новым** для каждого прогона. Проверка записывает заметку, отправляет синтетическое Kafka-событие и его повтор, сверяет результат в QA PostgreSQL и проверяет принадлежность API/SQLite данному запуску. Эти учебные записи остаются в QA-данных.

В каталоге результата сохраняются `result.json`, `tests.xml` и, после успешного сквозного сценария, `observations.json`. `status: PASS` относится только к выполненному профилю. `FAIL` требует разбора отчёта и журналов; доступность страницы Swagger сама по себе не означает успешный прогон.

Профиль запускается через `qa/tools/run_checks.py`; параметр `--run-integration` не нужен. Обычные изолированные тесты проекта остаются отдельным набором.

Граница проверки зафиксирована: реальные HTTP-проверки готовности и цепочка **API → Kafka → обработчик учебной сводки → PostgreSQL**, включая повтор события. Полный AML-анализ с моделью, браузерные сценарии Playwright, LLM Judge и принятие отчёта экспертом в этот шаг не входят.

Python-соединения тестового профиля разрешены только к четырём QA-портам на `127.0.0.1`. Для нативных драйверов PostgreSQL/Kafka адреса ограничиваются проверенной конфигурацией. Это не системный firewall и не изоляция произвольного стороннего кода или браузера. Ресурсы Swagger этот сервис отдаёт локально.

## 4. Остановить с сохранением данных

```powershell
& .\.venv\Scripts\python.exe -I -B .\qa\tools\stand.py stop --run-id $qaRunId
& .\.venv\Scripts\python.exe -I -B .\qa\tools\stand.py status --run-id $qaRunId
```

Ожидаемый ответ `stop`: `status: stopped`, `data: retained`. Остановка относится к ресурсам указанного запуска. Данные PostgreSQL, SQLite и Kafka сохраняются; `start` с тем же `run_id` продолжает работу. Автоматическое удаление и сброс данных пока не реализованы. Для нового независимого опыта сначала остановите текущий запуск, затем выполните новый `prepare`.

## Если возникла ошибка

Журналы находятся в `<qaBase>/logs`: `api.log`, `ui.log`, `worker.log`, `review-worker.log`, `postgres.log` и журналы запуска/инициализации.

```powershell
Get-Content -LiteralPath (Join-Path $qaBase 'logs\api.log') -Tail 60
```

| Сообщение / признак | Действие |
|---|---|
| `QA_SERVICE_PORT_BUSY`, `QA_POSTGRES_PORT_BUSY`, `QA_KAFKA_PORT_IN_USE` | Сверьте `run_id` и выполните `status`. Если порт занят вашим прежним QA-запуском, остановите именно его через `stop --run-id`. Чужие процессы не завершайте. |
| `ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST` | Завершите предыдущий QA-запуск по его сохранённому `run_id`, затем повторите текущий `start`. |
| `QA_OWNER_MISMATCH`, `QA_PROCESS_OWNER_MISMATCH` или ошибка принадлежности Kafka/PostgreSQL | Проверьте идентификатор и путь проекта. Не редактируйте маркеры владельца и не удаляйте БД: сохраните код ошибки для разбора. |
| Состояние `stale`, `QA_SERVICE_STATE_REQUIRES_STOP` или `QA_SERVICE_START_ALREADY_PENDING` | Выполните `status`, затем `stop --run-id` для своего запуска; после успешной остановки повторите `start`. |
| `QA_SERVICE_START_TIMEOUT` или `QA_SERVICE_EXITED` | Прочитайте соответствующий журнал; выполните `stop` своего запуска перед повторным стартом. Часть зависимостей может ещё работать. |
| `QA_SERVICE_STOP_TIMEOUT` или `dependencies-left-running` | Служба ещё не подтвердила остановку; сохраните логи, проверьте `status` и повторите `stop` после устранения причины. Не запускайте другой QA-стенд до подтверждённой остановки. |
| Ошибка Docker / отсутствует образ | Убедитесь, что Docker Desktop запущен с Linux engine. Этот launcher не устанавливает Docker и не скачивает отсутствующий образ. |
| `QA_EVIDENCE_ALREADY_EXISTS` | Создайте новое значение `$qaEvidence`; старые результаты не перезаписываются. |
| `QA_RUN_ALREADY_EXISTS` | Для существующего запуска используйте `start --run-id`; для нового — `prepare` без заданного идентификатора. |

Не используйте массовое завершение `python`, `postgres` или всех контейнеров: оно может остановить обычный учебный сервис. Для обратной связи достаточно `run_id`, команды, кода ошибки и фрагмента соответствующего журнала без секретов.
