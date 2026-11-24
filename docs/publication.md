# Как опубликовать этап 9 Dark Factory в main

**Актуально на 23 ноября 2026 года.** Этапы 6–8 уже опубликованы. Состав обновления сверен с сохранённым деревом main для коммита `eb84b1651e32a5a88871b3ba3cdc92295fafd712`. После локальных проверок владелец отправляет два коммита. Первый содержит учебные расследования и изменения QA, второй — инструкции.

Первичная публикация и этапы 5–8 уже выполнены. Повторно переносить неизменённые компоненты не требуется.

## Состав обновления

| Коммит | Что обновляем | Источник путей |
| --- | --- | --- |
| 1. QA и подготовка публикации (20 файлов) | Практикум из шести расследований, генератор свидетельств, проверка целостности и версии, формы UI, CLI, тесты, включая браузерный сценарий оператора, карта влияния и проверка базы публикации | Новые и изменившиеся файлы из [03-qa.txt](publication/03-qa.txt) |
| 2. Инструкции (8 файлов) | README сервиса, эксплуатация этапа 9, порядок отправки и перечни добавлений и замен | Новые и изменившиеся файлы из [04-docs.txt](publication/04-docs.txt) |

В обновлении **14 новых и 14 изменённых файлов**. Все пять списков вместе содержат **681 файл**. С сохранённым `reports/iteration_log.md` итоговое дерево содержит **682 файла**. Удалений и изменений пяти входов Judge нет. [Состав по коммитам](publication/what-to-update.md), [добавления](publication/added-files.txt), [замены](publication/updated-files.txt).

Локально остаются var/, рабочие PostgreSQL/SQLite, сообщения Kafka и Docker volumes, DBeaver workspace, модели/индексы, окружения Python, секреты, .env, логи и результаты занятий. Из verification/ в Git уже входят только пять явно перечисленных синтетических входов Judge; новые результаты проверок не публикуются. Из .rebuild/ публикуются только origin.json и release-version.json. Личный файл портфолио и незакоммиченные изменения исходного checkout автоматически не включаются.

## 1. Подготовить терминал

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

Помощник проверяет имя каталога, origin, ветку main, чистоту клона и оба базовых SHA, затем копирует только согласованные пути и сверяет байты. Он не удаляет файлы и не создаёт коммиты. Если main продвинулся относительно сохранённой базы, сначала сопоставьте новую базу с rebuild. После частичного копирования сохраните каталог для разбора; не повторяйте copy-to поверх грязного клона.

## 3. Проверить полноту переноса и состав diff

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

## 4. Коммит 1 и push 1: учебные расследования QA

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

## 5. Коммит 2 и push 2: инструкции и перечни

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

## 6. Итоговая проверка

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

Ожидаются два новых коммита, 682 файла в конечном дереве, чистая рабочая копия и совпадающие HEAD/origin/main. Проверьте README, ссылки на [руководство эксплуатации](operator/service-and-lab.md) и Actions для последнего SHA. Результаты удалённого CI фиксируются после отправки владельцем.

## Как продолжить после остановки

- До commit: изучите git status --short и git diff --cached; выясните причину, сохраняя предыдущую попытку.
- Commit создан, push не прошёл: после устранения причины повторите push. Такой же коммит повторно создавать не нужно. При расхождении историй сравните их перед продолжением.
- Терминал закрыт: восстановите настройки, функцию и переменные раздела 1, перейдите в $pub, задайте $lists = Join-Path $pub 'docs\publication'. Из раздела 3 восстановите только присваивания $groups, $paths, $added, $updated, $changes, $qaPaths, $docPaths, $expectedQa, $expectedDocs; проверки первоначального diff после частичного коммита повторять не надо. По git log -2 --oneline и staged diff определите следующий блок.
- Если защита main отклоняет push, нужен согласованный владельцем процесс публикации. Не обходите защиту force push; созданные локальные коммиты сохраняются.
