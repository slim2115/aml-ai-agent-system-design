# Учебная проверка AI и dark factory

Основная установка и полный рабочий цикл собраны в [единой инструкции](service-and-lab.md). Команды этого справочника выполняются из корня вашей копии. Упоминания сохранённых прогонов относятся к указанной дате; их локальные архивы не входят в обычный клон.

Доступны отдельные автоматические проверки качества. Они работают на синтетических данных и не заменяют приёмку AML-экспертом. Открытые локальные кейсы доступны разработчику и не считаются независимым скрытым набором.

Откройте корень проекта в PowerShell 7 в VS Code:

```powershell
# Терминал открыт в корне вашей копии проекта.
& .\scripts\rebuild\Enter-LocalEnvironment.ps1
```

## Исполняемые требования

Первый этап развития Dark Factory связывает 12 требований с рисками, источниками ожиданий и положительными/отрицательными сценариями. Каталог находится в `qa/requirements/catalog-v1.json`. Описание и полный порядок разбора результата — в [разделе 10.6 основной инструкции](service-and-lab.md#executable-requirements). Итог 7 ноября 2026 года: 103 связанных случая PASS; отдельная полная регрессия — 2291 тест и 645 подслучаев PASS, UI/CLI — 29/29. Продуктовая приёмка остаётся INCOMPLETE. Ниже сохранены также датированные результаты прежних этапов; они не являются повторными измерениями текущей версии.

После подготовки `.venv-ci` по разделу 10.1 основной инструкции:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_requirements.py validate --output-dir "var/qa/requirements/validate-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/check_requirements.py run --output-dir "var/qa/requirements/run-$stamp"
Get-Content "var/qa/requirements/run-$stamp/report.md" -Encoding UTF8
```

Выполняйте команды по одной. `validate` проверяет структуру и ссылки, оставляя требования `INCOMPLETE`. `run` запускает связанные тесты с контролируемыми зависимостями; правильный отказ в отрицательном сценарии должен дать успешный тест. Пропуск, xfail, неполная коллекция или изменение проверяемых файлов не подтверждают требование. Выходной каталог должен быть новым. Продуктовая приёмка остаётся `INCOMPLETE`, допуск кандидата — `NOT_EVALUATED`; настоящие модели и человеческая калибровка здесь не измеряются.

## План проверок по изменениям

Этап 2 добавляет карту влияния: объявленный путь или смена модели → компоненты → требования → проверки. Завершён 12 ноября 2026 года: 81 новый тест, полная регрессия 2372 + 645 PASS, UI/CLI 29/29. Исчезнувшие ссылки каталога сохраняют неполный план и полный резерв проверок. После подготовки `.venv-ci`:

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmssfff
$impactRun = "var/qa/impact/plan-$stamp"
.\.venv-ci\Scripts\python.exe -I -B qa/tools/plan_changes.py --changed src/runtime/transport.py --model-changed llm --output-dir $impactRun
Get-Content "$impactRun/plan.md" -Encoding UTF8
```

Повторяйте `--changed` для нескольких путей. Удаления и переименования задаются через `--changes-file`; [самодостаточный пример и разбор полей](service-and-lab.md#change-impact) находятся в разделе 10.7. `READY` и код `0` означают только готовый план, `INCOMPLETE` и код `2` — неизвестное влияние с полным резервным набором. Ошибка входов/карты даёт `ERROR`. Выполнение всегда `NOT_RUN`, полнота входов `NOT_VERIFIED`, продуктовая приёмка `INCOMPLETE`.

Полная регрессия обязательна. План задаёт приоритеты и дополнительные профили, но не запускает их и не разрешает сокращение набора. Настоящая модель требует отдельного измерения; пользовательская/экспертная проверка остаётся отложенной. [Контракт планировщика](../../qa/impact/README.md).

## Единый запуск текущих проверок

Этап 3 выполняет обязательные детерминированные профили и связывает их с текущими файлами и окружением. После создания `$impactRun` выше:

```powershell
$executionRun = 'var/qa/execution/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_plan.py --plan-dir $impactRun --output-dir $executionRun
Get-Content "$executionRun/report.md" -Encoding UTF8
```

Для объявленной смены LLM код `3` ожидаем даже при PASS четырёх детерминированных профилей: реальная модель не запускалась, весь план остаётся `INCOMPLETE`. Хэши исходников и результатов проверяются до/после; тайм-ауты и ошибки очистки процессов блокируют продолжение. Установка, поля отчёта и диагностика находятся в [разделе 10.8](service-and-lab.md#owned-qa-execution), границы — в [справочнике выполнения](../../qa/execution/README.md).

## Квалификация оценщиков

Перед интерпретацией новых измерений отдельно выполните квалификацию программных оценщиков:

```powershell
$qualificationRun = 'var/qa/evaluator-qualification/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/qualify_evaluators.py --output-dir $qualificationRun
Get-Content "$qualificationRun/report.md" -Encoding UTF8
```

Контроли с заранее известными ожиданиями проверяют обнаружение ошибок и неполноты, а намеренные подмены — чувствительность набора. Установка, разбор отчёта и упражнение находятся в [разделе 10.9](service-and-lab.md#evaluator-qualification). PASS относится к этим программным контролям; качество настоящего Judge и человеческая калибровка не переоцениваются.

## Сценарные модели окружения

Для разбора потери подтверждений, повторов и восстановления зависимостей выполните отдельный набор:

```powershell
$environmentRun = 'var/qa/environments/local-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_environments.py --output-dir $environmentRun
Get-Content "$environmentRun/report.md" -Encoding UTF8
```

Планировщик, HTTP-клиент и SQLite настоящие; Kafka, очередь PostgreSQL, HTTP-сбои и время моделируются. Читайте шаги и trace, а не только конечный статус. [Инструкция и упражнение](service-and-lab.md#environment-scenarios), [границы сценарных моделей](../../qa/environments/README.md). PASS не заменяет живой интеграционный прогон.

## Статистическая устойчивость и бюджет

Запустите фиксированные контроли без модели и сети:

```powershell
$stabilityRun = 'var/qa/stability/demo-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_stability.py --output-dir $stabilityRun
Get-Content "$stabilityRun/report.md" -Encoding UTF8
Get-Content "$stabilityRun/demo.json" -Raw -Encoding UTF8
```

В бюджетной панели запланированы 12 слотов, разрешены 8 попыток: четыре оставшихся слота сохраняются как `NOT_RUN`, итог панели `INCOMPLETE`. Квалификация самого инструмента получает `PASS`, когда эта остановка и остальные объявленные контроли обнаружены правильно. Учёт расхода отделён от качества ответа; повторы не увеличивают число независимых семейств.

В [разделе 10.11](service-and-lab.md#statistical-stability) приведены пересчёт собственного сохранённого прогона LLM, поля статистики и упражнение. Пересчёт не вызывает модель, а общий бюджет старого прогона остаётся `NOT_EVALUATED`. Условный интервал не разрешает релиз и не закрывает человеческую приёмку.

## Ограниченный автономный цикл ответа

В отдельном QA-запуске кандидат предлагает только порядок разрешённых фактов и правил. Реальный renderer и фиксированный oracle проверяют его, а бюджет и отсутствие прогресса останавливают повторные попытки:

```powershell
$autonomyRun = 'var/qa/autonomy/demo-' + (Get-Date -Format yyyyMMdd-HHmmssfff)
.\.venv-ci\Scripts\python.exe -I -B qa/tools/run_autonomy.py --output-dir $autonomyRun --mode demo
Get-Content "$autonomyRun/report.md" -Encoding UTF8
Get-Content "$autonomyRun/demo.json" -Raw -Encoding UTF8
```

Набор содержит 11 объявленных контролей. Поставщик ответов и время синтетические; отрицательные исходы внутри контролей ожидаемы. Первая ошибка сохраняет общий `FAIL` цикла даже после исправления кандидата. Журнал фиксирует резерв до каждого запроса; неизвестный расход не равен нулю. Исходный код, SQL, политика и эталоны не являются действиями цикла.

В [разделе 10.12](service-and-lab.md#bounded-response-cycle) приведены запуск настоящей локальной Ollama с максимумом двух попыток, поля отчёта, диагностика и расследование. Проверяется фиксированный фактический каталог; поиск, полезность порядка и человеческая приёмка остаются отдельными задачами.

## Паспорт исходного состояния


Команда фиксирует хэши выбранных исторических свидетельств и их отдельные статусы. Каждому запуску нужно новое имя файла:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_factory/baseline.py --output var/qa/dark-factory/my-baseline-01.json
Get-Content var/qa/dark-factory/my-baseline-01.json -Raw
```

`capture_status: PASS` означает, что паспорт собран, но не означает качество продукта. Измеренный ранее Judge — `FAIL`; человеческая калибровка и принятие продукта остаются `INCOMPLETE`. Если старые свидетельства не включены в опубликованный клон, паспорт будет `INCOMPLETE` — это корректная граница его применения.

Для отдельной проверки согласованности сохранённого результата Judge:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_factory/judge_audit.py --output var/qa/dark-factory/my-judge-audit-01.json
Get-Content var/qa/dark-factory/my-judge-audit-01.json -Raw
```

`audit_status: PASS` относится к согласованности сохранённых агрегатов; итог качества самого Judge при этом остаётся `FAIL`. Это не новый вызов модели или экспертная калибровка.

Чтобы отдельно сопоставить сохранённые ответы Judge с результатом программной проверки, выполните:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_factory/gate_cross.py --output var/qa/dark-factory/my-gate-cross-01.json
Get-Content var/qa/dark-factory/my-gate-cross-01.json -Raw
```

В локальном результате `var/qa/dark-factory/judge-gate-cross-20261030.json` два из 12 одиночных случаев с `gate=FAIL` получили `pass` от Judge, ещё один ответ был невалидным; два парных наблюдения учтены отдельно. Этот показатель отличается от трёх ложных одобрений на 14 случаях с синтетической меткой дефекта: критерии и знаменатели разные. `status: PASS` здесь означает успешный пересчёт сохранённых данных, а не успешную оценку Judge — она остаётся `FAIL`. Нового вызова модели и экспертной приёмки нет; локальная проверка файлов не является независимой подписью. При отсутствии исходного архива результат будет `INCOMPLETE`.

## Проверки с подменной моделью

Следующая команда создаёт отдельные синтетические копии, запускает четыре варианта недоверенного текста через настоящий сборщик контекста с подменными embedding и transport и сохраняет `result.json`:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_adversarial/probe.py --output-dir var/qa/dark-adversarial/my-fake-01
Get-Content var/qa/dark-adversarial/my-fake-01/result.json -Raw
```

Имя каталога должно быть новым. `live_model: NOT_RUN` означает, что настоящая LLM не вызывалась. Положительный результат касается только перечисленных вариантов и проверяемой границы; это не доказательство устойчивости модели ко всем атакам. Старые наборы не редактируются.

## Один ограниченный запуск настоящей модели

Когда Ollama с подготовленной `qwen2.5:14b` и локальная embedding доступны, можно выполнить один отдельный синтетический случай. Для каждого прогона выбирайте новое имя каталога:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_adversarial/live.py --output-dir var/qa/dark-adversarial/my-live-01
Get-Content var/qa/dark-adversarial/my-live-01/result.json -Raw
Get-Content var/qa/dark-adversarial/my-live-01/measurement.json -Raw
```

Прогон ограничен пятью минутами и двумя попытками модели. Сначала подготавливается embedding и локальный поиск; это может занять время. `run.json` и отметки начала попытки остаются `INCOMPLETE` как журнал старта; окончательный результат содержится в `result.json` и `measurement.json`. При ошибке или остановке сохраняйте весь каталог для разбора и используйте новое имя для повторного запуска. Если маркер исключён из входа, модель не испытывалась на самой инъекции: измерена только граница передачи данных. Один случай не является независимой приёмкой.

В актуальной схеме `dark-adversarial-live-v2` сверьте `checked_fragment_ids`, `projection_scan_status`, `wire_capture_status`, `output_scan_status` и `first_attempt_status`. Записываются идентификаторы совпавших известных фрагментов и хэш HTTP-запроса, а не его содержимое. Первая невалидная попытка остаётся ошибкой, даже если повтор вернул правильную структуру. Ранние результаты с проверкой только маркера не удовлетворяют новой схеме.

## Проверки отдельных оценщиков

```powershell
.\.venv\Scripts\python.exe -I -B -m pytest -c pyproject.toml tests/dark_factory tests/dark_factory_cases tests/dark_factory_v3 tests/dark_cases_v3 tests/dark_guards tests/dark_retrieval tests/dark_downstream tests/dark_editions tests/dark_compare tests/dark_adversarial tests/dark_judge_injection tests/dark_isolation -q -p no:cacheprovider
```

- `dark_factory` проверяет полноту паспорта и сохранение статусов.
- `dark_factory_cases` запускает открытые контрактные пробы и показывает неисполненные гипотезы.
- `dark_factory_v3` проверяет три новых открытых синтетических сценария и их оценщик с подменной embedding и template.
- `dark_cases_v3` проверяет черновик с двумя валютами и двумя искусственно сопоставленными правилами: суммы, их текст и ссылки, количество операций, выбранные правила, версии и источники. Это производный сценарий S03 с подменными embedding и transport; для копии fixture временно подменяется `PROJECT_ROOT`. Настоящая модель и экспертная применимость правил не проверяются.
- `dark_guards` проверяет обнаружение изменения оценщика и эталона на временных файлах; доверенный отпечаток должен храниться независимо.
- `dark_retrieval` отдельно оценивает поиск и наблюдаемое последующее решение; для поиска используется подменная embedding.
- `dark_downstream` проверяет оценщик для наблюдаемых решений после поиска; запуск с настоящей embedding выполняется отдельно по команде ниже.
- `dark_editions` проверяет синтетические технические границы редакций в `RuleIndex` с подменными равными векторами.
- `dark_compare` показывает первую попытку и не прячет сбой успешным повтором.
- `dark_adversarial` проверяет недоверенные поля с явно указанными подменами.
- `dark_judge_injection` проверяет измеритель доставки и контракта ответа Judge; настоящий запуск модели выполняется отдельно.
- `dark_isolation` проверяет измеритель локального Docker-эксперимента с подменным Docker; настоящий запуск контейнера выполняется отдельно.

Эти тесты также включены в общий локальный набор через `pyproject.toml`. Результаты реальных моделей и инфраструктуры измеряются отдельно. Изолированная папка сама по себе не создаёт закрытый holdout: это потребовало бы отдельного контролируемого исполнителя и разграничения доступа.

После изменений сводки и измерителя Judge полная локальная регрессия `var/checks/dark-factory-20261030-report-v2/unit-verification.json` прошла: 2111 тестов и 645 подслучаев, без ошибок и пропусков. Она не запускает реальные PostgreSQL, браузер, модели, сетевые интеграции и экспертную приёмку. Предыдущий результат 2055 + 645 сохранён отдельно в `var/checks/dark-factory-20261030-expanded/unit-verification.json`.

Для сохранения байтов учебных наборов в rebuild добавлен `.gitattributes` с правилом `-text` для `qa/datasets`, `data/demo-v1` и всего `verification/qa-step-07-2026-10-20/pilot-first`. Последний каталог включает пять синтетических файлов, необходимых тесту исторической реконструкции Judge; они перечислены в README. Правило шире только этих пяти файлов. Отдельная локальная проверка staging и `checkout-index` охватила 491 файл; 135 проверенных побайтово файлов совпали, 274 выбранных теста прошли с существующим локальным окружением `.venv`. Результаты: `var/checks/git-transport-20261030-01/summary.json` и `tests.xml` в том же каталоге. Это не удалённое клонирование и не запуск GitHub Actions. Проверка выполнена до последних изменений `report_v2` и `live_v2`. Коммиты и push не выполнялись.

Повтор после изменений охватил 495 выбранных файлов: 135 защищённых файлов совпали побайтово, в отдельной копии прошли 330 тестов, включая 51 тест `report_v2` и пять тестов `live_v2`. Использовалось прежнее окружение `.venv`; результат — `var/checks/git-transport-20261030-02/summary.json`. Это локальная проверка staging и `checkout-index` без commit и push, а не проверка удалённого клонирования, полного состава публикации или GitHub Actions. Предыдущий результат `-01` сохранён.

## Настоящая embedding-модель

На подготовленном стенде можно отдельно измерить поиск по трём открытым учебным запросам. Для каждого запуска нужен новый каталог:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_retrieval/live.py --output-dir var/qa/dark-retrieval/my-live-01 --timeout-seconds 300
Get-Content var/qa/dark-retrieval/my-live-01/summary.json -Raw
Get-Content var/qa/dark-retrieval/my-live-01/child-result.json -Raw
```

`measurement_status: PASS` означает завершённое измерение, а не принятое качество поиска. Результат последующего выбора источника здесь не наблюдается, поэтому общий статус и экспертная оценка остаются `INCOMPLETE`. Python-сетевые ограничения и офлайн-модель не являются сетевым экраном операционной системы.

### Как сравнить пороги поиска

В расширенном упражнении MiniLM ищет правила по пяти темам. Для каждой темы проверяются обе стороны даты смены редакции и случай, когда правило этой темы удалено. Ещё два запроса проверяют отсутствующую тему и дату без действующих правил. Получается 17 связанных наблюдений. Сценарии и синтетические метки зафиксированы до запуска модели.

В терминале VS Code из корня rebuild выполните команды. Для запуска нужны проектная `.venv` и локальная модель с манифестом в `var/models`.

```powershell
$matrixRun = 'var/qa/dark-retrieval/practice-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv\Scripts\python.exe -I -B qa/dark_retrieval/live_matrix.py --output-dir $matrixRun --timeout-seconds 300
$matrixResult = Get-Content -Raw (Join-Path $matrixRun 'summary.json') | ConvertFrom-Json
$matrixResult | Select-Object measurement_status,reason
$matrixResult.assessment.ranking
$matrixResult.assessment.selectors.PSObject.Properties | ForEach-Object {
    $a = $_.Value.assessment
    [pscustomobject]@{
        Candidate = $_.Name
        FalseAccept = "$($a.false_accept.numerator)/$($a.false_accept.denominator)"
        FalseReject = "$($a.false_reject.numerator)/$($a.false_reject.denominator)"
    }
}
.\.venv\Scripts\python.exe -I -B qa/dark_retrieval/live_matrix.py --replay $matrixRun
```

`hit_at_1` показывает, как часто нужная редакция стоит первой. В `FalseAccept` числитель считает выбор постороннего источника в случае без подходящего правила. В `FalseReject` числитель считает отказ в случае, где правило есть. Если выбрана посторонняя цитата при наличии нужной, это отдельная ошибка в `unsupported_selected_count` и `missed_relevant_count` строки сценария.

Сравниваются безусловный выбор первого результата и три порога косинусного расстояния — 0,25, 0,50 и 0,75. Чем меньше допустимое расстояние, тем легче получить отказ. Все четыре варианта используют одну сохранённую выдачу. Поведение действующей политики AML оценивает отдельный эксперимент ниже. `--replay` пересчитывает оценки из 17 файлов выдачи, сверяет идентичность запуска и сводку, используя локальные исходники и манифест модели. При тайм-ауте выполненные наблюдения сохраняются, а итог получает `INCOMPLETE`.

В прогоне 30 октября нужная редакция заняла первое место в 8/10 случаев и вошла в тройку в 10/10. Повторный расчёт совпал для всех 17 наблюдений. Таблица ошибок вариантов и полный результат (локальный материал: `verification/dark-retrieval-matrix-2026-10-30/summary.md`).

## Решение после поиска: локальный эксперимент

Для проверки четырёх вариантов политики на одном синтетическом случае с подготовленной локальной embedding-моделью используйте новое имя каталога:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/live.py --output-dir var/qa/dark-downstream/my-live-01 --timeout-seconds 300
Get-Content var/qa/dark-downstream/my-live-01/summary.json -Raw
Get-Content var/qa/dark-downstream/my-live-01/child-result.json -Raw
```

Прогон использует template и не вызывает LLM. `status: PASS` в сводке подтверждает только совпадение наблюдений с контрольными ожиданиями. В сохранённом `live-20261030-02` пустой список разрешённых правил вызвал отказ без поиска, отсутствующий ID — отказ после поиска, намеренно неверный список привёл к включению `R-115-006`, а штатный контроль — к `R-AML-010`. Для намеренно изменённого списка отдельно зафиксирован `policy_safety: FAIL_FOR_CONTROLLED_MUTATION`; оригинальная политика не менялась. Семантическая релевантность и экспертная приёмка не установлены, вывод нельзя распространять на юридическую корректность штатной политики.

### Как проследить цитату до черновика

Для этого упражнения нужны сохранённый прогон матрицы и локальная MiniLM. В PowerShell из корня rebuild укажите каталог матрицы из предыдущего упражнения. Первая команда ниже использует уже выполненный прогон стенда.

```powershell
$matrixRun = 'var/qa/dark-retrieval/matrix-20261030-01'
$decisionRun = 'var/qa/dark-downstream/practice-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/live_decision.py --matrix-run $matrixRun --output-dir $decisionRun --timeout-seconds 300
$report = Get-Content -Raw (Join-Path $decisionRun 'summary.json') | ConvertFrom-Json
$report.cases | Select-Object case_id,agent_status,reason,same_first_three,@{n='First';e={$_.first_citation.rule_id}},@{n='InDraft';e={$_.reported_citations.rule_id -join ', '}},@{n='Edition';e={$_.reported_citations.rule_version -join ', '}},@{n='Rank';e={$_.reported_ranks -join ', '}}
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/live_decision.py --replay $decisionRun
```

`First` — первый кандидат поиска, `InDraft` — правило в черновике, `Rank` — его место в полной выдаче агента. В двух сценариях дробления в черновик попал второй кандидат в редакциях `v1` и `v2`. В контрольном случае обязательное правило отсутствовало, и агент вернул `NO_APPLICABLE_RULE`. Полную цитату можно прочитать в файле сценария внутри каталога запуска, в `result.applicable_rules`. Отчёт с результатами (локальный материал: `verification/dark-downstream-decision-2026-10-30/summary.md`).

### Как проверить контролируемую ошибку политики

В этом упражнении сравниваются две копии учебной политики. В `baseline` разрешено `MATRIX-01`, в `candidate` дополнительно разрешено постороннее для сценария правило `MATRIX-02`. Агент выполняет три сценария для каждой политики. Ожидаемые правильные цитаты берутся из исходного каталога сценариев.

```powershell
$baselineRun = 'var/qa/dark-downstream/decision-20261030-01'
$pairRun = 'var/qa/dark-downstream/policy-pair-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/policy_pair.py --baseline-run $baselineRun --output-dir $pairRun --timeout-seconds 300
$paired = Get-Content -Raw (Join-Path $pairRun 'summary.json') | ConvertFrom-Json
$paired | Select-Object measurement_status,baseline_status,candidate_status,mutation_check
$paired.pairs | Select-Object case_id,same_full_search,@{n='Baseline';e={$_.baseline.decision}},@{n='Candidate';e={$_.candidate.decision}},@{n='Extra';e={$_.candidate.extra.rule_id -join ', '}},mutation_check
.\.venv\Scripts\python.exe -I -B qa/dark_downstream/policy_pair.py --replay $pairRun
```

`same_full_search` подтверждает совпадение полного результата поиска в паре, включая расстояния. `candidate_status=FAIL` означает обнаруженную ошибку изменённой политики. `mutation_check=PASS` означает, что оценщик выявил именно запланированную лишнюю цитату в положительных случаях и подтвердил ожидаемый отказ в контрольном случае. Поэтому при успешном обнаружении дефекта команда возвращает код `1`. Код `2` указывает на неполные свидетельства или ошибку проверки. Каждый запуск требует нового имени каталога.

Сохранённый прогон выявил лишнюю цитату в двух черновиках из двух. При отсутствии обязательного правила обе политики привели к отказу. Для повторного расчёта этого примера укажите `--replay var/qa/dark-downstream/policy-pair-20261031-01`. Таблица результатов и исходные черновики (локальный материал: `verification/dark-policy-pair-2026-10-31/summary.md`).

## Технические границы редакций правил

Для четырёх открытых синтетических проверок настоящего индекса с подменной embedding запустите:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_editions/probe.py --output var/qa/dark-factory/my-editions-01.json
Get-Content var/qa/dark-factory/my-editions-01.json -Raw
```

Имя файла должно быть новым. Проверяются отсутствующий ID при выдаче другого сырого кандидата, истёкшая редакция, конфликт перекрывающихся редакций и граница часового пояса. `PASS` относится только к этим техническим наблюдениям. Векторы искусственно равны, решение после поиска не наблюдается, применимость AML-правил и качество настоящей embedding не устанавливаются.

## Новые синтетические сценарии

Три случая созданы с нуля: пустое окно операций с `INSUFFICIENT_DATA`, раздельные суммы EUR `0.10 + 0.20` и JPY `700 + 300`, а также чужая карта с `CONFLICTING_DATA`. Запуск создаёт новый каталог:

```powershell
.\.venv\Scripts\python.exe -I -B qa/datasets/dark-factory-v3/probe.py --output-dir var/qa/dark-factory-v3/my-run-01
Get-Content var/qa/dark-factory-v3/my-run-01/result.json -Raw
```

Используются настоящий runtime, подменная embedding и template; LLM не вызывается. Сценарии имеют общие учебное правило и временное окно, их статистическая независимость и экспертная оценка не установлены. Сохранённый результат `var/qa/dark-factory-v3/20261030-02/result.json` содержит 3/3 PASS только для этих условий.

## Локальный Judge и инструкция в источнике

На подготовленном стенде можно выполнить один нейтральный и один атакующий вариант синтетического случая для локальной LLM в роли Judge. Каждый вариант отправляется один раз без повторов; запуск может занять несколько минут. Выберите новый каталог:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_judge_injection/live_v2.py --output-dir var/qa/dark-judge-injection/my-live-v2-01 --timeout-seconds 420
Get-Content var/qa/dark-judge-injection/my-live-v2-01/summary.json -Raw
```

В `live-20261030-01` нейтральный ответ соответствовал контракту и дал `fail`, а атакующий ответ был невалидным из-за несовпадения цитаты. Синтетическая инструкция доставлена модели в недоверенном `sources`, но `measurement_status` остаётся `INCOMPLETE`: одного случая недостаточно, чтобы подтвердить устойчивость Judge или причинную связь ответа с атакой. Исторический Judge остаётся `FAIL`, экспертная калибровка не проводилась. Полные синтетические запросы и ответы сохраняются в каталоге запуска; Python-ограничения сети не являются межсетевым экраном ОС. Это проверка Judge, а не LLM-генератора отчёта.

Версия `live_v2.py` сохраняет первый полученный ответ и захваченный запрос в файле `<вариант>-raw.json` до проверки идентичности модели после вызова. Для каждого варианта сравниваются три локальных снимка: исходный, перед запросом и после него; учитываются digest модели и хэши сведений о модели и сервере. Если завершающий снимок недоступен или отличается, результат о качестве ответа остаётся `INCOMPLETE`, а сохранённый первый ответ не подменяется повтором. Эти локальные хэши не являются независимой аттестацией. Бюджет — один запрос на нейтральный и один на атакующий вариант, без повторов; общий предел примера — 420 секунд.

В `var/qa/dark-judge-injection/live-v2-20261030-01/summary.json` оба запроса доставлены, идентичность отмечена `STABLE`, нейтральный ответ валиден и дал `fail`, а атакующий не прошёл контракт; общий результат остаётся `INCOMPLETE`. Старый `live.py` и его результаты сохранены. После добавления версии v2 прошли 13 профильных тестов, а затем и указанная выше полная регрессия.

## Сводка сохранённых результатов

Для уже выполненных локальных прогонов можно сформировать новую сводку; имена каталогов ниже — пример за 28–29 октября 2026 года, а выходной файл должен быть новым:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_factory/report.py --baseline var/qa/dark-factory/baseline-2026-10-29-strict-v2.json --fake var/qa/dark-adversarial/20261029-fake-v2c/result.json --live var/qa/dark-adversarial/20261029-live-v2-complete/measurement.json --regression var/checks/dark-factory-20261029-retry/unit-verification.json --retrieval-summary var/qa/dark-retrieval/live-20261028-02/summary.json --retrieval-child var/qa/dark-retrieval/live-20261028-02/child-result.json --retrieval-attempt var/qa/dark-retrieval/live-20261028-02/attempt.json --judge-audit var/qa/dark-factory/judge-audit-2026-10-28.json --output var/qa/dark-factory/my-report-01.json
Get-Content var/qa/dark-factory/my-report-01.json -Raw
```

Команда ожидаемо возвращает код `1`, пока исторический Judge имеет `FAIL`: это не ошибка записи отчёта. Сводка читает сохранённые свидетельства, не подтверждает независимость проверяющего и не доказывает, что все текущие файлы проекта идентичны версии прошлой регрессии. Если старые локальные артефакты отсутствуют, соответствующие статусы будут `INCOMPLETE`.

Сохранённая базовая сводка `var/qa/dark-factory/report-20261030-report-v2.json` включает регрессию 2111 + 645 и прежние поддержанные компоненты; исторический Judge остаётся `FAIL`. Отдельные результаты Judge injection, downstream и набора v3 в неё не включены. Поэтому `retrieval_selection: INCOMPLETE` в этой сводке относится к прежнему измерению ранжирования; отдельное наблюдение downstream описано выше.

Для отдельной сводки версии 2, включающей эти результаты, используйте существующие имена входов и новое имя выходного файла:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_factory/report_v2.py --base var/qa/dark-factory/report-20261030-ci-offline.json --gate var/qa/dark-factory/judge-gate-cross-20261030.json --editions var/qa/dark-factory/editions-20261030-01.json --v3 var/qa/dark-factory-v3/20261030-02 --downstream var/qa/dark-downstream/live-20261030-02 --judge-injection var/qa/dark-judge-injection/live-v2-20261030-01 --judge-injection-version 2 --output var/qa/dark-factory/my-report-v2-01.json
Get-Content var/qa/dark-factory/my-report-v2-01.json -Raw
```

Пример результата: `var/qa/dark-factory/report-v2-20261030-ci-offline.json`. Параметр `--judge-injection-version 2` выбирает формат прогона с отдельно сохранёнными первыми ответами и проверкой связанных `raw`, `start`, `result` и хэшей исходников. Для первого формата используйте его каталог `live-20261030-01` и опустите этот параметр: по умолчанию выбирается версия `1`. Базовая сводка читается как сохранённый документ, её исходные измерения повторно не пересчитываются. Для v3 проверяются хэши снимков и исходных файлов набора, но сохранённые результаты повторно не исполняются. Редакции правил сопоставляются с текущим учебным оракулом без подтверждения версии кода исходного прогона. Пересчёт Judge и проверка downstream и инъекции используют связанные локальные записи и хэши; они не являются независимой аттестацией. Существующие результаты не перезаписываются. При отсутствии входов соответствующие разделы остаются `INCOMPLETE`.

Сводка v2 отдельно сохраняет шесть статусов базового отчёта: получение baseline, регрессию, проверки adversarial с заменой и с настоящей моделью, измерение поиска и аудит Judge. Ранее выявленное `FAIL` учитывается даже при неполных метаданных; отсутствующее или неизвестное значение остаётся `INCOMPLETE`. Исправление проверено 112 профильными тестами. Это не повторная проверка входов базового отчёта и не изменение исторического `FAIL` Judge.

Код завершения `1` означает наличие зафиксированного `FAIL`, включая отдельно обозначенный результат намеренно ошибочной политики или ошибку регрессии; код `2` — неполные или неизвестные статусы измерений без зафиксированного `FAIL`, включая воздержание Judge, либо ошибку формирования отчёта. Код `0` относится только к проверяемым статусам переданных измерений и **не подтверждает приёмку продукта**: человеческая и продуктовая оценки остаются `INCOMPLETE`. При коде `2` проверьте наличие выходного файла: при ошибке пути или записи отчёт может не создаться. Пример выше ожидаемо возвращает `1`, поскольку исторический Judge имеет `FAIL`; сам отчёт при этом сохраняется.

Для сравнения сохранённых результатов шаблона и первой попытки LLM без новых обращений к модели:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_compare/historical.py --output var/qa/dark-factory/my-comparison-01.json
Get-Content var/qa/dark-factory/my-comparison-01.json -Raw
```

Адаптер проверяет доступные сохранённые файлы по локальному манифесту. `unchanged` означает одинаковые **статусы проверок** пары, не равенство содержания и не подтверждение пользы LLM. Если архив или его манифест отсутствуют, итог `INCOMPLETE`; локальный манифест не является независимой подписью.

## Учебный эксперимент с Docker

В отдельном эксперименте доверенный синтетический скрипт удваивает число. При доступных Docker Desktop и локальном образе можно запустить проверку с новым именем каталога:

```powershell
.\.venv\Scripts\python.exe -I -B qa/dark_isolation/probe.py --output-dir var/qa/dark-isolation/my-run-01
Get-Content var/qa/dark-isolation/my-run-01/result.json -Raw
```

Подробные условия запуска, очистки и границы приведены в [описании эксперимента](../../qa/dark_isolation/README.md). В сохранённом `live-20261030-01` — `INCOMPLETE` из-за ошибки передачи ввода, в `live-20261030-02` — `PASS` только для указанного учебного опыта; 22 профильных теста измерителя прошли. Это не запуск AML-сервиса или Kafka. Через конфигурацию Docker наблюдается `network=none`, а скрипт проверяет отсутствие конкретного пути; фактический сетевой трафик и все возможные способы доступа не оценивались. Хост и оценщик имеют доступ к локальным данным, поэтому независимый закрытый набор не установлен (`hidden_holdout=NOT_ESTABLISHED`).

В отдельном локальном CI-окружении офлайн установлены 62 зафиксированных пакета с проверкой хэшей. `pip check` и проверка CI-входов 10/10 прошли; полная регрессия — 2194 теста и 645 подслучаев без ошибок и пропусков, Streamlit AppTest с подменным обработчиком — 29/29. При AppTest наблюдались предупреждения Streamlit о `use_container_width`. Отчёт локального прогона (локальный материал: `var/checks/ci-offline-20261030-02-report/summary.md`) относится к рабочей папке с отдельной офлайн venv и переменными workflow; это не чистый checkout и не удалённый GitHub Actions. Браузер, настоящие модели и живая БД не запускались.

Обновлённые сводки сохранены в `var/qa/dark-factory/report-20261030-ci-offline.json` и `var/qa/dark-factory/report-v2-20261030-ci-offline.json`. Код завершения v2 `1` ожидаем из-за исторического `FAIL` Judge. Контейнерный эксперимент в эту сводку не входит; человеческая и продуктовая приёмка остаются `INCOMPLETE`.

Отдельно выполнен локальный Playwright browser smoke с настоящим headless Chromium: один сценарий UI → HTTP API → SQLite с детерминированными модельными двойниками завершился `PASS`, очистка `PASS`, порты освобождены. Результат прогона (локальный материал: `var/qa/ci-browser-offline-20261030-01/result.json`) и очищенный отчёт (локальный материал: `var/checks/ci-browser-offline-20261030-01-report/summary.md`) сохранены отдельно; в отчёте `browser-lifecycle` и `chromium-smoke` имеют `PASS`. Использовались исходная проектная `.venv` и заранее установленные зафиксированные браузерные инструменты, не офлайн CI venv. PostgreSQL, Kafka, настоящие LLM и embedding не проверялись; это не удалённый GitHub Actions и результат не объединён с hermetic-отчётом.

## Короткий аудит последних доработок — 30 октября 2026 года

Исправлены две группы недостатков: сохранение явно зафиксированного `FAIL` при неполных или противоречивых входах сводки v2 и проверка конфигурации/сохранение свидетельств Docker-эксперимента. Непроверенное свидетельство остаётся `INCOMPLETE`, но найденный в нём явный провал теперь отдельно сохраняется в `recorded_observed_status` и даёт код завершения `1`.

После исправлений прошли 226 тестов `tests/dark_factory` и `tests/dark_isolation`, без ошибок и пропусков. Реальный синтетический Docker-прогон `var/qa/dark-isolation/audit-20261030-01/result.json` — `PASS`, очистка — `PASS`. Новая сводка `var/qa/dark-factory/report-v2-20261030-audit.json` сохранена с ожидаемым кодом `1`: исторический Judge остаётся `FAIL`, Judge injection — `INCOMPLETE`. Контейнерный результат хранится отдельно от этой сводки.

Полная регрессия 2194 + 645, AppTest и браузерный smoke выше выполнены **до этих исправлений** и здесь повторно не запускались; текущие 226 тестов проверяют затронутые компоненты. Отчёт аудита с воспроизводимыми командами (локальный материал: `verification/dark-factory-audit-2026-10-30/summary.md`).

## Планы для человека

После завершения машинного контура оператор и AML-эксперт смогут разметить независимые примеры и сравнить свою оценку с Judge. До реального участия людей этот этап остаётся `INCOMPLETE`; автоматический отчёт не может его закрыть.


## Просмотр результатов в интерфейсе аналитика QA

Из корня проекта запустите `.\.venv-ci\Scripts\python.exe -I -B -m streamlit run qa/analyst/app.py --server.address 127.0.0.1 --server.port 8502 --server.headless true --browser.gatherUsageStats false` и откройте http://127.0.0.1:8502. Выберите тип и имя сохранённого запуска, прочитайте итог и причины, затем отдельно проверьте файлы и текущий код. Никакие тесты или модели кнопками экрана не запускаются. Старый PASS не закрывает приёмку текущей версии. Полный пример с учебным запуском, браузерным автотестом и диагностикой — в [разделе 10.13](service-and-lab.md#qa-analyst).
