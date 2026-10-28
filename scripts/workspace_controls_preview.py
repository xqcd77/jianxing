"""Isolated, synthetic browser acceptance fixture; never starts a paid model."""
import asyncio
import os
from pathlib import Path

import uvicorn
import httpx

from bank_agent.app import create_app
from bank_agent.config import Settings
from bank_agent.model_registry import PROVIDERS, PRESET_MODELS
from bank_agent.storage import AgentRun, ChatTask, RuntimeEvent, now_iso


def main():
    root = Path(__file__).resolve().parents[1]
    data_dir = Path(os.getenv('BANK_UI_PREVIEW_DATA_DIR', str(root / '.data/workspace_controls_preview'))).resolve()
    if not data_dir.is_relative_to(root / '.data') or not data_dir.name.startswith('workspace_controls_preview'):
        raise ValueError('Browser fixture must use its dedicated .data/workspace_controls_preview* directory')
    settings = Settings(data_dir=data_dir, model_mode='demo', memory_backend='numpy',
        model_providers={'deepseek': {'api_key': 'synthetic-browser-key'}})
    app = create_app(settings)
    def list_models(request):
        provider = next(key for key, spec in PROVIDERS.items() if httpx.URL(spec[3]).host == request.url.host)
        return httpx.Response(200, json={'data': [{'id': model} for model in PRESET_MODELS[provider]]})
    app.state.model_registry.discovery_transport = httpx.MockTransport(list_models)
    original_chat = app.state.chat_tasks.intake.chat

    async def delayed_chat(*args, **kwargs):
        await asyncio.sleep(2)
        return await original_chat(*args, **kwargs)

    app.state.chat_tasks.intake.chat = delayed_chat
    with app.state.db.read() as session:
        exists = session.get(ChatTask, 'browser-history-1')
    if exists is None:
        conversation = app.state.workspace.create('u_demo')['conversation_id']
        with app.state.db.write() as session:
            for index, (question, answer) in enumerate([
                ('先看看这个月的收支，暂时不要购买理财。', '好的，先核对本月收支与资金条件，本次不办理理财购买。'),
                ('下一步先核对可用余额，再给妈妈转400元。', '已保留400元转账需求，待核对收款人与可用余额后，展示办理卡片由你确认。'),
            ], 1):
                session.add(ChatTask(id='browser-history-' + str(index), owner_id='u_demo', conversation_id=conversation,
                    request_key='browser-history-key-' + str(index), message=question, status='SUCCEEDED',
                    result={'status': 'ANSWERED', 'message': answer}, created_at=now_iso(), updated_at=now_iso()))
            session.add(AgentRun(id='browser-usage', owner_id='u_demo', conversation_id=conversation, task_id='browser-usage',
                role='business', schema_name='Intent', runtime='agents_sdk', model_name='deepseek-flash', status='SUCCEEDED',
                prompt_tokens=12480, completion_tokens=400, total_tokens=12880, created_at=now_iso()))
            session.add(RuntimeEvent(event_id='browser-usage-event', owner_id='u_demo', conversation_id=conversation,
                run_id='browser-usage', kind='budget.checked', created_at=now_iso(),
                details={'estimated_input_tokens': 31900, 'context_limit': 131072, 'output_reserved': 4096, 'safety_margin': 2048}))
    uvicorn.run(app, host='127.0.0.1', port=8140)


if __name__ == '__main__':
    main()
