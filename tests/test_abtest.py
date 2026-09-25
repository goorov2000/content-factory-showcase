from cf.abtest import interleave_plan, select_versions

VERSIONS = [
    {"prompt_id": "brief-pets-reel", "version": "v1",
     "github_path": "prompts/briefs/pets/reel.md", "active": "FALSE"},
    {"prompt_id": "brief-pets-reel", "version": "v2",
     "github_path": "prompts/briefs/pets/reel.md", "active": "TRUE"},
    {"prompt_id": "brief-pets-reel", "version": "v3",
     "github_path": "prompts/briefs/pets/reel-v3.md", "active": "CANDIDATE"},
    {"prompt_id": "brief-food-reel", "version": "v1",
     "github_path": "prompts/briefs/food/reel.md", "active": "TRUE"},
]


def test_select_versions_picks_active_and_candidate():
    active, candidate = select_versions(VERSIONS, "brief-pets-reel")
    assert active["version"] == "v2"
    assert candidate["version"] == "v3"


def test_select_versions_no_candidate_returns_none():
    active, candidate = select_versions(VERSIONS, "brief-food-reel")
    assert active["version"] == "v1"
    assert candidate is None


def test_select_versions_last_status_wins():
    # append дописывает свежую версию в конец — последняя active/candidate выигрывает.
    versions = [
        {"prompt_id": "p", "version": "old", "github_path": "a", "active": "TRUE"},
        {"prompt_id": "p", "version": "new", "github_path": "b", "active": "TRUE"},
    ]
    active, _ = select_versions(versions, "p")
    assert active["version"] == "new"


def test_interleave_plan_pairs_active_and_candidate():
    # Есть candidate -> пара active/candidate (~50/50, честный A/B). Порядок внутри пары
    # задаётся стартовой чётностью от prompt_id — проверяем набор, не порядок.
    plan = interleave_plan(VERSIONS, "brief-pets-reel", 2)
    assert {p["cohort"] for p in plan} == {"active", "candidate"}
    assert {p["prompt_version"] for p in plan} == {"v2", "v3"}
    assert [p["index"] for p in plan] == [0, 1]
    by_version = {p["prompt_version"]: p["github_path"] for p in plan}
    assert by_version["v3"] == "prompts/briefs/pets/reel-v3.md"


def _ab_versions(prompt_id):
    return [
        {"prompt_id": prompt_id, "version": "a", "github_path": "x", "active": "TRUE"},
        {"prompt_id": prompt_id, "version": "c", "github_path": "y", "active": "CANDIDATE"},
    ]


def test_interleave_plan_start_parity_varies_by_prompt_id():
    # count=1 у разных prompt_id: кандидат НЕ должен быть стерилен навсегда — встречаются
    # оба стартовых варианта (детерминированный хеш prompt_id даёт разный старт).
    cohorts = {interleave_plan(_ab_versions(f"prompt-{n}"), f"prompt-{n}", 1)[0]["cohort"]
               for n in range(30)}
    assert cohorts == {"active", "candidate"}


def test_interleave_plan_start_parity_is_deterministic():
    # Один и тот же prompt_id стартует одинаково между прогонами (стабильность).
    assert (interleave_plan(_ab_versions("p"), "p", 3)
            == interleave_plan(_ab_versions("p"), "p", 3))


def test_interleave_plan_even_count_is_exactly_5050_regardless_of_start():
    # Чётный count -> ровно 50/50 при любой стартовой чётности.
    for n in range(20):
        pid = f"p{n}"
        plan = interleave_plan(_ab_versions(pid), pid, 4)
        assert sum(1 for p in plan if p["cohort"] == "active") == 2
        assert sum(1 for p in plan if p["cohort"] == "candidate") == 2


def test_interleave_plan_all_active_without_candidate():
    plan = interleave_plan(VERSIONS, "brief-food-reel", 4)
    assert all(p["cohort"] == "active" for p in plan)
    assert {p["prompt_version"] for p in plan} == {"v1"}


def test_interleave_plan_count10_is_5050():
    plan = interleave_plan(VERSIONS, "brief-pets-reel", 10)
    assert sum(1 for p in plan if p["cohort"] == "active") == 5
    assert sum(1 for p in plan if p["cohort"] == "candidate") == 5


def test_interleave_plan_roughly_5050_over_many_formulas():
    # Приёмка P5.13: распределение версий по брифам ~50/50 на фикстуре.
    # 20 формул, у каждой active+candidate, по 2 брифа -> ровно 50/50.
    plan_all = []
    for n in range(20):
        vs = [
            {"prompt_id": f"p{n}", "version": "a", "github_path": "x", "active": "TRUE"},
            {"prompt_id": f"p{n}", "version": "c", "github_path": "y", "active": "CANDIDATE"},
        ]
        plan_all += interleave_plan(vs, f"p{n}", 2)
    active_n = sum(1 for p in plan_all if p["cohort"] == "active")
    cand_n = sum(1 for p in plan_all if p["cohort"] == "candidate")
    assert active_n == cand_n == 20


def test_interleave_plan_no_active_returns_empty():
    # Нет активной версии — брифу нечего нести, генерировать нельзя.
    vs = [{"prompt_id": "p", "version": "c", "github_path": "y", "active": "CANDIDATE"}]
    assert interleave_plan(vs, "p", 2) == []
