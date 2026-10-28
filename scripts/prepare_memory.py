"""Prepare and verify local semantic embeddings without sending banking data."""

from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_agent.config import Settings
from bank_agent.memory import Embeddings


def main():
    settings = replace(Settings.from_env(), memory_embedding="fastembed")
    provider = Embeddings(settings, allow_download=True)
    vectors = provider.encode(["查询账户余额", "核对银行卡可用资金", "生日鲜花配送"])
    query = provider.encode(["账户里还有多少钱"], query=True)[0]
    if provider.backend != "fastembed":
        raise SystemExit("本地语义模型未准备成功：" + provider.error)
    print("Embedding ready:", provider.model_id, "dimension:", vectors.shape[1])
    print("Chinese retrieval scores:", [round(float(v), 4) for v in vectors @ query])


if __name__ == "__main__":
    main()
