# Как опубликовать дополнение этапа 5 в main

**Актуально на 17 ноября 2026 года.** Основная версия сервиса и исходники учебного стенда уже опубликованы. База сравнения — ecf3817daa1af95b2c8cbe326572786f99e72d54. Следующая отправка состоит из **двух коммитов и двух push**: проверки сценариев окружения, затем инструкции. Commit и push выполняет владелец после завершения локальных проверок текущей версии.

План первой публикации из четырёх коммитов уже выполнен. Повторный коммит runtime, добавление пяти входов Judge и удаление tests/pytest.ini не требуются.

## Состав обновления

| Коммит | Что обновляем | Источник путей |
| --- | --- | --- |
| 1. QA и подготовка публикации | Проверка trace и инвариантов сценариев окружения, её тесты, базовый SHA помощника копирования | Изменившиеся файлы из [03-qa.txt](publication/03-qa.txt) |
| 2. Инструкции | Порядок отправки, раздел 11 руководства, перечни добавлений/замен | Изменившиеся файлы из [04-docs.txt](publication/04-docs.txt) |

Точный состав: [что добавить и заменить](publication/what-to-update.md), [добавления](publication/added-files.txt), [замены](publication/updated-files.txt). Полные пять списков содержат **633 файла**; они описывают всю поставку, а не размер очередного diff. С сохранённым reports/iteration_log.md итоговое Git-дерево содержит **634 файла**. Новых файлов и удалений в этом дополнении нет.

Локально остаются var/, рабочие PostgreSQL/SQLite, сообщения Kafka и Docker volumes, DBeaver workspace, модели/индексы, окружения Python, секреты, .env, логи и результаты занятий. Из verification/ в Git уже входят только пять явно перечисленных синтетических входов Judge; новые результаты проверок не публикуются. Из .rebuild/ публикуются только origin.json и release-version.json. Личный файл портфолио и незакоммиченные изменения исходного checkout автоматически не включаются.

## 1. Подготовить терминал

Используйте PowerShell 7.3+ в VS Code. Выполняйте по одному блоку, останавливайтесь при первой ошибке. Дождитесь завершения доработок и проверки текущего rebuild перед копированием; во время переноса источник не редактируйте.

```powershell
if ($PSVersionTable.PSVersion -lt [version]'7.3') { throw 'Нужен PowerShell 7.3 или новее' }
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$src = 'D:\work\aml-ai-agent-system-rebuild'
$pub = 'D:\work\publication-stage5\aml-ai-agent-system-publish'
$base = 'ecf3817daa1af95b2c8cbe326572786f99e72d54'
$python = Join-Path $src '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Не найден Python стенда' }
& $python -I -B "$src\scripts\release\publication_files.py" check
git --version
git config --get user.name
git config --get user.email
```

check проверяет допустимость полных перечней и наличие файлов. user.name и user.email станут автором коммитов; при отсутствии настройте свои значения до продолжения. Выбран новый каталог внутри publication-stage5; существующий клон и учебный стенд сохраняются.

## 2. Создать чистый клон и скопировать поставку

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

Помощник проверяет имя каталога, origin, ветку main, чистоту клона и оба базовых SHA, затем копирует только согласованные пути и сверяет байты. Он не удаляет файлы и не создаёт коммиты. Если main продвинулся после 17 ноября, сначала сопоставьте новую базу с rebuild. После частичного копирования сохраните каталог для разбора; не повторяйте copy-to поверх грязного клона.

## 3. Проверить полноту переноса и состав diff

```powershell
$groups = @('01-git', '02-service', '03-qa', '03-judge-fixtures', '04-docs')
$paths = @($groups | ForEach-Object { Get-Content -LiteralPath (Join-Path $lists "$_.txt") })
if ($paths.Count -ne 633 -or @($paths | Sort-Object -Unique).Count -ne $paths.Count) { throw 'Неверный состав полных перечней' }
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
if ($added.Count -ne 0) { throw 'Это дополнение не предусматривает новых файлов' }
$actualChanges = @(git diff --name-only | Sort-Object)
if (@(Compare-Object @($updated | Sort-Object) $actualChanges).Count -ne 0) { throw 'Diff отличается от перечня замен' }
if (@(git diff --name-only --diff-filter=D).Count -ne 0) { throw 'Удаления не предусмотрены' }
if (@(git ls-files --others --exclude-standard).Count -ne 0) { throw 'Обнаружены незапланированные новые файлы' }
if (@(git diff --cached --name-only).Count -ne 0) { throw 'Перед первым блоком индекс должен быть пуст' }
$qaPaths = @(Get-Content -LiteralPath "$lists\03-qa.txt")
$docPaths = @(Get-Content -LiteralPath "$lists\04-docs.txt")
$expectedQa = @($updated | Where-Object { $_ -in $qaPaths } | Sort-Object)
$expectedDocs = @($updated | Where-Object { $_ -in $docPaths } | Sort-Object)
if ($expectedQa.Count -eq 0 -or $expectedDocs.Count -eq 0 -or $expectedQa.Count + $expectedDocs.Count -ne $updated.Count) { throw 'Замены не разделены между QA и документацией' }
git diff --stat
Write-Output "Проверено файлов: $($paths.Count); замен: $($updated.Count)"
```

Пять входов Judge входят в проверку хэшей, но их diff должен отсутствовать. Перечни читаются из подготовленного клона, сохраняя единый состав отправки. Перед коммитом просмотрите staged diff; ожидаются только M. Команды git add ., git add -A, git rm и force push для этого дополнения не нужны.

## 4. Коммит 1 и push 1: проверки сценариев окружения

```powershell
git --literal-pathspecs add --pathspec-from-file="$lists\03-qa.txt"
$stagedQa = @(git diff --cached --name-only | Sort-Object)
if (@(Compare-Object $expectedQa $stagedQa).Count -ne 0) { throw 'В QA-коммите неожиданные пути' }
if (@(git diff --cached --name-only --diff-filter=AD).Count -ne 0) { throw 'Новые файлы и удаления не предусмотрены' }
git diff --cached --name-status
git diff --cached --stat
git diff --cached
git commit -m "test(qa): усилить проверку trace сценариев окружения" -m "Проверка порядка операций, подтверждений и инвариантов, регрессионные тесты; помощник копирования привязан к опубликованной базе main."
git show --stat --oneline HEAD
git push origin main
```

После push откройте [GitHub Actions](https://github.com/slim2115/aml-ai-agent-system-design/actions) и дождитесь hermetic-windows и browser-smoke-windows для нового SHA. При падении сохраните ссылку на запуск и разберите причину до следующего push. Локальная проверка не заменяет удалённый CI. Deployment автоматически не выполняется.

## 5. Коммит 2 и push 2: инструкции и перечни

```powershell
if (@(git diff --cached --name-only).Count -ne 0) { throw 'Перед следующим блоком индекс должен быть пуст' }
git --literal-pathspecs add --pathspec-from-file="$lists\04-docs.txt"
$stagedDocs = @(git diff --cached --name-only | Sort-Object)
if (@(Compare-Object $expectedDocs $stagedDocs).Count -ne 0) { throw 'В коммите документации неожиданные пути' }
if (@(git diff --cached --name-only --diff-filter=AD).Count -ne 0) { throw 'Новые файлы и удаления не предусмотрены' }
git diff --cached --name-status
git diff --cached --stat
git commit -m "docs: актуализировать публикацию дополнения Dark Factory" -m "Инкрементальный порядок двух коммитов и двух push, новая база main, раздел 11 эксплуатации и точные перечни изменяемых файлов. Личное состояние учебного стенда остаётся локально."
git show --stat --oneline HEAD
git push origin main
```

## 6. Итоговая проверка

```powershell
git fetch origin main
$localHead = git rev-parse HEAD
$remoteHead = git rev-parse origin/main
if ($localHead -ne $remoteHead) { throw 'Локальная и удалённая вершины различаются' }
git status --short --branch
git log -2 --oneline
git diff --name-status $base HEAD
$expectedTree = @($paths + 'reports/iteration_log.md' | Sort-Object)
$actualTree = @(git ls-tree -r --name-only HEAD | Sort-Object)
if (@(Compare-Object $expectedTree $actualTree).Count -ne 0) { throw 'Git-дерево не совпадает с согласованной поставкой' }
if (@(git status --porcelain).Count -ne 0) { throw 'Остались незакоммиченные файлы' }
Write-Output "Опубликовано: $localHead; файлов: $($actualTree.Count)"
```

Ожидаются два новых коммита, 634 файла в конечном дереве, чистая рабочая копия и совпадающие HEAD/origin/main. Проверьте README, ссылки на [руководство эксплуатации](operator/service-and-lab.md) и Actions для последнего SHA. Результаты удалённого CI фиксируются после отправки владельцем.

## Как продолжить после остановки

- До commit: изучите git status --short и git diff --cached; выясните причину, сохраняя предыдущую попытку.
- Commit создан, push не прошёл: после устранения причины повторите push. Такой же коммит повторно создавать не нужно. При расхождении историй сравните их перед продолжением.
- Терминал закрыт: восстановите настройки и переменные раздела 1, перейдите в $pub, задайте $lists = Join-Path $pub 'docs\publication'. Из раздела 3 прочитайте переменные $groups, $paths, $updated, $qaPaths, $docPaths, $expectedQa, $expectedDocs; проверки первоначального diff после частичного коммита повторять не надо. По git log -2 --oneline и staged diff определите следующий блок.
- Если защита main отклоняет push, нужен согласованный владельцем процесс публикации. Не обходите защиту force push; созданные локальные коммиты сохраняются.
