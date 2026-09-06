# Кастомные доработки Hermes

Ветка `custom` содержит только изменения поверх `main`, описанные ниже.
Полная сохраненная история прежних доработок находится в ветке
`backup-20260907`.

## Единые настройки Docker для execute_code и terminal

`execute_code` использует общий `_container_config_from_config`, поэтому
настройки контейнера совпадают с terminal: forwarding, mounts, environment и
изоляция. Списки и словари из `terminal` сериализуются общим
`_terminal_env_value`, включая Docker volumes.

Новых ключей конфигурации нет. Используется существующая секция `terminal`.

Код: `tools/code_execution_tool.py`, `tools/terminal_scope.py`.
Проверка: `tests/tools/test_code_execution_config.py`.

## Ручной перезапуск gateway после обновления

```yaml
updates:
  restart_gateways: false
```

При `false` обновляются код и зависимости, но Hermes не перезапускает gateway
и связанные dashboard-процессы. Работающие процессы продолжают использовать
загруженный код до явного `hermes gateway restart`. В update receipt сохраняется
пропущенный этап, а проверка runtime-версий откладывается.

Настройка действует на весь этап перезапуска установки, включая обновления из
gateway. На Windows при работающих gateway обновление останавливается: процессы
нужно выключить вручную, поскольку они блокируют файлы установки.

Значение по умолчанию: `true`.

Код: `hermes_cli/update_policy.py`, `hermes_cli/update_cmd_fleet.py`,
`hermes_cli/update_cmd_maint.py`, `hermes_cli/update_cmd_windows.py`.
Проверка: `tests/hermes_cli/test_update_restart_policy.py`.
