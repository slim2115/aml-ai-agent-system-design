# Практикум: факты, повторяемость и ошибки модельного оценщика

Основная установка и полный рабочий цикл собраны в [единой инструкции](service-and-lab.md). Команды этого справочника выполняются из корня вашей копии. Упоминания сохранённых прогонов относятся к указанной дате; их локальные архивы не входят в обычный клон.

Шаг 10, 24.10.2026. Основные упражнения читают уже сохранённые свидетельства шагов 6–9. Они **не вызывают модели**, не требуют API, Kafka, PostgreSQL или браузера и не изменяют исходные данные. Откройте проект `.` в VS Code и используйте терминал PowerShell.

В нашем сервисе `local-model` выбирает порядок допустимых идентификаторов фактов и правил. Суммы, тексты фактов, состав операций и ссылки формирует код. Поэтому здесь отдельно проверяем точность данных, повторяемость порядка и качество ответа Judge. Пригодность свободного аналитического заключения этот эксперимент не измеряет.

```powershell
# Терминал открыт в корне вашей копии проекта.
$qaLlmSaved = 'verification/qa-step-07-2026-10-20/pilot-first'
$qaJudgeSaved = 'verification/qa-step-08-2026-10-21/live-complete-2026-10-23'
$qaAmountSaved = 'verification/qa-step-06-2026-10-19/model-final/metamorphic/MT-03'
```

Записывайте свои ответы в отдельную заметку: упражнение, путь к свидетельству, ожидаемое поведение, наблюдение, вывод и ограничение вывода. Не редактируйте файлы в `verification/qa-step-06-*`, `qa-step-07-*`, `qa-step-08-*`, `qa-step-09-*` и замороженные наборы `qa/datasets`. Завершение упражнения оператором пока не подтверждено: приведённые ниже ответы описывают сохранённые измерения.

## 1. Сопоставить шаблон и два повторных ответа настоящей LLM

Откройте шаблон QA-S01 (локальный материал: `verification/qa-step-07-2026-10-20/pilot-first/cases/QA-S01/template.json`), [повтор 1](../../verification/qa-step-07-2026-10-20/pilot-first/cases/QA-S01/repeat-01.json) и повтор 2 (локальный материал: `verification/qa-step-07-2026-10-20/pilot-first/cases/QA-S01/repeat-02.json`). Это два наблюдения одной серии на одинаковом сценарии, а не два независимых инцидента.

```powershell
$qaTemplate = Get-Content -LiteralPath "$qaLlmSaved/cases/QA-S01/template.json" -Raw | ConvertFrom-Json
$qaFirst = Get-Content -LiteralPath "$qaLlmSaved/cases/QA-S01/repeat-01.json" -Raw | ConvertFrom-Json
$qaSecond = Get-Content -LiteralPath "$qaLlmSaved/cases/QA-S01/repeat-02.json" -Raw | ConvertFrom-Json
@($qaFirst, $qaSecond) | ForEach-Object {
    [pscustomobject]@{
        Repeat = $_.evaluation.repeat_index
        Check = $_.evaluation.status
        Facts = @($_.observation.result.found_facts).Count
        SemanticHash = $_.evaluation.draft.semantic_fingerprint
        OrderedHash = $_.evaluation.draft.ordered_fingerprint
        HttpAttempts = @($_.http_attempts).Count
        Seconds = $_.evaluation.latency_seconds
    }
} | Format-List
$qaFirst.observation.result.found_facts | Select-Object fact, evidence | Format-List
$qaFirst.http_attempts | ForEach-Object { $_.response_json.message.content }
```

Найдите 12 операций по `95000.00 RUB`, итог `1140000.00 RUB`, 17 фактов и ссылки на исходные операции. В ответе HTTP находятся `fact_order` и `rule_order`; модель не пишет новую сумму. Проверьте, что нет пропущенных или повторяющихся идентификаторов.

Для этого сохранённого сценария можно сравнить готовые отчёты, исключив два изменяемых служебных поля:

```powershell
$qaTemplateText = $qaTemplate.observation.result | Select-Object -Property * -ExcludeProperty report_id,generated_at | ConvertTo-Json -Depth 30 -Compress
$qaFirstText = $qaFirst.observation.result | Select-Object -Property * -ExcludeProperty report_id,generated_at | ConvertTo-Json -Depth 30 -Compress
$qaSecondText = $qaSecond.observation.result | Select-Object -Property * -ExcludeProperty report_id,generated_at | ConvertTo-Json -Depth 30 -Compress
[pscustomobject]@{
    FirstMatchesTemplateIncludingOrder = $qaFirstText -ceq $qaTemplateText
    SecondMatchesTemplateIncludingOrder = $qaSecondText -ceq $qaTemplateText
    SameSemantics = $qaFirst.evaluation.draft.semantic_fingerprint -ceq $qaSecond.evaluation.draft.semantic_fingerprint
    SameOrder = $qaFirst.evaluation.draft.ordered_fingerprint -ceq $qaSecond.evaluation.draft.ordered_fingerprint
}
```

**Ожидается:** четыре `True`, оба повтора `PASS`, по одному HTTP-вызову. Первый ответ занял около 62,03 с, второй — 15,39 с. Первый включал загрузку модели; это наблюдение, не SLA. У всех 15 сохранённых генераций состав и порядок совпали с шаблоном; свод сравнения (локальный материал: `verification/qa-step-07-2026-10-20/template-order-comparison.json`) содержит отдельные строки, метрики (локальный материал: `verification/qa-step-07-2026-10-20/pilot-first/descriptive-metrics.json`) — времена и повторяемость.

**Вывод упражнения:** технические проверки пройдены, дополнительная польза модельного порядка не установлена. На новом прогоне иной порядок при том же составе может быть допустим. Нельзя требовать побайтового совпадения всего JSON: `report_id` и `generated_at` меняются. Равные ответы двух повторов не доказывают детерминированность модели для любых входов.

## 2. Отличить изменение источника от ошибки суммы в отчёте

Сначала разберите сохранённое преобразование `MT-03`. В копии источника сумма `TX-7700` изменена с `95000.00` на `95000.01`; вслед за ней итог RUB должен измениться с `1140000.00` на `1140000.01`. Состав операций и остальные факты должны сохраниться.

```powershell
$qaBeforeRows = Get-Content -LiteralPath "$qaAmountSaved/before/transactions.json" -Raw | ConvertFrom-Json
$qaAfterRows = Get-Content -LiteralPath "$qaAmountSaved/after/transactions.json" -Raw | ConvertFrom-Json
$qaBeforeRows | Where-Object tx_id -eq 'TX-7700' | Select-Object tx_id,amount,currency
$qaAfterRows | Where-Object tx_id -eq 'TX-7700' | Select-Object tx_id,amount,currency
$qaBeforeReport = Get-Content -LiteralPath "$qaAmountSaved/before-observation.json" -Raw | ConvertFrom-Json
$qaAfterReport = Get-Content -LiteralPath "$qaAmountSaved/after-observation.json" -Raw | ConvertFrom-Json
$qaBeforeReport.result.found_facts | Where-Object { $_.fact -match 'TX-7700|Сумма всех' } | Select-Object fact
$qaAfterReport.result.found_facts | Where-Object { $_.fact -match 'TX-7700|Сумма всех' } | Select-Object fact
Get-Content -LiteralPath "$qaAmountSaved/result.json" -Raw
```

**Ожидается:** `PASS`, `issues=[]`. Результат изменился согласованно с входом. Это шаблон и настоящий поиск, без генерации LLM. Для денег сверяйте десятичные строки или `Decimal`, не округлённый `float`.

Теперь откройте J-002 (локальный материал: `verification/qa-step-08-2026-10-21/live-complete-2026-10-23/cases/J-002.json`). Здесь источник прежний, а в кандидате отчёта намеренно подставлен итог `1140100.00`.

```powershell
$qaWrongAmount = Get-Content -LiteralPath "$qaJudgeSaved/cases/J-002.json" -Raw | ConvertFrom-Json
$qaWrongAmount.payload.sources | Where-Object ref_id -eq 'source/aggregates' | Format-List
$qaWrongAmount.payload.candidates[0].fragments | Where-Object { $_.text -match '1140100' } | Format-List
$qaWrongAmount.observation.programmatic_gate | ConvertTo-Json -Depth 5
$qaWrongAmount.observation.response | Select-Object verdict,rationale | Format-List
```

**Ожидается:** источник `1140000.00`, кандидат `1140100.00`, программная проверка `FAIL` с `A:EVIDENCE_CONTEXT_INVALID` и `A:FACT_CATALOG_MISMATCH`; Judge также ответил `fail`. Это намеренно созданный дефект для проверки оценщика, а не обнаруженная ошибка исходного генератора. Даже если бы Judge ответил `pass`, программный запрет остался бы в силе.

В заметке объясните различие: изменение входа с точным пересчётом — корректное поведение; изменение только суммы отчёта при прежнем источнике — нарушение фактов. Самостоятельное внесение и восстановление ошибки делайте по основному практикуму в отдельной рабочей копии, а эти свидетельства оставьте неизменными.

## 3. Найти ошибку в ответе самого Judge

Упражнение ниже использует исторический `legacy-v1`. Для нового `compact-v2` есть [отдельная инструкция](qa-judge-quickstart.md#5-новый-профиль-compact-v2-25102026) и отчёт (локальный материал: `verification/qa-judge-v2-2026-10-25/summary.md`); разные серии сохраняются отдельно.

В J-001 (локальный материал: `verification/qa-step-08-2026-10-21/live-complete-2026-10-23/cases/J-001.json`) исходный отчёт прошёл программную проверку, но Judge сослался на источник и процитировал пересказ из отчёта. Для проверки выберите одну ссылку:

```powershell
$qaBadQuote = Get-Content -LiteralPath "$qaJudgeSaved/cases/J-001.json" -Raw | ConvertFrom-Json
$qaRawJudge = $qaBadQuote.transport.response_text | ConvertFrom-Json
$qaReference = $qaRawJudge.references[0]
$qaSource = $qaBadQuote.payload.sources | Where-Object ref_id -eq $qaReference.ref_id
[pscustomobject]@{
    RefId = $qaReference.ref_id
    Quote = $qaReference.quote
    ExactSubstring = $qaSource.text.Contains($qaReference.quote)
    JudgeObservation = $qaBadQuote.observation.status
    ErrorCode = $qaBadQuote.observation.error_code
}
$qaSource | Format-List
```

**Ожидается:** `ExactSubstring=False`, `invalid_response`, `QA_JUDGE_REFERENCE_QUOTE`. Сам HTTP-запрос завершился; проверка ссылки отвергла ответ. Исходный текст с `verdict=pass` нельзя считать валидной оценкой. Цитата должна быть точной непустой подстрокой именно указанного `ref_id`, а не похожим по смыслу предложением из другого фрагмента.

Затем разберите J-016 (локальный материал: `verification/qa-step-08-2026-10-21/live-complete-2026-10-23/cases/J-016.json`):

```powershell
$qaTruncated = Get-Content -LiteralPath "$qaJudgeSaved/cases/J-016.json" -Raw | ConvertFrom-Json
$qaEnvelope = $qaTruncated.transport.response_body | ConvertFrom-Json
[pscustomobject]@{
    Status = $qaTruncated.transport.status
    ErrorCode = $qaTruncated.transport.error_code
    DoneReason = $qaEnvelope.done_reason
    GeneratedTokens = $qaEnvelope.eval_count
}
```

**Ожидается:** `error`, `QA_JUDGE_INVALID_ENVELOPE`, `length`, `512`. Это усечение ответа на лимите токенов, не таймаут. Нельзя дописать закрывающую скобку за модель и засчитать исправленный вручную ответ. Увеличение лимита или изменение рубрики — уже новый эксперимент с отдельным протоколом и результатом; прежний неуспех сохраняется.

## 4. Прочитать общий результат без ложного PASS

Откройте общий QA-отчёт (локальный материал: `verification/qa-step-09-2026-10-23/report-qualified/report.html`) и итог Judge (локальный материал: `verification/qa-step-08-2026-10-21/summary.md`). Найдите четыре разных утверждения:

| Утверждение | Что действительно сохранено |
|---|---|
| Факты в серии генератора | 15/15 генераций и 8/8 контролей прошли проверки шага 7. |
| Ответы Judge | 20 результатов: 11 валидных, 6 ошибок цитат, 3 усечения; итог `FAIL`. |
| Согласие с человеком | 0 оценок, `DEFERRED_BY_USER`; согласие и Cohen’s kappa не измерены. |
| Приёмка продукта | `INCOMPLETE`; пригодность Judge — `NOT_ESTABLISHED`. |

Среди только валидных ответов Judge совпадение с учебными метками — 11/11, но покрытие всего плана валидными совпавшими ответами — **11/20**. Девять невалидных нельзя удалить из знаменателя или засчитать как обнаруженные дефекты. Оба сравнения A/B усечены: позиционная устойчивость не установлена.

Последний случай Judge выполнен на Ollama 0.40.8, остальные 19 — на 0.40.7. Свод содержит `model_identity_stable=false` и не является однородной серией. Эти разные наборы не дают сравнения качества версий. Двадцать сохранённых результатов также не гарантируют ровно двадцать HTTP-вызовов: прерванные процессы могли отправить запрос без сохранённого ответа.

Человеческую оценку пользователь отложил. Заполненные машиной формы и ваши ответы после просмотра эталонов не превращаются в независимую калибровку. Для будущего независимого оценщика есть пакет (локальный материал: `verification/qa-step-08-2026-10-21/materials/human-review-packet.md`) и пустая форма (локальный материал: `verification/qa-step-08-2026-10-21/materials/human-review-template.json`); процедура описана в [инструкции Judge](qa-judge-quickstart.md). На этом шаге их заполнять не требуется.

## 5. Необязательный новый запуск настоящей модели

Для завершения основной практики достаточно сохранённых данных. Новый запуск расходует время и ресурсы компьютера. Сначала проверьте подготовку по [инструкции LLM](qa-llm-quickstart.md): существующая `.venv`, локальная модель поиска с манифестом, запущенный Ollama и установленная `qwen2.5:14b` ожидаемого digest. Ничего скачивать этой командой не нужно.

Короткий эксперимент ограничен одним сценарием и двумя повторами:

```powershell
$qaNewLlm = 'var/qa/llm/workshop-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_llm.py --case-id QA-S01 --repeats 2 --output-dir $qaNewLlm
$qaNewLlmExit = $LASTEXITCODE
if (Test-Path -LiteralPath "$qaNewLlm/result.json") {
    Get-Content -LiteralPath "$qaNewLlm/result.json" -Raw
}
$qaNewLlmExit
```

Успешно собранный сокращённый эксперимент имеет **`INCOMPLETE`, код 3**, поскольку не покрывает всю матрицу шага 7. Внутри отдельных наблюдений ожидается `evaluation.status=PASS`. При нарушении будет `FAIL`; при ранней ошибке может не появиться `result.json`. Два повтора — два запуска сценария, но возможны исправляющие запросы runtime: считайте реальные `http_attempts`. Дедлайн генерации каждого запуска — 180 с; подготовка поиска и вся серия не имеют единого жёсткого лимита процесса. Не воспринимайте 360 с как гарантированный предел общего времени.

Для второго отдельного запуска повторите этот блок: GUID даст новый каталог. Сначала сравните `run.json`, модель, версии Ollama, параметры и `source-hashes.json` двух запусков. Затем сопоставляйте семантические и порядковые fingerprints одного `case_id/repeat_index`, ошибки и первые ответы. Изменившееся окружение фиксируйте отдельным условием; не объявляйте разницу следствием одной только случайности LLM. Повторение того же входа не добавляет независимых инцидентов в выборку.

Отдельный короткий вызов Judge, если нужен именно новый ответ оценщика:

```powershell
$qaNewJudge = 'var/qa/judge/workshop-' + [guid]::NewGuid().ToString('N')
& .\.venv\Scripts\python.exe -I -B .\qa\tools\run_judge.py --case-id J-002 --output-dir $qaNewJudge
$qaNewJudgeExit = $LASTEXITCODE
if (Test-Path -LiteralPath "$qaNewJudge/result.json") {
    Get-Content -LiteralPath "$qaNewJudge/result.json" -Raw
}
$qaNewJudgeExit
```

Предусмотрен один запрос, до 180 с на кейс, до 512 выходных токенов; автоматического исправления нет. Генератор отчётов повторно не вызывается. Даже правильный ответ не делает одиночный кейс полной проверкой Judge: ожидается `INCOMPLETE`, либо `FAIL` при измеренной ошибке. Дедлайны проверяются между этапами и таймаутом сокета; отдельной принудительной отмены всего процесса нет. Старые и новые результаты не перезаписываются.

## 6. Предложить новый модельный кейс

Пример постановки: «две операции в разных валютах, изменение одной суммы на 0.01; меняются только соответствующий факт и итог этой валюты, остальные валюты и набор источников сохраняются». В отдельной заметке задайте синтетические входы, ожидаемые точные суммы, проверяемые поля, связь с исходной семьёй, критерий отказа и будущую версию набора. Укажите `NOT_EXECUTED`: это пока проект теста.

Текущие CLI `run_llm.py`, `run_judge.py` и `run_retrieval.py` не имеют параметра `--dataset-dir` и используют закреплённые наборы. Простое копирование каталога с новым кейсом не подключит его к запуску. Не меняйте `qa/datasets/*-v1` ради упражнения и не повышайте число покрытых сценариев. Подключение новой версии модельного набора требует отдельной доработки загрузчика, манифеста и проверки независимости данных. Для уже исполняемого упражнения по добавлению нового автотеста используйте [практикум Playwright](qa-workshop-browser.md).

Практика считается разобранной, когда вы можете показать факт и источник, объяснить различие состава и порядка, найти две ошибки Judge и правильно назвать ограничения результата. Это учебная самопроверка; автоматическая квалификация файлов и реальное прохождение оператором учитываются отдельно.
