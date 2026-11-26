# AG-01: результаты прогона

Оценка автоматическая: контракт 20, факты 35, источники 20, статус 15, русский 10.
Смысловая проверка формулировок и различия «факт / сообщение участника» — в ручном ревью.

| Модель | Кейсов | Оценка | Контракт | Статус | Факты | Источники | Русский | ₽ | p50, мс | Вызовов | Hard fail |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Claude Haiku 5.5 | 10 | 99.0 | 100% | 100% | 100% | 100% | 100% | 1.518 | 11792 | 3.8 | 0 |
| GLM 5.3 Flash | 10 | 98.5 | 100% | 90% | 100% | 100% | 100% | 5.946 | 7736 | 3.8 | 0 |
| DeepSeek V4.1 Flash 1017 | 10 | 96.5 | 90% | 90% | 100% | 100% | 100% | 14.988 | 4429 | 3.9 | 0 |
| GigaChat 3.5 Ultra | 10 | 93.0 | 90% | 90% | 100% | 85% | 100% | 6.449 | 9699 | 2.3 | 0 |
| Gemini 3.1 Flash Lite | 10 | 92.5 | 100% | 80% | 90% | 95% | 100% | 1.189 | 3368 | 2.5 | 0 |
| Qwen3.7 Flash | 10 | 90.0 | 100% | 80% | 90% | 85% | 100% | 0.417 | 14404 | 3.2 | 0 |

## Кейсы

| Модель | Кейс | Оценка | Статус | Ошибки |
|---|---|---:|---|---|
| DeepSeek V4.1 Flash 1017 | C01 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C02 | 100.0 | insufficient_data | — |
| DeepSeek V4.1 Flash 1017 | C03 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C04 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C05 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C06 | 85.0 | insufficient_data | — |
| DeepSeek V4.1 Flash 1017 | C07 | 100.0 | access_denied | — |
| DeepSeek V4.1 Flash 1017 | C08 | 100.0 | insufficient_data | — |
| DeepSeek V4.1 Flash 1017 | C09 | 80.0 | source_error | фактов с пустыми или неверными полями: 3 |
| DeepSeek V4.1 Flash 1017 | C10 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C01 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C02 | 75.0 | insufficient_data | не вызваны инструменты: read_task_description |
| GigaChat 3.5 Ultra | C03 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C04 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C05 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C06 | 90.0 | ok | — |
| GigaChat 3.5 Ultra | C07 | 100.0 | access_denied | — |
| GigaChat 3.5 Ultra | C08 | 65.0 | ok | фактов с пустыми или неверными полями: 2 |
| GigaChat 3.5 Ultra | C09 | 100.0 | source_error | — |
| GigaChat 3.5 Ultra | C10 | 100.0 | ok | — |
| GLM 5.3 Flash | C01 | 100.0 | ok | — |
| GLM 5.3 Flash | C02 | 100.0 | insufficient_data | — |
| GLM 5.3 Flash | C03 | 100.0 | ok | — |
| GLM 5.3 Flash | C04 | 100.0 | ok | — |
| GLM 5.3 Flash | C05 | 100.0 | ok | — |
| GLM 5.3 Flash | C06 | 100.0 | ok | — |
| GLM 5.3 Flash | C07 | 100.0 | access_denied | — |
| GLM 5.3 Flash | C08 | 85.0 | ok | — |
| GLM 5.3 Flash | C09 | 100.0 | source_error | — |
| GLM 5.3 Flash | C10 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C01 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C02 | 50.0 | ok | — |
| Gemini 3.1 Flash Lite | C03 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C04 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C05 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C06 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C07 | 100.0 | access_denied | — |
| Gemini 3.1 Flash Lite | C08 | 85.0 | ok | — |
| Gemini 3.1 Flash Lite | C09 | 100.0 | source_error | — |
| Gemini 3.1 Flash Lite | C10 | 90.0 | ok | — |
| Claude Haiku 5.5 | C01 | 100.0 | ok | — |
| Claude Haiku 5.5 | C02 | 98.0 | insufficient_data | — |
| Claude Haiku 5.5 | C03 | 94.0 | ok | — |
| Claude Haiku 5.5 | C04 | 100.0 | ok | — |
| Claude Haiku 5.5 | C05 | 98.0 | ok | — |
| Claude Haiku 5.5 | C06 | 100.0 | ok | — |
| Claude Haiku 5.5 | C07 | 100.0 | access_denied | — |
| Claude Haiku 5.5 | C08 | 100.0 | insufficient_data | — |
| Claude Haiku 5.5 | C09 | 100.0 | source_error | — |
| Claude Haiku 5.5 | C10 | 100.0 | ok | — |
| Qwen3.7 Flash | C01 | 100.0 | ok | — |
| Qwen3.7 Flash | C02 | 100.0 | insufficient_data | — |
| Qwen3.7 Flash | C03 | 100.0 | ok | — |
| Qwen3.7 Flash | C04 | 100.0 | ok | — |
| Qwen3.7 Flash | C05 | 100.0 | ok | — |
| Qwen3.7 Flash | C06 | 85.0 | ok | не вызваны инструменты: read_task_comments |
| Qwen3.7 Flash | C07 | 100.0 | access_denied | — |
| Qwen3.7 Flash | C08 | 85.0 | ok | — |
| Qwen3.7 Flash | C09 | 30.0 | insufficient_data | — |
| Qwen3.7 Flash | C10 | 100.0 | ok | — |
