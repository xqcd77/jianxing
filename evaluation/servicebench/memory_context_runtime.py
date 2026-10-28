"""Public memory interactions and neutral lifecycle events for Lite66 only."""
from evaluation.schemas import Step
from .runner import ServiceEpisode
from .specialist_runtime import SpecialistEpisode


class MemoryContextEpisode(ServiceEpisode):
    def step(self, step):
        if step.kind == 'memory':
            body = {'text': step.text, 'domains': step.params.get('domains', ['wealth', 'bills'])}
            body.update({k: step.params[k] for k in ('key', 'expected_version') if k in step.params})
            return super().step(Step(kind='api', label=step.label, user=step.user,
                params={'method': 'POST', 'path': '/api/memory/preferences', 'body': body}))
        if step.kind == 'fixture_event' and step.params.get('event') == 'memory_maintenance':
            curator = self.app.state.memory.curator
            curator.bootstrap()
            for _ in range(20):
                if not curator.run_pending()['more']:
                    break
            return super().step(Step(kind='api', label=step.label, user=step.user,
                params={'path': '/api/memory/maintenance'}))
        if step.kind == 'restart':
            plan_id = self.plan.get('id') if self.plan else None
            self.close()
            self.open()
            self.trace.observe(self.app.state.graph.planner.model)
            self.trace.observe(self.app.state.research.model)
            verifier = getattr(self.app.state.graph, 'semantic_verifier', None)
            if verifier:
                self.trace.observe(verifier.model)
            data = self.request('GET', f'/api/plans/{plan_id}', user=step.user) if plan_id else {'status': 'RESTARTED'}
            return self.capture(step, data)
        return super().step(step)


class MemoryContextSpecialistEpisode(MemoryContextEpisode, SpecialistEpisode):
    """Keep all original specialist fixture controls through cooperative super()."""
