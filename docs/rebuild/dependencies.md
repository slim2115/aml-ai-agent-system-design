# Зависимости и повторяемая установка

Актуализировано 6 ноября 2026 года. Поддерживаемый профиль — Windows x64 и CPython 3.12. Установка использует точные версии и SHA256 бинарных wheels. Первоначальная квалификация блока 3 сохранена локально; для новой установки используются команды ниже.

## Наборы пакетов

| Файл | Назначение |
| --- | --- |
| `requirements/runtime.in`, `requirements/unit.in` | Прямые зависимости и инструменты проверки |
| `requirements/windows-py312.lock` | Полный основной Windows runtime с закреплёнными wheels |
| `requirements/unit-windows-py312.lock` | Минимальный исторический профиль изолированных тестов |
| `requirements/ci-windows-py312.lock` | Текущий детерминированный CI, включая UI/API |
| `requirements.txt` | Точка входа в основной lock |
| `requirements/lab-extra-windows-py312.lock`, `requirements/kafka-extra-windows-py312.lock` | Дополнительные зависимости учебного API и Kafka |
| `qa/requirements-browser.lock` | Отдельный инструментарий Playwright |

Изменение `.in` само по себе не обновляет lock. Новое разрешение зависимостей требует проверки установки, совместимости и тестов. Наличие LangGraph в зависимостях не означает, что текущий runtime использует прежнюю оркестрацию. Фиксация хэшей обеспечивает проверку конкретных файлов; оценка уязвимостей и лицензий остаётся отдельной задачей.

## Основное окружение

Откройте PowerShell 7 в корне клона:

```powershell
py -3.12 -m venv .venv
.\scripts\rebuild\Enter-LocalEnvironment.ps1
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/windows-py312.lock
.\.venv\Scripts\python.exe -m pip check
.\scripts\rebuild\Prepare-Embedding.ps1 -Download
```

Без доступа к PyPI к команде pip добавьте `--no-index --find-links <каталог-wheels>`. Для локальной установки MiniLM замените `-Download` на `-SourceDirectory <каталог-снимка>`. Веса не входят в Git. Скрипт модели сверяет десять файлов ревизии `e8f8c211226b894fcb81acc59f3b34ba3efd5f42` с `config/embedding-files.json`. Команда без параметров проверяет установленную модель.

Загрузчик работает с `local_files_only=True`, CPU и `trust_remote_code=False`. Без корректного локального артефакта возвращает `EMBEDDING_NOT_READY`. Он не скачивает другую модель для обхода ошибки.

## Окружение проверок

```powershell
py -3.12 -m venv .venv-ci
.\.venv-ci\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements/ci-windows-py312.lock
.\.venv-ci\Scripts\python.exe -m pip check
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$checkDir = 'var/checks/dependencies-' + (Get-Date -Format yyyyMMdd-HHmmss)
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/check_ci_inputs.py --output-dir $checkDir
.\.venv-ci\Scripts\python.exe -I -B scripts/rebuild/run_checks.py --output-dir $checkDir
```

Конфигурация тестов находится в `pyproject.toml`. Унаследованный `tests/pytest.ini` удаляется из поставки. Исторические тесты прежних контрактов не являются текущей приёмкой продукта. GitHub Actions выполняет два Windows-задания; результат удалённого запуска подтверждается после публикации владельцем.

## Учебный стенд и браузерные инструменты

После установки основного окружения используйте `scripts/lab/Install-LabDependencies.ps1` для SQL/API, `scripts/kafka/Install-KafkaDependencies.ps1 -UsePackageIndex` для Kafka и `qa/tools/browser_tooling.py install` через основной Python для Playwright. Настройка DBeaver выполняется `scripts/lab/Setup-DBeaver.ps1 -Download` при закрытом клиенте. Скрипты содержат закреплённые версии и проверки файлов; установленные компоненты сохраняются в `var`.

Полная последовательность с инициализацией, авторизацией и остановкой приведена в [основной инструкции](../operator/service-and-lab.md). Все команды первоначальной установки включены и в неё; этот документ объясняет назначение наборов зависимостей.
