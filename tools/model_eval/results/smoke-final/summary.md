# AG-01: результаты прогона

Оценка автоматическая: контракт 20, факты 35, источники 20, статус 15, русский 10.
Смысловая проверка формулировок и различия «факт / сообщение участника» — в ручном ревью.

| Модель | Кейсов | Оценка | Контракт | Статус | Факты | Источники | Русский | ₽ | p50, мс | Вызовов | Hard fail |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| DeepSeek V4.1 Flash 1017 | 3 | 100.0 | 100% | 100% | 100% | 100% | 100% | 4.93 | 10340 | 3.7 | 0 |
| Claude Haiku 5.5 | 3 | 100.0 | 100% | 100% | 100% | 100% | 100% | 0.39 | 16763 | 3.7 | 0 |
| GLM 5.3 Flash | 3 | 95.0 | 100% | 67% | 100% | 100% | 100% | 1.552 | 5974 | 3.7 | 0 |
| Mistral Large 4 | 3 | 95.0 | 100% | 67% | 100% | 100% | 100% | 1.382 | 12936 | 3.7 | 0 |
| GPT-6.2 Sol | 3 | 95.0 | 100% | 67% | 100% | 100% | 100% | 2.094 | 17348 | 3.3 | 0 |
| Gemini 3.1 Flash Lite | 3 | 95.0 | 100% | 67% | 100% | 100% | 100% | 0.396 | 3785 | 3.3 | 0 |
| Qwen3.7 Flash | 3 | 93.7 | 100% | 67% | 100% | 100% | 100% | 0.167 | 16202 | 4.3 | 0 |
| Qwen3.7 Plus | 3 | 91.7 | 100% | 67% | 100% | 83% | 100% | 0.894 | 20923 | 3.7 | 0 |
| GPT-OSS 20B | 3 | 91.7 | 100% | 67% | 100% | 100% | 100% | 0.072 | 6419 | 2.7 | 0 |
| GPT-5 Nano | 3 | 91.7 | 100% | 67% | 100% | 83% | 100% | 0.738 | 15720 | 3.3 | 0 |
| GigaChat 3.5 Ultra | 3 | 88.3 | 67% | 67% | 100% | 100% | 100% | 2.087 | 14953 | 2.7 | 0 |
| GigaChat 3 Pro | 3 | 88.3 | 67% | 67% | 100% | 100% | 100% | 2.343 | 2797 | 2.0 | 0 |
| GPT-6 Luna | 3 | 88.3 | 67% | 67% | 100% | 100% | 100% | 0.165 | 5823 | 3.3 | 0 |
| GPT-OSS 120B | 3 | 88.3 | 67% | 67% | 100% | 100% | 100% | 0.063 | 18928 | 1.7 | 0 |
| Alice AI LLM Flash | 3 | 50.0 | 100% | 67% | 33% | 33% | 100% | 1.019 | 4287 | 0.3 | 0 |

## Кейсы

| Модель | Кейс | Оценка | Статус | Ошибки |
|---|---|---:|---|---|
| GigaChat 3.5 Ultra | C01 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C04 | 100.0 | ok | — |
| GigaChat 3.5 Ultra | C08 | 65.0 | ok | фактов с пустыми или неверными полями: 5 |
| GigaChat 3 Pro | C01 | 100.0 | ok | — |
| GigaChat 3 Pro | C04 | 100.0 | ok | — |
| GigaChat 3 Pro | C08 | 65.0 | ok | не вызваны инструменты: read_task_comments, read_task_worklog; фактов с пустыми или неверными полями: 1 |
| Alice AI LLM Flash | C01 | 100.0 | ok | — |
| Alice AI LLM Flash | C04 | 20.0 | insufficient_data | не вызваны инструменты: read_task_card, read_task_comments |
| Alice AI LLM Flash | C08 | 30.0 | insufficient_data | не вызваны инструменты: read_task_card, read_task_comments, read_task_worklog |
| DeepSeek V4.1 Flash 1017 | C01 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C04 | 100.0 | ok | — |
| DeepSeek V4.1 Flash 1017 | C08 | 100.0 | insufficient_data | — |
| GLM 5.3 Flash | C01 | 100.0 | ok | — |
| GLM 5.3 Flash | C04 | 100.0 | ok | — |
| GLM 5.3 Flash | C08 | 85.0 | ok | — |
| Claude Haiku 5.5 | C01 | 100.0 | ok | — |
| Claude Haiku 5.5 | C04 | 100.0 | ok | — |
| Claude Haiku 5.5 | C08 | 100.0 | insufficient_data | — |
| GPT-6 Luna | C01 | 100.0 | ok | — |
| GPT-6 Luna | C04 | 85.0 | insufficient_data | — |
| GPT-6 Luna | C08 | 80.0 | insufficient_data | фактов с пустыми или неверными полями: 2 |
| Qwen3.7 Plus | C01 | 100.0 | ok | — |
| Qwen3.7 Plus | C04 | 90.0 | ok | — |
| Qwen3.7 Plus | C08 | 85.0 | ok | — |
| Mistral Large 4 | C01 | 100.0 | ok | — |
| Mistral Large 4 | C04 | 100.0 | ok | — |
| Mistral Large 4 | C08 | 85.0 | ok | — |
| GPT-6.2 Sol | C01 | 100.0 | ok | — |
| GPT-6.2 Sol | C04 | 85.0 | insufficient_data | — |
| GPT-6.2 Sol | C08 | 100.0 | insufficient_data | — |
| GPT-OSS 120B | C01 | 100.0 | ok | — |
| GPT-OSS 120B | C04 | 100.0 | ok | — |
| GPT-OSS 120B | C08 | 65.0 | ok | не вызваны инструменты: read_task_comments, read_task_worklog; фактов с пустыми или неверными полями: 2 |
| Qwen3.7 Flash | C01 | 98.0 | ok | — |
| Qwen3.7 Flash | C04 | 98.0 | ok | — |
| Qwen3.7 Flash | C08 | 85.0 | ok | — |
| Gemini 3.1 Flash Lite | C01 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C04 | 100.0 | ok | — |
| Gemini 3.1 Flash Lite | C08 | 85.0 | ok | — |
| GPT-OSS 20B | C01 | 100.0 | ok | — |
| GPT-OSS 20B | C04 | 100.0 | ok | — |
| GPT-OSS 20B | C08 | 75.0 | ok | не вызваны инструменты: read_task_comments, read_task_worklog |
| GPT-5 Nano | C01 | 100.0 | ok | — |
| GPT-5 Nano | C04 | 90.0 | ok | — |
| GPT-5 Nano | C08 | 85.0 | ok | — |
