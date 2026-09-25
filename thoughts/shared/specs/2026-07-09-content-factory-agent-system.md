# Content Factory Agent System — Specification

## Executive Summary

Агентская система для контент-завода, которая анализирует raw-данные из TikTok/Instagram, выводит формулы успешного контента, генерирует и валидирует брифы, и непрерывно улучшается через eval-loop с обратной связью по реальным метрикам.

Система строится как intelligence-слой поверх существующей инфраструктуры (n8n + Google Sheets + GitHub), не заменяя её, а дополняя аналитикой и decision-making.

## Problem Statement

**Текущая ситуация:**
- n8n собирает raw-данные из TikTok/Instagram через Apify
- Данные складываются в Google Sheets
- GitHub хранит markdown-промпты
- n8n умеет генерировать брифы из промптов

**Что болит:**
- Нет анализа паттернов успешного контента
- Формулы не выводятся систематически
- Prompt updates происходят ad-hoc без evidence
- Нет eval-loop для связи результатов с промптами
- Нет атрибуции причин: prompt failure vs production failure

**Цель:** Построить агентскую систему на Claude Code, которая замыкает цикл: анализ → формулы → брифы → оценка → улучшение промптов.

## Success Criteria

MVP успешен, когда работает полный evidence-backed цикл:

```
raw rows → pattern analysis → formula → prompt proposal → human review → brief → brief review → publication → performance → eval → next iteration
```

**Измеримые критерии:**
1. Брифы соответствуют формулам (brief reviewer pass rate > 80%)
2. Время на создание брифа сокращается vs ручной процесс
3. Eval-loop показывает связь между prompt_version и performance
4. Prompt proposals содержат evidence (ссылки, метрики, confidence)

## User Personas

### Primary: System Operator (вы)
- Технический уровень: высокий
- Взаимодействие: Claude Code в VS Code, slash-команды, агенты
- Задачи: запуск анализа, ревью proposals, принятие решений, мониторинг
- Частота: ежедневно/еженедельно в зависимости от задачи

### Secondary: Producer (будущее расширение)
- Технический уровень: средний
- Взаимодействие: получает брифы, публикует контент
- Задачи: коммуникация с креаторами, постинг
- Частота: ежедневно

## System Architecture

### Layer Separation

| Layer | Responsibility | Technology |
|-------|---------------|------------|
| **Data Collection** | Raw data from TikTok/Instagram | n8n + Apify |
| **Storage (Operational)** | Raw rows, briefs, reels, metrics | Google Sheets |
| **Storage (Versioned)** | Prompts, specs, decisions, changelog | GitHub |
| **Storage (Memory)** | Agent long-term memory | `.claude/memory/` + `agent-runtime/` |
| **Runtime (Generation)** | Brief generation from approved inputs | n8n |
| **Intelligence (Analysis)** | Patterns, formulas, proposals, eval | Claude Code Agents |

### What n8n Does (NOT replaced)
- Сбор raw-данных TikTok/Instagram через Apify
- Запись в Google Sheets
- Подгрузка GitHub prompts
- Runtime brief generation из approved inputs
- Сбор performance evidence

### What Claude Code Agents Do
- Raw batch profiling (качество данных)
- Pattern analysis (winner/loser, same-account comparison)
- Formula extraction и обновление
- Approved inputs preparation
- Prompt proposals с evidence
- Brief review (валидация по формулам)
- Eval decisions (атрибуция причин)

## Agent Definitions

### 1. Raw Batch Profiler
**Trigger:** Перед запуском pattern analysis
**Input:** Ссылка на sheet/range с raw rows
**Output:** Profiling report

```yaml
responsibilities:
  - Проверка качества raw rows
  - Missing fields detection
  - Dedupe (по source_url или signature)
  - Готовность к анализу (sufficient data check)
  
outputs:
  - profile_report.json
  - issues_list (if any)
  - ready_for_analysis: boolean
  
rules:
  - Если critical fields отсутствуют → insufficient_data
  - Если duplicates > threshold → warning + dedupe suggestions
```

### 2. Pattern Analyzer
**Trigger:** Ручной запуск через CLI после накопления batch
**Input:** Profiled raw rows (ready_for_analysis: true)
**Output:** Pattern candidates

```yaml
responsibilities:
  - Анализ успешного/слабого контента
  - Winner/loser selection
  - Same-account comparison
  - Группировка по нишам/форматам
  
outputs:
  - patterns/YYYY-MM-DD-{niche}-patterns.json
  - evidence links
  - confidence scores
  
rules:
  - Нет выводов без evidence
  - Конфликтующие паттерны → оба с условиями применения → human review
  - insufficient_data → не делать выводов, указать что не хватает
```

### 3. Formula Writer
**Trigger:** После pattern analysis
**Input:** Validated patterns
**Output:** Reusable formulas

```yaml
formula_schema:
  name: string
  niche: string
  hook_structure:
    timing: "0-3s"
    elements: ["question", "statement", "visual_hook"]
  problem_definition: string
  solution_structure: object
  visual_requirements: string[]
  cta_type: string
  prohibitions: string[]
  evidence:
    source_urls: string[]
    avg_views: number
    avg_er: number
  confidence: "high" | "medium" | "low"
  conditions: string  # когда применять
  
outputs:
  - formulas/{niche}/{formula-name}.json
  - formulas/{niche}/{formula-name}.md (human-readable)
```

### 4. Brief Reviewer
**Trigger:** После brief generation (вызывается из n8n или вручную)
**Input:** Generated brief + applicable formulas
**Output:** Review verdict

```yaml
responsibilities:
  - Соответствие формуле
  - Schema validation
  - Reference quality check
  - Producibility check (можно ли снять)
  - Product connection (если применимо)
  
verdicts:
  - approved: brief готов
  - revise: причина + что исправить
  - reject: причина (для logging)
  
outputs:
  - review_result.json
  - rejection_reason (if any)
  
rules:
  - Повторяющиеся rejection reasons → input для Prompt Optimizer
```

### 5. Prompt Optimizer
**Trigger:** Ручной запуск после накопления evidence
**Input:** Current prompts + patterns + eval results + rejection history
**Output:** Evidence-backed proposal

```yaml
responsibilities:
  - Анализ performance by prompt_version
  - Rejection pattern analysis
  - Формулировка изменений
  - Подготовка proposal с evidence
  
outputs:
  - proposals/YYYY-MM-DD-{prompt-name}-proposal.md
    - current_version_link
    - proposed_changes (diff)
    - evidence:
        - source_patterns: []
        - performance_delta: {}
        - rejection_reasons_addressed: []
    - confidence
    - risks
  
rules:
  - Не auto-commit, только proposal
  - Human review обязателен
  - После approval → git commit с changelog
```

### 6. Eval Agent
**Trigger:** Еженедельно после накопления performance rows
**Input:** Published reels + performance metrics + prompt_version
**Output:** Evaluation report

```yaml
responsibilities:
  - Связь performance с prompt_version
  - Связь performance с pattern/formula
  - Attribution analysis:
      - prompt_failure: плохая генерация
      - reference_failure: слабый референс
      - production_failure: плохая съёмка/монтаж
      - publishing_failure: плохое время/описание
  - Trend detection
  
outputs:
  - eval/YYYY-MM-DD-weekly-eval.json
  - insights для Prompt Optimizer
  - attribution breakdown
```

## Data Schema

### Google Sheets Structure

```
CF Raw TikTok
├── source_url (unique)
├── account
├── views, likes, comments, shares, saves
├── duration
├── transcript
├── hook_text (first 3s)
├── posted_at
├── scraped_at
├── niche
├── language
└── raw_metadata (JSON)

CF Raw Instagram
├── (similar structure)

CF Creative Briefs
├── brief_id
├── source_pattern_ids[]
├── formula_id
├── prompt_version
├── hook
├── script
├── visual_direction
├── cta
├── references[]
├── review_status: pending | approved | rejected | revised
├── rejection_reason
├── generated_at
└── reviewer_notes

CF Published Reels
├── reel_id
├── brief_id (FK)
├── platform
├── post_url
├── posted_at
├── creator_id
└── production_notes

CF Performance
├── reel_id (FK)
├── brief_id (FK)
├── prompt_version (FK)
├── pattern_ids[]
├── views
├── likes, comments, shares, saves
├── er (engagement rate)
├── measured_at
└── delta_from_prev (growth)

CF Prompt Versions
├── prompt_id
├── version
├── github_path
├── active: boolean
├── activated_at
├── deactivated_at
└── changelog

CF Run Log
├── run_id
├── agent
├── trigger_type: scheduled | manual | event
├── started_at
├── completed_at
├── status: success | failed | insufficient_data
├── input_summary
├── output_paths[]
└── errors[]
```

### Relationship Chain
```
source_url → pattern_id → formula_id → brief_id → prompt_version → reel_id → performance_row
```

## File Structure

```
CF/
├── .claude/
│   ├── memory/
│   │   ├── MEMORY.md (index)
│   │   ├── patterns/
│   │   │   └── {niche}-observations.md
│   │   ├── decisions/
│   │   │   └── {date}-{decision}.md
│   │   └── evaluations/
│   │       └── {date}-insights.md
│   └── settings.json
├── prompts/                    # Versioned in GitHub
│   ├── briefs/
│   │   ├── {niche}/
│   │   │   └── {format}.md
│   │   └── _shared/
│   │       └── schema.md
│   └── agents/
│       └── {agent-name}.md
├── formulas/                   # Versioned in GitHub
│   └── {niche}/
│       ├── {formula-name}.json
│       └── {formula-name}.md
├── proposals/                  # Versioned in GitHub
│   └── YYYY-MM-DD-{name}.md
├── agent-runtime/              # NOT committed
│   ├── profiles/
│   ├── patterns/
│   ├── reviews/
│   └── evals/
├── thoughts/
│   └── shared/
│       └── specs/
│           └── this-file.md
└── CLAUDE.md
```

## Workflow Triggers

| Workflow | Trigger | Frequency | Owner |
|----------|---------|-----------|-------|
| Raw data collection | Schedule | Daily | n8n |
| Performance evidence | Schedule | Daily | n8n |
| Raw batch profiling | Manual (CLI) | Before analysis | You |
| Pattern analysis | Manual (CLI) | When batch ready | You |
| Formula writing | After patterns | Follows analysis | You |
| Prompt proposal | Manual (CLI) | When evidence accumulated | You |
| Prompt update | After human review | On approval | You |
| Brief generation | Event (approved inputs) | Continuous | n8n |
| Brief review | After generation | Continuous | Agent |
| Eval loop | Schedule | Weekly | You |

## Slash Commands (MVP)

```
/cf-profile [sheet_range]
  → Run Raw Batch Profiler

/cf-analyze [--niche X] [--since YYYY-MM-DD]
  → Run Pattern Analyzer

/cf-formula [pattern_ids...]
  → Run Formula Writer

/cf-review-brief [brief_id]
  → Run Brief Reviewer

/cf-propose-update [prompt_path]
  → Run Prompt Optimizer

/cf-eval [--since YYYY-MM-DD]
  → Run Eval Agent

/cf-status
  → Show current state: pending reviews, recent runs, queue
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Insufficient data | Return `insufficient_data` + specify what's missing |
| Conflicting patterns | Show both with evidence + conditions → human review |
| Brief rejected | Log reason, route to revise, track for pattern detection |
| API failure (Sheets/GitHub) | Retry 3x, then fail with clear error |
| Agent timeout | Save partial state, log, allow resume |

## Security & Access

- **Prompt updates:** Evidence + human review + git commit required
- **No auto-push to production prompts**
- **Sheets credentials:** Managed via n8n (not in Claude Code)
- **GitHub access:** Standard git workflow (SSH/HTTPS)
- **No sensitive data in commits:** Runtime artifacts in `agent-runtime/`, not versioned

## Out of Scope (MVP)

- Dashboard/web UI для команды
- Telegram/Slack bot
- Автоматическая публикация контента
- Multi-user access control
- Real-time notifications
- Автоматическое A/B тестирование промптов
- Video generation / editing
- Image generation

## Open Questions for Implementation

1. **Sheets integration:** MCP-сервер для Google Sheets или прямой доступ через API?
2. **GitHub integration:** Использовать встроенный git или MCP-сервер?
3. **n8n ↔ Claude Code:** Как триггерить агентов из n8n? (webhook? file watch?)
4. **Formula confidence thresholds:** Какие пороги для high/medium/low confidence?
5. **Eval attribution weights:** Как взвешивать разные типы failure?

## Appendix: Discovery Findings

### Current Infrastructure
- n8n workflows: работают, собирают данные
- Google Sheets: 7 листов (Raw TikTok, Raw Instagram, Briefs, Reels, Performance, Prompt Versions, Run Log)
- GitHub: markdown-промпты, версионирование
- Apify: scraping TikTok/Instagram

### Scale
- Target: 20+ briefs/week
- Raw data: daily collection

### Key Design Decisions
1. **Separation of concerns:** n8n = runtime, Claude Code = intelligence
2. **Human-in-the-loop:** Mandatory for prompt updates
3. **Evidence-first:** No conclusions without data
4. **Attribution clarity:** Distinguish prompt vs production failures
5. **File-based memory:** `.claude/memory/` + versioned files, not Sheets
