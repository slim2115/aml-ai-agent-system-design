# Эксплуатация AML Copilot и учебного стенда

Это основной документ для установки, ежедневной работы и практики. Все примеры относятся к синтетическим данным. Они выполняются из корня вашей копии проекта в **PowerShell 7** в терминале VS Code. В DBeaver выполняется только SQL, в Swagger — HTTP-запросы. Команды запускайте по одной; после ошибки сначала устраните её причину.

## Содержание

1. [Устройство и выбор режима](#modes)
2. [Установка с нуля](#setup)
3. [Ежедневный запуск](#start)
4. [Работа оператора: пять шагов](#operator)
5. [SQL в DBeaver](#sql)
6. [Swagger и проверка API через SQL](#api)
7. [Полная цепочка Kafka → AML](#kafka)
8. [Остановка и восстановление](#stop)
9. [Диагностика](#troubleshooting)
10. [QA, модели и Dark Factory](#qa)
11. [Публикация по логическим блокам](#publication)
12. [Разбор решения для системного аналитика](#analysis-case)

<a id="modes"></a>
## 1. Устройство и выбор режима

| Режим | Что запускается | Где результат |
| --- | --- | --- |
| Автономный сервис | Streamlit владеет обработчиком | UI 8501, `var/workflow/workflow.sqlite3` |
| SQL/API | PostgreSQL и FastAPI; модели для упражнений не требуются | DBeaver и Swagger 8765 |
| Интегрированный сервис | API владеет обработчиком; UI обращается к API | UI 8501 и API используют `var/workflow/api-workflow.sqlite3` |
| Аналитик QA | Отдельный Streamlit читает сохранённые QA-свидетельства и учебные расследования | UI 8502, файлы `var/qa` |
| Полный событийный путь | SQL/API + Kafka + AML worker + интегрированный UI | Событие и статус в PostgreSQL, задание и отчёт в SQLite, решение в UI |

Порты: UI `8501`, API `8765`, PostgreSQL `55432`, Kafka `9092`, Ollama `11434`. Все адреса локальные. Держите одну активную копию стенда. Два режима UI используют один порт; перед переключением остановите текущий UI через Ctrl+C. Их истории различаются: задание из API нужно искать в интегрированном режиме.

`template` ищет правила настоящей MiniLM на CPU и формирует текст кодом. `local-model` вызывает Ollama для упорядочивания разрешённых фактов и правил. В обоих режимах источником отчёта остаётся фиксированный JSON-снимок. SQL-таблицы `practice` служат для упражнений и не меняют этот снимок. `reference` содержит эталон, `kafka_lab` — события и статусы обработки.

Для первых занятий достаточно `INC-000123`, учебной политики и `template`. Значения сумм, валют и дат берите из результата, а не из предположений о реальных банковских правилах.

<a id="setup"></a>
## 2. Установка с нуля

### 2.1. Среда и исходники

Нужны Windows x64, Python 3.12 x64, PowerShell 7 и Git. Для Kafka дополнительно установите WSL 2 и Docker Desktop, самостоятельно примите условия Docker и дождитесь готовности Linux engine. Ollama нужна только для локальной генерации и Judge. Работа на Linux/macOS отдельно не проверена.

После публикации rebuild владельцем:

```powershell
git clone --branch main https://github.com/slim2115/aml-ai-agent-system-design.git aml-learning
Set-Location aml-learning
Get-Item .rebuild/origin.json, scripts/rebuild/Prepare-Embedding.ps1
py -3.12 -m venv .venv
.\scripts\rebuild\Enter-LocalEnvironment.ps1
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/windows-py312.lock
.\.venv\Scripts\python.exe -m pip check
```

Если вы уже работаете в подготовленном rebuild, повторно клонировать его и создавать `.venv` для обычного занятия не требуется. Отсутствие `Prepare-Embedding.ps1` после clone означает, что вы получили прежнюю версию. Проверьте ветку и опубликованный коммит.

`Enter-LocalEnvironment.ps1` задаёт переменные текущего PowerShell-процесса и локальные пути. Сообщение `No application or model started` нормально: запуск сервисов выполняется отдельно. После закрытия терминала настройки нужно применить снова. Пароли и ключи создаются локально; в документацию они не включаются.

При установке без PyPI используйте подготовленный каталог wheels вместо обычной команды pip:

```powershell
$wheelDir = Read-Host 'Полный путь к проверенным wheels для Windows Python 3.12'
.\.venv\Scripts\python.exe -m pip install --no-index --find-links $wheelDir --only-binary=:all: --require-hashes -r requirements/windows-py312.lock
.\.venv\Scripts\python.exe -m pip check
```

### 2.2. Поисковая модель

Для расследования нужен локальный снимок MiniLM, около 480 МБ:

```powershell
.\scripts\rebuild\Prepare-Embedding.ps1 -Download
```

Скрипт сверяет десять файлов ревизии `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` с `config/embedding-files.json`. Для автономной установки вместо `-Download` укажите `-SourceDirectory 'C:\models\MiniLM-snapshot'`. Каталог должен содержать все файлы, включая `1_Pooling`. Команда без параметров проверяет установленный снимок. Не подменяйте манифест при ошибке хэша.

### 2.3. PostgreSQL и API

```powershell
.\scripts\lab\Install-LabDependencies.ps1
.\scripts\lab\Initialize-Lab.ps1
.\scripts\lab\Start-Lab.ps1
Invoke-RestMethod http://127.0.0.1:8765/health -NoProxy
```

Установка получает дополнительные зависимости и проверяет архив PostgreSQL 17.11. Инициализация создаёт собственный кластер в `var/postgres`, базу `aml_lab`, роли, пароли и синтетические таблицы. Режим `Install-LabDependencies.ps1 -Offline` требует заранее подготовленных wheels и закреплённого архива в `var/lab-downloads`. Не используйте повторную инициализацию как способ очистить результаты занятий.

`/health` с `status=ok` подтверждает HTTP. Готовность БД проверяется отдельно с ключом:

```powershell
$access = Get-Content var/postgres/lab-connection.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{'X-Lab-Key'=$access.api_token}
Invoke-RestMethod http://127.0.0.1:8765/lab/status -Headers $headers -NoProxy
$access.api_token | Set-Clipboard
```

Откройте [Swagger](http://127.0.0.1:8765/docs), выберите **Authorize**, вставьте только значение ключа и закройте диалог. Ожидается `GET /lab/status → ready`. После вставки можно очистить буфер командой `Set-Clipboard -Value ' '`. Переменные `$access` и `$headers` действуют только в текущем терминале; для нового терминала повторите их создание.

### 2.4. DBeaver

Закройте DBeaver перед настройкой:

```powershell
.\scripts\lab\Setup-DBeaver.ps1 -Download
.\scripts\lab\Start-SqlDesktop.ps1 -ConnectRole reader
```

Скрипт подготавливает отдельные клиент, драйвер и workspace в `var`. Завершите мастер первого запуска. Если DBeaver спрашивает разрешение на получение пароля командой `Get-DBeaverPassword.ps1`, разрешите команду проекта для выбранной роли. Это локальное получение уже созданного пароля.

Ожидаются два подключения: **AML — просмотр** (`aml_reader`) и **AML — практика** (`aml_student`). Параметры: host `127.0.0.1`, port `55432`, database `aml_lab`, timezone `UTC`. Пользователь Windows не используется как роль PostgreSQL. Подробный первый запрос — в разделе 5.

### 2.5. Kafka

После готовности Docker Desktop выполните:

```powershell
docker info
.\scripts\kafka\Install-KafkaDependencies.ps1 -UsePackageIndex
.\scripts\kafka\Initialize-KafkaLab.ps1
.\scripts\kafka\Initialize-AmlIntegration.ps1
.\scripts\kafka\Broker.ps1 -Action Start -Flow AML
.\scripts\kafka\Broker.ps1 -Action Topics -Flow AML
```

Инициализации создают таблицы двух учебных маршрутов в PostgreSQL. `Broker.ps1` управляет брокером проекта и нужными топиками. Docker volumes и накопленные сообщения остаются локальными.

### 2.6. Ollama, если нужна LLM

Установите Ollama, запустите её локальный сервер и загрузите модель:

```powershell
ollama pull qwen2.5:14b
ollama list
Invoke-RestMethod http://127.0.0.1:11434/api/tags -NoProxy
```

Если сервер уже работает как приложение, второй `ollama serve` не нужен. Для расследования выберите `local-model`. Подготовка поиска проверяет MiniLM и индекс; она не подтверждает готовность Ollama. Живые QA-профили дополнительно проверяют закреплённую идентичность модели. Обновлённый тег может иметь другой digest: результат такого запуска нельзя выдавать за повтор исходного эксперимента.

<a id="start"></a>
## 3. Ежедневный запуск

### 3.1. Только сервис

В одном терминале:

```powershell
.\scripts\rebuild\Enter-LocalEnvironment.ps1
.\.venv\Scripts\python.exe -I -B -m streamlit run src/ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
```

Откройте [UI](http://127.0.0.1:8501). Пока он нужен, оставляйте терминал работающим. PostgreSQL и Kafka для этого режима не нужны.

### 3.2. Полная цепочка

Откройте три терминала в корне проекта:

| Терминал | Команды | Что оставить работать |
| --- | --- | --- |
| A | `Start-Lab.ps1`, подготовка ключа, запуск брокера | API и PostgreSQL работают в фоне; терминал используется для HTTP-примеров |
| B | `Start-IntegratedUI.ps1` | Streamlit |
| C | `Run-AmlWorker.ps1` | Обработчик событий |

В A:

```powershell
.\scripts\lab\Start-Lab.ps1
$access = Get-Content var/postgres/lab-connection.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{'X-Lab-Key'=$access.api_token}
Invoke-RestMethod http://127.0.0.1:8765/lab/status -Headers $headers -NoProxy
.\scripts\kafka\Broker.ps1 -Action Start -Flow AML
Invoke-RestMethod http://127.0.0.1:8765/lab/kafka/analyses/status -Headers $headers -NoProxy
```

У базы и брокера должны быть соответствующие вложенные статусы `ready`. В B:

```powershell
.\scripts\rebuild\Start-IntegratedUI.ps1
```

В UI должна быть надпись **Интегрированный режим**. Нажмите **Подготовить поиск**, дождитесь готовности. В C:

```powershell
.\scripts\kafka\Run-AmlWorker.ps1
```

Для занятия только SQL/API достаточно `Start-Lab.ps1` и клиента DBeaver или Swagger. Развёртывание зависимостей повторять каждый день не нужно.

<a id="operator"></a>
## 4. Работа оператора: пять шагов

1. **Подготовка.** Прочитайте назначение режима, нажмите «Подготовить поиск». `not_ready` означает, что поиск ещё не готов; `preparing` — идёт загрузка; `ready` — можно создать задание; `busy` — обрабатывается другое задание. При затянувшемся ожидании обновите состояние и перейдите к диагностике.
2. **Инцидент.** Подтвердите использование учебной политики и синтетического снимка. Введите `INC-000123`, выберите «Проверяемый шаблон (без LLM)» и создайте задание. Сохраните `job_id`. Для события Kafka выберите соответствующее задание в истории, не создавая ещё одно вручную.
3. **Проверка отчёта.** Дождитесь терминального состояния. `succeeded` означает, что черновик готов к вашей проверке. Разверните источники каждого факта: строка, исходное значение, файл, JSON Pointer и хэш. Проверьте даты, валюты и участников агрегата. В правилах проверьте редакцию и фрагмент. Подтверждение источника не равно одобрению отчёта.
4. **Решение.** Выберите «Одобрить учебный отчёт», «Отклонить отчёт» или «Запросить изменения» и сохраните решение. Оно связано с конкретной версией и хэшем. При конфликте ревизии обновите данные и проверьте их снова. Запрос изменений сохраняет замечание; автоматическая переработка отчёта не запускается.
5. **Сохранение и экспорт.** После одобрения доступен локальный учебный экспорт. Сверьте версию, выполните действие и проверьте квитанцию `mode=local_simulator`. При повторе незавершённой доставки используйте предусмотренный повтор той же операции. Скачанный отчёт и квитанция не означают закрытие банковского кейса.

Отказ `refused` — отдельный результат с основанием, например отсутствие применимого учебного правила. Ошибка `failed`, таймаут `timed_out` и отмена `cancelled` требуют разбора, а не одобрения. Ручная очередь сохраняет возможность передать такой случай оператору. После отмены/таймаута может потребоваться повторная подготовка поиска.

<a id="sql"></a>
## 5. SQL в DBeaver

Запустите `.\scripts\lab\Start-SqlDesktop.ps1 -ConnectRole reader`. Выберите **AML — просмотр**, откройте SQL Editor и выполняйте выделенный запрос через Ctrl+Enter:

```sql
SELECT current_database(), current_user, current_setting('TimeZone');

SELECT i.incident_id, i.client_id, c.risk_category
FROM practice.incidents AS i
JOIN practice.clients AS c ON c.client_id = i.client_id
WHERE i.incident_id = 'INC-000123';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'practice'
ORDER BY table_name;
```

Первый запрос должен вернуть `aml_lab / aml_reader / UTC`. В `reference` и `practice` исходно по 5 клиентов, 4 карты, 5 инцидентов, 5 правил и 15 операций; количество заметок меняется в упражнениях.

Для записи переключитесь на **AML — практика**. Потренируйтесь на заметке, которую создадите через API в следующем разделе. Выделите весь блок и выполните как сценарий. По умолчанию в DBeaver это Alt+X; проверьте назначение в меню SQL Editor.

```sql
BEGIN;
SELECT id, incident_id, body, revision
FROM practice.case_notes
WHERE incident_id = 'INC-000123';
UPDATE practice.case_notes
SET body = 'Пробное изменение внутри транзакции'
WHERE FALSE;
ROLLBACK;
```

Сначала `WHERE FALSE` даёт упражнение без изменения существующей строки. Замените его на `WHERE id = 'UUID-вашей-заметки'::uuid`, повторите блок и убедитесь отдельным SELECT, что после ROLLBACK исходное значение восстановлено. ID заметки имеет тип UUID. Для постоянных изменений используйте API с `expected_revision`: прямой SQL способен обходить прикладную проверку ревизий.

Завершайте открытые транзакции явным Commit или Rollback. Не оставляйте заблокированную запись перед запросом из Swagger. Дополнительные упражнения и ответы находятся в `lab/sql/exercises.sql` и `lab/sql/solutions.sql`.

<a id="api"></a>
## 6. Swagger и проверка API через SQL

Откройте [Swagger](http://127.0.0.1:8765/docs), авторизуйтесь ключом из раздела 2.3. В нужном методе нажмите **Try it out**, заполните параметры и JSON, затем **Execute**. Смотрите код ответа и Response body.

### 6.1. Создать и прочитать заметку

`POST /lab/notes`:

```json
{"incident_id":"INC-000123","body":"Проверяю связь API и SQL."}
```

Ожидается `201`. Сохраните `id` и `revision=1`. Выполните `GET /lab/notes/{note_id}`, подставив полученный ID. В DBeaver:

```sql
SELECT id, incident_id, body, revision
FROM practice.case_notes
WHERE incident_id = 'INC-000123'
ORDER BY id;
```

Ожидается та же заметка. ID записи, `event_id`, `job_id` и `report_id` относятся к разным объектам и не взаимозаменяемы.

### 6.2. Проверить конкурирующее изменение

`PATCH /lab/notes/{note_id}`:

```json
{"body":"Запись сверена","expected_revision":1}
```

После успеха ревизия станет `2`. Повтор с `expected_revision=1` должен дать `409`: клиент использовал устаревшее состояние. Снова прочитайте запись и принимайте следующее решение по её текущей ревизии. Для удаления своей заметки выполните `DELETE /lab/notes/{note_id}?expected_revision=2`; ожидается `204`, последующий GET — `404`.

Другие полезные исходы: `401` — отсутствует или неверен ключ, `400` — отсутствует обязательное поле или JSON/параметры не соответствуют контракту. В API проекта обработчик ошибок преобразует ошибки валидации в `MISSING_INPUT` или `INVALID_INPUT_FORMAT`. Отказ инфраструктуры проверяйте по тексту кода ошибки и `/lab/status`. Готовые HTTP-примеры лежат в `lab/api/exercises.http`.

<a id="kafka"></a>
## 7. Полная цепочка Kafka → AML

### 7.1. Два маршрута

| Маршрут | Топик и worker | Что проверять |
| --- | --- | --- |
| `POST /lab/kafka/events` | `aml.lab.incident-review.v1`, `Run-Worker.ps1`, `-Flow Bookkeeping` | Сводка операций в `kafka_lab.processed_events`, `GET /lab/kafka/results/{event_id}` |
| `POST /lab/kafka/analyses` | `aml.lab.analysis-requested.v1`, `Run-AmlWorker.ps1`, `-Flow AML` | Входящая заявка в PostgreSQL, задание AML API, отчёт и решение в UI |

В обоих случаях невалидные сообщения могут направляться в отдельный DLQ. Для первой сквозной практики используйте AML-маршрут. Терминалы A/B/C должны работать как в разделе 3.2.

### 7.2. Подготовить и отправить событие

В A сформируйте тело один раз и сохраните его для проверки повторной доставки:

```powershell
$policy = Get-Content config/demo-policy.json -Raw -Encoding UTF8 | ConvertFrom-Json
$eventId = [guid]::NewGuid().ToString()
$body = [ordered]@{
    event_id = $eventId
    occurred_at = [DateTime]::UtcNow.ToString('o')
    incident_id = 'INC-000123'
    schema_version = 1
    event_type = 'aml.analysis.requested.v1'
    synthetic = $true
    engine = 'template'
    window = $policy.window
}
$folder = New-Item -ItemType Directory -Force 'var/practice-sessions'
$payload = Join-Path $folder.FullName "$eventId.json"
$body | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $payload
Get-Content -Raw -LiteralPath $payload | Set-Clipboard
$eventId
```

В Swagger выберите **Kafka AML analysis → POST /lab/kafka/analyses**, вставьте JSON и отправьте. Альтернатива из A:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/lab/kafka/analyses -Headers $headers -ContentType 'application/json' -Body (Get-Content -Raw -LiteralPath $payload) -NoProxy
Invoke-RestMethod "http://127.0.0.1:8765/lab/kafka/analyses/$eventId" -Headers $headers -NoProxy
```

`202` подтверждает запись в Kafka. Сразу после отправки GET может вернуть `404`, пока worker не сохранил событие. Повторите GET через несколько секунд. После появления `job_id` откройте `GET /v1/jobs/{job_id}` в Swagger и то же задание в истории интегрированного UI.

### 7.3. Проверить связь в SQL

Подставьте UUID из `$eventId`:

```sql
SELECT event_id, status, job_id, report_id, report_version, last_error_code
FROM kafka_lab.analysis_requests
WHERE event_id = 'ПОДСТАВЬТЕ_UUID'::uuid;

SELECT topic, partition, offset_value, duplicate
FROM kafka_lab.analysis_deliveries
WHERE event_id = 'ПОДСТАВЬТЕ_UUID'::uuid
ORDER BY partition, offset_value;
```

`queued` означает сохранение входящего запроса; `submitted` — передачу в AML API. `succeeded` означает готовность отчёта. Возможны `refused`, `failed`, `cancelled`, `timed_out`, `rejected`: сверяйте `last_error_code`, ответ API и сохранённый результат задания. PostgreSQL хранит состояние интеграции, окончательный отчёт находится в хранилище workflow. Далее пройдите проверку, решение и экспорт из раздела 4.

### 7.4. Повтор, остановка worker и DLQ

1. Повторно отправьте **сохранённый** JSON с тем же `event_id`. Не пересоздавайте `occurred_at`. Ожидается ещё одна доставка с признаком дубля и тот же логический запрос/задание.
2. Для нового расследования создайте новый UUID. Изменённое тело с прежним UUID является конфликтом. HTTP `202` ещё возможен, поскольку конфликт обнаруживается при обработке; проверьте статус, журнал и DLQ.
3. Для упражнения с очередью остановите C через Ctrl+C, отправьте новое событие, проверьте накопление сообщений. Запустите C и проследите появление задания. Сохранение входящего события предшествует фиксации Kafka offset, поэтому повторная доставка ожидаема.
4. Посмотрите состояние группы и DLQ:

```powershell
.\scripts\kafka\Broker.ps1 -Action Lag -Flow AML
.\scripts\kafka\Broker.ps1 -Action ReadDlq -Flow AML
```

`lag=0` означает, что consumer догнал очередь; это не свидетельство готовности отчёта. Универсальная гарантия exactly-once отсутствует. Согласование обеспечивают сохранённый inbox, идентификатор события, хэш содержимого и ключ API `kafka-aml:<event_id>`.

### 7.5. Упрощённое упражнение без AML-модели

Запустите `.\scripts\kafka\Broker.ps1 -Action Start -Flow Bookkeeping`, а в отдельном терминале `.\scripts\kafka\Run-Worker.ps1`. Отправьте в `POST /lab/kafka/events` JSON с новым UUID в `event_id`, текущим UTC-временем в `occurred_at`, `incident_id: "INC-000123"`, `schema_version: 1`, `event_type: "incident.review.requested.v1"`, `synthetic: true`. Сверьте `GET /lab/kafka/results/{event_id}` с `SELECT * FROM kafka_lab.processed_events`. Этот маршрут завершает обработку сводкой операций; черновик AML через него не создаётся.

<a id="stop"></a>
## 8. Остановка и восстановление

1. Остановите UI и worker через Ctrl+C в их терминалах.
2. В DBeaver завершите транзакции, отключите подключения или закройте клиент.
3. Остановите брокер, затем API и PostgreSQL:

```powershell
.\scripts\kafka\Broker.ps1 -Action Stop
.\scripts\lab\Stop-Lab.ps1
```

Для SQL/API без Kafka пропустите команду брокера. Для автономного UI достаточно Ctrl+C. Обычная остановка сохраняет данные. Следующее занятие начинайте с раздела 3.

После аварийного завершения проверьте статус сохранённых заданий. Не рассчитывайте на автоматический повтор модели. Сначала восстановите сервисы, затем подготовьте поиск. Для воспроизведения события используйте сохранённое тело и прежний UUID.

Резервную копию локального стенда делайте после остановки сервисов. Помимо исходников сохраняйте нужные данные из `var/postgres`, `var/workflow`, файлы упражнений и конфигурацию. Kafka volumes копируются отдельными средствами Docker и не входят в копию `var`. Такая резервная копия содержит секреты и предназначена для вашего хранения; она не входит в поставку GitHub.

<a id="troubleshooting"></a>
## 9. Диагностика

| Симптом | Что проверить и сделать |
| --- | --- |
| `No application or model started` | Подготовлено окружение терминала. Выполните команду запуска нужного сервиса. |
| `pg_ctl: no server running`, затем запуск | Это проверка перед стартом. Дождитесь завершения инициализации. При остановке смотрите `var/logs/pgctl-start.err.log` и `var/logs/postgres-lab.log`. |
| `API did not start` | Прочитайте `var/logs/lab-api.err.log`, проверьте 8765/55432. Не ограничивайтесь сообщением `server started`: оно относится к PostgreSQL. Проверьте `/health`, затем `/lab/status` с ключом. |
| `preparing` держится дольше нескольких минут | Обновите состояние. При `not_ready` проверьте модель командой `Prepare-Embedding.ps1` без параметров и ошибку подготовки в UI/терминале. Доступность Ollama здесь не является причиной готовности поиска. |
| `EMBEDDING_NOT_READY` / ошибка хэша | Установите полный закреплённый снимок. Не отключайте проверку файлов. |
| `local-model` завершается ошибкой | Проверьте Ollama `/api/tags`, наличие `qwen2.5:14b` и ресурсы машины. Для отдельной проверки поиска используйте `template`. |
| В DBeaver ошибка `pg_hba.conf`, указан пользователь Windows | Закройте DBeaver, выполните `Setup-DBeaver.ps1 -RepairUsers`, затем `Start-SqlDesktop.ps1 -ConnectRole reader`. Ожидаемая роль — `aml_reader`. Правила доступа PostgreSQL ослаблять не нужно. |
| Swagger возвращает `401` | Повторно получите ключ из своей `var/postgres/lab-connection.json` и выполните Authorize. Проверьте, что открыта нужная копия стенда. |
| Задание из Kafka не видно в UI | Проверьте «Интегрированный режим», совпадение `job_id`, работающий AML worker и статус API. Автономная история использует другую SQLite. |
| После `202` долго нет отчёта | Проверьте worker, PostgreSQL inbox, `job_id`, готовность обработчика, `last_error_code`, lag и DLQ. |
| После PATCH получен `409` | Прочитайте текущую ревизию, повторите изменение осознанно по свежим данным. |
| Экспорт недоступен | Нужны одобренная текущая версия и доступные неизменённые источники. Обновите отчёт и проверьте причины блокировки. |

Прочитать последние строки ошибки API:

```powershell
Get-Content var/logs/lab-api.err.log -Tail 60
Get-Content var/logs/postgres-lab.log -Tail 60
Get-NetTCPConnection -State Listen -LocalPort 8501,8765,55432,9092,11434 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

Определите владельца занятого порта перед остановкой процесса. Не удаляйте `postmaster.pid`, БД или Docker volumes как универсальное исправление ошибки запуска. Для обращения за помощью приложите режим, команду, код ошибки и обезличенный фрагмент лога.

<a id="qa"></a>
## 10. QA, модели и Dark Factory

### 10.1. Детерминированные проверки

Один раз создайте отдельное окружение, затем запускайте проверки с новым каталогом результатов:

```powershell
py -3.12 -m venv .venv-ci
.\.venv-ci\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/ci-windows-py312.lock
.\.venv-ci\Scripts\python.exe -m pip check
.\scripts\rebuild\Enter-LocalEnvironment.ps1
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$checkDir = 'var/checks/manual-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/check_ci_inputs.py --output-dir $checkDir
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/run_checks.py --output-dir $checkDir
.\.venv-ci\Scripts\python.exe -I -B scripts/diagnostics/qualify_ci_ui.py --output-dir "$checkDir/ui"
```

Это проверки контрактов, вычислений, состояния, отказов, API и UI/CLI с контролируемыми зависимостями. Они не измеряют качество настоящей LLM.

### 10.2. Playwright

Установите отдельный браузерный инструментарий и Chromium, затем запустите один детерминированный smoke:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
.\.venv\Scripts\python.exe -I -B qa/tools/browser_tooling.py install --output-dir "var/qa/browser-install-$stamp"
.\.venv\Scripts\python.exe -I -B qa/tools/run_ci_browser.py --output-dir "var/qa/browser-smoke-$stamp"
```

Runner поднимает собственный тестовый стенд, использует настоящий браузер и подменённый вычислитель, сохраняет результат и завершает процессы. При разборе ошибки проверяйте также результат остановки. Trace открывается локально командой `qa/tools/browser_tooling.py trace --path <путь-к-trace.zip>` через `.venv\Scripts\python.exe -I -B`; архив должен находиться в `var/qa` или `verification`.

### 10.3. Реальные модели и вариативность

Подготовьте MiniLM, а для LLM — работающую Ollama с закреплённой моделью. Каталоги каждого эксперимента должны быть новыми:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
.\.venv\Scripts\python.exe -I -B qa/tools/run_retrieval.py --output-dir "var/qa/retrieval/manual-$stamp"
.\.venv\Scripts\python.exe -I -B qa/tools/run_llm.py --output-dir "var/qa/llm/manual-$stamp" --repeats 3
.\.venv\Scripts\python.exe -I -B qa/tools/run_judge.py --output-dir "var/qa/judge/manual-$stamp" --profile compact-v2
```

Смотрите `result.json`, полноту матрицы, идентичность модели, неизменность источников, факты и отказы. В повторных LLM-прогонах важно сравнивать допустимый порядок при сохранении фактов. Число успешных ответов не доказывает корректность свободного текста за пределами контракта. Ошибка модели, таймаут или неполная матрица сохраняются в результате.

Judge оценивает сохранённые примеры; генератор при этой оценке повторно не вызывается. Человеческие оценки не синтезируйте. Без независимой калибровки Judge остаётся консультативным, даже если отдельный машинный прогон успешен. Невалидный ответ Judge означает неполное измерение; это не положительная оценка проверяемого текста.

### 10.4. Dark Factory: измерение и контролируемая регрессия

Выполните парное сравнение на настоящем retrieval и сохранённом baseline:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$matrix = "var/qa/dark-retrieval/matrix-$stamp"
$baselineRun = "var/qa/dark-downstream/decision-$stamp"
$pairRun = "var/qa/dark-downstream/policy-pair-$stamp"
.\.venv\Scripts\python.exe -I -B qa/dark_retrieval/live_matrix.py --output-dir $matrix --timeout-seconds 300
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/live_decision.py --matrix-run $matrix --output-dir $baselineRun --timeout-seconds 300
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/policy_pair.py --baseline-run $baselineRun --output-dir $pairRun --timeout-seconds 300
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/policy_pair.py --replay $pairRun
```

Между командами проверяйте `summary.json`: при неполном исходном измерении не переходите к следующему этапу. Кандидат содержит намеренно внесённый дефект допуска правила. Ожидаемое обнаружение даёт `baseline_status=PASS`, `candidate_status=FAIL`, `measurement_status=PASS`, `mutation_check=PASS`. Код завершения `1` для найденной регрессии кандидата ожидаем; проверяйте отдельные поля, не превращайте все исходы в общий «успех».

Дополнительная открытая проверка порога поиска:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_retrieval/threshold_challenge.py --output-dir "var/qa/dark-retrieval/challenge-$stamp"
```

Открытые примеры не являются скрытой контрольной выборкой. Автономный цикл может выявлять дефекты и воспроизводить измерения, но не заменяет независимую предметную оценку.

### 10.5. Что подтверждено и что осталось

Исторический результат этапа 2: 11 ноября 2026 года прошли 2372 теста и 645 подслучаев, оба контракта совпали; 12 ноября — 29 UI/CLI-проверок, 10 проверок входов CI и 103 случая каталога требований. Ошибок и пропусков нет. Из 2372 тестов 81 добавлен для планирования влияния и CLI. Эти результаты не являются запуском живых моделей, браузера или Kafka-цепочки.

После этапа исполняемых требований 7 ноября 2026 года прошли 2291 тест и 645 подслучаев, 29 UI/CLI-проверок и сверка OpenAPI/JSON Schema. Ошибок и пропусков нет. Локальные проверки входов CI прошли 10/10. Это регрессия с контролируемыми зависимостями; браузер и настоящие модели в этом этапе не запускались.

Репетиция поставки 5 ноября 2026 года: 2233 детерминированных теста, 645 подслучаев, 29 UI/CLI-проверок и один браузерный smoke прошли. Первая установка из отдельной копии, запуск настоящего MiniLM и SQL/API также проверены. Живая Kafka-цепочка имеет отдельные октябрьские протоколы; в ноябрьской репетиции она не повторялась.

Парный Dark Factory-прогон обнаружил две регрессии на трёх сценариях. Исторический Judge имеет `FAIL` из-за ложных одобрений; повтор с атакующим входом дал неполный ответ. Человеческая калибровка и продуктовая приёмка остаются `INCOMPLETE`, скрытый holdout — `NOT_ESTABLISHED`. Удалённый GitHub Actions проверяется после push владельца. Эти статусы относятся к разным доказательствам и не складываются в одну оценку готовности.

<a id="executable-requirements"></a>
### 10.6. Исполняемые требования: от риска к результату проверки

Каталог `qa/requirements/catalog-v1.json` содержит 12 требований: подготовка и допуск задания, отмена и дедлайн, источники и точность фактов, редакции правил, решение и экспорт, сохранение Kafka-события и идемпотентность, недоверенный текст, роль Judge и полнота результатов. Для каждого заданы риск, предусловия, допустимые исходы, источник ожиданий, ограничения и два сценария. Всего связаны 24 сценария и 41 селектор тестов. Параметризованный тест может дать несколько собранных случаев; число селекторов не равно числу выполненных тестов.

**Положительный сценарий** проверяет разрешённое действие. **Отрицательный сценарий** проверяет правильный отказ или блокировку. Например, экспорт одобренной версии должен выполниться, экспорт без одобрения — блокироваться; оба теста должны завершиться `PASS`.

Используйте отдельное окружение `.venv-ci`, подготовленное в разделе 10.1. PostgreSQL, Kafka, Streamlit и Ollama для этой команды запускать не нужно. Программные проверки используют контролируемые зависимости; отдельные тесты создают временные SQLite и дочерние процессы. Выполняйте команды из корня проекта, по одной:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
$requirementsValidation = "var/qa/requirements/validate-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_requirements.py validate --output-dir $requirementsValidation
Get-Content "$requirementsValidation/result.json" -Raw -Encoding UTF8
```

Ожидается `catalog_validation=PASS`, `execution_status=NOT_RUN`, `requirements_status=INCOMPLETE`. Код завершения `0` в режиме `validate` означает только корректность каталога: проверены структура, версия политики и существование объявлений тестов. Сами тесты ещё не выполнялись. Если есть `error_code`, исправьте причину до запуска.

```powershell
$requirementsRun = "var/qa/requirements/run-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_requirements.py run --output-dir $requirementsRun
$requirementsExit = $LASTEXITCODE
Get-Content "$requirementsRun/report.md" -Encoding UTF8
Get-Content "$requirementsRun/result.json" -Raw -Encoding UTF8
$requirementsExit
```

Для следующего прогона задайте новый `$stamp`. Разрешён только новый каталог вида `var/qa/requirements/<имя>`: прежние результаты не перезаписываются. Не редактируйте каталог, политику и связанные файлы во время запуска. Команда очищает внешние фильтры pytest и отключает автоматически загружаемые плагины, чтобы параметры терминала не сокращали выбранные проверки.

| Поле или код | Как трактовать |
| --- | --- |
| `catalog_validation=PASS` | Каталог и его ссылки прошли статическую проверку |
| `execution_status=PASS`, `requirements_status=PASS`, код `0` в `run` | Все связанные проверки и все их собранные параметрические случаи завершились успешно в этом запуске |
| `requirements_status=FAIL`, код `1` | Наблюдалось нарушение хотя бы одного проверяемого условия; смотрите требование, сценарий и лог |
| `INCOMPLETE` или `ERROR`, обычно код `2` | Полное подтверждение не получено: пропуск, xfail, неполная коллекция/фазы, ошибка инструмента или изменение проверяемых файлов |
| `unconfirmed_requirements` | Идентификаторы требований без полного `PASS`; это список для разбора, а не разрешение пропустить их |
| `product_acceptance=INCOMPLETE`, `candidate_admission=NOT_EVALUATED` | Этот отчёт не принимает продукт и не разрешает выпуск кандидата |
| `human_calibration=NOT_MEASURED`, `historical_judge=NOT_REASSESSED` | Человеческая оценка и повторная оценка исторического Judge в этом прогоне не выполнялись |

Сначала прочитайте `report.md`, затем найдите сценарий и `selector` в `result.json`. Подробности выполнения находятся в `pytest.log`, `junit.xml` и `observations.json`. `catalog.json` сохраняет использованное описание, `catalog-validation.json` — результат его проверки; `source-before.json` и `source-after.json` фиксируют выбранные файлы до и после запуска. При аварийном завершении может остаться только `started.json` со статусом `INCOMPLETE` — это не законченный результат. Повтор запуска не стирает предыдущую неудачу.

Статусы относятся к текущей коллекции связанных механических проверок. Каталог не измеряет качество настоящей LLM, банковскую применимость правила или удобство работы человека. Хэши выбранных файлов не являются полным паспортом runtime, модели и окружения. Исторический `FAIL` Judge сохраняется. Тесты самого инструмента входят в обычную регрессию раздела 10.1; отдельного задания CI для публикации этого отчёта пока нет.

Это этап 1 плана развития Dark Factory. В этапе 2 добавлена связь изменений с требованиями и обоснование состава проверок — раздел 10.7. Этап 3 объединяет детерминированные запуски и связывает свидетельства с текущими исходниками — раздел 10.8. Этап 4 квалифицирует программные оценщики — раздел 10.9; этап 5 проверяет сценарии окружения — раздел 10.10; этап 6 добавляет статистику и бюджет — раздел 10.11; этап 7 связывает их с ограниченным исправлением ответа — раздел 10.12. Этап 8 добавляет интерфейс аналитика QA — раздел 10.13. Этап 9 добавляет учебные расследования — раздел 10.14. Формат каталога описан в [техническом справочнике](../../qa/requirements/README.md).

Локальное измерение 7 ноября 2026 года, `run-20261107-03`: 103 собранных случая прошли; все 12 требований подтверждены в указанной механической области. Продуктовая приёмка осталась `INCOMPLETE`. Результат конкретного запуска хранится локально в `var/qa/requirements`; в опубликованной копии выполните собственный прогон.

<a id="change-impact"></a>
### 10.7. Анализ влияния изменений и план проверок

Используйте планировщик перед проверкой доработки. Вы перечисляете изменённые файлы и, при необходимости, смену модели. Инструмент показывает, какие компоненты и требования могут быть затронуты, почему они выбраны и какие профили проверок нужны. Карта `qa/impact/map-v1.json` содержит 20 компонентов с вручную заданными зависимостями. Это этап 2 развития Dark Factory.

Работайте из корня проекта в окружении `.venv-ci` из раздела 10.1. Для планирования не нужно запускать PostgreSQL, Kafka, UI или Ollama. Следующая команда анализирует объявленное изменение активного промпта/транспорта и LLM; она сама не меняет файл или модель:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmssfff
$impactRun = "var/qa/impact/plan-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/plan_changes.py --changed src/runtime/transport.py --model-changed llm --output-dir $impactRun
$impactExit = $LASTEXITCODE
Get-Content "$impactRun/plan.md" -Encoding UTF8
Get-Content "$impactRun/result.json" -Raw -Encoding UTF8
$impactExit
```

Для нескольких файлов повторяйте `--changed`. Для внешней смены embedding-модели используйте `--model-changed embedding`; допустим запуск только с этим признаком. Планировщик не обнаруживает изменения Ollama или весов самостоятельно и не читает Git diff. Включайте в декларацию все изменения, которые хотите оценить.

Для добавлений, удалений и переименований создайте JSON внутри проекта. Этот пример описывает изменение генерации и предполагаемое переименование транспорта в неизвестный карте каталог. Он не выполняет само переименование:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmssfff
$changesFile = "var/qa/impact-changes-$stamp.json"
New-Item -ItemType Directory -Force var/qa | Out-Null
@'
{
  "schema_version": "1.0",
  "changes": [
    {"status": "M", "path": "src/runtime/generation.py"},
    {"status": "R", "previous_path": "src/runtime/transport.py", "path": "src/new_domain/transport.py"}
  ],
  "model_changes": ["llm"]
}
'@ | Set-Content -LiteralPath $changesFile -Encoding utf8
$renamePlan = "var/qa/impact/rename-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/plan_changes.py --changes-file $changesFile --output-dir $renamePlan
$impactExit = $LASTEXITCODE
Get-Content "$renamePlan/plan.md" -Encoding UTF8
$impactExit
```

Здесь ожидается код `2` и `planning_status=INCOMPLETE`: новый путь неизвестен карте. Старый путь сохраняет свои связи; резервный план включает все требования и профили, а неизвестный путь остаётся в `unknown_paths`. `A`, `M`, `D` означают добавление, изменение, удаление и требуют только `status`/`path`; `R` дополнительно требует `previous_path`. Удалённый файл не обязан существовать. Неизвестные поля, дубликаты, абсолютные пути и выход за корень отклоняются.

| Что читать | Как использовать |
| --- | --- |
| `planning_status=READY`, код `0` | План построен по известным связям объявленных входов; это не успешный тест |
| `planning_status=INCOMPLETE`, код `2` | Есть неизвестные пути или исчезли ссылки каталога: сохранить полный набор и восстановить связи |
| `planning_status=ERROR`, код `2` | Разобрать `error_code`; при ошибке пути/записи итоговый файл может отсутствовать |
| `path_impacts`, `requirements[].reasons` | Проследить путь → компонент → потребители → требование и основание выбора |
| `selected_test_selectors` | Начать разбор с приоритетных тестов, сохраняя обязательную полную регрессию |
| `profiles` | Выполнить подходящие профили отдельно; у всех в этом плане статус `NOT_RUN` |
| `input_completeness=NOT_VERIFIED` | Инструмент не доказал полноту списка изменений или модели |

После чтения плана выполните общую регрессию из 10.1. Профиль `requirements` означает весь каталог из 10.6, а не сокращённый набор селекторов. `ui_cli` выполняется по 10.1, `browser` — по 10.2, `retrieval`/`llm`/`judge` — по 10.3. `live_integration` требует живой цепочки из раздела 7. Записи `documentation` и `mapping_review` обозначают проверку инструкций и обоснованности карты; сами по себе они не подтверждают автоматическое выполнение. Человеческая часть этих проверок остаётся отложенной до конца плана.

Сопоставление путей не требует содержимого изменённых файлов. Отдельно инструмент проверяет ссылки каталога, читая связанные тесты и основания ожиданий. При удалении такого файла или определения теста план сохраняется со статусом `INCOMPLETE`, полным резервным набором и `catalog_validation_error`. Сначала восстановите ссылки каталога; исчезновение теста не означает, что его требование больше не нужно проверять. Несовпадение версии политики даёт `ERROR` и требует согласованного пересмотра.

Результат сохраняется в новом `var/qa/impact/<имя>`: читаемый `plan.md`, итог `result.json`, входы `changes.json`, копии `catalog.json`/`map.json` и `catalog-validation.json`. Для повтора задавайте новый `$stamp`; прежние результаты не перезаписываются. При прерывании `started.json` без результата не является готовым планом.

`execution_status` остаётся `NOT_RUN`, `requirements_status` и `product_acceptance` — `INCOMPLETE`, `candidate_admission` — `NOT_EVALUATED`. Список `requirements_without_declared_impact` не доказывает отсутствие влияния: он относится только к объявленным входам и текущей карте. Полная модель зависимостей, подтверждение исполнения и привязка всех свидетельств к версии этим этапом не устанавливаются. Форматы карты и отчёта описаны в [справочнике планировщика](../../qa/impact/README.md).

<a id="owned-qa-execution"></a>
### 10.8. Единый запуск QA и связь результата с версией

Этап 3 объединяет полную регрессию со сверкой контрактов, весь каталог требований, UI/CLI и входы CI. Каждый результат относится к текущим исходникам и новому собственному запуску. Подготовьте `.venv-ci` по разделу 10.1; PostgreSQL, Docker, Ollama и рабочий UI для этой команды не требуются.

Из корня проекта создайте новый план и выполните его. В примере объявлено изменение UI; команда сама его не изменяет:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmssfff
$impactRun = "var/qa/impact/ui-$stamp"
$executionRun = "var/qa/execution/ui-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/plan_changes.py --changed src/ui/app.py --output-dir $impactRun
if ($LASTEXITCODE -ne 0) { throw 'План требует разбора; прочитайте plan.md.' }
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_plan.py --plan-dir $impactRun --output-dir $executionRun --timeout-seconds 1800
$executionExit = $LASTEXITCODE
Get-Content "$executionRun/report.md" -Encoding UTF8
Get-Content "$executionRun/result.json" -Raw -Encoding UTF8
$executionExit
```

Для UI ожидаемо может получиться **код 3, `deterministic_status=PASS`, `plan_fulfillment=INCOMPLETE`**: браузерный smoke нужен по плану, но этот инструмент его не запускает. Выполните отдельный профиль по разделу 10.2; результат останется отдельным свидетельством. Старые PASS не закрывают текущий план автоматически.

Можно начать непосредственно с декларации:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmssfff
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_plan.py --changed src/runtime/transport.py --model-changed llm --output-dir "var/qa/execution/transport-$stamp"
```

Поддерживается также `--changes-file` с JSON из раздела 10.7. Смена модели добавляет требование измерения, но не запускает Ollama. Сохранённый `--plan-dir` нельзя дополнить новым `--model-changed`: сначала перестройте план. При изменении карты, каталога или декларации старый план отклоняется с `STALE_IMPACT_PLAN`.

| Что смотреть | Что это означает |
| --- | --- |
| `deterministic_status` | Итог четырёх обязательных программных профилей |
| `plan_fulfillment` | Закрытие всего плана; модели, браузер, живая интеграция и экспертные действия могут оставаться `NOT_RUN` |
| `version_binding` | Совпадение наблюдаемых исходников, окружения и закреплённых результатов |
| `profiles` | Команды, коды выхода, причины и хэши результатов каждого профиля |
| `source-before.json` / `source-after.json` | Файлы поставки и обнаруженные исходники; добавления, удаления и изменения влияют на итог |
| `environment-before.json` / `environment-after.json` | Python и версии пакетов без выгрузки переменных окружения |
| `evidence.json` | Закреплённые отчёты и журналы с хэшами |

Коды: **0 — PASS, 1 — FAIL, 2 — ERROR, 3 — INCOMPLETE**. Успешные команды не означают приёмку продукта: `product_acceptance=INCOMPLETE`, `candidate_admission=NOT_EVALUATED`. Исторический FAIL Judge и отложенные человеческие проверки сохраняются. Для разбора неполной регрессии читайте `full_regression/unit-observations.json`: фильтры из `addopts` очищаются, исключение тестов, пропуск, xfail/XPASS и незавершённые фазы блокируют PASS. Явный провал остаётся видимым, даже если другой файл результата отсутствует.

Не редактируйте проверяемые исходники во время прогона. При изменении их хэшей результат не подтверждает единую версию — повторите проверку с новым именем. Повторная запись существующего каталога запрещена. Каталог требований пишет в отдельный принадлежащий этому запуску `var/qa/requirements/execution-…`; точный путь указан в `profiles`. Остальные отчёты и журналы находятся в `$executionRun`.

Лимит `--timeout-seconds` применяется к каждому профилю отдельно. При тайм-ауте или прерывании очищаются принадлежащие запуску процессы; ошибка очистки останавливает дальнейшее выполнение. `started.json` без итогового `result.json` — незавершённая проверка. Исправления исходников, запуск моделей и публикация автоматически не выполняются. Границы сохранённой версии описаны в [справочнике выполнения](../../qa/execution/README.md).

<a id="evaluator-qualification"></a>

### 10.9. Проверить оценщики до интерпретации качества AI

Этап 4 проверяет программные функции, которыми измеряются результаты сервиса. Даже корректно выполненный тест бесполезен, если оценщик пропускает подменённый источник, теряет ошибку модели из знаменателя или превращает неполный отчёт в PASS. Контрольный каталог задаёт ожидаемые результаты заранее и хранится отдельно от результатов запусков.

Подготовьте `.venv-ci` по разделу 10.1 и выполните из корня проекта:

```powershell
$qualificationRun = 'var/qa/evaluator-qualification/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/qualify_evaluators.py --output-dir $qualificationRun
Get-Content "$qualificationRun/report.md" -Encoding UTF8
Get-Content "$qualificationRun/result.json" -Raw -Encoding UTF8
```

PostgreSQL, Kafka, Ollama и интерфейс сервиса не нужны. Команда вызывает реальные программные оценщики на синтетических входах, затем отдельно искажает их наблюдения, чтобы проверить чувствительность самого набора. Исходники оценщиков и рабочие данные не изменяются. Каждый запуск требует нового каталога; предыдущие результаты сохраняются.

Проверяются офлайн-оценка replay, сценарные наблюдения, сохранение фактов в ответах LLM, метрики retrieval, схема и агрегаты консультативного Judge, разбор результатов автоматических тестов. Точные ожидания и границы каждой группы находятся в `qa/qualification/catalog-v1.json` и [паспорте квалификации](../../qa/qualification/README.md).

| Поле или файл | Для чего нужен |
| --- | --- |
| `qualification_status` | Совпали ли полные наблюдения с ожиданиями всех контролей |
| `fault_injection_status` | Обнаружены ли шесть намеренных подмен наблюдений |
| `version_binding` | Стабильны ли исходники, наблюдаемое окружение и закреплённые свидетельства |
| `baseline.json` | Полная матрица expected/observed, ошибки, пропуски и метрики по группам |
| `fault-injections.json` | Искажённые копии наблюдений и результат обнаружения каждой подмены |
| `catalog.json`, `observations.json` | Каталог и исходные наблюдения конкретного запуска |
| `evidence.json` | Байтовые хэши сохранённых свидетельств |

`matched_rate` описывает совпадение полной проекции с ожиданием. `decision_false_approval_rate` и `decision_false_alarm_rate` учитывают только ошибки верхнего решения: ноль в них не отменяет ошибки метрики, отсутствие ответа или сбой исполнения. Всегда читайте их вместе с `planned`, `matched`, `mismatched`, `missing` и `errors`.

Читайте отчёт в следующем порядке:

1. Убедитесь, что выполнены все запланированные контроли. Потерянные, повторные и посторонние записи не должны давать PASS. Ошибка исполнения оценивается отдельно от ожидаемого отклонения некорректного входа.
2. Сопоставьте ожидаемый и наблюдавшийся ответ каждой проверки. Совпадение общего статуса без нужной метрики или кода ошибки недостаточно.
3. Посмотрите полный знаменатель и отдельные результаты по оценщикам. Отсутствие наблюдения не должно улучшать долю успешных контролей; при нулевом знаменателе оценка не определена.
4. Проверьте результаты намеренных подмен. Их обнаружение — успех контрольного набора, а не дефект рабочего оценщика. Это fault injection наблюдений; процент покрытия мутаций исходного кода здесь не измеряется.
5. Проверьте связь с исходниками и окружением. Если файлы изменились во время проверки, новый итог не относится к единой версии.

Коды команды: **0 — PASS, 1 — FAIL, 2 — ERROR, 3 — INCOMPLETE**. FAIL означает расхождение с зафиксированным ожиданием; ERROR — недопустимый вход, сбой исполнения или нарушение целостности; INCOMPLETE — неполную проверку. `started.json` без итогового отчёта означает незавершённый запуск. Не исправляйте сохранённый результат вручную: устраните причину и используйте новое имя каталога.

**Учебное упражнение.** До просмотра отчёта запишите, как оценщик должен реагировать на неверную версию цитаты, ошибочный факт, пропавший ответ модели и skipped-тест. Найдите соответствующие контроли в каталоге, затем объясните расхождение между «ответ модели синтаксически валиден», «ответ качественный» и «измерение полно». По результатам намеренных подмен покажите, какой контроль остановил ложный PASS. Ожидания каталога и замороженные эталоны ради зелёного результата не меняйте.

Программная квалификация не подтверждает полезность AML-анализа, полноту покрытия реальных дефектов или пригодность настоящей LLM как судьи. Исторический FAIL Judge сохраняется; человеческое согласие и продуктовая приёмка остаются незавершёнными. Исполнитель раздела 10.8 по-прежнему запускает четыре детерминированных профиля: данный CLI является отдельным измерением. Его модульные проверки входят в полную регрессию, но это не подстановка готового отчёта в чужой запуск.

<a id="environment-scenarios"></a>

### 10.10. Сценарии сбоев и восстановления окружения

Этот практикум показывает, что происходит между шагами: событие сохранено, но подтверждение потеряно; API принял задание, но ответ не дошёл; запись результата не удалась; общий бюджет закончился до сохранения проекции. Ожидания задаются заранее в `qa/environments/catalog-v1.json`, а отчёт сохраняет каждое промежуточное состояние и trace операций.

После подготовки `.venv-ci` по разделу 10.1 выполните из корня проекта:

```powershell
$environmentRun = 'var/qa/environments/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_environments.py --output-dir $environmentRun
Get-Content "$environmentRun/report.md" -Encoding UTF8
Get-Content "$environmentRun/result.json" -Raw -Encoding UTF8
```

Запускать PostgreSQL, Kafka, Ollama и UI не требуется. Настоящие компоненты здесь — intake, планировщик, `WorkflowClient` и `SQLiteStore`. Для каждого сценария создаётся отдельная SQLite-база в `var/workflow/environment-models/`; рабочая история оператора не используется. Kafka, очередь PostgreSQL и транспортные ошибки моделируются. Терминальный отказ синтетический: расследование и генерация не выполняются.

Каталог ограничивает действия и виды сбоев: приём события, tick планировщика, синтетическое завершение и пересоздание клиентских объектов. Произвольный код, адреса, файловые пути и бесконечные циклы в сценариях не допускаются. Каждый новый запуск требует нового каталога результатов и рабочего пространства; предыдущие свидетельства сохраняются.

| Где смотреть | Значение |
| --- | --- |
| `result.json` → `scenario_status`, `assessment.metrics` | Совпадения сценариев/шагов с фиксированными ожиданиями; пропуски и ошибки остаются в знаменателе |
| `execution_status` | Полнота выполнения; успешное исполнение ещё не означает правильного поведения |
| `canary_status`, `canaries.json` | Обнаружение трёх намеренных подмен наблюдений |
| `assessment.json` | По каждому шагу: expected, observed, статус и инварианты |
| `observations.json` | Trace операций, наблюдения и настоящие/моделируемые зависимости |
| `version_binding`, `evidence.json`, `workspace` | Привязка к исходникам и окружению; хэши свидетельств и отдельных SQLite-баз |

Откройте сначала `report.md`, затем при расхождении найдите первый несовпавший шаг в `assessment.json`. Код команды: **0 — PASS, 1 — FAIL, 2 — ERROR, 3 — INCOMPLETE**. `started.json` без `result.json` означает незавершённый запуск. При `ENVIRONMENT_NESTED_PROFILE_UNSUPPORTED` используйте обычный отдельный терминал без активного QA-профиля; при занятом пути задайте новое имя прогона. Сохранённые результаты вручную не правьте. Сценарный CLI запускается отдельно от четырёх детерминированных профилей раздела 10.8.

Разбирайте сценарий последовательно:

1. Найдите шаг с отказом и его точный код. Убедитесь, что отработал запланированный сбой, а не случайная ошибка тестового инструмента.
2. Сопоставьте состояние очереди и настоящей SQLite до и после повтора. Потеря HTTP ACK после принятия не должна создавать второе задание при том же ключе идемпотентности.
3. Проверьте trace: сохранение очереди или подтверждение DLQ должно предшествовать подтверждению входного offset. При недоступности зависимости offset не считается безопасно обработанным.
4. Для истёкшего бюджета проверьте отсутствие новых операций до следующего явного tick. Виртуальное время моделирует границу бюджета; длительность сценария не является измерением SLA.
5. Убедитесь, что выполнены все шаги и не нарушены инварианты. Совпадение конечного статуса не отменяет расхождений в промежуточных состояниях или счётчиках.

**Учебное упражнение.** До запуска запишите, сколько заданий должно остаться после потери ответа API и повторного запроса. Затем найдите сценарий потери ACK, сравните количество запросов, уникальных ключей и заданий. Объясните, почему несколько HTTP-вызовов могут соответствовать одному заданию и почему это не доказывает exactly-once для всей Kafka-цепочки. Отдельно разберите DLQ: повтор диагностической записи после потери offset ACK допускается и не равен повторному AML-расследованию.

Намеренные подмены сохранённых наблюдений проверяют чувствительность проверяющего к пропущенному шагу, ошибочному числу заданий и нарушению инварианта. Их обнаружение — ожидаемый результат контроля; работающая инфраструктура при этом не ломается.

Коды CLI: **0 — PASS, 1 — FAIL, 2 — ERROR, 3 — INCOMPLETE**. Ошибка выполнения сохраняется отдельно от ожидаемого отказа зависимости. `started.json` без итогового отчёта означает незавершённый запуск. Не редактируйте исходники во время проверки: изменившаяся версия не получает подтверждения целостности.

Результат не подтверждает транзакции настоящего PostgreSQL, сетевые тайм-ауты, перезапуск ОС/процесса, производительность, качество модели или человеческую приёмку. Живой контур проверяется отдельно по разделам 7 и 10. Этот CLI дополняет четыре профиля раздела 10.8; модульные тесты сценарного инструмента входят в общую регрессию. [Паспорт модели и ограничения](../../qa/environments/README.md).

<a id="statistical-stability"></a>
### 10.11. Статистическая устойчивость и бюджет

Этот этап помогает отличать повторяемость отдельных генераций от охвата разных случаев и заранее ограничивать расход попыток. Выполняйте команды из корня проекта в VS Code после подготовки `.venv-ci` из раздела 10.8. Для учебных контролей PostgreSQL, Kafka и Ollama не нужны.

**Учебный запуск.** Каждому запуску задавайте новый каталог:

```powershell
$stabilityRun = 'var/qa/stability/demo-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_stability.py --output-dir $stabilityRun
Get-Content "$stabilityRun/report.md" -Encoding UTF8
Get-Content "$stabilityRun/demo.json" -Raw -Encoding UTF8
```

План содержит baseline и candidate, три случая из двух семейств и два повтора каждого случая: всего 12 слотов. Четыре статистических контроля проверяют полную успешную панель, известную ошибку, отсутствующее наблюдение и остановку по бюджету. Ещё шесть контролей проверяют лимит вызовов, токенов и времени, расход неудачных попыток, неизвестную телеметрию и превышение резерва. Ожидаемый результат квалификации — `PASS` для десяти контролей, когда ошибки и неполнота обнаружены правильно.

В бюджетной панели лимит допускает восемь вызовов из 12. Остальные четыре слота явно записаны как `NOT_RUN`; общий результат этой панели — `INCOMPLETE`. Отрицательные контроли не превращаются в успешные ответы модели. Время в упражнении виртуальное, скорость реальной машины не измеряется.

**Пересчёт своих сохранённых ответов LLM.** Используйте каталог, полученный существующей командой `qa/tools/run_llm.py` из практикума настоящих моделей. Замените значение `$llmSource` на свой путь; указанный пример имени сам по себе не существует:

```powershell
$llmSource = 'var/qa/llm/имя-вашего-прогона'
$stabilityRun = 'var/qa/stability/history-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_stability.py --source-dir $llmSource --output-dir $stabilityRun
Get-Content "$stabilityRun/report.md" -Encoding UTF8
Get-Content "$stabilityRun/historical-metadata.json" -Raw -Encoding UTF8
```

Исходный каталог читается без изменения. План берётся из `run.json`, доступные повторы заново проверяются программным оценщиком, файлы и модельная идентичность сопоставляются по сохранённым данным. Отсутствующие повторы остаются в плановом знаменателе. Новых генераций нет. Локальный pilot-first целиком не публикуется: новый клон содержит только отдельные ранее отобранные синтетические примеры для тестов.

Старый collector не объявлял общего лимита токенов/вызовов/времени заранее. Его расход можно описать, но `budget_enforcement=NOT_EVALUATED`. Ledger используется в учебном исполнителе и отдельном цикле исправления ответа из раздела 10.12; существующий `run_llm.py` автоматически к нему не подключён. Исторический пересчёт не подтверждает текущую версию LLM, живую интеграцию или качество Judge.

**Как читать результат.**

| Артефакт / поле | Что проверять |
| --- | --- |
| `result.json` | `analysis_status`, `qualification_status`, `version_binding` и итоговый `status` относятся к разным проверкам; продуктовая приёмка остаётся `INCOMPLETE` |
| `plan.json`, `observations.json` | Заранее запланированные варианты, случаи, семейства и повторы; выполненные, ошибочные и остановленные слоты |
| `statistics.json` | Полные знаменатели, доли, статусы каждого случая и семейства, задержки, токены и парное сравнение |
| `missing_slots` / `not_run_slots` | Отсутствующая запись / явно зафиксированный невыполненный слот; оба сохраняют неполноту |
| Вариативность | Число смысловых и упорядоченных отпечатков среди минимум двух прошедших проверку черновиков; отсутствие черновиков не означает стабильность |
| Задержка | p50/p95 по ближайшему рангу, минимум, максимум и число реально измеренных значений; это не SLA |
| Расход | Все записанные попытки, включая repair, timeout и error; известные суммы и количество неизвестных измерений отдельно |
| `demo.json` | Входы контролей, ожидания, наблюдения, бюджет и журнал резервирования/списания |
| `historical-metadata.json` | Согласованность старых файлов и модели, прежний итог, повторная оценка, доступные cold/warm наблюдения |
| `evidence.json`, `source-*.json`, `environment-*.json` | Хэши входов/выходов и неизменность текущих исходников и окружения во время пересчёта |

Повторы одного случая не считаются независимыми примерами. Семейство получает один исход «все запланированные проверки успешны»; при неопределённых исходах условный 95% интервал Wilson не вычисляется. Даже при полной панели он предполагает независимые репрезентативные семейства. Для наших отобранных примеров эти предпосылки не установлены: `population_inference=NOT_ESTABLISHED`. Интервал не служит автоматическим допуском релиза.

Baseline и candidate сопоставляются только на одинаковых case/repeat слотах. Неполные пары остаются в плановом знаменателе; разница является описательной, без заявления о статистическом превосходстве. Исторический прогон с одним вариантом не создаёт искусственный baseline.

**Как работает бюджет.** До каждого вызова резервируются одна попытка и консервативное число всех токенов запроса/ответа. Известный меньший расход освобождает остаток; неизвестный сохраняет весь резерв и неполный статус учёта. Ошибка расход не обнуляет. Превышение резерва, времени или ошибка часов блокируют следующие попытки; автоматического retry до успешного результата нет. Ledger контролирует допуск и замечает превышение после возврата, но не прерывает зависший callback: deadline транспорта и изоляция процесса нужны отдельно. Токены не переводятся в денежную стоимость или энергию.

**Упражнение.** Найдите в `demo.json` бюджетную панель и объясните, почему при восьми выполненных попытках её знаменатель равен 12, а число полных пар меньше шести. Затем сравните контролы неизвестного расхода и превышения резерва: первый даёт неполный учёт, второй — нарушение бюджета. Сопоставьте с `statistics.json`, где основная контрольная панель завершена целиком. Сохранённые свидетельства вручную не редактируйте.

Коды выхода: **0 PASS, 1 FAIL, 2 ERROR, 3 INCOMPLETE**. Если остался только `started.json`, запуск не завершён. `ERROR` при несовместимых или повреждённых источниках требует разбора кода ошибки; он не равен доказанному ухудшению модели. Для повторного запуска используйте новое имя. Модульные проверки входят в полную регрессию раздела 10.8; этот CLI не закрывает семь отдельных профилей моделей, браузера, живой интеграции и экспертной проверки. [Описание формата и метода](../../qa/stability/README.md).

<a id="bounded-response-cycle"></a>
<a id="bounded-autonomy"></a>
### 10.12. Ограниченный автономный цикл ответа

Цикл выполняет последовательность «предложить порядок фактов → проверить кандидата → вернуть допустимый код ошибки → исправить или остановиться». Кандидат содержит только JSON-перестановку разрешённых идентификаторов фактов и правил. Его обрабатывают настоящий renderer сервиса и программная проверка по замороженным синтетическим данным. Исходники, SQL, политика и эталоны не входят в набор действий цикла; ответ модели не исполняется как код.

**Учебный запуск без модели.** Откройте PowerShell в корне проекта и подготовьте `.venv-ci` по разделу 10.8. PostgreSQL, Kafka и Ollama для этого режима не нужны:

```powershell
$autonomyRun = 'var/qa/autonomy/demo-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_autonomy.py --output-dir $autonomyRun --mode demo
Get-Content "$autonomyRun/report.md" -Encoding UTF8
Get-Content "$autonomyRun/demo.json" -Raw -Encoding UTF8
```

Одиннадцать заранее объявленных контролей проверяют правильный первый ответ, исправление, отсутствие прогресса, предел попыток, ограничения вызовов/токенов/времени, тайм-аут, неизвестный расход, превышение резерва и ошибку оценщика. Генератор ответов и время внутри контролей моделируются. Renderer, oracle, контроллер бюджета и управление дочерним процессом настоящие. Ограничения каждого контроля сохранены в `demo.json`; параметры бюджета живого режима не меняют его фиксированные ожидания. В `demo` не задавайте `--case-id`, изменённые `--max-attempts` или `--max-tokens`: принимается стандартная конфигурация 6/98304 без пользовательского выбора случаев. Эти значения не являются общим лимитом шести попыток на весь набор контролей. `--wall-seconds` задаёт внешний тайм-аут процесса и действует также для учебного запуска.

Успешная квалификация означает совпадение ожидаемых и наблюдавшихся исходов. Например, контроль исправления сохраняет первую ошибку и общий `FAIL` вложенного цикла, хотя второй кандидат получает `PASS`. Контроль бюджета должен остановить последующие вызовы. Поэтому отрицательный исход внутри `demo.json` сам по себе не означает поломку практикума: сопоставьте `expected`, `observed` и статус конкретного контроля.

**Ограниченный запуск настоящей локальной Ollama.** Сначала подготовьте Ollama и модель `qwen2.5:14b` по разделам установки и измерений LLM. Используются только `127.0.0.1:11434` и закреплённый digest модели; этот CLI не скачивает и не заменяет модель. Для первого запуска возьмите один случай и максимум две попытки:

```powershell
$autonomyRun = 'var/qa/autonomy/ollama-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_autonomy.py --output-dir $autonomyRun --mode ollama --case-id QA-S01 --max-attempts 2 --max-tokens 32768 --wall-seconds 180
Get-Content "$autonomyRun/report.md" -Encoding UTF8
Get-Content "$autonomyRun/worker-result.json" -Raw -Encoding UTF8
if (Test-Path "$autonomyRun/cycle.json") { Get-Content "$autonomyRun/cycle.json" -Raw -Encoding UTF8 }
```

Разрешены `QA-S01`, `QA-S02`, `QA-S03`, `QA-S04`, `QA-S14`. Для нескольких случаев повторяйте `--case-id`; без этого параметра выбраны все пять. В отдельном случае есть максимум две условные попытки; `--max-attempts` ограничивает весь запуск. Это потолок вызовов, а не обязательство выполнить их все. Успех первого кандидата оставляет следующий слот `NOT_NEEDED`. Случаи, не начатые из-за остановки, сохраняют `NOT_RUN`.

Перед каждым вызовом резервируются одна попытка и **16 384 токена** из общего бюджета. При известном расходе остаток резерва освобождается; неизвестный расход сохраняет весь резерв и останавливает следующие вызовы. Ошибки уже потраченные попытки не обнуляют. Резерв является консервативным допуском, а не точным tokenizer или гарантией расхода сервера. Запрос ограничивает ответ параметром `num_predict=512`; сообщённое сервером превышение отклоняет кандидата, а расход остаётся в учёте. Поля токенов описаны в [chat API Ollama](https://docs.ollama.com/api/chat), ограничение ответа — в [справочнике параметров](https://docs.ollama.com/modelfile#valid-parameters-and-values).

Контекст и применимые правила берутся из проверенного замороженного каталога. Этот запуск проверяет ограниченную генерацию и сохранность фактов; он не измеряет MiniLM, живой поиск или SQL/API/Kafka-цепочку. Для них остаются отдельные практикумы. `run_llm.py` из предыдущего раздела остаётся самостоятельным измерителем; новый CLI не меняет бюджет старых прогонов.

**Как принимается решение о продолжении.** Базовый ответ проверяется до выдачи входов генератору. Модель получает фактический каталог, ключи правил и контракт ответа. Для исправления разрешены только `INVALID_MODEL_JSON`, `INVALID_MODEL_RESPONSE` и `INCOMPLETE_OR_UNKNOWN_SELECTION`; эталонный отчёт и произвольные инструкции обратной связи не передаются. Одинаковый ошибочный текст останавливает случай с `NO_PROGRESS`. Ошибка, для которой нет разрешённого кода исправления, завершает случай; сбои оценщика, транспорта, часов или журнала, неизвестный расход и нарушение бюджета прекращают дальнейшие вызовы.

Первая ошибка остаётся видимой в `first_attempt_status` и общем статусе цикла. У исправленного случая `final_candidate_status` может быть `PASS`, а общий результат — `FAIL`. Для частоты ошибок используйте все попытки, а не только итоговый кандидат. Условные repair-слоты не являются независимыми повторами статистического эксперимента этапа 6.

| Артефакт | Как использовать |
| --- | --- |
| `result.json`, `report.md` | Итог запуска, привязка к версии и состояние очистки процесса |
| `worker-config.json` | Объявленные режим, случаи, лимиты и исходный digest |
| `worker-result.json` | Результат worker; при принудительной остановке файл может отсутствовать |
| `journal.jsonl` | Последовательность резервирования до вызова, предложения, оценки и остановки |
| `demo.json` | Полные планы, ожидания, наблюдения и циклы 11 учебных контролей |
| `baseline.json`, `cycle-plan.json` | Проверка базового ответа и действующий план живого цикла |
| `cycle.json` | Все попытки, первый и итоговый исходы, условные слоты, ledger и причина остановки |
| `candidates.json` | Успешно проверенные черновики как отдельные артефакты; не разрешение релиза |
| `http-attempts.json` | Синтетические HTTP-запросы/ответы и телеметрия; файл остаётся локальным |
| `model-before.json`, `model-after.json` | Согласованность сохранённой идентичности локальной модели |
| `source-*.json`, `environment-*.json`, `evidence.json` | Хэши версии, окружения и свидетельств |

Фиксированный доверенный worker запускается под `run_owned`; внешний тайм-аут и Windows Job управляют принадлежащим запуску клиентом и его потомками. Это управление процессами, не системная песочница. Ollama работает отдельным сервером: завершение клиента не доказывает прекращение серверного вычисления. При принудительной остановке журнал может закончиться резервированием или незавершённой строкой. Такой вызов имеет неизвестный исход и расход; его нельзя считать нулевым или успешным.

**Учебное расследование.** В `demo.json` найдите контроль `repair`: сопоставьте обе попытки, общий статус и статус итогового кандидата. Затем изучите `no_progress` и `call_limit`: объясните причину остановки и судьбу остальных слотов. В собственном живом прогоне сопоставьте резервирование в журнале с HTTP-телеметрией и ledger. Не редактируйте сохранённые свидетельства для получения другого статуса; повторите эксперимент с новым именем каталога.

Коды выхода: **0 PASS, 1 FAIL, 2 ERROR, 3 INCOMPLETE**. При `ERROR` смотрите `error_code`, `worker.log`, целостность и очистку процесса; при `INCOMPLETE` — тайм-аут, журнал и оставшиеся случаи. `started.json` без `result.json` означает незавершённую работу. Локальные хэши не являются независимой подписью и не создают закрытую контрольную выборку. Полезность порядка, AML-приёмка и калибровка Judge не измеряются: `candidate_admission=NOT_EVALUATED`, `product_acceptance=INCOMPLETE`. Просмотр сохранённого запуска доступен в интерфейсе аналитика QA — раздел 10.13. [Технический паспорт](../../qa/autonomy/README.md).

<a id="qa-analyst"></a>
### 10.13. Интерфейс аналитика QA

Экран предназначен для разбора сохранённых проверок Dark Factory. Он работает отдельно от оператора AML, читает локальные файлы и не требует запуска моделей, БД, API или Kafka. Порт — **8502**, адрес — [http://127.0.0.1:8502](http://127.0.0.1:8502).

**Подготовка.** Используйте `.venv-ci` из раздела 10.1. Если результатов ещё нет, в первом терминале создайте учебный запуск без модели:

```powershell
$qaRun = 'var/qa/autonomy/analyst-demo-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_autonomy.py --output-dir $qaRun
Split-Path -Leaf $qaRun
```

Сохраните выведенное имя. Во втором терминале из того же корня проекта запустите экран:

```powershell
.\.venv-ci\Scripts\python.exe -I -B -m streamlit run qa/analyst/app.py --server.address 127.0.0.1 --server.port 8502 --server.headless true --browser.gatherUsageStats false
```

**Работа в браузере.**

1. Оставьте раздел «Результаты проверок», выберите тип проверки и имя запуска слева. Для созданного примера нужен «Исправление ответа и бюджет». Имена отсортированы по алфавиту в обратном порядке; первое имя не обязательно означает последний запуск.
2. Прочитайте «Записанный итог» и «Чтение отчёта». PASS относится к конкретной проверке. FAIL — обнаруженный провал, ERROR — ошибка выполнения или чтения, INCOMPLETE — недостаточно данных, NOT_RUN — проверка не выполнялась.
3. Нажмите «Проверить файлы и текущий код». MATCH у свидетельств подтверждает совпадение с локальным перечнем хэшей. MATCH у исходников подтверждает совпадение кода на момент проверки; DIFFERENT означает другую версию. При ERROR или UNAVAILABLE опираться на запуск как на подтверждение качества нельзя. Эта кнопка не повторяет тесты и не измеряет сегодняшнюю LLM.
4. Откройте паспорт и таблицы. В общем прогоне видны невыполненные профили, требования и обоснование выбора; в оценщиках — контроли; в окружениях — шаги сценариев; в статистике — план, пропуски, семейства и расход; в цикле ответа — первая и окончательная оценки, попытки, бюджет и хронология.
5. Нажатие «Обновить список» перечитывает файлы. Проверка актуальности сбрасывается в NOT_CHECKED после следующего действия, чтобы устаревший зелёный результат не сохранялся. Для новой версии запускайте новый прогон с новым именем, сохраняя старые результаты.

**Упражнение.** В учебном запуске найдите `repair`: первый ответ FAIL, итоговый кандидат PASS, цикл остаётся FAIL. Квалификация контроля при этом PASS, потому что именно такое поведение ожидалось. У `unknown_usage` полный расход не измерен; известная часть и резерв не заменяют неизвестный итог нулём. Откройте общий execution-прогон: четыре программных профиля могут пройти, а весь план оставаться INCOMPLETE из-за NOT_RUN моделей и других отдельных проверок.

**Браузерный автотест.** После установки фиксированных browser tools командой `.\scripts\qa\Install-BrowserTools.ps1` (раздел 10.2; требуется основная `.venv`) оставьте UI работающим. В первом терминале, где сохранился `$qaRun`, выполните:

```powershell
$browserCheck = 'var/qa/analyst-browser/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_analyst_browser.py --kind autonomy --run-id (Split-Path -Leaf $qaRun) --output-dir $browserCheck
Get-Content "$browserCheck/result.json" -Raw -Encoding UTF8
```

Команда проверяет настоящий Chromium: выбор запуска, записанный итог, явную проверку файлов и кода, неизменную границу приёмки и сброс проверки при обновлении. Шесть проверок должны завершиться PASS, код выхода 0; ERROR даёт код 2. Артефакты — `result.json` и `verified.png` в новом каталоге. Это проверка экрана аналитика, а не AML-оператора или всей интеграционной цепочки. Unit/AppTest этого экрана также входят в полную регрессию.

**Диагностика.** Пустой список означает отсутствие запусков выбранного типа. Каталог без result.json считается незавершённым; экран не определяет, жив ли процесс. `ANALYST_DETAIL_CHANGED` означает изменённый файл детализации, `EVIDENCE_CHANGED_OR_MISSING` — изменённое/отсутствующее свидетельство. Сначала сохраните имя запуска и код ошибки, разберите причину; повтор выполняйте в новом каталоге. Запрещённые ссылки, чужие пути, слишком большие или повреждённые файлы вызывают явную ошибку. При занятом порте остановите прежний процесс экрана в его терминале. Не меняйте порт сервиса оператора.

Завершайте UI через **Ctrl+C** во втором терминале. Он не меняет отчёты, БД или решения, не вызывает модели, не делает Git-операций и не выполняет команды из файлов. Локальные хэши не являются независимой подписью; семантическая оценка сохранённых отчётов не пересчитывается. Product acceptance остаётся INCOMPLETE, candidate admission — NOT_EVALUATED. Учебные расследования доступны в соседнем разделе экрана, описанном ниже. [Технические ограничения чтения](../../qa/analyst/README.md).

<a id="quality-investigations"></a>
### 10.14. Учебные расследования качества Dark Factory

Практикум помогает объяснить, что именно доказывает проверка, почему возникло отклонение и какое действие нужно следующим. Для занятия не нужны модели, Docker, PostgreSQL или API. CLI создаёт новый пакет из исполняемых синтетических контролей, включая отдельную SQLite; браузер читает готовые факты и проверяет ваш ответ по открытой рубрике.

**Подготовка в терминале VS Code.** Откройте корень проекта в PowerShell 7. Если `.venv-ci` ещё нет, выполните один раз:

```powershell
py -3.12 -m venv .venv-ci
.\.venv-ci\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/ci-windows-py312.lock
.\.venv-ci\Scripts\python.exe -m pip check
```

Создайте пакет. Во время подготовки не редактируйте исходники и рубрику:

```powershell
$learningRun = 'learning-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/build_investigations.py --run-id $learningRun
if ($LASTEXITCODE -ne 0) { throw 'Пакет не подготовлен; сохраните каталог и разберите ошибку' }
$learningDir = Join-Path 'var/qa/investigations' $learningRun
Get-Content (Join-Path $learningDir 'result.json') -Raw -Encoding UTF8
```

Ожидается `status=PASS`, `lesson_count=6`, `new_model_calls=0`. Это квалификация подготовки шести учебных случаев, а не оценка оператора. `operator_practice` остаётся `NOT_MEASURED`, `product_acceptance` — `INCOMPLETE`. Имя допускает латинские буквы, цифры, `_` и `-`, до 64 символов; первый символ — буква или цифра. Существующее имя использовать повторно нельзя.

Если экран аналитика уже работает на 8502, обновите его страницу. Иначе запустите его в отдельном терминале из того же корня:

```powershell
.\.venv-ci\Scripts\python.exe -I -B -m streamlit run qa/analyst/app.py --server.address 127.0.0.1 --server.port 8502 --server.headless true --browser.gatherUsageStats false
```

**Работа в браузере.** Откройте [http://127.0.0.1:8502](http://127.0.0.1:8502).

1. Слева выберите раздел **«Учебные расследования»**, затем своё имя пакета. Список упорядочен по имени, а не по времени подготовки.
2. Проверьте строку целостности: пакет и текущие исходники должны иметь `MATCH`. При `DIFFERENT` можно читать старые факты, но для оценки понадобится новый пакет.
3. Выберите расследование. Прочитайте его утверждение, область и связанные требования. Раскройте четыре факта `F01`–`F04`; у каждого показан источник в JSON-свидетельстве.
4. Выберите вывод, первопричину и следующий шаг. Отметьте факты, непосредственно подтверждающие выбранный вывод. Контекстный факт не обязательно является доказательством причины.
5. Запишите обоснование своими словами, до 2000 символов. Укажите наблюдение, объяснение, границу вывода и предлагаемую проверку. Этот текст автоматически не оценивается.
6. Нажмите **«Проверить разбор»**. `PASS` означает совпадение с открытой рубрикой, `FAIL` — расхождение, `INCOMPLETE` — незаполненные обязательные части. Прочитайте результат каждого критерия, объяснение и подсказку для регрессии. Рубрика доступна в раскрывающемся блоке.
7. Нажмите **«Скачать учебный разбор JSON»**. Сохраните файл в своей папке занятий. Он содержит ответ, оценку и привязку к пакету; сервер не записывает его в исходные свидетельства. Для следующего случая выберите другое расследование и повторите разбор.

**Программа занятий.**

| Случай | Задание | Что проверить в фактах |
| --- | --- | --- |
| L01 — исправленный ответ | Объясните, почему второй PASS не отменяет первый FAIL | Первую оценку, итогового кандидата и сохранённый статус цикла; отдельно — квалификацию контроля |
| L02 — неизвестные токены | Объясните, почему нельзя объявить нулевой или полностью известный расход | Неизвестную попытку, полный итог `null`, состояние учёта и остановку цикла |
| L03 — пропущенный повтор | Установите полноту измерения по плану | 12 запланированных наблюдений, 11 полученных, один пропуск и `INCOMPLETE` |
| L04 — зависимые наблюдения | Отделите число генераций от независимых случаев | Три случая, два семейства, шесть повторов на вариант; различие семантического содержания и порядка; перенос на популяцию не установлен |
| L05 — изменённый файл | Объясните, какой вывод допустим после несовпадения хэша | Исходный и новый хэши одного специально созданного файла, успешную проверку до изменения и ошибку после него |
| L06 — потерянное HTTP-подтверждение | Объясните, почему повтор запроса не обязан создавать второй эффект | Один job после потери ACK, два вызова submit после повтора, один ключ идемпотентности и один job |

L05 меняет только файл нового учебного пакета. Общий перечень хэшей закрепляет его итоговое состояние; старые отчёты и эталоны не изменяются. L06 использует реальный обработчик, HTTP-клиент и SQLite с моделями Kafka, очереди PostgreSQL, времени и сбоев. Его успех не подтверждает работу живой инфраструктуры.

**Что сохраняется локально.** Пакет находится в `var/qa/investigations/<run-id>`: `catalog.json`, наблюдения, выбранные факты, квалификация, снимки исходников/окружения, итог и `evidence.json`. Учебная SQLite принадлежит только этому запуску: `var/workflow/investigations/<run-id>/http-ack-replay`. Скачанные ответы выбираете и храните вы. Каталоги `var` и личные учебные ответы не входят в публикацию.

**Диагностика.** При `LEARNING_BUILD_ERROR` сохраните имя и каталог; отсутствие итогового пакета не означает провал вашего ответа. Устраните причину и повторите с новым именем. `LEARNING_PACK_ERROR` означает, что файлы, структура, рубрика или контрольные факты не прошли проверку. Не исправляйте хэши вручную: сохраните пакет для разбора и подготовьте новый. После изменения исходников старый пакет остаётся историческим. При изменении файлов во время заполнения формы проверка блокируется; обновите страницу. Для освобождения 8502 завершите UI через **Ctrl+C** в его терминале.

Открытая рубрика помогает учиться, но не измеряет независимое знание материала. Автопроверка не оценивает свободное обоснование (`NOT_SCORED`) и не закрывает практику человека (`NOT_MEASURED`). Хэши подтверждают согласованность локальных байтов, а не независимое происхождение. Исторический FAIL Judge, человеческая калибровка, продуктовая приёмка и удалённый CI после публикации остаются отдельными условиями. [Технический контракт практикума](../../qa/investigations/README.md).

<a id="publication"></a>
## 11. Публикация по логическим блокам

**Актуально на 23 ноября 2026 года.** Этапы 6–8 уже опубликованы. Состав обновления сверен с сохранённым деревом main для коммита `eb84b1651e32a5a88871b3ba3cdc92295fafd712`. После локальных проверок владелец отправляет два коммита. Первый содержит учебные расследования и изменения QA, второй — инструкции.

Первичная публикация и этапы 5–8 уже выполнены. Повторно переносить неизменённые компоненты не требуется.

### Состав обновления

| Коммит | Что обновляем | Источник путей |
| --- | --- | --- |
| 1. QA и подготовка публикации (20 файлов) | Практикум из шести расследований, генератор свидетельств, проверка целостности и версии, формы UI, CLI, тесты, включая браузерный сценарий оператора, карта влияния и проверка базы публикации | Новые и изменившиеся файлы из [03-qa.txt](../publication/03-qa.txt) |
| 2. Инструкции (8 файлов) | README сервиса, эксплуатация этапа 9, порядок отправки и перечни добавлений и замен | Новые и изменившиеся файлы из [04-docs.txt](../publication/04-docs.txt) |

В обновлении **14 новых и 14 изменённых файлов**. Все пять списков вместе содержат **681 файл**. С сохранённым `reports/iteration_log.md` итоговое дерево содержит **682 файла**. Удалений и изменений пяти входов Judge нет. [Состав по коммитам](../publication/what-to-update.md), [добавления](../publication/added-files.txt), [замены](../publication/updated-files.txt).

Локально остаются var/, рабочие PostgreSQL/SQLite, сообщения Kafka и Docker volumes, DBeaver workspace, модели/индексы, окружения Python, секреты, .env, логи и результаты занятий. Из verification/ в Git уже входят только пять явно перечисленных синтетических входов Judge; новые результаты проверок не публикуются. Из .rebuild/ публикуются только origin.json и release-version.json. Личный файл портфолио и незакоммиченные изменения исходного checkout автоматически не включаются.

### 1. Подготовить терминал

Используйте PowerShell 7.3+ в VS Code. Выполняйте по одному блоку, останавливайтесь при первой ошибке. Дождитесь завершения доработок и проверки текущего rebuild перед копированием; во время переноса источник не редактируйте.

```powershell
if ($PSVersionTable.PSVersion -lt [version]'7.3') { throw 'Нужен PowerShell 7.3 или новее' }
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$src = 'D:\work\aml-ai-agent-system-rebuild'
$pub = 'D:\work\publication-stage9\aml-ai-agent-system-publish'
$base = 'eb84b1651e32a5a88871b3ba3cdc92295fafd712'
$python = Join-Path $src '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Не найден Python стенда' }
function Assert-PathSet {
    param([string[]]$Expected, [string[]]$Actual, [string]$Message)
    if ((@($Expected | Sort-Object) -join "`n") -cne (@($Actual | Sort-Object) -join "`n")) { throw $Message }
}
& $python -I -B "$src\scripts\release\publication_files.py" check
git --version
git config --get user.name
git config --get user.email
```

check проверяет допустимость полных перечней и наличие файлов. user.name и user.email станут автором коммитов; при отсутствии настройте свои значения до продолжения. Выбран новый каталог внутри publication-stage9; существующий клон и учебный стенд сохраняются.

### 2. Создать чистый клон и скопировать поставку

```powershell
if (Test-Path -LiteralPath $pub) { throw 'Каталог публикации уже существует: разберите предыдущую попытку' }
$publicationParent = Split-Path -Parent $pub
if (-not (Test-Path -LiteralPath $publicationParent)) { New-Item -ItemType Directory -Path $publicationParent | Out-Null }
git clone --branch main --single-branch https://github.com/slim2115/aml-ai-agent-system-design.git $pub
Set-Location -LiteralPath $pub
git status --short --branch
git remote -v
git log -1 --oneline
if ((git rev-parse HEAD) -ne $base) { throw 'Main изменился: нужна повторная сверка' }
if ((git rev-parse origin/main) -ne $base) { throw 'origin/main отличается от проверенной базы' }
& $python -I -B "$src\scripts\release\publication_files.py" copy-to $pub
$lists = Join-Path $pub 'docs\publication'
git status --short
```

Помощник проверяет имя каталога, origin, ветку main, чистоту клона и оба базовых SHA, затем копирует только согласованные пути и сверяет байты. Он не удаляет файлы и не создаёт коммиты. Если main продвинулся относительно сохранённой базы, сначала сопоставьте новую базу с rebuild. После частичного копирования сохраните каталог для разбора; не повторяйте copy-to поверх грязного клона.

### 3. Проверить полноту переноса и состав diff

```powershell
$groups = @('01-git', '02-service', '03-qa', '03-judge-fixtures', '04-docs')
$paths = @($groups | ForEach-Object { Get-Content -LiteralPath (Join-Path $lists "$_.txt") })
if ($paths.Count -ne 681 -or @($paths | Sort-Object -Unique).Count -ne $paths.Count) { throw 'Неверный состав полных перечней' }
foreach ($relative in $paths) {
    $sourceFile = Join-Path $src $relative
    $copiedFile = Join-Path $pub $relative
    if (-not (Test-Path -LiteralPath $copiedFile -PathType Leaf)) { throw "Не перенесён файл: $relative" }
    if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $copiedFile -Algorithm SHA256).Hash) {
        throw "Различаются байты: $relative"
    }
}
$added = @(Get-Content -LiteralPath "$lists\added-files.txt")
$updated = @(Get-Content -LiteralPath "$lists\updated-files.txt")
$changes = @($added + $updated)
if ($added.Count -ne 14 -or $updated.Count -ne 14 -or @($changes | Sort-Object -Unique).Count -ne $changes.Count) { throw 'Неверный состав добавлений и замен' }
if (@($changes | Where-Object { $_ -notin $paths }).Count -ne 0) { throw 'Изменения вне полной поставки' }
Assert-PathSet $updated @(git diff --name-only --no-renames) 'Diff отличается от перечня замен'
Assert-PathSet $updated @(git diff --name-only --no-renames --diff-filter=M) 'Ожидались только изменения существующих файлов'
Assert-PathSet $added @(git ls-files --others --exclude-standard) 'Новые файлы отличаются от перечня добавлений'
if (@(git diff --name-only --no-renames --diff-filter=D).Count -ne 0) { throw 'Удаления не предусмотрены' }
$fixturePaths = @(Get-Content -LiteralPath "$lists\03-judge-fixtures.txt")
if (@($changes | Where-Object { $_ -in $fixturePaths }).Count -ne 0) { throw 'Изменения входов Judge не предусмотрены' }
if (@(git diff --cached --name-only).Count -ne 0) { throw 'Перед первым блоком индекс должен быть пуст' }
$qaPaths = @(Get-Content -LiteralPath "$lists\03-qa.txt")
$docPaths = @(Get-Content -LiteralPath "$lists\04-docs.txt")
$expectedQa = @($changes | Where-Object { $_ -in $qaPaths } | Sort-Object)
$expectedDocs = @($changes | Where-Object { $_ -in $docPaths } | Sort-Object)
if ($expectedQa.Count -ne 20 -or $expectedDocs.Count -ne 8 -or $expectedQa.Count + $expectedDocs.Count -ne $changes.Count) { throw 'Ожидалось 20 изменений QA и 8 изменений документации' }
git diff --stat
Write-Output "Проверено файлов: $($paths.Count); добавлений: $($added.Count); замен: $($updated.Count)"
```

Пять входов Judge входят в проверку хэшей, но их diff должен отсутствовать. Перечни читаются из подготовленного клона, сохраняя единый состав отправки. Перед коммитом просмотрите staged diff; ожидаются A для новых файлов и M для замен. Команды git add ., git add -A, git rm и force push для этого дополнения не нужны.

### 4. Коммит 1 и push 1: учебные расследования QA

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\03-qa.txt"
Assert-PathSet $expectedQa @(git diff --cached --name-only --no-renames) 'В QA-коммите неожиданные пути'
Assert-PathSet @($added | Where-Object { $_ -in $qaPaths }) @(git diff --cached --name-only --no-renames --diff-filter=A) 'В QA-коммите неожиданные добавления'
Assert-PathSet @($updated | Where-Object { $_ -in $qaPaths }) @(git diff --cached --name-only --no-renames --diff-filter=M) 'В QA-коммите неожиданные замены'
if (@(git diff --cached --name-only --no-renames --diff-filter=D).Count -ne 0) { throw 'Удаления не предусмотрены' }
git diff --cached --name-status
git diff --cached --stat
git diff --cached
git commit -m "test(qa): добавить учебные расследования качества Dark Factory" -m "Этап 9: шесть расследований на исполняемых синтетических контролях, проверка фактов и целостности пакетов, открытая рубрика, формы аналитика и экспорт учебного ответа. Добавлены CLI, тесты и браузерная проверка; обновлены карта влияния и базовый SHA помощника публикации. Приёмка продукта и практика человека остаются отдельными."
git show --stat --oneline HEAD
git push origin main
```

После push откройте [GitHub Actions](https://github.com/slim2115/aml-ai-agent-system-design/actions) и дождитесь hermetic-windows и browser-smoke-windows для нового SHA. При падении сохраните ссылку на запуск и разберите причину до следующего push. Локальная проверка не заменяет удалённый CI. Deployment автоматически не выполняется.

### 5. Коммит 2 и push 2: инструкции и перечни

```powershell
if (@(git diff --cached --name-only).Count -ne 0) { throw 'Перед следующим блоком индекс должен быть пуст' }
git --literal-pathspecs add --pathspec-from-file="$lists\04-docs.txt"
Assert-PathSet $expectedDocs @(git diff --cached --name-only --no-renames) 'В коммите документации неожиданные пути'
Assert-PathSet @($added | Where-Object { $_ -in $docPaths }) @(git diff --cached --name-only --no-renames --diff-filter=A) 'В документации неожиданные добавления'
Assert-PathSet @($updated | Where-Object { $_ -in $docPaths }) @(git diff --cached --name-only --no-renames --diff-filter=M) 'В документации неожиданные замены'
if (@(git diff --cached --name-only --no-renames --diff-filter=D).Count -ne 0) { throw 'Удаления не предусмотрены' }
git diff --cached --name-status
git diff --cached --stat
git diff --cached
git commit -m "docs: описать практикум расследований Dark Factory и его публикацию" -m "Обновлены README, руководство эксплуатации и QA-практикум: создание пакета, шесть упражнений, ограничения оценивания и диагностика. Пересчитаны точные списки добавлений и замен относительно текущего main; локальные базы, ответы и свидетельства не публикуются."
git show --stat --oneline HEAD
git push origin main
```

### 6. Итоговая проверка

```powershell
git fetch origin main
$localHead = git rev-parse HEAD
$remoteHead = git rev-parse origin/main
if ($localHead -ne $remoteHead) { throw 'Локальная и удалённая вершины различаются' }
git status --short --branch
git log -2 --oneline
git diff --name-status --no-renames $base HEAD
Assert-PathSet $added @(git diff --name-only --no-renames --diff-filter=A $base HEAD) 'Итоговые добавления отличаются от плана'
Assert-PathSet $updated @(git diff --name-only --no-renames --diff-filter=M $base HEAD) 'Итоговые замены отличаются от плана'
Assert-PathSet $changes @(git diff --name-only --no-renames $base HEAD) 'Итоговый diff отличается от плана'
$expectedTree = @($paths + 'reports/iteration_log.md' | Sort-Object)
$actualTree = @(git ls-tree -r --name-only HEAD | Sort-Object)
Assert-PathSet $expectedTree $actualTree 'Git-дерево не совпадает с согласованной поставкой'
if (@(git status --porcelain).Count -ne 0) { throw 'Остались незакоммиченные файлы' }
Write-Output "Опубликовано: $localHead; файлов: $($actualTree.Count)"
```

Ожидаются два новых коммита, 682 файла в конечном дереве, чистая рабочая копия и совпадающие HEAD/origin/main. Проверьте README, ссылки на [руководство эксплуатации](service-and-lab.md) и Actions для последнего SHA. Результаты удалённого CI фиксируются после отправки владельцем.

### Как продолжить после остановки

- До commit: изучите git status --short и git diff --cached; выясните причину, сохраняя предыдущую попытку.
- Commit создан, push не прошёл: после устранения причины повторите push. Такой же коммит повторно создавать не нужно. При расхождении историй сравните их перед продолжением.
- Терминал закрыт: восстановите настройки, функцию и переменные раздела 1, перейдите в $pub, задайте $lists = Join-Path $pub 'docs\publication'. Из раздела 3 восстановите только присваивания $groups, $paths, $added, $updated, $changes, $qaPaths, $docPaths, $expectedQa, $expectedDocs; проверки первоначального diff после частичного коммита повторять не надо. По git log -2 --oneline и staged diff определите следующий блок.
- Если защита main отклоняет push, нужен согласованный владельцем процесс публикации. Не обходите защиту force push; созданные локальные коммиты сохраняются.

<a id="analysis-case"></a>
## 12. Разбор решения для системного аналитика

**Проблема.** В исходной форме начинающему оператору было трудно различить подготовку модели, обработку задания, проверку результата и окончательное решение. Наличие технического статуса само по себе не объясняло следующее действие. Ошибка могла возникнуть и при трактовке HTTP `202` как готового отчёта.

**Выбранное решение.** Пять последовательных этапов сохраняют связь «состояние → доступное действие → ожидаемый результат». Для опытного оператора остаются история, идентификаторы и раскрываемые технические сведения. Раскрытие источников позволяет сверить факт до принятия решения. Термины `ready`, `succeeded`, `approved` относятся к разным объектам и отражаются отдельно.

| Вариант | Польза | Компромисс |
| --- | --- | --- |
| Все поля и действия на одном экране | Быстрый доступ для опытного пользователя | Junior должен сам восстановить порядок и зависимости |
| Пошаговая форма, выбранный вариант | Явный порядок, подсказки, объяснение блокировок | Больше переходов; полезно сохранять историю и текущий контекст |
| Автоматический проход до решения | Меньше ручных действий | Не соответствует роли оператора и текущей надёжности AI-оценки |

**Контракты и инварианты.** Задание имеет собственный жизненный цикл. Решение связано с `report_id`, версией, хэшем и ревизией. Экспорт требует одобрения и фиксирует локальную квитанцию. Повтор события узнаётся по `event_id` и содержимому; повтор доставки не должен создавать новое логическое задание. Изменение `practice` не меняет основание AML-отчёта. Проверки API, БД и интерфейса подтверждают эти границы совместно.

**Как проверять решение.** Машинные проверки покрывают разрешённые переходы, блокировки, конфликт ревизий, повтор событий и показ источников. С оператором ещё нужно измерить понятность следующего действия, число ошибок, время выполнения сценария и качество сверки фактов. Эти пользовательские показатели пока не измерены; автоматические тесты не подтверждают удобство интерфейса.

**Как представить работу на собеседовании.** Разберите один `INC-000123` по цепочке: цель пользователя → выбранный UX → JSON-контракт → таблицы и ключи → асинхронный процесс → альтернативные исходы → критерии приёмки → имеющиеся доказательства и открытые вопросы. Коммерческий эффект и независимую AML-экспертизу представляйте как задачи последующей проверки.
