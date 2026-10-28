"""Isolated browser acceptance server; public sources are deliberately fixed."""
import argparse
import asyncio
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from bank_agent.app import create_app  # noqa: E402
from bank_agent.config import Settings  # noqa: E402
from bank_agent.information import FUND_URL  # noqa: E402


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8124)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--research-delay',type=float,default=0)
    args=parser.parse_args()
    app=create_app(Settings(data_dir=Path(args.data_dir).resolve(),model_mode='demo',memory_embedding='lexical',scheduler_mode='disabled'))
    async def source(request):
        if str(request.url)==FUND_URL:
            await asyncio.sleep(2)
            return httpx.Response(200,text="<a href='/Services/cn/html/product/index.shtml?fundcode=159919'>嘉实测试ETF</a>")
        return httpx.Response(200,text='风险等级：R3 管理费率：0.15% 托管费率：0.05%')
    app.state.graph.information.http_transport=httpx.MockTransport(source)
    # Each lookup is slow, including cached calls, so partial/cancel/reload
    # assertions test actual background behavior rather than network timing luck.
    answer=app.state.graph.information.answer
    async def delayed(query):
        await asyncio.sleep(1.2)
        return await answer(query)
    app.state.graph.information.answer=delayed
    if args.research_delay>0:
        invoke=app.state.research.graph.ainvoke
        async def delayed_research(*values,**options):
            await asyncio.sleep(args.research_delay)
            return await invoke(*values,**options)
        app.state.research.graph.ainvoke=delayed_research
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':
    main()
