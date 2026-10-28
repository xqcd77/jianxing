"""Dataset, driver and rubric contracts, without paid model calls."""
from types import SimpleNamespace

from evaluation.schemas import Step
from evaluation.servicebench.dataset import audit
from evaluation.servicebench.lite58 import scenarios as lite58
from evaluation.servicebench.lite66 import scenarios
from evaluation.servicebench.memory_context_runtime import MemoryContextEpisode, MemoryContextSpecialistEpisode
from evaluation.servicebench.runner import ServiceEpisode, freeze
from evaluation.servicebench.specialist_runtime import SpecialistEpisode


def test_preserves58_and_adds_four_each():
    rows = scenarios()
    assert len(rows) == 66
    assert rows[:58] == lite58()
    assert audit(suite='lite66')['cases'] == 66
    assert sum('longterm_memory66' in s.execution.tags for s in rows) == 4
    assert sum('context_understanding66' in s.execution.tags for s in rows) == 4
    assert len({s.family for s in rows[58:]}) == 8
    assert all(len(s.requirements) >= 4 and s.execution.checks for s in rows[58:])
    assert all(not any(t.kind == 'business' for t in s.execution.steps) for s in rows)


def test_specialist_controller_is_preserved():
    mro = MemoryContextSpecialistEpisode.mro()
    assert mro.index(MemoryContextEpisode) < mro.index(SpecialistEpisode) < mro.index(ServiceEpisode)


def test_memory_driver_forwards_version_through_public_api(monkeypatch):
    episode = object.__new__(MemoryContextEpisode)
    calls = []
    monkeypatch.setattr(ServiceEpisode, 'step', lambda self, step: calls.append(step) or {})
    episode.step(Step(kind='memory', text='默认月份为2026-08', label='updated', user='wang',
        params={'key': 'bills.default_period', 'expected_version': 1, 'domains': ['bills']}))
    step = calls[0]
    assert step.kind == 'api' and step.user == 'wang' and step.label == 'updated'
    assert step.params['body'] == {'key': 'bills.default_period', 'expected_version': 1,
        'domains': ['bills'], 'text': '默认月份为2026-08'}


def test_restart_without_business_plan_observes_all_models():
    episode = object.__new__(MemoryContextEpisode)
    episode.plan = None
    models = []
    lifecycle = []
    episode.trace = SimpleNamespace(observe=models.append)
    episode.app = SimpleNamespace(state=SimpleNamespace(graph=SimpleNamespace(
        planner=SimpleNamespace(model='planner'), semantic_verifier=SimpleNamespace(model='verifier')),
        research=SimpleNamespace(model='research')))
    episode.close = lambda: lifecycle.append('close')
    episode.open = lambda: lifecycle.append('open')
    episode.capture = lambda step, data: data
    assert episode.step(Step(kind='restart')) == {'status': 'RESTARTED'}
    assert lifecycle == ['close', 'open']
    assert models == ['planner', 'research', 'verifier']


def test_v12_keeps_strict_v11_and_adds_phase_memory_boundaries():
    from evaluation.servicebench.rubric_v11 import PROMPT as previous
    from evaluation.servicebench.rubric_v12 import PROMPT, VERSION
    assert PROMPT.startswith(previous)
    assert 'v12' in VERSION
    for phrase in ['同键新偏好', '历史同意不构成本次授权', '否定作用范围', '不是候选系统已经向用户解释', '未决留在分母']:
        assert phrase in PROMPT


def test_freeze_same_call_ceiling_and_new_subsets(tmp_path, monkeypatch):
    import evaluation.servicebench.runner as runner
    monkeypatch.setattr(runner, 'root_path', lambda root: tmp_path / 'frozen')
    _, rows, manifest = freeze(tmp_path / 'frozen', selected=['memory_l01', 'context_u01'],
        mode='demo', memory='lexical', variants=('S_AGENT', 'SUPERVISOR'), suite='lite66')
    assert len(rows) == 2
    assert manifest['max_model_calls_per_episode'] == 16
    assert manifest['variants'] == ['S_AGENT', 'SUPERVISOR']
    assert (tmp_path / 'frozen/source_snapshot.zip').exists()
