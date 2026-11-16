# Практикум: найти дефект интерфейса через Playwright

Основная установка и полный рабочий цикл собраны в [единой инструкции](service-and-lab.md). Команды этого справочника выполняются из корня вашей копии. Упоминания сохранённых прогонов относятся к указанной дате; их локальные архивы не входят в обычный клон.

PowerShell 7 в терминале VS Code, 24.10.2026. За одно занятие вы запустите отдельный UI/API-стенд, проверите поле, обнаружите неверную сумму, прочитаете trace и подтвердите восстановление. Для каждого опыта сохраняйте новый каталог результата.

Нужны `.venv`, закреплённые инструменты браузера и Chromium из [инструкции установки](qa-playwright-quickstart.md). Здесь используются настоящий Chromium, интерфейс, API и SQLite, а ответы исполнителя управляются тестом. Этот профиль не запускает PostgreSQL, Kafka или модели и не измеряет качество LLM. Для интеграции используйте [полный QA-стенд](qa-stand-quickstart.md).

## 1. Подготовить отдельный стенд

```powershell
# Терминал открыт в корне вашей копии проекта.
$qaDescription = & .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py prepare
if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить стенд' }
$qaStand = $qaDescription | ConvertFrom-Json
$qaRunId = $qaStand.run_id
$qaRunId
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py start --run-id $qaRunId
if ($LASTEXITCODE -ne 0) { throw 'Стенд не запущен' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py status --run-id $qaRunId
```

Сохраните `run_id`. Ожидается код `0`, `services.api: true`, `services.ui: true`. Тест сам открывает интерфейс на [127.0.0.1:18501](http://127.0.0.1:18501). Одновременно выполняется один QA-прогон; во время теста не меняйте его данные вручную.

Если уже работает другой QA-стенд, остановите его штатной командой по сохранённому идентификатору: браузерный — `browser_stand.py stop --run-id ...`, полный — `stand.py stop --run-id ...`. Данные и результаты остановка сохраняет.

## 2. Выбрать один тест и увидеть его действия

Сначала запустите проверку отчёта без видимого окна — это режим headless по умолчанию:

```powershell
$qaBaseline = 'verification/qa-workshop-browser-pass-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaBaseline --suite operator --test test_template_job_finishes_and_shows_exact_sources
$qaBaselineExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaBaseline 'result.json') -Raw
```

Ожидается код `0`, `status: PASS`, `testcases: 1`, `failure/error/skipped: 0`. Тест проверяет сохранённые факты и сумму, отображение суммы, источники и доступность перехода к решению. Одиночный PASS относится только к выбранному сценарию.

Повторите с `--headed`, чтобы увидеть Chromium:

```powershell
$qaVisible = 'verification/qa-workshop-browser-visible-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaVisible --suite operator --test test_template_job_finishes_and_shows_exact_sources --headed
$qaVisibleExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaVisible 'result.json') -Raw
```

Ожидается тот же результат. Не нажимайте кнопки в управляемом браузере: это меняет проверяемый сценарий. Полный профиль из 12 проверок запускается без `--test`; его результат храните отдельно.

## 3. Проверить поле и добавить свой случай

Сначала посмотрите готовый пример выбора поля и блокировки отправки:

```powershell
$qaField = 'verification/qa-workshop-browser-field-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaField --suite operator --test test_incident_format_and_synthetic_consent_gate_submission --headed
$qaFieldExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaField 'result.json') -Raw
```

Ожидается `PASS`, код `0`, один тест. Неверный идентификатор показывает объяснение и блокирует кнопку; корректный `INC-000123` разрешает отправку после согласия на синтетические данные. Снятие согласия снова блокирует отправку, а задания не создаются.

В [исходнике сценария](../../qa/e2e/test_operator.py) поле выбирается по роли и доступному имени:

```python
incident = page.get_by_role('textbox', name='Идентификатор инцидента', exact=True)
incident.fill('INC-000123')
incident.press('Tab')
expect(incident).to_have_value('INC-000123')
```

`Tab` снимает фокус: Streamlit принимает изменение поля при потере фокуса. `to_have_value` проверяет значение поля; `to_be_enabled` у кнопки проверяет другой результат. Проверка значения полезна, например, после перехода между этапами формы.

Для собственных изменений есть отдельный [учебный файл](../../qa/e2e/test_workshop.py). Набор `workshop` запускается явно и не меняет основной `operator`. В нём уже добавлены две проверки:

- `test_incident_field_defaults_and_keeps_edited_value`: начальное значение `INC-000123`, ввод `INC-000250` и сохранение значения после перехода между этапами.
- `test_decision_requires_nonblank_reason`: кнопка сохранения остаётся выключенной при пустом обосновании или пробелах и включается после заполнения решения и текста. Решение в историю не записывается.

```powershell
$qaWorkshop = 'verification/qa-workshop-fields-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaWorkshop --suite workshop --headed
$qaWorkshopExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaWorkshop 'result.json') -Raw
```

До собственных дополнений ожидается **2 теста, PASS, код 0**. Для одного теста добавьте `--test test_decision_requires_nonblank_reason`. Набор `workshop` допускает только детерминированный профиль; `--inject-defect` из следующего упражнения относится к набору `operator`.

Задание: откройте `qa/e2e/test_workshop.py` в VS Code и добавьте в конец новый случай, который проверяет пустой идентификатор:

```python
def test_empty_incident_shows_format_help(page, qa_api, browser_control):
    browser_control()
    ui = OperatorPage(page, qa_api).open()
    ui.prepare()
    ui.incident()
    incident = page.get_by_role('textbox', name='Идентификатор инцидента', exact=True)
    incident.fill('')
    incident.press('Tab')
    expect(incident).to_have_value('')
    expect(page.get_by_text(
        'Введите идентификатор в формате INC-000123: INC- и шесть цифр.', exact=True
    )).to_be_visible()
    expect(page.get_by_role('button', name='Создать задание', exact=True)).to_be_disabled()
```

Сохраните файл **до** запуска. Затем выберите новый тест:

```powershell
$qaOwnCase = 'verification/qa-workshop-own-case-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaOwnCase --suite workshop --test test_empty_incident_shows_format_help
$qaOwnCaseExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaOwnCase 'result.json') -Raw
```

Ожидается один PASS. После этого повторите `workshop` без `--test` в новый каталог: теперь ожидаются **3 PASS**. Это новый случай UI-проверки; наборы `qa/datasets/aml-v1` и `qa/judge` остаются замороженными. Для нового модельного кейса требуется отдельная версия набора и ожидаемых результатов, его нельзя добавлять в исторический baseline. Основные регрессионные сценарии находятся в `test_operator.py`; их менять ради практикума не нужно.

## 4. Обнаружить неверную сумму

Режим `corrupt_total` подменяет только отображаемый текст суммы в ответе QA-исполнителя. Типизированные исходные значения остаются корректными: так можно увидеть, почему одной проверки API недостаточно.

```powershell
$qaDefect = 'verification/qa-workshop-browser-defect-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaDefect --suite operator --test test_template_job_finishes_and_shows_exact_sources --inject-defect corrupt_total
$qaDefectExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaDefect 'result.json') -Raw
Get-Content -LiteralPath (Join-Path $qaDefect 'pytest.log') -Tail 50
```

Ожидается **код `1`, `status: FAIL`, один провал**, связанный с отображаемой суммой. Подмена поддерживается только для этого сценария в детерминированном профиле. Ошибка старта стенда, браузера или сбора тестов не подтверждает обнаружение суммы.

Откройте trace:

```powershell
$qaTrace = Get-ChildItem -LiteralPath (Join-Path $qaDefect 'artifacts') -Filter trace.zip -Recurse | Select-Object -First 1
if ($null -eq $qaTrace) { throw 'Trace отсутствует; сначала прочитайте tests.xml и pytest.log' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_tooling.py trace --path $qaTrace.FullName
```

В Trace Viewer найдите последнее неуспешное ожидание текста суммы. Сравните ожидаемую строку в ошибке со снимком страницы; затем найдите `assert_base_display` в [проверках отображения](../../qa/e2e/pages.py). Это проверка текста, который читает оператор. В `artifacts/<id>/observations.jsonl` найдите `job_id` и `report_id` именно этого опыта; идентификаторы других запусков не подставляйте.

Сформулируйте дефект в трёх строках: что ожидалось, что увидел оператор, какой тест и trace это подтверждают. Закройте Trace Viewer, при необходимости завершите его команду через `Ctrl+C`. Для сценария Swagger trace и снимки отключены, потому что он вводит ключ.

## 5. Подтвердить восстановление

Повторите тот же тест без подмены:

```powershell
$qaRestored = 'verification/qa-workshop-browser-restored-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_browser.py --run-id $qaRunId --output-dir $qaRestored --suite operator --test test_template_job_finishes_and_shows_exact_sources
$qaRestoredExit = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $qaRestored 'result.json') -Raw
```

Ожидается `PASS`, код `0`, один тест без провалов, ошибок и пропусков. Сохраните оба каталога: `FAIL` с подменой и `PASS` без неё. В этом упражнении восстанавливается нормальный ответ QA-исполнителя; код сервиса не исправляется, прежний отчёт с подменой не переписывается. Ожидаемую сумму в тесте менять не нужно.

## 6. Завершить занятие

```powershell
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py stop --run-id $qaRunId
if ($LASTEXITCODE -ne 0) { throw 'Проверьте остановку стенда' }
& .\.venv\Scripts\python.exe -I -B .\qa\tools\browser_stand.py status --run-id $qaRunId
```

Ожидается `stopped`, `data: retained`, затем `services.api: false`, `services.ui: false`. Оставьте краткую запись:

В шаге 10 при завершении серии обнаружено зависание QA UI. Лаунчер теперь ограничивает ожидание завершения соединений Uvicorn до 5 с, а ожидание остановки Streamlit — до 20 с. Если появляется `QA_UI_SHUTDOWN_TIMEOUT` или команда `stop` возвращает ошибку, сохраните `run_id`, текст ошибки и журнал `<qaBase>/logs/ui.log`; такой результат не считается успешной остановкой. Не запускайте второй QA-стенд до разбора состояния первого. Закрытие страницы браузера само по себе не завершает UI/API.

| Опыт | Ожидаемый результат | Что сохранить |
|---|---|---|
| Отчёт, headless/headed | PASS, код 0 | Каталоги результатов каждого запуска |
| Проверка поля | PASS, код 0 | Имя теста и проверенное условие |
| Свой случай в `workshop` | Один PASS; весь набор после добавления — 3 PASS | Изменённый учебный файл и новый каталог |
| Неверная сумма | FAIL, код 1 | `pytest.log`, `tests.xml`, trace и снимок |
| Без подмены | PASS, код 0 | Новый каталог, ссылка на предшествующий FAIL |
| Остановка | API/UI выключены | `run_id` и результат status |

Runner возвращает `2` при ошибке подготовки до запуска тестов; завершившийся неуспешный прогон возвращает `1`, при этом `result.json` различает `FAIL` и `ERROR`. Всегда читайте статус, число тестов и причину вместе с кодом выхода. Ноль собранных тестов не считается успехом.

Следующий уровень — [реальные SQL/API/Kafka](qa-stand-quickstart.md), [сводный QA-отчёт](qa-reporting-quickstart.md) и [план QA](../rebuild/qa-practice-plan.md).
