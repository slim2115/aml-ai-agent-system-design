# Playwright: учебная проверка интерфейса

Основная установка и полный рабочий цикл собраны в [единой инструкции](service-and-lab.md). Команды этого справочника выполняются из корня вашей копии. Упоминания сохранённых прогонов относятся к указанной дате; их локальные архивы не входят в обычный клон.

Инструкция для PowerShell 7 в терминале VS Code, 17.10.2026. Нужен подготовленный проект `.` и его `.venv` (Windows x64, Python 3.12).

Шаг 4 завершён: основной профиль **12/12 PASS**, отдельный сценарий в видимом окне **1/1 PASS**, намеренно неверная сумма обнаружена. Итог и границы проверки (локальный материал: `verification/qa-step-04-2026-10-14/summary.md`). QA-стенд после проверки остановлен; для собственной практики выполните команды ниже.

Основной профиль проверяет настоящий Chromium, пошаговый интерфейс, HTTP API и сохранение в SQLite. Исполнитель моделей управляется тестом: так воспроизводятся успех, отказ, отмена и тайм-аут. Строка в панели Kafka — учебная подстановка; PostgreSQL, брокер Kafka и настоящие модели в этом профиле не запускаются. Результат не является оценкой качества LLM.

## 1. Установить инструменты один раз

```powershell
# Терминал открыт в корне вашей копии проекта.
.\scripts\qa\Install-BrowserTools.ps1
```

Установка загружает закреплённые пакеты Playwright и Chromium в `var\qa\tooling`. Пакеты сервиса в `.venv` не изменяются. Нужен доступ к PyPI и официальному CDN Playwright. Успешный ответ содержит `status: PASS`; запись установки и проверка запуска браузера сохраняются в отдельном новом каталоге `var\qa\tooling\install-*`.

При сетевом тайм-ауте проверьте соединение и повторите команду. Не продолжайте к тестам, пока проверка установки не завершилась успешно. Используемые версии закреплены в `qa\requirements-browser.lock`; обновление версий — отдельное изменение, а не способ скрыть сбой теста. [Официальная установка Playwright](https://playwright.dev/python/docs/intro).

Установка уже проверена: **Playwright 1.63.0**, pytest-playwright **0.10.0**, Chromium **153.0.8010.12**. Установка из проверенных локальных архивов завершилась `PASS`: запись установки (локальный материал: `verification/qa-step-04-2026-10-14/tooling/install-2026-10-16-cached/installation/installation.json`), проверка запуска Chromium без окна (локальный материал: `verification/qa-step-04-2026-10-14/tooling/install-2026-10-16-cached/installation/browser-smoke.json`). Это подтверждает готовность инструмента; результаты проверки сервиса смотрите отдельно в каталоге своего прогона.

Если загрузка Chromium через Node снова завершится тайм-аутом, можно повторно использовать уже скачанные официальные архивы. Следующая команда проверяет их SHA256 и передаёт исходному установщику Playwright через временный локальный кеш. Нужны сохранённые архивы из прогона 15.10; каталог результата каждый раз новый:

```powershell
$qaInstallEvidence = 'verification/qa-step-04-2026-10-14/tooling/reinstall-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_cache_install.py --output-dir $qaInstallEvidence --reuse-downloads 'verification/qa-step-04-2026-10-14/tooling/install-2026-10-15-curl/downloads.json'
if ($LASTEXITCODE -ne 0) { throw 'Установка не завершена; проверьте installer.log' }
Get-Content -LiteralPath (Join-Path $qaInstallEvidence 'installation/installation.json') -Raw
```

Ожидается `status: PASS`; при отсутствующих или изменённых архивах команда остановится. Этот резервный способ не нужен, когда обычный установщик работает.

## 2. Подготовить и запустить отдельный стенд

```powershell
$qaDescription = & .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py prepare
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить браузерный QA-стенд' }
$qaStand = $qaDescription | ConvertFrom-Json
$qaRunId = $qaStand.run_id
$qaBase = $qaStand.data_directory
$qaRunId

& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py start --run-id $qaRunId
if ($LASTEXITCODE -ne 0) { throw 'Стенд не запущен; проверьте status и логи' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py status --run-id $qaRunId
```

Сохраните `run_id`: он нужен для следующих команд и остановки. В `status` оба значения `services.api` и `services.ui` должны быть `true`. Интерфейс доступен на [127.0.0.1:18501](http://127.0.0.1:18501), Swagger — на [127.0.0.1:18765/docs](http://127.0.0.1:18765/docs). При запуске теста браузер открывает их сам.

Одновременно работает один QA-стенд. Если остался запущен полный стенд из шага 2, сначала остановите его через `qa\tools\stand.py stop --run-id СОХРАНЁННЫЙ_ID`. Для браузерного стенда используйте именно `browser_stand.py`; запуск полного стенда с его идентификатором запрещён.

В новом терминале восстановите переменные, подставив свой идентификатор:

```powershell
# Терминал открыт в корне вашей копии проекта.
$qaRunId = 'ВСТАВЬТЕ_СОХРАНЁННЫЙ_RUN_ID'
$qaBase = Join-Path (Get-Location) ('var\qa\runs\' + $qaRunId)
```

## 3. Запустить тесты

```powershell
$qaEvidence = 'verification/qa-browser-user-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaEvidence --profile deterministic
$qaExitCode = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaEvidence 'result.json') -Raw
```

Ожидается `status: PASS`, код выхода `0`, отсутствие ошибок и пропусков. Профиль содержит 12 сценариев: подготовка и её отказ; проверка ввода; отчёт и источники; отказ анализа; отмена; тайм-аут; история после перезагрузки; конфликт решений; одобрение, экспорт и скачивание; открытие связанного задания из панели Kafka; авторизация и запрос в Swagger.

Каждый прогон получает **новый** каталог результата. Тесты создают задания и решения в собственных QA-данных. Во время прогона не работайте вручную в этом QA-интерфейсе: тесты последовательно переключают режим исполнителя. Для обычной работы сервиса остаются его отдельные данные и порты.

Чтобы наблюдать действия в окне Chromium, добавьте `--headed`. В видимом режиме проверен один сценарий отчёта и источников — 1/1 PASS (локальный материал: `verification/qa-step-04-2026-10-14/browser-headed-2026-10-17/result.json`); полный набор из 12 проверен без окна. Пример запуска видимого сценария:

```powershell
$qaEvidence = 'verification/qa-browser-visible-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaEvidence --headed --test test_template_job_finishes_and_shows_exact_sources
```

По умолчанию `--test` принимает точное имя одной функции из `qa\e2e\test_operator.py` (`--suite operator`). Для собственных упражнений есть отдельный `--suite workshop` с файлом `qa\e2e\test_workshop.py`: [практикум](qa-workshop-browser.md). Успех одного выбранного сценария не означает успех всех 12. [Параметры и фикстуры официального pytest-плагина](https://playwright.dev/python/docs/test-runners).

## 4. Прочитать результат и разобрать сбой

| Файл в каталоге результата | Для чего нужен |
|---|---|
| `summary.md` | Краткий итог, число тестов и область проверки |
| `result.json` | Машинный статус, версии, выбранный профиль и список артефактов |
| `tests.xml` | Результаты отдельных тестов в формате JUnit |
| `pytest.log` | Сообщение ошибки и ожидаемое/полученное значение |
| `artifacts\<id>\capture.json` | Имя теста и правила сохранения его диагностики |
| `artifacts\<id>\observations.jsonl` | Коды ответов и идентификаторы заданий/отчётов для сверки, без ключа и тел запросов |
| `artifacts\<id>\trace.zip`, `failure.png` | Действия и снимок страницы при сбое обычного UI-теста |

`FAIL` означает провал проверки. `ERROR` означает, что запуск или сбор тестов не позволил получить результат; сначала разбирайте эту причину. Отсутствие тестов или пропуски не дают успешную квалификацию. Ошибка до создания каталога выводится в терминал.

Для Swagger-сценария trace и снимки отключены, поскольку форма и запрос содержат приватный ключ QA. Используйте его JUnit и журнал; runner маскирует точное значение ключа в этих текстах. Файл `lab-connection.json` не включайте в пересылаемую диагностику. При ошибке подготовки или завершения фикстуры trace также может отсутствовать: сначала прочитайте JUnit и журнал.

Открыть trace локально:

```powershell
$qaTrace = Get-ChildItem -LiteralPath (Join-Path $qaEvidence 'artifacts') -Filter trace.zip -Recurse | Select-Object -First 1
if ($null -eq $qaTrace) { throw 'В этом прогоне trace не сохранён' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_tooling.py trace --path $qaTrace.FullName
```

Команда открывает локальный Trace Viewer; выберите последнее действие перед ошибкой и сравните ожидаемый элемент со снимком страницы. После просмотра закройте окно, при необходимости завершите команду через `Ctrl+C`. Просмотрщик принимает локальные архивы из `var\qa` и `verification`.

### Учебный опыт: намеренно неверная сумма

Этот режим меняет сумму только в ответе учебного исполнителя данного задания. Тест должен обнаружить расхождение:

```powershell
$qaDefectEvidence = 'verification/qa-browser-defect-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaDefectEvidence --test test_template_job_finishes_and_shows_exact_sources --inject-defect corrupt_total
$qaDefectExitCode = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaDefectEvidence 'result.json') -Raw
```

Здесь ожидается **`FAIL` и код `1`**, а в журнале — именно проверка суммы. Ошибка старта браузера не считается обнаружением дефекта. Найдите trace и снимок, затем запустите тот же тест **без** `--inject-defect` в новый каталог: ожидается `PASS`. Исправлять ожидаемое значение теста под неверный ответ не нужно.

## 5. Остановить стенд

```powershell
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py stop --run-id $qaRunId
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py status --run-id $qaRunId
```

Ожидается `status: stopped`, `data: retained`; API и UI становятся `false`. Результаты и SQLite остаются для разбора. Продолжить тот же стенд можно командой `start` с прежним `run_id`; для нового набора данных используйте новый `prepare` после остановки.

Логи стенда: `<qaBase>\logs\api.log` и `ui.log`. При занятом порте или `ANOTHER_QA_RUN_MUST_BE_STOPPED_FIRST` остановите прежний **свой** QA-запуск по его идентификатору. При `QA_EVIDENCE_ALREADY_EXISTS` задайте новый каталог результата. `QA_BROWSER_RUN_ALREADY_ACTIVE` означает, что тесты этого стенда уже выполняются в другом терминале: дождитесь их завершения. Не завершайте все процессы Python и не удаляйте данные ради повторного запуска.

## Отдельный профиль настоящих моделей

`--profile real-model` выбирает один дополнительный smoke-тест с настоящим поиском и генерацией. Он **не выполняется** вместе с основными 12 сценариями и не использует `browser_stand.py`.

Сначала остановите браузерный стенд. Подготовьте и запустите **другой** полный QA-стенд по [инструкции шага 2](qa-stand-quickstart.md). Для этого профиля дополнительно нужны уже установленные модель поиска `paraphrase-multilingual-MiniLM-L12-v2` и её manifest в `var\models`, запущенный локальный Ollama с `qwen2.5:14b`. Команда теста не скачивает модели. Используйте `run_id` полного стенда:

```powershell
$qaRealRunId = 'ВСТАВЬТЕ_RUN_ID_ПОЛНОГО_QA_СТЕНДА'
$qaEvidence = 'verification/qa-browser-real-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRealRunId --output-dir $qaEvidence --profile real-model
```

Профиль проверяет, что отчёт сохраняет факты и требует решения оператора перед экспортом. Подготовка и обработка могут занимать несколько минут. После проверки остановите полный стенд через `qa\tools\stand.py stop --run-id $qaRealRunId`.

В квалификацию основного шага 4 этот профиль не включён; подтверждённого результата его запуска пока нет. Даже успешный одиночный smoke не измеряет устойчивость модели, качество по выборке или согласие с экспертом. Эти измерения и LLM as a Judge выполняются отдельно по [плану QA](../rebuild/qa-practice-plan.md).
